"""Prospective selected status/ledger descriptor IO, not file/parent settlement.

All actual returned descriptors stay strongly owned before qualification.
Successful IO observations do not release helper leases or supply the missing
ALL-writer fence, whole blocking-IO deadline, or cross-restart proof.
"""
from dataclasses import dataclass
import hashlib
import os
import stat

from feishu_bridge.managed import fleet_managed_ingress as ingress

_ORIGINALS = {}
_ISSUED = {}  # One original Session per consumed helper, never reconstructed.
_ROOTS = {}
_READERS = {}
_STATES = {}
_CLAIMS = set()
_PRIORS = {}
_DONE = {}
_END_ORIGINALS = {}  # Strong exact IO end; never reconstructed from a report.
_END_ATTEMPTS = {}  # Original final stage consumed before final sync/close.


def need(ok):
    if not ok:
        raise ingress.Refused('managed_native_persistence_unknown')


@dataclass(frozen=True, repr=False)
class Original:
    observation: object
    helper_original: object
    grant: object
    home: object
    path: object
    name: str
    kind: str
    root_identity: tuple


class Descriptor:
    __slots__ = ()


@dataclass(frozen=True, repr=False)
class Pin:
    owner: Descriptor
    session: object
    name: str
    fd: int
    directory: bool
    identity: tuple | None


@dataclass(frozen=True, repr=False)
class IOObservation:
    session: object
    bytes_written: int
    sha256: str
    descriptors_closed: bool
    readback_equal: bool
    fsync_calls_returned: bool
    all_writer_exclusion_verified: bool = False
    blocking_io_deadline_verified: bool = False
    cross_restart_verified: bool = False
    file_settlement_verified: bool = False
    parent_ready: bool = False


@dataclass(frozen=True, repr=False)
class EndOriginal:
    session: object
    original: Original
    authority: object
    pins: tuple
    written: bytes
    readback: bytes
    readback_identity: tuple
    result: IOObservation | None
    result_fields: tuple | None


def _result_fields(result):
    return (result.session, result.bytes_written, result.sha256,
            result.descriptors_closed, result.readback_equal,
            result.fsync_calls_returned, result.all_writer_exclusion_verified,
            result.blocking_io_deadline_verified, result.cross_restart_verified,
            result.file_settlement_verified, result.parent_ready)


def _same_fields(left, right):
    return (type(left) is type(right) and
            (len(left) == len(right) and all(_same_fields(a, b) for a, b in zip(left, right))
             if type(left) is tuple else left == right))


def _end_binding(session):
    from feishu_bridge.managed.fleet_managed_persistence import _original_refusal
    end = _END_ORIGINALS.get(id(session))
    need(type(end) is EndOriginal and end.session is session
         and type(session) is Files and session._original() is end.original
         and _original_refusal(end.original.observation) is end.authority)
    current = tuple((k, p) for k, p in _READERS.items() if p.session is session)
    need(len(current) == len(end.pins)
         and all(k == old_k and p is old_p for (k, p), (old_k, old_p) in zip(current, end.pins)))
    return end


def original_io_end(observed):
    """RAM-only original closed IO, never a file-tail release or readiness API.

    CLOSED descriptor numbers are intentionally not inspected or reused here.
    After turn unwind this record is still only historical IO facts: a future
    trusted predicate must independently derive live fence/deadline/persistence.
    """
    session = _ISSUED.get(id(observed))
    end = _end_binding(session)
    result = _DONE.get(id(session))
    need(end.original.observation is observed and type(result) is IOObservation
         and result is end.result and type(end.result_fields) is tuple
         and _same_fields(_result_fields(result), end.result_fields)
         and _END_ATTEMPTS.get(id(session)) == 'ORIGINAL_IO_OBSERVED'
         and all(_STATES.get(k) == 'CLOSED' for k, _ in end.pins))
    need(type(result.bytes_written) is int and result.bytes_written == len(end.written)
         and type(result.sha256) is str and result.sha256 == hashlib.sha256(end.written).hexdigest()
         and all(type(x) is bool and x is True for x in end.result_fields[3:6])
         and all(type(x) is bool and x is False for x in end.result_fields[6:]))
    return end  # Private full bytes/binding/pins stay RAM-only, no export.


def key(s):
    return (s.st_dev, s.st_ino, s.st_uid, s.st_mode, s.st_nlink,
            s.st_size, s.st_mtime_ns, s.st_ctime_ns)


class Files:
    __slots__ = ()

    def __init__(self):
        raise ingress.Refused('managed_native_persistence_unknown')

    def _original(self):
        o = _ORIGINALS.get(id(self))
        need(type(o) is Original and o.observation.files is self
             and _ISSUED.get(id(o.observation)) is self
             and o.observation.original is o.helper_original
             and o.observation.grant is o.grant
             and o.helper_original.home is o.home and o.helper_original.path is o.path
             and o.helper_original.kind == o.kind
             and o.helper_original.root_identity == o.root_identity)
        return o

    def _guard(self):
        o = self._original()
        o.observation._live()
        need(self._original() is o and not o.observation.unknown)
        return o

    def _fail(self):
        mark_unknown(self)
        o = _ORIGINALS[id(self)]
        o.observation.refuse()

    def _claim(self, stage):
        need((id(self), stage) not in _CLAIMS and id(self) not in _DONE)
        _CLAIMS.add((id(self), stage))  # BEFORE any fallible prerequisite or IO.
        return self._guard()

    def _open(self, name, flags, *, directory=False):
        o = self._guard()
        need(name == str(o.home) if directory else
             name in {o.name, 'status.tmp'} and not directory)
        if directory:
            fd = os.open(str(o.home), flags | os.O_NOFOLLOW | os.O_CLOEXEC)
        else:
            root = self._pin(_ROOTS[id(self)])
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=root.fd)
        owner = Descriptor()
        _READERS[id(owner)] = Pin(owner, self, name, fd, directory, None)
        _STATES[id(owner)] = 'OPEN'  # Strong BEFORE fstat/guard/refusal.
        return owner

    def _pin(self, owner):
        pin = _READERS.get(id(owner))
        need(type(owner) is Descriptor and type(pin) is Pin and pin.owner is owner
             and pin.session is self and _STATES.get(id(owner)) == 'OPEN'
             and type(pin.fd) is int and pin.fd >= 0)
        self._guard()
        need(_READERS.get(id(owner)) is pin and _STATES[id(owner)] == 'OPEN')
        s = os.fstat(pin.fd)
        need(s.st_uid == os.getuid()
             and (stat.S_ISDIR(s.st_mode) and stat.S_IMODE(s.st_mode) == 0o700
                  if pin.directory else stat.S_ISREG(s.st_mode) and s.st_nlink == 1
                  and stat.S_IMODE(s.st_mode) in {0o600, 0o644}))
        # Directory link counts may change while this very operation creates
        # its temp/ledger leaf. Root contract is dev/inode/UID/mode; ordinary
        # files still require exact identity plus SINGLE link.
        identity = key(s)[:4] if pin.directory else key(s)[:5]
        if pin.identity is None:
            pin = Pin(owner, self, pin.name, pin.fd, pin.directory, identity)
            _READERS[id(owner)] = pin
        else:
            need(pin.identity == identity)
        self._guard()
        need(_READERS.get(id(owner)) is pin and _STATES[id(owner)] == 'OPEN')
        return pin

    def _root(self):
        if id(self) not in _ROOTS:
            o = self._guard()
            owner = self._open(str(o.home), os.O_RDONLY | os.O_DIRECTORY, directory=True)
            _ROOTS[id(self)] = owner  # BEFORE the first actual root identity query.
        pin = self._pin(_ROOTS[id(self)])
        need(pin.identity[:3] + (stat.S_IMODE(pin.identity[3]),) == self._original().root_identity)
        return pin

    def _named(self, name):
        root = self._root()
        try:
            result = os.stat(name, dir_fd=root.fd, follow_symlinks=False)
        except FileNotFoundError:
            self._guard()
            return None
        self._guard()
        return key(result)

    def _same_named(self, name, expected):
        need(self._named(name) == expected)

    def _close(self, owner):
        try:
            pin = self._pin(owner)
            self._guard()
            need(_READERS.get(id(owner)) is pin and _STATES[id(owner)] == 'OPEN')
            _STATES[id(owner)] = 'CLOSE_ATTEMPTED'
            os.close(pin.fd)  # ONLY the immutable actual original returned number.
            _STATES[id(owner)] = 'CLOSED'
            self._guard()
        except BaseException:
            # Also cover qualification/interrupt BEFORE CLOSE_ATTEMPTED.
            if id(owner) in _READERS and _STATES[id(owner)] != 'CLOSED':
                _STATES[id(owner)] = 'UNKNOWN'
            self._fail()

    def _read(self, name, limit, *, absent=False):
        before_named = self._named(name)
        if before_named is None:
            need(absent)
            return b'', None
        owner = self._open(name, os.O_RDONLY)
        pin = self._pin(owner)
        before = key(os.fstat(pin.fd))
        need(before == before_named and before[5] <= limit)
        raw = bytearray()
        while len(raw) <= limit:
            pin = self._pin(owner)
            part = os.read(pin.fd, min(65536, limit + 1 - len(raw)))
            self._guard()
            if not part:
                break
            raw.extend(part)
        need(len(raw) == before[5] and len(raw) <= limit)
        pin = self._pin(owner)
        need(key(os.fstat(pin.fd)) == before)
        self._same_named(name, before)
        self._close(owner)
        return bytes(raw), before

    def previous(self):
        try:
            o = self._claim('previous')
            limit = 2*1024*1024 if o.kind == 'status' else 16*1024*1024
            raw, identity = self._read(o.name, limit, absent=True)
            _PRIORS[id(self)] = (raw, identity)
            return raw if identity is not None else None
        except BaseException:
            self._fail()

    def _write_all(self, owner, raw):
        offset = 0
        while offset < len(raw):
            pin = self._pin(owner)
            count = os.write(pin.fd, raw[offset:])
            need(type(count) is int and 0 < count <= len(raw)-offset)
            self._guard()
            self._pin(owner)
            offset += count

    def _sync(self, owner):
        pin = self._pin(owner)
        os.fsync(pin.fd)
        self._guard()
        self._pin(owner)

    def _finish(self, raw, readback, identity):
        need(id(self) not in _END_ATTEMPTS)
        _END_ATTEMPTS[id(self)] = 'UNKNOWN'  # BEFORE final guard/sync/close.
        from feishu_bridge.managed.fleet_managed_persistence import _original_refusal
        original = self._guard()
        authority = _original_refusal(original.observation)
        pins = tuple((k, p) for k, p in _READERS.items() if p.session is self)
        # Retain exact actual stage inputs BEFORE all later final guards/IO.
        end = EndOriginal(self, original, authority, pins, raw, readback, identity, None, None)
        _END_ORIGINALS[id(self)] = end
        need(type(raw) is bytes and type(readback) is bytes and type(identity) is tuple
             and len(identity) == 8 and all(type(x) is int for x in identity)
             and 0 <= identity[5] == len(readback) <= 16*1024*1024)
        expected = raw if original.kind == 'status' else _PRIORS[id(self)][0] + raw
        need(readback == expected and len(raw) <= 2*1024*1024)
        root = _ROOTS[id(self)]
        self._sync(root)
        need(_end_binding(self) is end)
        self._close(root)
        self._guard()
        need(_end_binding(self) is end and all(_STATES[k] == 'CLOSED' for k, _ in pins))
        result = IOObservation(self, len(raw), hashlib.sha256(raw).hexdigest(), True, True, True)
        completed = EndOriginal(self, original, authority, pins, raw, readback, identity,
                                result, _result_fields(result))
        _END_ORIGINALS[id(self)] = completed  # Strong before final publication guard.
        _DONE[id(self)] = result
        _END_ATTEMPTS[id(self)] = 'ORIGINAL_IO_OBSERVED'
        self._guard()
        need(original_io_end(original.observation) is completed)
        return result

    def status(self, text):
        try:
            o = self._claim('write')
            need(o.kind == 'status' and type(text) is str and id(self) in _PRIORS)
            raw = text.encode('utf-8', 'strict')
            need(len(raw) <= 2*1024*1024)
            self._same_named(o.name, _PRIORS[id(self)][1])
            need(self._named('status.tmp') is None)
            owner = self._open('status.tmp', os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            self._write_all(owner, raw)
            self._sync(owner)
            pin = self._pin(owner)
            written = key(os.fstat(pin.fd))
            need(written[5] == len(raw))
            self._same_named('status.tmp', written)
            self._close(owner)
            self._same_named(o.name, _PRIORS[id(self)][1])
            self._same_named('status.tmp', written)
            root = self._root()
            os.replace('status.tmp', o.name, src_dir_fd=root.fd, dst_dir_fd=root.fd)
            self._guard()
            readback, identity = self._read(o.name, 2*1024*1024)
            need(readback == raw and identity[:5] == written[:5])
            return self._finish(raw, readback, identity)
        except BaseException:
            self._fail()

    def ledger(self, text):
        try:
            o = self._claim('write')
            need(o.kind == 'ledger' and type(text) is str)
            raw = text.encode('utf-8', 'strict')
            need(raw.endswith(b'\n') and len(raw) <= 2*1024*1024)
            self.previous()
            previous, identity = _PRIORS[id(self)]
            need(len(previous)+len(raw) <= 16*1024*1024
                 and (not previous or previous.endswith(b'\n')))
            previous.decode('utf-8', 'strict')
            self._same_named(o.name, identity)
            flags = os.O_WRONLY | os.O_APPEND
            if identity is None:
                flags |= os.O_CREAT | os.O_EXCL
            owner = self._open(o.name, flags)
            pin = self._pin(owner)
            if identity is not None:
                need(key(os.fstat(pin.fd)) == identity)
            self._write_all(owner, raw)
            self._sync(owner)
            pin = self._pin(owner)
            written = key(os.fstat(pin.fd))
            need(written[5] == len(previous)+len(raw))
            self._same_named(o.name, written)
            self._close(owner)
            readback, after = self._read(o.name, 16*1024*1024)
            need(readback == previous+raw and after[:5] == written[:5])
            return self._finish(raw, readback, after)
        except BaseException:
            self._fail()


def capture(observed):
    from feishu_bridge.managed.fleet_managed_persistence import Observation, Original as HelperOriginal
    need(type(observed) is Observation and type(observed.original) is HelperOriginal)
    prior = _ISSUED.get(id(observed))
    if prior is not None:
        need(type(prior) is Files and observed.files is prior
             and prior._original().observation is observed)
        return prior
    need(observed.files is None)
    original = observed.original
    session = object.__new__(Files)
    _ORIGINALS[id(session)] = Original(observed, original, observed.grant, original.home,
        original.path, original.path.name, original.kind, original.root_identity)
    _ISSUED[id(observed)] = session  # Consume original before callback/descriptor IO.
    observed.files = session  # Real Entry strong ownership BEFORE callbacks/open.
    session._guard()
    return session


def mark_unknown(session):
    o = _ORIGINALS.get(id(session))
    if type(session) is Files and type(o) is Original:
        if id(session) in _END_ATTEMPTS:
            _END_ATTEMPTS[id(session)] = 'UNKNOWN'
        for k, p in tuple(_READERS.items()):
            if p.session is session and _STATES[k] != 'CLOSED':
                _STATES[k] = 'UNKNOWN'


def refuse_observation(observed):
    # The authoritative originals, not a potentially replaced public pointer.
    for original in tuple(_ORIGINALS.values()):
        if original.observation is observed:
            for pin in tuple(_READERS.values()):
                if _ORIGINALS.get(id(pin.session)) is original:
                    mark_unknown(pin.session)


def observed_io(observed):
    session = observed.files
    need(type(session) is Files and session._original().observation is observed)
    result = _DONE.get(id(session))
    need(type(result) is IOObservation and result.session is session
         and result.descriptors_closed is True and result.readback_equal is True
         and result.fsync_calls_returned is True)
    need(original_io_end(observed).result is result)
    session._guard()
    return result  # File/parent/ALL-writer/whole-IO readiness remain false.
