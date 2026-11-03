"""One actual Codex child lease, bounded natural shutdown, no signal fallback.

This covers the owned leader/group and stdio only. Escaped descendants, native
home writers, account/Scope/thread admission and full terminal settlement still
need their own reviewed actual proof. Unknown never releases the child lease.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import uuid

import fleet_codex_storage as storage_module
import fleet_managed_ingress as ingress
import fleet_managed_runtime as runtime_module


MAX_STDERR = 1024 * 1024
MAX_WRITE = 32 * 1024 * 1024
MAX_FRAME = 32 * 1024 * 1024
MAX_DEPTH = 64
EXIT_DEADLINE = 5
_TURN_CONSUMER_METHODS = frozenset({
    "item/agentMessage/delta", "item/started", "item/completed",
    "item/reasoning/summaryTextDelta", "error",
    "thread/tokenUsage/updated", "turn/completed",
})


def require(ok, code="managed_native_child_unknown"):
    if not ok:
        raise ingress.Refused(code)


def _frame(raw, limit):
    """Original one-line JSON only; no lossy decode/coercion or error export."""
    require(type(raw) is bytes and 0 < len(raw) <= min(limit, MAX_FRAME)
            and raw.endswith(b"\n") and b"\n" not in raw[:-1],
            "managed_native_frame_unknown")
    try:
        text = raw.decode("utf-8", "strict")
        depth = 0
        quoted = escaped = False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                require(depth <= MAX_DEPTH, "managed_native_frame_unknown")
            elif char in "]}":
                depth -= 1
                require(depth >= 0, "managed_native_frame_unknown")

        def pairs(rows):
            value = {}
            for key, item in rows:
                require(key not in value, "managed_native_frame_unknown")
                value[key] = item
            return value

        def integer(value):
            require(len(value) <= 64, "managed_native_frame_unknown")
            return int(value)

        def number(value):
            require(len(value) <= 64, "managed_native_frame_unknown")
            value = float(value)
            require(math.isfinite(value), "managed_native_frame_unknown")
            return value

        def constant(value):
            raise ingress.Refused("managed_native_frame_unknown")

        value = json.loads(text, object_pairs_hook=pairs, parse_int=integer,
                           parse_float=number, parse_constant=constant)
        require(type(value) is dict, "managed_native_frame_unknown")
        pending = [value]
        while pending:
            item = pending.pop()
            if type(item) is str:
                item.encode("utf-8", "strict")
            elif type(item) is dict:
                pending.extend(item.keys());pending.extend(item.values())
            elif type(item) is list:
                pending.extend(item)
        if "id" in value:
            require(type(value["id"]) in (int, str), "managed_native_frame_unknown")
        if "method" in value:
            require(type(value["method"]) is str and bool(value["method"]),
                    "managed_native_frame_unknown")
        return value
    except ingress.Refused:
        raise
    except Exception:
        raise ingress.Refused("managed_native_frame_unknown") from None


@dataclass(frozen=True)
class NaturalExit:
    pid: int
    returncode: int
    owned_group_absent: bool
    stdio_eof: bool
    complete_descendant_exclusion: bool = False
    native_ready: bool = False


@dataclass(frozen=True, repr=False)
class TurnEnd:
    owner: object
    action: runtime_module.Action
    thread: str
    turn: str
    status: str


@dataclass(frozen=True, repr=False)
class NativeSettlement:
    owner: object
    action: runtime_module.Action
    process: object
    scope: storage_module.Scope
    terminal: TurnEnd | None
    physical: NaturalExit
    parent_ready: bool = False


class NativeChild:
    def __init__(self, entry):
        self.entry = entry
        self.runtime = entry.runtime
        self.action = self.runtime.action()
        require(self.action.scope.engine == "codex")
        self.grant = self.runtime.control.fork_writer(self.action.parent,
            "codex:" + uuid.uuid4().hex, "codex_child")
        self._attempted = self._closing = False
        self._process = self._scope = None
        self._stdin = self._stdout = self._stderr = None
        self._stream_limit = None
        self._reading = self._read_failed = False
        self._stdout_task = None
        self._stderr_task = None
        self._stderr_failure = None  # Private original failure, never formatted.
        self._stderr_observed = False
        self._pid = None
        self._observed_pid = self._observed_returncode = self._observed_group = None
        self._turn_binding = self._terminal = None

    def _live(self):
        from fleet_managed_diagnostic import validate_native
        validate_native(self)
        require(self.runtime.action() is self.action,
                "managed_native_action_drift")
        require(self.entry._native_owners.get(self.action.request.work_id) is self,
                "managed_native_owner_unknown")
        self.runtime.control.validate_writer(self.grant, self.action.scope)
        self.entry.binding()

    def _hold(self):
        from fleet_managed_persistence import hold_native_original
        if hold_native_original(self):
            return  # Pending helper's immutable original; never foreign fallback.
        from fleet_managed_diagnostic import hold_native
        hold_native(self)  # Unissued or unknown originals never select a mutable fallback.

    def _stdio_live(self, proc, *, writing=False, closing=False):
        self._live()
        require((closing or self._closing is False) and proc is self._process
                and self._pid is not None and type(proc.pid) is int and proc.pid == self._pid
                and (proc.returncode is None or type(proc.returncode) is int)
                and proc.stdin is self._stdin and proc.stdout is self._stdout
                and proc.stderr is self._stderr and self._stdin is not None
                and self._stdout is not None and self._stderr is not None,
                "managed_native_owner_unknown")
        require(storage_module.select() == self._scope, "managed_native_scope_drift")
        if writing:
            require(proc.returncode is None and os.getpgid(self._pid) == self._pid,
                    "managed_native_owner_unknown")

    async def read_frame(self, proc):
        """One owned stdout reader; UNKNOWN consumes possibly read bytes."""
        try:
            require(self._reading is False and self._read_failed is False,
                    "managed_native_reader_unknown")
            self._stdio_live(proc)
            self._reading = True
            self._stdout_task = asyncio.current_task()  # Strong ownership across await.
            raw = await self._stdout.readline()
            self._stdio_live(proc)
            require(type(raw) is bytes, "managed_native_frame_unknown")
            value = _frame(raw, self._stream_limit) if raw else None
            if value is not None and "id" in value and "method" in value:
                # Even a negative legacy approval reply starts an unleased task.
                # No managed server-request lifecycle is installed. Refuse before
                # startup accumulation, approval/card dispatch or response writes.
                require(False, "managed_native_notification_unknown"
                        if value["method"] in _TURN_CONSUMER_METHODS
                        else "managed_native_server_request_unbound")
            self._reading = False
            self._stdout_task = None
            return value  # EOF is physical input, not turn/account success.
        except BaseException:
            self._read_failed = True
            self._hold()
            raise  # Never switch pipes, reread uncertain bytes or retry.

    def require_response(self, message, request_id):
        try:
            self._stdio_live(self._process)
            require(type(request_id) is int and type(message) is dict
                    and type(message.get("id")) is int and message["id"] == request_id
                    and (set(message) == {"id", "result"} and type(message["result"]) is dict
                         or set(message) == {"id", "error"} and type(message["error"]) is dict),
                    "managed_native_response_unknown")
        except BaseException:
            self._hold()
            raise

    def response_error(self):
        self._hold()
        raise ingress.Refused("managed_native_rpc_error")

    def identifier(self, response, kind):
        try:
            self._stdio_live(self._process)
            require(kind in {"thread", "turn"} and type(response) is dict
                    and type(response.get("result")) is dict
                    and type(response["result"].get(kind)) is dict,
                    "managed_native_identifier_unknown")
            value = response["result"][kind].get("id")
            require(type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value),
                    "managed_native_identifier_unknown")
            return value  # Original identifier, never str()/fallback/rebased.
        except BaseException:
            self._hold()
            raise

    def terminal_matches(self, params, thread, turn):
        try:
            self._stdio_live(self._process)
            require(type(params) is dict and type(params.get("turn")) is dict
                    and type(params["turn"].get("id")) is str,
                    "managed_native_terminal_unknown")
            if params["turn"]["id"] != turn:
                return False  # A different native turn cannot settle this one.
            require(type(params.get("threadId")) is str and params["threadId"] == thread
                    and type(params["turn"].get("status")) is str
                    and params["turn"]["status"] in {"completed", "failed", "interrupted"},
                    "managed_native_terminal_unknown")
            return True  # Wire scope only; no parent/descendant/native-ready proof.
        except BaseException:
            self._hold()
            raise

    def bind_turn(self, thread, turn):
        """Bind the actual startup response IDs once, before stream consumers."""
        try:
            self._stdio_live(self._process)
            require(self._turn_binding is None and self._terminal is None
                    and all(type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
                            for value in (thread, turn)), "managed_native_turn_binding_unknown")
            self._turn_binding = (thread, turn)
        except BaseException:
            self._hold()
            raise

    def observe_terminal(self, params, thread, turn):
        """Retain this owned wire end; it grants no parent/runtime readiness."""
        try:
            require(self._turn_binding == (thread, turn) and self._terminal is None
                    and self.terminal_matches(params, thread, turn),
                    "managed_native_terminal_unknown")
            self._terminal = TurnEnd(self, self.action, thread, turn, params["turn"]["status"])
        except BaseException:
            self._hold()
            raise

    def require_terminal(self, thread, turn):
        """EOF without the actual matching end is UNKNOWN, never settlement."""
        try:
            self._stdio_live(self._process)
            end = self._terminal
            require(type(end) is TurnEnd and end.owner is self and end.action is self.action
                    and self._turn_binding == (thread, turn) == (end.thread, end.turn)
                    and end.status in {"completed", "failed", "interrupted"},
                    "managed_native_terminal_missing")
            return end
        except BaseException:
            self._hold()
            raise

    def notification_matches(self, event, thread, turn):
        """Qualify current-turn display/usage/error input before its consumers.

        This is not a question/approval grant or complete notification schema.
        Unknown methods retain their existing typed consumers; legacy aliases
        cannot turn an unscoped frame into display content in this branch.
        """
        try:
            self._stdio_live(self._process)
            require(type(event) is dict and not ({"type", "event"} & set(event)),
                    "managed_native_notification_unknown")
            method = event.get("method")
            if method is None:
                return "id" in event  # Existing control responses, not display.
            require(type(method) is str and bool(method),
                    "managed_native_notification_unknown")
            if "replay" in event:
                require(type(event["replay"]) is bool,
                        "managed_native_notification_unknown")
                if event["replay"]:
                    return False
            if method in {"item/autoApprovalReview/started", "item/autoApprovalReview/completed"}:
                require("id" not in event and type(event.get("params")) is dict,
                        "managed_native_notification_unknown")
                params = event["params"]
                got_thread, got_turn = params.get("threadId"), params.get("turnId")
                require(all(type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
                            for value in (thread, turn, got_thread, got_turn)),
                        "managed_native_notification_unknown")
                if got_thread != thread or got_turn != turn:
                    return False
                # No managed review/approval lifecycle or publication grant exists.
                # Refuse before the legacy consumer reads status/rationale bodies.
                require(False, "managed_native_approval_review_lifecycle_unbound")
            if method not in _TURN_CONSUMER_METHODS:
                return True  # No blanket semantic approval of other consumers.
            require("id" not in event and type(event.get("params")) is dict,
                    "managed_native_notification_unknown")
            params = event["params"]
            if method == "turn/completed":
                return self.terminal_matches(params, thread, turn)
            got_thread, got_turn = params.get("threadId"), params.get("turnId")
            require(all(type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
                        for value in (thread, turn, got_thread, got_turn)),
                    "managed_native_notification_unknown")
            if got_thread != thread or got_turn != turn:
                return False  # No body, question, usage, error or tool borrowing.
            if method in {"item/agentMessage/delta", "item/reasoning/summaryTextDelta"}:
                require(type(params.get("delta")) is str,
                        "managed_native_notification_unknown")
            elif method in {"item/started", "item/completed"}:
                item = params.get("item")
                require(type(item) is dict and type(item.get("type")) is str
                        and bool(item["type"]), "managed_native_notification_unknown")
                if (method == "item/completed" and item["type"] == "agentMessage"
                        and item.get("questions") is not None):
                    # Managed answers and the complete card lifecycle are unbound.
                    # Do not enter early map registration, default intents or the
                    # legacy card publisher even for a valid selectable shape.
                    require(False, "managed_native_question_lifecycle_unbound")
                if item["type"] == "agentMessage" and "text" in item:
                    require(type(item["text"]) is str, "managed_native_notification_unknown")
            elif method == "error":
                error = params.get("error")
                require(type(error) is dict and type(error.get("message")) is str
                        and type(params.get("willRetry")) is bool,
                        "managed_native_notification_unknown")
            elif method == "thread/tokenUsage/updated":
                require(type(params.get("tokenUsage")) is dict,
                        "managed_native_notification_unknown")
            return True
        except BaseException:
            self._hold()
            raise

    def turn_consumer_event(self, event):
        """Auxiliary/control input cannot enter current-turn display or usage.

        Called after existing typed question/approval/control consumers. The
        same method set was scope-qualified by notification_matches first.
        """
        try:
            self._stdio_live(self._process)
            require(type(event) is dict, "managed_native_notification_unknown")
            return event.get("method") in _TURN_CONSUMER_METHODS
        except BaseException:
            self._hold()
            raise

    async def write(self, proc, raw, *, guard=None):
        """One same-owner stdio write; drain is not native acceptance.

        The original child lease survives every partial/uncertain write. No
        failed bytes are replayed, and a changed action cannot borrow this owner.
        """
        try:
            self._stdio_live(proc, writing=True)
            require(type(raw) is bytes and 0 < len(raw) <= MAX_WRITE
                    and raw.endswith(b"\n") and b"\n" not in raw[:-1],
                    "managed_native_frame_unknown")
            require(storage_module.select() == self._scope
                    and os.getpgid(self._pid) == self._pid,
                    "managed_native_scope_unknown")
            if guard is not None:
                guard()
            self._stdio_live(proc, writing=True)
            self._stdin.write(raw)
            await asyncio.wait_for(self._stdin.drain(), EXIT_DEADLINE)
            self._stdio_live(proc, writing=True)
        except BaseException:
            self._hold()
            raise  # Bytes may have been written; preserve lease, never retry.

    async def spawn(self, scope, binary, cwd, limit):
        require(self._attempted is False, "managed_native_attempt_consumed")
        self._attempted = True  # Before prerequisites/exec; no alternate attempt.
        try:
            self._live()
            require(type(scope) is storage_module.Scope,
                    "managed_native_scope_unknown")
            require(type(binary) is str and binary and type(cwd) is str
                    and Path(cwd).is_absolute() and str(Path(cwd).resolve()) == cwd)
            require(type(limit) is int and 0 < limit <= 32 * 1024 * 1024)
            argv = scope.command([binary, "app-server", "--listen", "stdio://"])
            env = scope.env()
            selection = ingress.Selection.from_env(env, self.runtime.selection.target_home)
            require(selection == self.runtime.selection
                    and env.get("FLEET_BRIDGE_HOME") == str(selection.target_home)
                    and env.get("CODEX_HOME") == str(scope.home)
                    and env.get("CODEX_SQLITE_HOME") == str(scope.home)
                    and argv[:5] == [binary, "--strict-config", "app-server", "--listen", "stdio://"],
                    "managed_native_scope_unknown")
            self._scope = scope
            self._stream_limit = limit
            proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=limit, start_new_session=True)
            self._process = proc  # Retain returned owner even if identity check fails.
            self._stdin, self._stdout, self._stderr = proc.stdin, proc.stdout, proc.stderr
            self._observed_pid = proc.pid
            self._observed_returncode = proc.returncode
            require(type(self._observed_pid) is int and self._observed_pid > 0
                    and self._observed_returncode is None,
                    "managed_native_owner_unknown")
            self._observed_group = os.getpgid(self._observed_pid)
            require(self._observed_group == self._observed_pid,
                    "managed_native_owner_unknown")
            self._pid = self._observed_pid
            self._stdio_live(proc)
            return proc
        except BaseException:
            self._hold()
            raise

    def _own_stderr_task(self, task):
        require(type(task) is asyncio.Task and (self._stderr_task is None or self._stderr_task is task),
                "managed_native_stderr_owner_unknown")
        if self._stderr_task is None:
            self._stderr_task = task
            task.add_done_callback(self._stderr_done)

    def _stderr_done(self, task):
        # Observe a completed original Task even when UNKNOWN prevented finish.
        # No disk IO, release, retry or raw private exception export here.
        if task is self._stderr_task:
            try:
                self._stderr_failure = task.exception()
            except BaseException as error:
                self._stderr_failure = error
            self._stderr_observed = True

    def start_stderr(self):
        self._stdio_live(self._process)
        require(self._stderr_task is None, "managed_native_stderr_owner_unknown")
        task = asyncio.create_task(self.read_stderr())
        self._own_stderr_task(task)  # Strong ownership before any later guard.
        return task

    async def read_stderr(self):
        try:
            self._own_stderr_task(asyncio.current_task())
            self._stdio_live(self._process, closing=True)
            chunks, total = [], 0
            while True:
                self._stdio_live(self._process, closing=True)
                raw = await self._stderr.read(min(65536, MAX_STDERR + 1 - total))
                self._stdio_live(self._process, closing=True)
                require(type(raw) is bytes)
                if not raw:
                    return b"".join(chunks)  # Original private RAM, no helper log.
                total += len(raw)
                require(total <= MAX_STDERR, "managed_native_stderr_bound")
                chunks.append(raw)
        except BaseException:
            self._hold()
            raise

    async def finish(self, proc, stderr_task):
        require(self._closing is False, "managed_native_cleanup_consumed")
        self._closing = True  # Refusal/timeout/cancellation is not a retry prompt.
        try:
            self._own_stderr_task(stderr_task)
            self._live()
            require(self._pid is not None and proc is self._process
                    and type(stderr_task) is asyncio.Task and self._reading is False
                    and self._read_failed is False,
                    "managed_native_owner_unknown")
            self._stdio_live(proc, closing=True)
            # asyncio's watcher may already have reaped the leader. Never signal
            # this PGID: it could be reused. A timeout holds; legacy TERM/wait is
            # unreachable in this selected branch.
            self._stdin.close()
            code = await asyncio.wait_for(proc.wait(), EXIT_DEADLINE)
            require(type(code) is int and type(proc.returncode) is int and proc.returncode == code,
                    "managed_native_exit_unknown")
            self._stdio_live(proc, closing=True)
            tail = await asyncio.wait_for(self._stdout.read(MAX_STDERR + 1), EXIT_DEADLINE)
            self._stdio_live(proc, closing=True)
            require(type(tail) is bytes and tail == b"" and self._stdout.at_eof(),
                    "managed_native_stdout_tail_unknown")
            await asyncio.wait_for(asyncio.shield(stderr_task), EXIT_DEADLINE)
            self._stdio_live(proc, closing=True)
            require(self._stderr.at_eof(), "managed_native_stdio_unknown")
            try:
                os.killpg(self._pid, 0)  # Existence query only; never TERM/KILL.
            except ProcessLookupError:
                pass
            else:
                raise ingress.Refused("managed_native_group_unknown")
            self._live()
            require(storage_module.select() == self._scope,
                    "managed_native_scope_drift")
            work_id = self.action.request.work_id
            require(work_id not in self.entry._native_settlements,
                    "managed_native_settlement_conflict")
            self.runtime.control.release_writer(self.grant)
            # Exact accepted settlement only. A returned child whose identity
            # was refused stays strongly reachable even after the turn unwinds.
            require(self.entry._native_owners.get(self.action.request.work_id) is self,
                    "managed_native_owner_unknown")
            physical = NaturalExit(self._pid, code, True, True)
            # Keep the original owner/action/process/Scope/wire facts after
            # physical release; a future parent predicate cannot invent them
            # from returncode or a generic readiness boolean. Other lifecycle
            # tails and escaped descendants are still unproved.
            self.entry._native_settlements[work_id] = NativeSettlement(
                self, self.action, proc, self._scope, self._terminal, physical)
            del self.entry._native_owners[self.action.request.work_id]
            return physical
        except BaseException:
            self._hold()
            raise  # Preserve grant and uncertain process; no signal/retry/replay.
