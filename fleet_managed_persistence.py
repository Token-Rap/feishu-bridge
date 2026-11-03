"""Actual post-turn file helper leases; no successful-file settlement issuer.

Body return is only an observation. Pending helper leases/owners intentionally
remain for the future complete parent predicate; no release/retry/READY API.
Default helpers keep their original fail-soft paths when no NativeChild exists.
"""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import stat
import uuid

import fleet_managed_child as child
import fleet_managed_ingress as ingress
import fleet_managed_runtime as runtime
import fleet_target_control as coordinator

_REFUSAL_ORIGINALS = {}  # Strong private originals; public pointers cannot select a DB.
_REFUSAL_GRANTS = {}
_REFUSAL_ATTEMPTS = {}  # Diagnostic uncertainty never permits another attempt.
_TURN_REFUSAL_ATTEMPTS = {}  # Separate one-shot actual parent-turn diagnostic.


def require(ok):
    if not ok:
        raise ingress.Refused('managed_native_persistence_unknown')


@dataclass(frozen=True, repr=False)
class Original:
    native: object
    entry: object
    action: object
    process: object
    terminal: object
    path: Path
    home: Path
    kind: str
    root_identity: tuple


@dataclass(frozen=True, repr=False)
class RefusalOriginal:
    observation: object
    helper: Original
    runtime: object
    control: object
    control_fields: tuple
    selection: object
    selection_fields: tuple
    action: object
    parent: object
    parent_fields: tuple
    scope: object
    scope_fields: tuple
    request: object
    request_fields: tuple
    home: Path
    epoch: int


def _exact(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is tuple:
        return len(left) == len(right) and all(_exact(a,b) for a,b in zip(left,right))
    return left == right


def _control_fields(c):
    return (c.directory,c.target,c.selector,c.db,c.selector_file,c.dir_identity,
            c.db_identity,c.selector_identity,c.selector_bytes)


def _grant_fields(g):
    return (g.target_home,g.selector_id,g.lease_id,g.kind,g.work_id,g.engine,g.token,g.work_scope)


def _request_fields(q):
    return (q.work_id,q.work_chat,q.display_chat,q.engine,q.payload)


def _remember_refusal(observed, original):
    a=original.action;r=a.runtime;c=r.control;s=r.selection
    require(type(a) is runtime.Action and type(r) is runtime.Runtime
            and type(c) is coordinator.Control and original.native.runtime is r
            and type(a.parent) is coordinator.Grant and type(a.scope) is coordinator.WorkScope
            and type(a.request) is ingress.Request and type(s) is ingress.Selection
            and s.target_home == original.home and c.target == str(original.home)
            and c.directory == s.directory and c.selector == s.selector_id
            and a.parent.work_id == a.request.work_id
            and a.parent.work_scope == a.scope.values())
    fields=_control_fields(c)
    with coordinator.Control.tx(c) as db:
        _,epoch,_=coordinator.Control.status(c,db,{'OPEN','DRAINING'})
        coordinator.Control.verify_grant(c,db,a.parent,{'work_dispatch'})
        require(db.execute('SELECT state FROM work WHERE work_id=?',(a.request.work_id,)).fetchone()==('DISPATCHING',)
                and _exact(_control_fields(c),fields))
    require(_exact(_control_fields(c),fields) and id(observed) not in _REFUSAL_ORIGINALS)
    _REFUSAL_ORIGINALS[id(observed)] = RefusalOriginal(observed,original,r,c,fields,s,
        (s.directory,s.selector_id,s.target_home),a,a.parent,_grant_fields(a.parent),
        a.scope,a.scope.values(),a.request,_request_fields(a.request),original.home,epoch)


def _original_refusal(observed):
    o=_REFUSAL_ORIGINALS.get(id(observed))
    require(type(o) is RefusalOriginal and o.observation is observed
            and type(o.control) is coordinator.Control and type(o.runtime) is runtime.Runtime
            and _exact(_control_fields(o.control),o.control_fields)
            and o.action.runtime is o.runtime and o.action.parent is o.parent
            and _exact(_grant_fields(o.parent),o.parent_fields)
            and o.action.scope is o.scope and _exact(o.scope.values(),o.scope_fields)
            and o.action.request is o.request and _exact(_request_fields(o.request),o.request_fields)
            and o.control.target == str(o.home)
            and o.selection.target_home == o.home
            and _exact((o.selection.directory,o.selection.selector_id,o.selection.target_home),o.selection_fields))
    # Deliberately never resolve observed.original/native.runtime/runtime.control.
    return o


def _hold_original(observed):
    key=id(observed)
    if key in _REFUSAL_ATTEMPTS:
        return
    _REFUSAL_ATTEMPTS[key]='UNKNOWN'  # BEFORE all qualification/diagnostic IO.
    o=_original_refusal(observed)
    with coordinator.Control.tx(o.control) as db:
        require(_original_refusal(observed) is o)
        state,_,_=coordinator.Control.status(o.control,db,{'OPEN','DRAINING','UNKNOWN'},o.epoch)
        coordinator.Control.verify_grant(o.control,db,o.parent,{'work_dispatch'})
        require(db.execute('SELECT state FROM work WHERE work_id=?',(o.request_fields[0],)).fetchone()==('DISPATCHING',))
        saved=_REFUSAL_GRANTS.get(key)
        if saved is not None:
            grant,fields=saved
            require(type(grant) is coordinator.Grant and _exact(_grant_fields(grant),fields))
            row=coordinator.Control.verify_grant(o.control,db,grant,{'helper'})
            require(row[4]==o.parent.lease_id and grant.work_scope==o.scope_fields)
        require(_original_refusal(observed) is o)
        if state!='UNKNOWN':
            db.execute("UPDATE control SET state='UNKNOWN',epoch=epoch+1")
    require(_original_refusal(observed) is o)
    _REFUSAL_ATTEMPTS[key]='ORIGINAL_DIAGNOSTIC_COMMITTED'


def hold_native_original(native):
    # Only already enrolled actual persistence owners select this diagnostic.
    # An uncertain enrolled original MUST NOT fall back to a mutable runtime.
    originals = tuple(o for o in _REFUSAL_ORIGINALS.values()
                      if o.helper.native is native)
    if not originals:
        return False
    for o in originals:
        o.observation.unknown = True
        try:
            from fleet_managed_file_io import refuse_observation
            refuse_observation(o.observation)
        except BaseException:
            pass
        try:
            _hold_original(o.observation)
        except BaseException:
            pass
    return True  # Diagnostic path only; NO grant/settlement/readiness is issued.


def _hold_turn_original(original):
    key = id(original.action)
    if key in _TURN_REFUSAL_ATTEMPTS:
        return _TURN_REFUSAL_ATTEMPTS[key] == 'ORIGINAL_TURN_DIAGNOSTIC_COMMITTED'
    _TURN_REFUSAL_ATTEMPTS[key] = 'UNKNOWN'  # BEFORE qualification/transaction.
    o = _original_refusal(original.observation)
    require(o is original)
    with coordinator.Control.tx(o.control) as db:
        require(_original_refusal(o.observation) is o)
        state,epoch,_ = coordinator.Control.status(o.control,db,{'OPEN','DRAINING','UNKNOWN'})
        # A still-owned DISPATCHING parent can settle tails during the one normal
        # drain transition. OPEN cannot rebind to any newer epoch; UNKNOWN is
        # observed only, never reopened or treated as a successful parent.
        require((epoch == o.epoch or state == 'DRAINING' and epoch == o.epoch+1
                 or state == 'UNKNOWN' and epoch in {o.epoch+1,o.epoch+2}))
        coordinator.Control.verify_grant(o.control,db,o.parent,{'work_dispatch'})
        require(db.execute('SELECT state FROM work WHERE work_id=?',(o.request_fields[0],)).fetchone()==('DISPATCHING',))
        saved = _REFUSAL_GRANTS.get(id(o.observation))
        require(saved is not None and _exact(_grant_fields(saved[0]),saved[1]))
        row = coordinator.Control.verify_grant(o.control,db,saved[0],{'helper'})
        require(row[4] == o.parent.lease_id and saved[0].work_scope == o.scope_fields
                and _original_refusal(o.observation) is o)
        if state != 'UNKNOWN':
            db.execute("UPDATE control SET state='UNKNOWN',epoch=epoch+1")
    require(_original_refusal(o.observation) is o)
    _TURN_REFUSAL_ATTEMPTS[key] = 'ORIGINAL_TURN_DIAGNOSTIC_COMMITTED'
    return True  # Qualified diagnostic only; unresolved parent/helpers stay held.


def hold_entry_original(entry):
    # The retained actual Entry identity, not entry.runtime or its control.
    originals = tuple(o for o in _REFUSAL_ORIGINALS.values()
                      if o.helper.entry is entry)
    if not originals:
        from fleet_managed_diagnostic import entry_needs_fixed_refusal
        return entry_needs_fixed_refusal(entry)
    for o in originals:
        hold_native_original(o.helper.native)
        try:
            _hold_turn_original(o)
        except BaseException:
            pass  # Original turn remains UNKNOWN; no foreign fallback/retry.
    return True  # Even qualification/IO UNKNOWN cannot enable foreign fallback.


def hold_runtime_original(actual):
    originals = tuple(o for o in _REFUSAL_ORIGINALS.values()
                      if o.runtime is actual)
    if not originals:
        return None  # Pre-persistence predecessor behavior remains exact.
    for o in originals:
        hold_native_original(o.helper.native)
        try:
            _hold_turn_original(o)
        except BaseException:
            pass
    # Diagnostic disposition only; no file/parent/native readiness follows.
    return all(_TURN_REFUSAL_ATTEMPTS.get(id(o.action)) == 'ORIGINAL_TURN_DIAGNOSTIC_COMMITTED'
               for o in originals)


def refuse_pending_delivery(actual):
    # Unresolved original helper owners cannot admit another delivery through a
    # replaced control, even if that foreign queue contains matching wire bytes.
    if any(o.runtime is actual for o in _REFUSAL_ORIGINALS.values()):
        raise coordinator.Refused('admission_closed')


class Observation:
    def __init__(self, original):
        self.original = original
        self.grant = None
        self.body_returned = False
        self.unknown = False
        self.files = None  # Original returned FD owners, published before file IO.
        # None-return/callback observations cannot qualify file close/durability.
        self.file_settlement_verified = False
        self.parent_ready = False

    def _live(self):
        authority = _original_refusal(self)
        o = authority.helper
        require(not self.unknown and type(o) is Original
                and self.original is o and o.native.runtime is authority.runtime
                and authority.runtime.control is authority.control
                and o.native.entry is o.entry and o.native.action is o.action
                and o.native._process is o.process and o.native._terminal is o.terminal
                and runtime.CURRENT.get() is o.action
                and o.entry._persistence_owners.get((o.action.request.work_id, o.kind)) is self)
        o.native._stdio_live(o.process)
        saved = _REFUSAL_GRANTS.get(id(self))
        require(saved is not None and self.grant is saved[0]
                and _exact(_grant_fields(saved[0]),saved[1]))
        coordinator.Control.validate_writer(authority.control, saved[0], authority.scope)
        require(o.native.runtime.selection.target_home == o.home)
        root = o.home.lstat()
        require(stat.S_ISDIR(root.st_mode) and not o.home.is_symlink()
                and str(o.home.resolve(strict=True)) == str(o.home)
                and (root.st_dev, root.st_ino, root.st_uid, stat.S_IMODE(root.st_mode)) == o.root_identity)
        try:
            s = o.path.lstat()
        except FileNotFoundError:
            return  # Exact absent leaf can use the ordinary first-write behavior.
        require(stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid() and s.st_nlink == 1
                and stat.S_IMODE(s.st_mode) in {0o600, 0o644}
                and s.st_size <= (2*1024*1024 if o.kind == 'status' else 16*1024*1024))

    def previous_sessions(self):
        self._live()
        require(self.original.kind == 'status')
        from fleet_managed_file_io import capture
        raw = capture(self).previous()
        if raw is None:
            return {}
        require(len(raw) <= 2*1024*1024)
        def pairs(rows):
            result = {}
            for key, value in rows:
                require(key not in result)
                result[key] = value
            return result
        def integer(text):
            require(len(text) <= 64)
            return int(text)
        def number(text):
            require(len(text) <= 64)
            value = float(text)
            require(math.isfinite(value))
            return value
        value = json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=pairs,
                           parse_int=integer, parse_float=number,
                           parse_constant=lambda _: require(False))
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            require(depth <= 64)
            if type(item) is dict:
                pending.extend((v, depth+1) for v in item.values())
                pending.extend((k, depth+1) for k in item)
            elif type(item) is list:
                pending.extend((v, depth+1) for v in item)
            elif type(item) is str:
                item.encode('utf-8', 'strict')
        require(type(value) is dict and 'sessions' in value
                and type(value['sessions']) is dict
                and set(value) <= {'ts', 'repo', 'model', 'effort', 'engine', 'defaults', 'sessions'})
        self._live()
        return value['sessions']  # Other exact rows remain; no text/ID coercion.

    def refuse(self):
        self.unknown = True  # BEFORE potentially fallible coordinator diagnostic.
        try:
            from fleet_managed_file_io import refuse_observation
            refuse_observation(self)
        except BaseException:
            pass  # Retain actual original registry/tails; never substitute a target.
        try:
            _hold_original(self)
        except BaseException:
            pass  # Sticky diagnostic UNKNOWN, unresolved original leases stay held.
        raise ingress.Refused('managed_native_persistence_unknown') from None

    def observe(self):
        try:
            self._live()
            require(not self.body_returned)
            from fleet_managed_file_io import observed_io
            observed_io(self)
            self.body_returned = True
        except BaseException:
            self.refuse()
        # Exact descriptor close/fsync/readback observations do not prove the
        # missing ALL-writer fence or whole blocked-IO deadline. Keep helper
        # leases pending: no file/parent/native settlement follows.

    def write_status(self, text):
        from fleet_managed_file_io import capture
        return capture(self).status(text)

    def append_ledger(self, text):
        from fleet_managed_file_io import capture
        return capture(self).ledger(text)


def begin(native, kind, chat, thread, path):
    require(type(native) is child.NativeChild and kind in {'status', 'ledger'})
    action = native.action
    entry = native.entry
    require(native.runtime.action() is action and type(chat) is str
            and chat == entry.route.native_chat and type(thread) is str
            and type(native._terminal) is child.TurnEnd
            and native._terminal.owner is native and native._terminal.action is action
            and native._terminal.thread == thread)
    key = (action.request.work_id, kind)
    require(key not in entry._persistence_owners)
    home = native.runtime.selection.target_home
    require(type(path) is type(home) and path == home / ('status.json' if kind == 'status' else 'codex_ledger.jsonl'))
    root = home.lstat()
    require(stat.S_ISDIR(root.st_mode) and root.st_uid == os.getuid()
            and stat.S_IMODE(root.st_mode) == 0o700)
    original = Original(native, entry, action, native._process, native._terminal,
                        path, home, kind, (root.st_dev, root.st_ino, root.st_uid, stat.S_IMODE(root.st_mode)))
    observed = Observation(original)
    _remember_refusal(observed, original)  # Trusted actual seam, before callbacks/IO.
    entry._persistence_owners[key] = observed  # Strong BEFORE fork/qualification/IO.
    try:
        native._stdio_live(native._process)
        authority = _original_refusal(observed)
        observed.grant = coordinator.Control.fork_writer(authority.control, authority.parent,
            'persistence:' + uuid.uuid4().hex, 'helper')
        _REFUSAL_GRANTS[id(observed)] = (observed.grant, _grant_fields(observed.grant))
        observed._live()
        if kind == 'status':
            tmp = path.with_suffix('.tmp')
            try:
                tmp.lstat()
            except FileNotFoundError:
                pass
            else:
                require(False)  # Never overwrite an old/uncertain temporary file.
        return observed
    except BaseException:
        observed.refuse()
