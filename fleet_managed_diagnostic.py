"""Original dispatched-work diagnostics, not readiness or lifecycle settlement.

Trusted execute/Entry seams retain authority before their first await/callback.
Mutable public runtime/native/Entry pointers never choose a diagnostic store.
Private records are RAM ownership under the existing trusted-code assumption,
not an external one-shot issuer, native-home fence or blocked-IO deadline.
"""
from dataclasses import dataclass

import fleet_managed_ingress as ingress
import fleet_target_control as coordinator

_ORIGINALS = {}
_EPOCHS = {}
_ATTEMPTS = {}
_ENTRIES = {}
_NATIVES = {}
_SETTLED = set()


def require(ok):
    if not ok:
        raise ingress.Refused('managed_native_diagnostic_unknown')


def exact(a, b):
    if type(a) is not type(b):
        return False
    if type(a) is tuple:
        return len(a) == len(b) and all(exact(x, y) for x, y in zip(a, b))
    return a == b


def control_fields(c):
    return (c.directory,c.target,c.selector,c.db,c.selector_file,c.dir_identity,
            c.db_identity,c.selector_identity,c.selector_bytes)


def grant_fields(g):
    return (g.target_home,g.selector_id,g.lease_id,g.kind,g.work_id,g.engine,g.token,g.work_scope)


@dataclass(frozen=True, repr=False)
class Original:
    runtime: object
    control: object
    physical: tuple
    selection: object
    selection_values: tuple
    action: object
    parent: object
    parent_values: tuple
    scope: object
    scope_values: tuple
    request: object
    request_values: tuple


@dataclass(frozen=True, repr=False)
class NativeOriginal:
    native: object
    entry: object
    dispatch: Original
    grant: object
    grant_values: tuple


def _facts(o):
    # Only privately captured objects are consulted; never a current public
    # runtime.control/selection, native.runtime/action or Entry.runtime target.
    require(_ORIGINALS.get(id(o.action)) is o and id(o.action) not in _SETTLED
            and type(o.control) is coordinator.Control
            and exact(control_fields(o.control), o.physical)
            and type(o.selection) is ingress.Selection
            and o.physical[0] == o.selection.directory
            and o.physical[1] == str(o.selection.target_home)
            and o.physical[2] == o.selection.selector_id
            and exact((o.selection.directory,o.selection.selector_id,o.selection.target_home),o.selection_values)
            and o.action.runtime is o.runtime and o.action.parent is o.parent
            and o.action.scope is o.scope and o.action.request is o.request
            and type(o.parent) is coordinator.Grant
            and exact(grant_fields(o.parent),o.parent_values)
            and type(o.scope) is coordinator.WorkScope and exact(o.scope.values(),o.scope_values)
            and type(o.request) is ingress.Request
            and exact((o.request.work_id,o.request.work_chat,o.request.display_chat,
                       o.request.engine,o.request.payload),o.request_values))


def _work(o, db):
    _facts(o)
    coordinator.Control.verify_grant(o.control, db, o.parent, {'work_dispatch'})
    require(o.parent.work_scope == o.scope_values and o.parent.work_id == o.request.work_id)
    row = db.execute('SELECT claim_id,state,payload,work_chat,display_chat,engine,actor,client,origin '
                     'FROM work WHERE work_id=?', (o.request.work_id,)).fetchone()
    require(exact(row, (o.parent.lease_id,'DISPATCHING',o.request.payload,*o.scope_values)))


def remember_dispatch(actual, action, c, physical):
    # Private trusted seam only. A constructed Original/Action alone cannot
    # enroll; the original CURRENT and exact persistent dispatched parent qualify.
    from fleet_managed_runtime import Runtime, Action, CURRENT
    require(type(actual) is Runtime and type(action) is Action and CURRENT.get() is action
            and action.runtime is actual and type(c) is coordinator.Control
            and id(action) not in _ORIGINALS)
    selection = actual.selection
    o = Original(actual,c,physical,selection,
                 (selection.directory,selection.selector_id,selection.target_home),action,
                 action.parent,grant_fields(action.parent),action.scope,action.scope.values(),
                 action.request,(action.request.work_id,action.request.work_chat,action.request.display_chat,
                                 action.request.engine,action.request.payload))
    _ORIGINALS[id(action)] = o  # Strong BEFORE qualification/transaction/await.
    require(actual.control is c)
    with coordinator.Control.tx(c) as db:
        _work(o,db)
        state, epoch, _ = coordinator.Control.status(c,db,{'OPEN','DRAINING'})
    _facts(o)
    _EPOCHS[id(action)] = (state,epoch)  # Only after positive transaction return.


def validate_action(actual, action):
    o = _ORIGINALS.get(id(action))
    if o is None:
        return  # Unenrolled predecessor interfaces are not certified here.
    _facts(o)
    require(o.runtime is actual and actual.control is o.control and actual.selection is o.selection)
    if id(action) in _ATTEMPTS:
        raise coordinator.Refused('admission_closed')  # Preserve original closed-admission refusal.


def bind_entry(entry):
    from fleet_managed_entry import Entry
    from fleet_managed_runtime import CURRENT
    require(type(entry) is Entry)
    o = _ORIGINALS.get(id(CURRENT.get()))
    require(o is not None and o.runtime is entry.runtime)
    validate_action(o.runtime,o.action)
    old = _ENTRIES.get((id(entry),id(o.action)))
    require(old is None or old == (entry,o))
    _ENTRIES[(id(entry),id(o.action))] = (entry,o)  # Before frontend/turn callbacks.


def bind_native(native, entry):
    from fleet_managed_child import NativeChild
    from fleet_managed_runtime import CURRENT
    o = _ORIGINALS.get(id(CURRENT.get()))
    require(type(native) is NativeChild and o is not None
            and _ENTRIES.get((id(entry),id(o.action))) == (entry,o)
            and native.entry is entry and native.runtime is o.runtime and native.action is o.action
            and entry._native_owners.get(o.request.work_id) is native and id(native) not in _NATIVES)
    n = NativeOriginal(native,entry,o,native.grant,grant_fields(native.grant))
    _NATIVES[id(native)] = n  # Strong before later guard or returned Process.
    validate_action(o.runtime,o.action)
    coordinator.Control.validate_writer(o.control,n.grant,o.scope)


def validate_native(native):
    n = _NATIVES.get(id(native))
    require(n is not None and n.native is native)
    o = n.dispatch
    validate_action(o.runtime,o.action)
    require(native.entry is n.entry and n.entry.runtime is o.runtime
            and native.runtime is o.runtime and native.action is o.action
            and native.grant is n.grant and exact(grant_fields(n.grant),n.grant_values))


def _hold(o):
    key = id(o.action)
    if key in _ATTEMPTS:
        return _ATTEMPTS[key] in {'ORIGINAL_DIAGNOSTIC_COMMITTED','ORIGINAL_UNKNOWN_OBSERVED'}
    _ATTEMPTS[key] = 'UNKNOWN_NO_RETRY'  # Before any qualification or coordinator IO.
    try:
        _facts(o)
        initial = _EPOCHS.get(key)
        require(type(initial) is tuple and type(initial[1]) is int)
        original_state, epoch = initial
        with coordinator.Control.tx(o.control) as db:
            _work(o,db)
            state, current, _ = coordinator.Control.status(o.control,db,{'OPEN','DRAINING','UNKNOWN'})
            if state == 'UNKNOWN':
                require(current in {epoch,epoch+1,epoch+2})
                disposition = 'ORIGINAL_UNKNOWN_OBSERVED'  # Read-only, never reopen.
            else:
                require((state == original_state and current == epoch)
                        or (original_state == 'OPEN' and state == 'DRAINING' and current == epoch+1))
                db.execute("UPDATE control SET state='UNKNOWN',epoch=epoch+1,runtime=NULL")
                disposition = 'ORIGINAL_DIAGNOSTIC_COMMITTED'
            _facts(o)
        _facts(o)
        _ATTEMPTS[key] = disposition
        return True
    except BaseException:
        return False  # Sticky original owners/grants, no foreign fallback/retry.


def hold_native(native):
    n = _NATIVES.get(id(native))
    if n is None or n.native is not native:
        return False  # No reconstructed/raw NativeChild can choose a target.
    _hold(n.dispatch)
    return True  # Handled even when the original qualification/IO is UNKNOWN.


def hold_entry(entry):
    found = [o for saved,o in _ENTRIES.values() if saved is entry and id(o.action) not in _SETTLED]
    for o in found:
        _hold(o)
    return bool(found)


def entry_needs_fixed_refusal(entry):
    # The existing turn/finally interface retains its predecessor exception
    # when all original public bindings are unchanged. Always diagnose through
    # the captured authority first. Drift uses fixed refusal before fallback.
    if not hold_entry(entry):
        return False
    found = [o for saved,o in _ENTRIES.values() if saved is entry and id(o.action) not in _SETTLED]
    for o in found:
        try:
            _facts(o)
            if entry.runtime is not o.runtime or o.runtime.control is not o.control or o.runtime.selection is not o.selection:
                return True
            for n in _NATIVES.values():
                if n.dispatch is o and (n.native.entry is not entry or n.native.runtime is not o.runtime
                        or n.native.action is not o.action or n.native.grant is not n.grant
                        or not exact(grant_fields(n.grant),n.grant_values)):
                    return True
        except BaseException:
            return True  # Unknown original qualification cannot select a fallback.
    return False


def hold_runtime(actual):
    found = [o for o in _ORIGINALS.values() if o.runtime is actual and id(o.action) not in _SETTLED]
    if not found:
        return None
    return all([_hold(o) for o in found])


def _pending(o):
    return id(o.action) in _ATTEMPTS and id(o.action) not in _SETTLED


def refuse_runtime_delivery(actual):
    if any(o.runtime is actual and _pending(o) for o in _ORIGINALS.values()):
        raise coordinator.Refused('admission_closed')


def refuse_entry_delivery(entry):
    if any(saved is entry and _pending(o) for saved,o in _ENTRIES.values()):
        raise coordinator.Refused('admission_closed')


def settled_dispatch(actual, action):
    # Called only AFTER the original coordinator settle transaction returned.
    validate_action(actual,action)
    _SETTLED.add(id(action))
