"""Original initial-display callback ownership; no delivery/readiness grant.

Only the actual enrolled Entry -> AppFrontend seam can issue a tail. Original
connection metadata is retained at the real accept seam, not supplied on wire.
Written/drained bytes are component observations, not recipient acceptance.
Helpers and strong owners remain pending on both observations and UNKNOWN.
"""
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import types
import uuid

from feishu_bridge import fleet_managed_diagnostic as diagnostic
from feishu_bridge import fleet_managed_ingress as ingress
from feishu_bridge import fleet_target_control as coordinator

_FRONTS = {}
_CONNECTIONS = {}
_CONNECTION_STATES = {}
_TAILS = {}
_STATES = {}
_ATTEMPTED = set()
MAX_RECIPIENTS = 128
MAX_BYTES = 2 * 1024 * 1024
DRAIN_TIMEOUT = 5.0


def require(ok):
    if not ok:
        raise ingress.Refused('managed_output_unknown')


def method(value):
    require(type(value) is types.MethodType)
    return (value.__self__, value.__func__)


def same_method(value, saved):
    return (type(value) is types.MethodType and value.__self__ is saved[0]
            and value.__func__ is saved[1])


@dataclass(frozen=True, repr=False)
class FrontOriginal:
    entry: object
    dispatch: object
    frontend: object
    loop: object
    task: object
    writers: object
    gateway_writers: object
    broadcast: tuple
    callback: tuple


@dataclass(frozen=True, repr=False)
class Connection:
    frontend: object
    reader: object
    writer: object
    loop: object
    accept_task: object
    transport: object
    protocol: object
    write: tuple
    drain: tuple
    close: tuple
    wait_closed: tuple


class Tail:
    __slots__ = ()
    def __new__(cls):
        raise ingress.Refused('managed_output_unissued')


@dataclass(frozen=True, repr=False)
class Original:
    tail: Tail
    front: FrontOriginal
    connections: tuple
    line: bytes
    digest: str
    order: int


@dataclass(repr=False)
class State:
    disposition: str = 'PENDING'
    grant: object = field(default=None, repr=False)
    grant_fields: tuple | None = field(default=None, repr=False)
    handle: object = field(default=None, repr=False)
    task: object = field(default=None, repr=False)
    callback_attempted: bool = False
    write_states: list = field(default_factory=list)
    drain_states: list = field(default_factory=list)
    drain_coroutines: list = field(default_factory=list, repr=False)
    drain_tasks: list = field(default_factory=list, repr=False)


def _dispatch(b):
    from feishu_bridge.fleet_managed_runtime import CURRENT
    require(_FRONTS.get((id(b.entry), id(b.dispatch.action))) is b
            and diagnostic._ENTRIES.get((id(b.entry), id(b.dispatch.action))) == (b.entry, b.dispatch)
            and CURRENT.get() is b.dispatch.action and b.entry.runtime is b.dispatch.runtime)
    diagnostic.validate_action(b.dispatch.runtime, b.dispatch.action)


def _front(b):
    _dispatch(b)
    f = b.frontend
    require(b.entry.frontend is f and f._loop is b.loop
            and f._writers is b.writers and f._gateway_writers is b.gateway_writers
            and same_method(f.broadcast, b.broadcast)
            and same_method(f._write_all, b.callback))


def remember_frontend(entry):
    """Actual Entry._execute only, before the initial frontend callback."""
    from feishu_bridge.fleet_managed_entry import Entry
    from feishu_bridge.fleet_managed_runtime import CURRENT
    from feishu_bridge.app_frontend import AppFrontend
    require(type(entry) is Entry)
    pair = diagnostic._ENTRIES.get((id(entry), id(CURRENT.get())))
    require(pair is not None and pair[0] is entry)
    o = pair[1]
    diagnostic.validate_action(o.runtime, o.action)
    f = entry.frontend
    require(type(f) is AppFrontend)
    # Legacy seam fixtures replace broadcast with a pure capture callback. They
    # do not execute the real broadcast branch or enroll any output authority.
    if not (type(f.broadcast) is types.MethodType
            and f.broadcast.__func__ is AppFrontend.broadcast):
        return
    key = (id(entry), id(o.action))
    require(key not in _FRONTS)
    b = FrontOriginal(entry, o, f, f._loop, asyncio.current_task(), f._writers,
                      f._gateway_writers, method(f.broadcast), method(f._write_all))
    _FRONTS[key] = b  # Strong before any later qualification/effect.
    require(b.task is not None and b.loop is asyncio.get_running_loop())
    _front(b)


def remember_connection(frontend, reader, writer):
    """Actual _on_conn metadata seam; this record grants no writer authority."""
    from feishu_bridge.fleet_managed_entry import Entry
    handler = frontend._managed_ingress
    if handler is None:
        return
    require(type(handler) is types.MethodType and type(handler.__self__) is Entry
            and handler.__func__ is Entry.handle_frame and handler.__self__.frontend is frontend
            and type(writer) is asyncio.StreamWriter and type(reader) is asyncio.StreamReader)
    key = (id(frontend), id(writer))
    require(key not in _CONNECTIONS)
    c = Connection(frontend, reader, writer, writer._loop, asyncio.current_task(),
                   writer.transport, writer._protocol, method(writer.write), method(writer.drain),
                   method(writer.close), method(writer.wait_closed))
    _CONNECTIONS[key] = c  # Original writer/transport before later checks.
    _CONNECTION_STATES[id(c)] = {'state': 'OPEN', 'close': 'OPEN', 'task': None, 'coroutine': None}
    require(c.loop is frontend._loop and c.loop is asyncio.get_running_loop()
            and c.accept_task is not None)
    _connection(c)


def _connection(c, *, closing=False):
    require(_CONNECTIONS.get((id(c.frontend), id(c.writer))) is c
            and type(c.writer) is asyncio.StreamWriter
            and c.writer._loop is c.loop and c.writer.transport is c.transport
            and c.writer._protocol is c.protocol
            and same_method(c.writer.write, c.write) and same_method(c.writer.drain, c.drain)
            and same_method(c.writer.close, c.close) and same_method(c.writer.wait_closed, c.wait_closed))
    state = _CONNECTION_STATES[id(c)]
    require(state['state'] == ('DISCONNECTED' if closing else 'OPEN'))


def _original(tail):
    o = _TAILS.get(id(tail))
    require(type(tail) is Tail and o is not None and o.tail is tail)
    return o


def _hold(o):
    s = _STATES[id(o.tail)]
    s.disposition = 'UNKNOWN_NO_RETRY'  # Before any diagnostic qualification/IO.
    diagnostic._hold(o.front.dispatch)  # Private original target, never mutable frontend/runtime.


def _live(o):
    require(_original(o.tail) is o and _STATES[id(o.tail)].disposition != 'UNKNOWN_NO_RETRY')
    b = o.front
    _front(b)
    require(type(o.line) is bytes and hashlib.sha256(o.line).hexdigest() == o.digest)
    s = _STATES[id(o.tail)]
    require(type(s.grant) is coordinator.Grant
            and diagnostic.exact(diagnostic.grant_fields(s.grant), s.grant_fields))
    d = b.dispatch
    with coordinator.Control.tx(d.control) as db:
        diagnostic._work(d, db)
        state, epoch, _ = coordinator.Control.status(d.control, db, {'OPEN', 'DRAINING'})
        original_state, original_epoch = diagnostic._EPOCHS[id(d.action)]
        require((state == original_state and epoch == original_epoch)
                or (original_state == 'OPEN' and state == 'DRAINING' and epoch == original_epoch + 1))
        row = coordinator.Control.verify_grant(d.control, db, s.grant, {'helper'})
        require(row[4] == d.parent.lease_id and s.grant.work_scope == d.scope_values)
    _front(b)
    for c in o.connections:
        _connection(c)
        require(c.frontend is b.frontend and c.loop is b.loop
                and c.writer in b.writers and c.writer in b.gateway_writers)


def begin(frontend, event, entry):
    """Real AppFrontend.broadcast branch for Entry.inject's original user event."""
    from feishu_bridge.fleet_managed_runtime import CURRENT
    b = _FRONTS.get((id(entry), id(CURRENT.get())))
    require(b is not None and b.entry is entry and b.frontend is frontend)
    key = (id(entry), id(b.dispatch.action))
    require(key not in _ATTEMPTED)
    _ATTEMPTED.add(key)  # Consume before event serialization, lease or scheduling.
    tail = object.__new__(Tail)
    connections = tuple(_CONNECTIONS[(id(frontend), id(w))]
        for w in tuple(b.writers) if w in b.gateway_writers
        and (id(frontend), id(w)) in _CONNECTIONS)
    # All values to be emitted are original fixed primitive fields. Never call
    # frontend._with_chat or reuse a mutable event after this boundary.
    o = Original(tail, b, connections, b'', '', 1)
    _TAILS[id(tail)] = o
    _STATES[id(tail)] = State(write_states=['NOT_ATTEMPTED'] * len(connections),
                              drain_states=['NOT_ATTEMPTED'] * len(connections))
    try:
        _front(b)
        require(type(event) is dict and all(type(k) is str for k in event)
                and all(type(v) in {str, int, float, bool, type(None)} for v in event.values()))
        payload = json.loads(b.dispatch.request.payload)
        expected = {'ev': 'user', 'text': payload['text'], 'origin': 'web',
                    'chat_id': payload['display_chat'], 'user': payload['display_actor'],
                    'work_id': payload['work_id'], 'work_chat': payload['work_chat'],
                    'work_actor': payload['actor'], 'client': payload['client']}
        if payload['display_actor']:
            expected['from'] = payload['display_actor']
        require(event == expected)  # Full exact original bytes/scope, no alias/coercion.
        line = (json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8', 'strict')
        require(0 < len(line) <= MAX_BYTES)
        o = Original(tail, b, connections, line, hashlib.sha256(line).hexdigest(), 1)
        _TAILS[id(tail)] = o
        s = _STATES[id(tail)]
        d = b.dispatch
        s.grant = coordinator.Control.fork_writer(d.control, d.parent, 'output:' + uuid.uuid4().hex, 'helper')
        s.grant_fields = diagnostic.grant_fields(s.grant)
        # No-recipient means no fabricated pending event or future recipient.
        require(b.loop is asyncio.get_running_loop() and 0 < len(connections) <= MAX_RECIPIENTS
                and len(connections) == len([w for w in b.writers if w in b.gateway_writers]))
        _live(o)
        s.handle = b.loop.call_soon_threadsafe(b.frontend._write_all, o.line, tail)
        # Retain returned handle BEFORE checking its shape/registration facts.
        require(isinstance(s.handle, asyncio.Handle))
        _live(o)
        return tail
    except BaseException:
        _hold(o)
        raise ingress.Refused('managed_output_unknown') from None


def callback(frontend, line, tail):
    o = _original(tail)
    s = _STATES[id(tail)]
    if s.callback_attempted:
        _hold(o)
        return
    s.callback_attempted = True  # Before any callback guard or possible write.
    try:
        require(o.front.frontend is frontend and type(line) is bytes and line == o.line
                and isinstance(s.handle, asyncio.Handle))
        _live(o)
        for i, c in enumerate(o.connections):
            _live(o)
            s.write_states[i] = 'ATTEMPTED'
            # Only the original, still-qualified bound method/recipient.
            c.write[1](c.write[0], o.line)
            s.write_states[i] = 'RETURNED'
            _live(o)
        coroutine = _drain(o)
        s.drain_coroutines.append(coroutine)
        s.task = o.front.loop.create_task(coroutine)
        require(type(s.task) is asyncio.Task)
        s.task.add_done_callback(lambda task: _done(o, task))
        _live(o)
    except BaseException:
        _hold(o)  # No event-loop exception/raw error export or re-write.


async def _drain(o):
    s = _STATES[id(o.tail)]
    try:
        require(asyncio.current_task() is s.task)
        for i, c in enumerate(o.connections):
            _live(o)
            s.drain_states[i] = 'ATTEMPTED'
            coroutine = c.drain[1](c.drain[0])
            s.drain_coroutines.append(coroutine)
            task = o.front.loop.create_task(coroutine)
            s.drain_tasks.append(task)  # Strong before later guard/await.
            require(type(task) is asyncio.Task)
            _live(o)
            await asyncio.wait_for(task, DRAIN_TIMEOUT)
            _live(o)
            s.drain_states[i] = 'RETURNED'
        s.disposition = 'WRITES_AND_DRAINS_OBSERVED_PENDING'
    except BaseException:
        _hold(o)


def _done(o, task):
    try:
        require(task is _STATES[id(o.tail)].task and task.done() and not task.cancelled())
        if task.exception() is not None:
            _hold(o)
    except BaseException:
        _hold(o)


def _pending(frontend=None, entry=None, runtime=None):
    return tuple(o for o in _TAILS.values() if
                 (frontend is None or o.front.frontend is frontend)
                 and (entry is None or o.front.entry is entry)
                 and (runtime is None or o.front.dispatch.runtime is runtime))


def refuse_entry_delivery(entry):
    if _pending(entry=entry):
        raise coordinator.Refused('admission_closed')


def refuse_runtime_delivery(runtime):
    if _pending(runtime=runtime):
        raise coordinator.Refused('admission_closed')


def before_greet(frontend, writer):
    pending = _pending(frontend=frontend)
    if pending:
        for o in pending:
            _hold(o)
        raise ingress.Refused('managed_output_replay_unbound')


def before_disconnect(frontend, writer):
    c = _CONNECTIONS.get((id(frontend), id(writer)))
    if c is None:
        return None
    state = _CONNECTION_STATES[id(c)]
    state['state'] = 'DISCONNECTED'
    for o in _pending(frontend=frontend):
        if any(saved is c for saved in o.connections):
            _hold(o)
    return c  # Private original metadata, no new grant or close authority by construction.


async def close_connection(c):
    state = _CONNECTION_STATES.get(id(c))
    require(state is not None and _CONNECTIONS.get((id(c.frontend), id(c.writer))) is c)
    if state['close'] != 'OPEN':
        return  # UNKNOWN/CLOSED integers/objects cannot be reused or retried.
    state['close'] = 'UNKNOWN_NO_RETRY'  # Covers qualification refusal BEFORE close.
    try:
        _connection(c, closing=True)
        c.close[1](c.close[0])
        _connection(c, closing=True)
        coroutine = c.wait_closed[1](c.wait_closed[0])
        state['coroutine'] = coroutine
        state['task'] = c.loop.create_task(coroutine)
        require(type(state['task']) is asyncio.Task)
        await asyncio.wait_for(state['task'], DRAIN_TIMEOUT)
        _connection(c, closing=True)
        state['close'] = 'CLOSE_AND_WAIT_OBSERVED'
    except BaseException:
        return  # Original connection/tasks/partial close retained; never retry.


def facts(tail):
    o = _original(tail)
    s = _STATES[id(tail)]
    return {'disposition': s.disposition, 'recipients': len(o.connections),
            'writes_returned': sum(x == 'RETURNED' for x in s.write_states),
            'drains_returned': sum(x == 'RETURNED' for x in s.drain_states),
            'event_sha256': o.digest, 'original_callback_attempted': s.callback_attempted,
            'gateway_durable_acceptance': False, 'client_receipt_verified': False,
            'output_settlement': False, 'helper_released': False,
            'parent_ready': False, 'native_ready': False, 'retry_allowed': False}
