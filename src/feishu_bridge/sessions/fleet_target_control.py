"""Prospective managed-home primitive. No production adoption on import.

Backend must validate authenticated wire and integrate every writer. OPS must
separately exclude legacy writers/supervisors and verify the actual loader.
Store-local offline/registration never certifies those external conditions.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat

MAX_PAYLOAD = 1048576
MAX_BYTES = 64 * MAX_PAYLOAD
MAX_RECORDS = 10000
STATES = {"OPEN", "DRAINING", "OFFLINE", "MAINTENANCE", "UNKNOWN"}
CHILD_KINDS = {"session_map", "claude_child", "codex_child", "pty",
               "question_continuation", "idle_listener", "handoff_tail", "rollover", "helper"}
KINDS = CHILD_KINDS | {"work_dispatch", "startup_loader", "operator"}


class Refused(Exception):
    """Fixed codes only, never private values or SQLite error text."""


def require(ok, code):
    if not ok:
        raise Refused(code)


def identity(v):
    require(type(v) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", v), "identity_invalid")
    return v


def scope(v):
    require(type(v) is str and v and "\0" not in v, "scope_invalid")
    try:
        require(len(v.encode()) <= 256, "scope_invalid")
    except UnicodeError:
        raise Refused("scope_invalid") from None
    return v


def canonical(v):
    require(type(v) is str and v.startswith("/"), "target_invalid")
    try:
        p = Path(v)
        require(str(p) == v and p.resolve() == p, "target_invalid")
    except OSError:
        raise Refused("target_invalid") from None
    return v


def physical(p, directory=False):
    try:
        p = Path(p); s = p.lstat()
        require(p.resolve() == p.absolute() and s.st_uid == os.getuid()
                and stat.S_IMODE(s.st_mode) == (0o700 if directory else 0o600)
                and (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode) and s.st_nlink == 1),
                "private_identity_unknown")
        return s.st_dev, s.st_ino
    except OSError:
        raise Refused("private_identity_unknown") from None


def fsync_dir(p):
    fd = os.open(p, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def exclusive(p, data):
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    fsync_dir(Path(p).parent)


@dataclass(frozen=True)
class WorkScope:
    work_chat: str
    display_chat: str
    engine: str
    actor: str
    client: str
    origin: str

    def values(self):
        return (scope(self.work_chat), scope(self.display_chat), identity(self.engine),
                scope(self.actor), identity(self.client), identity(self.origin))


@dataclass(frozen=True, repr=False)
class Grant:
    target_home: str
    selector_id: str
    lease_id: str
    kind: str
    work_id: str | None
    engine: str | None
    token: str = field(repr=False)
    work_scope: tuple | None = field(default=None, repr=False)


@dataclass(frozen=True, repr=False)
class QueueHandle:
    work_id: str
    order: int
    payload_sha256: str
    work_scope: WorkScope


SCHEMA = """
CREATE TABLE control (id INTEGER PRIMARY KEY CHECK(id=1), selector TEXT NOT NULL,
 target TEXT NOT NULL, state TEXT NOT NULL, epoch INTEGER NOT NULL, runtime TEXT);
CREATE TABLE work (seq INTEGER PRIMARY KEY AUTOINCREMENT, work_id TEXT NOT NULL UNIQUE,
 work_chat TEXT NOT NULL, display_chat TEXT NOT NULL, engine TEXT NOT NULL,
 actor TEXT NOT NULL, client TEXT NOT NULL, origin TEXT NOT NULL, payload BLOB NOT NULL,
 digest TEXT NOT NULL, state TEXT NOT NULL, claim_id TEXT UNIQUE, result TEXT);
CREATE TABLE leases (lease_id TEXT PRIMARY KEY, kind TEXT NOT NULL, work_id TEXT,
 engine TEXT, parent_id TEXT REFERENCES leases(lease_id), token_digest TEXT NOT NULL,
 release_binding TEXT, registered INTEGER NOT NULL);
PRAGMA user_version=1;
"""
EXPECTED_SQL = {s.split()[2]: "".join(s.split()) for s in SCHEMA.split(";")
                if s.strip().startswith("CREATE TABLE")}


def release_binding(v):
    require(type(v) is dict and set(v) == {"release_id", "closure_sha256", "namespace"}, "runtime_binding_invalid")
    identity(v["release_id"]); identity(v["namespace"])
    require(type(v["closure_sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", v["closure_sha256"]), "runtime_binding_invalid")
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def create(directory, target_home, selector_id):
    """Exclusive operator/SOURCE primitive; fresh store starts OFFLINE."""
    target_home, selector_id = canonical(target_home), identity(selector_id)
    d = Path(directory); physical(d.parent, True)
    require(d.resolve() == d.absolute(), "private_identity_unknown")
    try: d.mkdir(mode=0o700)
    except OSError: raise Refused("create_collision") from None
    # Retain partial creation; no automatic cleanup/reset/recreation.
    db = d / "control.sqlite3"; exclusive(db, b"")
    try:
        with sqlite3.connect(db) as c:
            c.execute("PRAGMA journal_mode=DELETE"); c.execute("PRAGMA synchronous=FULL")
            c.executescript(SCHEMA)
            c.execute("INSERT INTO control VALUES(1,?,?,'OFFLINE',0,NULL)", (selector_id, target_home))
        with db.open("rb") as f: os.fsync(f.fileno())
        fsync_dir(d)
        exclusive(d / "selector.json", (json.dumps({"version": 1, "target_home": target_home,
                  "selector_id": selector_id}, sort_keys=True) + "\n").encode())
    except (sqlite3.Error, OSError): raise Refused("create_unknown") from None
    return bind(d, target_home, selector_id)


def bind(directory, target_home, selector_id):
    return Control(directory, target_home, selector_id)


class Control:
    def __init__(self, directory, target_home, selector_id):
        self.directory = Path(directory); self.target = canonical(target_home); self.selector = identity(selector_id)
        self.db = self.directory / "control.sqlite3"; self.selector_file = self.directory / "selector.json"
        self.dir_identity = physical(self.directory, True); self.db_identity = physical(self.db)
        self.selector_identity = physical(self.selector_file); self.selector_bytes = self.read_selector()
        try:
            def unique_pairs(pairs):
                result = {}
                for k, v in pairs:
                    require(k not in result, "selector_drift")
                    result[k] = v
                return result
            selected = json.loads(self.selector_bytes, object_pairs_hook=unique_pairs)
            require(type(selected) is dict and set(selected) == {"version", "target_home", "selector_id"}
                    and type(selected["version"]) is int and selected["version"] == 1
                    and type(selected["target_home"]) is str and selected["target_home"] == self.target
                    and type(selected["selector_id"]) is str and selected["selector_id"] == self.selector, "selector_drift")
        except ValueError: raise Refused("selector_drift") from None
        with self.tx() as c: self.status(c)

    def read_selector(self):
        try:
            fd = os.open(self.selector_file, os.O_RDONLY | os.O_NOFOLLOW)
            try: b = os.read(fd, 4097)
            finally: os.close(fd)
            require(len(b) <= 4096, "selector_drift")
            return b
        except OSError: raise Refused("selector_drift") from None

    def check_identity(self):
        require(physical(self.directory, True) == self.dir_identity and physical(self.db) == self.db_identity
                and physical(self.selector_file) == self.selector_identity
                and self.read_selector() == self.selector_bytes, "selector_drift")

    @contextmanager
    def tx(self):
        """SQLite ACID recovery retains committed INTENTs, never external replay."""
        self.check_identity(); c = None
        try:
            c = sqlite3.connect(self.db.as_uri() + "?mode=rw", uri=True, timeout=3, isolation_level=None)
            c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA synchronous=FULL"); c.execute("BEGIN IMMEDIATE")
            require(c.execute("PRAGMA journal_mode").fetchone() == ("delete",)
                    and c.execute("PRAGMA user_version").fetchone() == (1,), "database_unknown")
            sql = {n: "".join(s.split()) for n, s in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            require(sql == EXPECTED_SQL and c.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('trigger','view')").fetchone() == (0,), "database_unknown")
            self.validate(c); self.check_identity()
            yield c
            self.validate(c); self.check_identity(); c.execute("COMMIT"); self.check_identity()
        except Refused:
            if c is not None and c.in_transaction:
                try: c.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError):
            if c is not None and c.in_transaction:
                try: c.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise Refused("database_unknown") from None
        finally:
            if c is not None: c.close()

    def status(self, c, allowed=None, epoch=None):
        rows = c.execute("SELECT selector,target,state,epoch,runtime FROM control").fetchall()
        require(len(rows) == 1, "database_unknown")
        selector, target_home, state, e, runtime = rows[0]
        require(selector == self.selector and target_home == self.target and state in STATES and type(e) is int and e >= 0, "database_unknown")
        if allowed is not None: require(state in allowed, "admission_closed")
        if epoch is not None: require(type(epoch) is int and e == epoch, "epoch_drift")
        return state, e, runtime

    def validate(self, c):
        state, _, _ = self.status(c)
        n, size = c.execute("SELECT count(*),coalesce(sum(length(payload)),0) FROM work").fetchone()
        require(n <= MAX_RECORDS and size <= MAX_BYTES, "database_unknown")
        for seq, wid, wc, dc, engine, actor, client, origin, phase, claim, result, payload, digest in c.execute("SELECT seq,work_id,work_chat,display_chat,engine,actor,client,origin,state,claim_id,result,payload,digest FROM work"):
            identity(wid); WorkScope(wc, dc, engine, actor, client, origin).values()
            require(type(seq) is int and seq > 0 and type(payload) is bytes and 0 < len(payload) <= MAX_PAYLOAD
                    and type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest)
                    and hashlib.sha256(payload).hexdigest() == digest
                    and phase in {"QUEUED", "CLAIMED", "DISPATCHING", "SETTLED"}, "database_unknown")
            if phase == "QUEUED": require(claim is None and result is None, "database_unknown")
            else: identity(claim)
            if phase in {"CLAIMED", "DISPATCHING"}:
                require(result is None and c.execute("SELECT kind,work_id,engine,parent_id FROM leases WHERE lease_id=?", (claim,)).fetchone() == ("work_dispatch", wid, engine, None), "database_unknown")
            if phase == "SETTLED": require(result in {"completed", "failed", "cancelled_before_dispatch"}, "database_unknown")
        operator = 0
        for lid, kind, wid, engine, parent, token, rb, registered in c.execute("SELECT * FROM leases"):
            identity(lid); require(kind in KINDS and type(token) is str and re.fullmatch(r"[0-9a-f]{64}", token) and registered in (0, 1), "database_unknown")
            if kind == "operator":
                operator += 1; require(wid is None and engine is None and parent is None and rb is None, "database_unknown")
            elif kind == "startup_loader":
                require(state in {"MAINTENANCE", "UNKNOWN"} and wid is None and engine is None and rb is not None
                        and c.execute("SELECT kind FROM leases WHERE lease_id=?", (parent,)).fetchone() == ("operator",), "database_unknown")
            elif wid is not None:
                row = c.execute("SELECT state,claim_id,engine FROM work WHERE work_id=?", (wid,)).fetchone()
                require(row is not None and row[2] == engine and row[0] in {"CLAIMED", "DISPATCHING"}, "database_unknown")
                require((kind == "work_dispatch" and parent is None and row[1] == lid) or
                        (kind in CHILD_KINDS and parent == row[1] and row[0] == "DISPATCHING"), "database_unknown")
            else: require(kind in CHILD_KINDS and parent is None and type(engine) is str, "database_unknown")
        require((state != "MAINTENANCE" or operator == 1) and (state in {"MAINTENANCE", "UNKNOWN"} or operator == 0), "database_unknown")
        require(not (state == "OFFLINE" and c.execute("SELECT count(*) FROM leases").fetchone()[0]), "database_unknown")

    def new_grant(self, c, lid, kind, wid=None, engine=None, parent=None, rb=None):
        lid = identity(lid)
        require(c.execute("SELECT 1 FROM leases WHERE lease_id=?", (lid,)).fetchone() is None, "lease_conflict")
        token = secrets.token_hex(32)
        c.execute("INSERT INTO leases VALUES(?,?,?,?,?,?,?,0)", (lid, kind, wid, engine, parent, hashlib.sha256(token.encode()).hexdigest(), rb))
        work_scope = c.execute("SELECT work_chat,display_chat,engine,actor,client,origin FROM work WHERE work_id=?", (wid,)).fetchone() if wid is not None else None
        return Grant(self.target, self.selector, lid, kind, wid, engine, token, work_scope)

    def verify_grant(self, c, g, kinds=None):
        require(type(g) is Grant and g.target_home == self.target and g.selector_id == self.selector
                and type(g.token) is str and re.fullmatch(r"[0-9a-f]{64}", g.token), "grant_unknown")
        identity(g.lease_id)
        row = c.execute("SELECT kind,work_id,engine,token_digest,parent_id,release_binding,registered FROM leases WHERE lease_id=?", (g.lease_id,)).fetchone()
        require(row is not None and row[:3] == (g.kind, g.work_id, g.engine)
                and secrets.compare_digest(row[3], hashlib.sha256(g.token.encode()).hexdigest()), "grant_unknown")
        actual_scope = c.execute("SELECT work_chat,display_chat,engine,actor,client,origin FROM work WHERE work_id=?", (g.work_id,)).fetchone() if g.work_id is not None else None
        require(g.work_scope == actual_scope, "grant_unknown")
        if kinds is not None: require(g.kind in kinds, "grant_kind_unknown")
        return row

    def snapshot(self):
        with self.tx() as c:
            state, epoch, runtime = self.status(c)
            rows = c.execute("SELECT seq,work_id,work_chat,display_chat,engine,actor,client,origin,digest,state,claim_id,result FROM work ORDER BY seq").fetchall()
            for payload, digest in c.execute("SELECT payload,digest FROM work ORDER BY seq"):
                require(hashlib.sha256(payload).hexdigest() == digest, "database_unknown")
            return {"version": 1, "state": state, "epoch": epoch,
                    "active_leases": c.execute("SELECT count(*) FROM leases").fetchone()[0],
                    "queued": sum(r[9] == "QUEUED" for r in rows), "unresolved": sum(r[9] in {"CLAIMED", "DISPATCHING"} for r in rows),
                    "queue_digest": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest(),
                    "runtime_registration_reported": runtime is not None, "external_writer_exclusion_verified": False}

    def admit(self, work_id, work_scope, payload):
        work_id = identity(work_id); require(type(work_scope) is WorkScope, "scope_invalid"); values = work_scope.values()
        require(type(payload) is bytes and 0 < len(payload) <= MAX_PAYLOAD, "payload_invalid")
        digest = hashlib.sha256(payload).hexdigest()
        with self.tx() as c:
            self.status(c, {"OPEN"})
            old = c.execute("SELECT seq,work_chat,display_chat,engine,actor,client,origin,payload,digest FROM work WHERE work_id=?", (work_id,)).fetchone()
            if old is not None:
                require(old[1:] == values + (payload, digest), "admission_conflict"); seq = old[0]
            else:
                n, size = c.execute("SELECT count(*),coalesce(sum(length(payload)),0) FROM work").fetchone()
                require(n < MAX_RECORDS and size + len(payload) <= MAX_BYTES, "queue_capacity")
                seq = c.execute("INSERT INTO work(work_id,work_chat,display_chat,engine,actor,client,origin,payload,digest,state) VALUES(?,?,?,?,?,?,?,?,?,'QUEUED')", (work_id,) + values + (payload, digest)).lastrowid
        with self.tx() as c:
            require(c.execute("SELECT seq,work_chat,display_chat,engine,actor,client,origin,payload,digest FROM work WHERE work_id=?", (work_id,)).fetchone() == (seq,) + values + (payload, digest), "database_unknown")
        return {"work_id": work_id, "order": seq, "payload_sha256": digest}

    def queued_handles(self, after_order=0, limit=50):
        """Internal bounded metadata only. Never returns private payload/token.

        No claim/dispatch or completion inference. Caller must retain original
        authenticated ownership and exact returned work/display/engine scope.
        """
        require(type(after_order) is int and after_order >= 0 and type(limit) is int and 1 <= limit <= 100, "page_invalid")
        with self.tx() as c:
            rows = c.execute("SELECT work_id,seq,digest,work_chat,display_chat,engine,actor,client,origin FROM work WHERE state='QUEUED' AND seq>? ORDER BY seq LIMIT ?", (after_order, limit)).fetchall()
            handles = [QueueHandle(r[0], r[1], r[2], WorkScope(*r[3:])) for r in rows]
            for h in handles: h.work_scope.values()
        return handles

    def claim(self, work_id, claim_id):
        work_id, claim_id = identity(work_id), identity(claim_id)
        with self.tx() as c:
            self.status(c, {"OPEN"})
            require(c.execute("SELECT work_id FROM work WHERE state='QUEUED' ORDER BY seq LIMIT 1").fetchone() == (work_id,), "claim_not_queued_head")
            engine, payload, digest = c.execute("SELECT engine,payload,digest FROM work WHERE work_id=?", (work_id,)).fetchone()
            require(hashlib.sha256(payload).hexdigest() == digest, "database_unknown")
            grant = self.new_grant(c, claim_id, "work_dispatch", work_id, engine)
            c.execute("UPDATE work SET state='CLAIMED',claim_id=? WHERE work_id=?", (claim_id, work_id))
        return grant, payload

    def mark_dispatching(self, g):
        with self.tx() as c:
            self.status(c, {"OPEN"}); self.verify_grant(c, g, {"work_dispatch"})
            require(c.execute("SELECT state FROM work WHERE work_id=?", (g.work_id,)).fetchone() == ("CLAIMED",), "claim_unknown")
            c.execute("UPDATE work SET state='DISPATCHING' WHERE work_id=?", (g.work_id,))

    def validate_writer(self, g, expected_scope=None):
        with self.tx() as c:
            self.status(c, {"OPEN", "DRAINING"}); self.verify_grant(c, g)
            require(g.kind not in {"operator", "startup_loader"}, "grant_kind_unknown")
            if g.work_id is not None:
                require(c.execute("SELECT state FROM work WHERE work_id=?", (g.work_id,)).fetchone() == ("DISPATCHING",), "claim_unknown")
            if expected_scope is not None:
                require(type(expected_scope) is WorkScope and expected_scope.values() == g.work_scope, "grant_scope_unknown")
        return True

    def fork_writer(self, parent, lease_id, kind):
        require(kind in CHILD_KINDS, "lease_kind_invalid")
        with self.tx() as c:
            self.status(c, {"OPEN", "DRAINING"}); self.verify_grant(c, parent, {"work_dispatch"})
            require(c.execute("SELECT state FROM work WHERE work_id=?", (parent.work_id,)).fetchone() == ("DISPATCHING",), "claim_unknown")
            g = self.new_grant(c, lease_id, kind, parent.work_id, parent.engine, parent.lease_id)
        return g

    def acquire_writer(self, lease_id, kind, engine):
        require(kind in CHILD_KINDS, "lease_kind_invalid"); engine = identity(engine)
        with self.tx() as c:
            self.status(c, {"OPEN"}); g = self.new_grant(c, lease_id, kind, engine=engine)
        return g

    def release_writer(self, g):
        with self.tx() as c:
            self.status(c, {"OPEN", "DRAINING"}); self.verify_grant(c, g, CHILD_KINDS)
            c.execute("DELETE FROM leases WHERE lease_id=?", (g.lease_id,))

    def return_undispatched(self, g):
        with self.tx() as c:
            self.status(c, {"OPEN", "DRAINING"}); self.verify_grant(c, g, {"work_dispatch"})
            require(c.execute("SELECT state FROM work WHERE work_id=?", (g.work_id,)).fetchone() == ("CLAIMED",), "claim_unknown")
            c.execute("UPDATE work SET state='QUEUED',claim_id=NULL WHERE work_id=?", (g.work_id,)); c.execute("DELETE FROM leases WHERE lease_id=?", (g.lease_id,))

    def settle(self, g, result):
        require(result in {"completed", "failed", "cancelled_before_dispatch"}, "result_invalid")
        with self.tx() as c:
            self.status(c, {"OPEN", "DRAINING"}); self.verify_grant(c, g, {"work_dispatch"})
            expected = "CLAIMED" if result == "cancelled_before_dispatch" else "DISPATCHING"
            require(c.execute("SELECT state FROM work WHERE work_id=?", (g.work_id,)).fetchone() == (expected,), "claim_unknown")
            require(c.execute("SELECT count(*) FROM leases WHERE parent_id=?", (g.lease_id,)).fetchone() == (0,), "descendants_unsettled")
            c.execute("UPDATE work SET state='SETTLED',result=? WHERE work_id=?", (result, g.work_id)); c.execute("DELETE FROM leases WHERE lease_id=?", (g.lease_id,))

    def begin_drain(self, expected_epoch):
        with self.tx() as c:
            _, e, _ = self.status(c, {"OPEN"}, expected_epoch)
            c.execute("UPDATE control SET state='DRAINING',epoch=epoch+1,runtime=NULL")
        return e + 1

    def seal_offline(self, expected_epoch):
        with self.tx() as c:
            _, e, _ = self.status(c, {"DRAINING"}, expected_epoch)
            require(c.execute("SELECT count(*) FROM leases").fetchone() == (0,), "writers_unsettled")
            require(c.execute("SELECT count(*) FROM work WHERE state IN ('CLAIMED','DISPATCHING')").fetchone() == (0,), "writers_unsettled")
            c.execute("UPDATE control SET state='OFFLINE',epoch=epoch+1")
        return e + 1

    def enter_maintenance(self, expected_epoch, operator_lease_id):
        with self.tx() as c:
            _, e, _ = self.status(c, {"OFFLINE"}, expected_epoch)
            g = self.new_grant(c, operator_lease_id, "operator")
            c.execute("UPDATE control SET state='MAINTENANCE',epoch=epoch+1,runtime=NULL")
        return e + 1, g

    def fork_startup(self, operator, lease_id, binding):
        rb = release_binding(binding)
        with self.tx() as c:
            self.status(c, {"MAINTENANCE"}); self.verify_grant(c, operator, {"operator"})
            require(c.execute("SELECT count(*) FROM leases WHERE kind='startup_loader'").fetchone() == (0,), "startup_conflict")
            g = self.new_grant(c, lease_id, "startup_loader", parent=operator.lease_id, rb=rb)
        return g

    def register_runtime(self, startup, binding):
        rb = release_binding(binding)
        with self.tx() as c:
            self.status(c, {"MAINTENANCE"}); row = self.verify_grant(c, startup, {"startup_loader"})
            require(row[5] == rb and row[6] == 0, "runtime_binding_unknown")
            c.execute("UPDATE leases SET registered=1 WHERE lease_id=?", (startup.lease_id,))
            c.execute("UPDATE control SET runtime=?", (rb,))

    def finish_startup(self, startup):
        with self.tx() as c:
            self.status(c, {"MAINTENANCE"}); row = self.verify_grant(c, startup, {"startup_loader"})
            require(row[6] == 1, "runtime_binding_unknown")
            c.execute("DELETE FROM leases WHERE lease_id=?", (startup.lease_id,))

    def finish_maintenance(self, expected_epoch, operator):
        with self.tx() as c:
            _, e, _ = self.status(c, {"MAINTENANCE"}, expected_epoch); self.verify_grant(c, operator, {"operator"})
            require(c.execute("SELECT count(*) FROM leases WHERE parent_id=?", (operator.lease_id,)).fetchone() == (0,), "descendants_unsettled")
            c.execute("DELETE FROM leases WHERE lease_id=?", (operator.lease_id,)); c.execute("UPDATE control SET state='OFFLINE',epoch=epoch+1")
        return e + 1

    def reopen(self, expected_epoch, binding, *, runtime_ready):
        require(runtime_ready is True, "runtime_readiness_unknown"); rb = release_binding(binding)
        with self.tx() as c:
            _, e, runtime = self.status(c, {"OFFLINE"}, expected_epoch)
            require(runtime == rb and c.execute("SELECT count(*) FROM leases").fetchone() == (0,), "runtime_binding_unknown")
            c.execute("UPDATE control SET state='OPEN',epoch=epoch+1")
        return e + 1

    def mark_unknown(self, expected_epoch):
        with self.tx() as c:
            _, e, _ = self.status(c, epoch=expected_epoch)
            c.execute("UPDATE control SET state='UNKNOWN',epoch=epoch+1")
        return e + 1
