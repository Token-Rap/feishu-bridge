"""Adapter between strict ingress, the one coordinator, and bridge writers.

No target store is created, repaired, reopened or replayed on construction.
An explicitly authorized loader and complete bridge lifecycle wiring are still
required before runtime adoption. Exceptions keep uncertain committed grants.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
from dataclasses import dataclass, field
import json
import uuid

from feishu_bridge import fleet_managed_ingress as ingress
from feishu_bridge import fleet_target_control as control


@dataclass(frozen=True, repr=False)
class Action:
    runtime: "Runtime"
    parent: control.Grant = field(repr=False)
    scope: control.WorkScope
    request: ingress.Request = field(repr=False)


CURRENT = contextvars.ContextVar("managed_bridge_action", default=None)


@dataclass(repr=False)
class _MapWriteGuard:
    runtime: "Runtime"
    action: Action
    child: control.Grant = field(repr=False)
    db: object = field(repr=False)
    path: object = field(repr=False)
    active: bool = True

    def __call__(self, path, data):
        # This closure is valid only inside the original, still-open coordinator
        # transaction and exact action. An inherited/escaped context is no grant.
        if (not self.active or CURRENT.get() is not self.action or path != self.path
                or type(data) is not dict):
            raise ingress.Refused("managed_map_scope_unknown")
        self.runtime.control.verify_grant(self.db, self.child, {"session_map"})
        if self.child.work_scope != self.action.scope.values():
            raise ingress.Refused("managed_map_scope_unknown")
        from feishu_bridge import fleet_managed_map
        before = fleet_managed_map.read(path)
        key = self.action.scope.work_chat
        # The immutable WORK scope is the reviewed canonical map key here.
        # Native chat -> engine-key binding still belongs to the actual loader;
        # no suffix guessing or alternate key can authorize another row.
        if key not in before or key not in data:
            raise ingress.Refused("managed_map_scope_unknown")
        old, new = before[key], data[key]
        def encoded(value):
            def json_shape(node):
                if type(node) is dict:
                    if any(type(k) is not str for k in node):
                        raise ingress.Refused("managed_map_scope_unknown")
                    for child in node.values():
                        json_shape(child)
                elif type(node) is list:
                    for child in node:
                        json_shape(child)
                elif type(node) not in {str, int, float, bool, type(None)}:
                    raise ingress.Refused("managed_map_scope_unknown")
            try:
                json_shape(value)
                return json.dumps(value, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), allow_nan=False).encode("utf-8")
            except (ValueError, TypeError, UnicodeError):
                raise ingress.Refused("managed_map_scope_unknown") from None
        if encoded({k: v for k, v in before.items() if k != key}) != encoded(
                {k: v for k, v in data.items() if k != key}):
            raise ingress.Refused("managed_map_scope_unknown")
        if (type(old) is not dict or type(new) is not dict
                or not {"active", "sessions"} <= set(old)
                or set(old) != set(new)
                or encoded({k: v for k, v in old.items() if k not in {"active", "sessions"}})
                != encoded({k: v for k, v in new.items() if k not in {"active", "sessions"}})
                or type(old["sessions"]) is not dict or type(new["sessions"]) is not dict):
            raise ingress.Refused("managed_map_scope_unknown")
        if old["active"] is not None:
            control.identity(old["active"])
            if old["active"] not in old["sessions"]:
                raise ingress.Refused("managed_map_scope_unknown")
        active = new["active"]
        if active is not None:
            control.identity(active)
            if active not in new["sessions"]:
                raise ingress.Refused("managed_map_scope_unknown")
        # All old SIDs remain. Only the selected active record's normal name/ts
        # fields may update; historic rows/extras cannot be lost or normalized.
        if not set(old["sessions"]) <= set(new["sessions"]):
            raise ingress.Refused("managed_map_scope_unknown")
        added = set(new["sessions"]) - set(old["sessions"])
        if added and added != {active}:
            raise ingress.Refused("managed_map_scope_unknown")
        for sid, record in old["sessions"].items():
            after = new["sessions"][sid]
            if sid != active:
                if encoded(record) != encoded(after):
                    raise ingress.Refused("managed_map_scope_unknown")
            elif (type(record) is not dict or type(after) is not dict
                  or set(record) != set(after)
                  or encoded({k: v for k, v in record.items() if k not in {"name", "ts"}})
                  != encoded({k: v for k, v in after.items() if k not in {"name", "ts"}})):
                raise ingress.Refused("managed_map_scope_unknown")
        if active is not None:
            record = new["sessions"][active]
            if (type(record) is not dict or not {"name", "ts"} <= set(record)
                    or type(record["name"]) is not str or type(record["ts"]) is not str):
                raise ingress.Refused("managed_map_scope_unknown")


class Runtime:
    def __init__(self, selection: ingress.Selection):
        self.selection = selection
        self.control = control.bind(selection.directory, str(selection.target_home), selection.selector_id)
        self._dispatch_lock = asyncio.Lock()

    def prepare(self, req, *, work_chat, display_chat, engine, bot, gateway_connection):
        request = ingress.gateway_message(req, work_chat=work_chat, display_chat=display_chat,
            engine=engine, bot=bot, gateway_connection=gateway_connection)
        payload = json.loads(request.payload)
        scope = control.WorkScope(work_chat, display_chat, engine, payload["actor"],
                                  payload["client"], "gateway_human")
        scope.values()
        return request, scope

    def admit(self, request, scope):
        if type(request) is not ingress.Request or type(scope) is not control.WorkScope:
            raise ingress.Refused("managed_scope_unknown")
        return self.control.admit(request.work_id, scope, request.payload)

    def verify_queued(self, request, scope):
        """Match the existing gateway reservation before a route ACK or spawn.

        No admit/claim/replay is performed here. The exact request may still be
        retained when drain wins before its dispatch task runs.
        """
        from feishu_bridge.fleet_managed_output import refuse_runtime_delivery
        refuse_runtime_delivery(self)
        from feishu_bridge.fleet_managed_persistence import refuse_pending_delivery
        refuse_pending_delivery(self)
        from feishu_bridge.fleet_managed_diagnostic import refuse_runtime_delivery
        refuse_runtime_delivery(self)
        if type(request) is not ingress.Request or type(scope) is not control.WorkScope:
            raise ingress.Refused("managed_scope_unknown")
        with self.control.tx() as db:
            self.control.status(db, {"OPEN"})
            row = db.execute("SELECT work_chat,display_chat,engine,actor,client,origin,payload,state "
                             "FROM work WHERE work_id=?", (request.work_id,)).fetchone()
            control.require(row == scope.values() + (request.payload, "QUEUED"),
                            "managed_reservation_unknown")

    def message(self):
        action = self.action()
        request = action.request
        if (type(request) is not ingress.Request or request.work_id != action.parent.work_id
                or (request.work_chat, request.display_chat, request.engine) != action.scope.values()[:3]):
            raise ingress.Refused("managed_message_unknown")
        # Request bytes were matched to the original durable row at claim. This
        # is the full payload, not a shortened inflight notice or a text lookup.
        return json.loads(request.payload)

    def hold_unknown(self):
        # A failed diagnostic/transition cannot become a claimed successful hold.
        # The original unresolved parent/child leases already remain committed.
        from feishu_bridge.fleet_managed_persistence import hold_runtime_original
        original = hold_runtime_original(self)
        if original is not None:
            return original  # No mutable-control fallback for an enrolled tail.
        from feishu_bridge.fleet_managed_diagnostic import hold_runtime
        dispatch = hold_runtime(self)
        if dispatch is not None:
            return dispatch
        try:
            snap = self.control.snapshot()
            if snap["state"] != "UNKNOWN":
                self.control.mark_unknown(snap["epoch"])
            return True
        except control.Refused:
            return False

    def action(self):
        action = CURRENT.get()
        if type(action) is not Action or action.runtime is not self:
            raise ingress.Refused("managed_writer_unknown")
        from feishu_bridge.fleet_managed_diagnostic import validate_action
        validate_action(self, action)
        self.control.validate_writer(action.parent, action.scope)
        return action

    @contextlib.contextmanager
    def map_transaction(self):
        action = self.action()
        from feishu_bridge import fleet_sessions
        path = self.selection.target_home / "sessions.json"
        if fleet_sessions.SESSIONS_FILE != path or path != path.resolve():
            raise ingress.Refused("managed_map_home_unknown")
        child = self.control.fork_writer(action.parent, "map:" + uuid.uuid4().hex, "session_map")
        guard = None
        try:
            # Same admission DB, same existing work scope, complete physical map
            # transaction. No flock or second lock/fence source is substituted.
            with self.control.tx() as db:
                self.control.status(db, {"OPEN", "DRAINING"})
                self.control.verify_grant(db, child, {"session_map"})
                if child.work_scope != action.scope.values():
                    raise ingress.Refused("managed_writer_scope_unknown")
                guard = _MapWriteGuard(self, action, child, db, path)
                yield guard
            self.control.release_writer(child)
        except BaseException:
            self.hold_unknown()
            raise  # Keep the committed child lease and any uncertain .tmp/map.
        finally:
            if guard is not None:
                guard.active = False

    async def execute_new(self, request, scope, execute, terminal):
        """Dispatch this exact newly admitted request once; never a boot drain.

        terminal is a bridge-owned lifecycle predicate, not a frame flag. It must
        positively attest actual terminal/reaping and all continuation settlement.
        The coordinator independently refuses settlement with any child lease.
        """
        async with self._dispatch_lock:
            from feishu_bridge.fleet_managed_output import refuse_runtime_delivery
            refuse_runtime_delivery(self)
            from feishu_bridge.fleet_managed_diagnostic import (control_fields, refuse_runtime_delivery,
                remember_dispatch, settled_dispatch)
            refuse_runtime_delivery(self)
            original_control = self.control
            original_physical = control_fields(original_control)
            parent, payload = original_control.claim(request.work_id, "dispatch:" + uuid.uuid4().hex)
            if payload != request.payload or parent.work_scope != scope.values():
                self.hold_unknown()
                raise ingress.Refused("managed_claim_scope_unknown")
            try:
                original_control.mark_dispatching(parent)
            except control.Refused:
                original_control.return_undispatched(parent)  # Exact pre-dispatch only.
                raise
            action = Action(self, parent, scope, request)
            token = CURRENT.set(action)
            try:
                remember_dispatch(self, action, original_control, original_physical)
                await execute(json.loads(payload))
                result = terminal(scope)
                if type(result) is not str or result not in {"completed", "failed"}:
                    raise ingress.Refused("managed_lifecycle_unknown")
                self.action()  # Reject callback drift before choosing a settlement target.
                original_control.settle(parent, result)
                settled_dispatch(self, action)
            except BaseException:
                self.hold_unknown()
                raise  # No cleanup/retry/replay of a committed dispatch INTENT.
            finally:
                CURRENT.reset(token)


def selected(environ, home):
    selection = ingress.Selection.from_env(environ, home)
    return Runtime(selection) if selection is not None else None
