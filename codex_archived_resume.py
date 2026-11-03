"""Exact native resume refusal; no archive control, retry or session mutation."""
from dataclasses import dataclass, asdict
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
try:
    import fcntl
except ImportError:
    fcntl = None


class StateUnknown(RuntimeError):
    pass


@dataclass(frozen=True)
class Binding:
    work: str
    sid: str
    runtime: str
    namespace: str

    def __post_init__(self):
        if (type(self.work) is not str or not self.work or len(self.work)>512
                or type(self.sid) is not str or not re.fullmatch('[A-Za-z0-9-]{1,128}',self.sid)
                or any(type(v) is not str or not re.fullmatch('[a-f0-9]{64}',v)
                       for v in (self.runtime,self.namespace))):
            raise StateUnknown('archive_binding_unknown')

    def key(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


class ArchivedResumeError(RuntimeError):
    def __init__(self, binding, code, message):
        super().__init__(message)
        self.binding, self.code = binding, code


def classify(frame, request_id, binding):
    """Only called on the owned stdout response to submitted thread/resume.

    Exact selected0.159.2 literal; no prose/substring/notification classifier.
    Code is preserved as a native RPC integer, not guessed as one error enum.
    """
    if type(binding) is not Binding or not re.fullmatch('[A-Za-z0-9-]{1,128}', binding.sid):
        return None
    if (type(frame) is not dict or not set(frame) <= {'id', 'error', 'jsonrpc'}
            or type(request_id) is not int or type(frame.get('id')) is not int
            or frame['id'] != request_id
            or ('jsonrpc' in frame and frame['jsonrpc'] != '2.0')):
        return None
    error = frame.get('error')
    if (type(error) is not dict or not set(error) <= {'code', 'message', 'data'}
            or type(error.get('code')) is not int or error.get('data') is not None):
        return None
    message = error.get('message')
    exact = f'session {binding.sid} is archived. Run `codex unarchive {binding.sid}` to unarchive it first.'
    if type(message) is not str or message != exact:
        return None
    return ArchivedResumeError(binding, error['code'], message)


# One actually reviewed physical Mac selector/literal. No version/help execution.
PIN_PATH = '/opt/homebrew/Caskroom/codex/0.159.2/bin/codex'
PIN_SHA256 = '16593cc2f422d5f398a8e40f550ebbaf1245392528957be342c295920a300704'
PIN_BYTES = 240184096


def physical_pin(selector):
    """Fresh no-follow descriptor hash + before/after selector identity.

    No cache or protocol-derived capability. File identity is not an attestation
    of already-loaded process bytes. Unknown Windows/bare/foreign selectors give
    no archived binding; no executable/version/model/probe is dispatched.
    """
    if os.name != 'posix' or type(selector) is not str or not selector:
        return None
    logical = Path(selector)
    fd = None
    try:
        if not logical.is_absolute():
            return None
        physical = logical.resolve(strict=True)
        if str(physical) != PIN_PATH:
            return None
        before = logical.lstat()
        fd = os.open(physical, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size != PIN_BYTES
                or info.st_nlink != 1 or info.st_uid not in {0, os.getuid()}
                or info.st_mode & 0o022):
            return None
        hasher = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            hasher.update(chunk)
        after = os.fstat(fd)
        identity = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns)
        if (hasher.hexdigest() != PIN_SHA256 or identity(info) != identity(after)
                or identity(before) != identity(logical.lstat())
                or logical.resolve(strict=True) != physical
                or identity(physical.lstat()) != identity(info)):
            return None
        return {'selector': selector, 'physical': str(physical),
                'sha256': PIN_SHA256, 'identity': identity(info)}
    except (OSError, ValueError, RuntimeError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


def namespace(home, sqlite):
    """Configured child namespace + bounded config metadata; no body/auth read.

    Not native effective SQLite/account proof. Unknown/link/foreign metadata
    gives no archived binding; never guess an origin from a title or SID alone.
    """
    if os.name != 'posix' or type(home) is not str or (sqlite is not None and type(sqlite) is not str):
        return None
    root = Path(home)
    try:
        s = root.lstat()
        if (not root.is_absolute() or root.resolve() != root.absolute()
                or not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid()):
            return None
        config = root/'config.toml'
        metadata = None
        if config.exists() or config.is_symlink():
            c = config.lstat()
            if (not stat.S_ISREG(c.st_mode) or c.st_nlink != 1 or c.st_uid != os.getuid()):
                return None
            metadata = (c.st_dev,c.st_ino,c.st_size,c.st_mtime_ns,c.st_ctime_ns)
        return hashlib.sha256(json.dumps((str(root),sqlite,s.st_dev,s.st_ino,metadata)).encode()).hexdigest()
    except OSError:
        return None


class Store:
    """One bounded private per-bot receipt; no task/card/peer journal writes.

    Existing records retained byte-for-value. An uncertain/partial record is a
    refusal, never repaired/retried. This SOURCE is not adopted or allocated live.
    """
    def __init__(self, root):
        self.path = Path(root)/'codex_archived_resume.jsonl'
        self.memory = {}

    @contextlib.contextmanager
    def _file(self, *, create=False):
        p = self.path
        if fcntl is None or os.name != 'posix':
            raise StateUnknown('archive_native_lock_acl_unreviewed')
        try:
            parent = p.parent.lstat()
        except OSError:
            raise StateUnknown('archive_receipt_root_unknown') from None
        if (p.parent.resolve() != p.parent.absolute() or not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.getuid() or parent.st_mode & 0o022):
            raise StateUnknown('archive_receipt_root_unknown')
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW
        fd = None
        try:
            fresh = False
            try:
                fd = os.open(p, flags)
            except FileNotFoundError:
                if not create:
                    raise
                fd = os.open(p, flags | os.O_CREAT | os.O_EXCL, 0o600)
                fresh = True
            s = os.fstat(fd)
            if (not stat.S_ISREG(s.st_mode) or s.st_nlink != 1
                    or s.st_uid != os.getuid() or s.st_mode & 0o077):
                raise StateUnknown('archive_receipt_provenance_unknown')
            fcntl.flock(fd, fcntl.LOCK_EX)
            current = p.lstat()
            if (current.st_dev,current.st_ino) != (s.st_dev,s.st_ino):
                raise StateUnknown('archive_receipt_replaced')
            yield fd,fresh
        except OSError:
            raise StateUnknown('archive_receipt_io_unknown') from None
        finally:
            if fd is not None:
                os.close(fd)

    def _rows(self, fd, fresh=False):
        os.lseek(fd,0,os.SEEK_SET)
        raw = os.read(fd,1024*1024+1)
        if (not raw and not fresh) or len(raw)>1024*1024 or (raw and not raw.endswith(b'\n')):
            raise StateUnknown('archive_receipt_tail_unknown')
        rows = {}
        try:
            for line in raw.splitlines():
                def pairs(items):
                    result = {}
                    for key, value in items:
                        if key in result:
                            raise ValueError('duplicate')
                        result[key] = value
                    return result
                row = json.loads(line, object_pairs_hook=pairs)
                if (type(row) is not dict or set(row) != {'binding','code','message'}
                        or type(row['binding']) is not dict or set(row['binding']) != {'work','sid','runtime','namespace'}
                        or any(type(v) is not str or not v for v in row['binding'].values())):
                    raise StateUnknown('archive_receipt_shape_unknown')
                b = Binding(**row['binding'])
                check = classify({'id':2,'error':{'code':row['code'],'message':row['message']}},2,b)
                if check is None or b.key() in rows:
                    raise StateUnknown('archive_receipt_identity_unknown')
                rows[b.key()] = row
        except (ValueError,TypeError,UnicodeError):
            raise StateUnknown('archive_receipt_json_unknown') from None
        if len(rows)>128:
            raise StateUnknown('archive_receipt_capacity_unknown')
        return rows

    def blocked(self, binding):
        if binding.key() in self.memory:
            return True
        if not self.path.exists() and not self.path.is_symlink():
            return False
        with self._file() as (fd,_fresh):
            return binding.key() in self._rows(fd)

    def record(self, error):
        if type(error) is not ArchivedResumeError:
            raise StateUnknown('native_resume_receipt_required')
        self.memory[error.binding.key()] = error
        with self._file(create=True) as (fd,fresh):
            rows = self._rows(fd,fresh)
            if error.binding.key() in rows:
                return
            if len(rows)>=128:
                raise StateUnknown('archive_receipt_capacity_unknown')
            row = {'binding':asdict(error.binding),'code':error.code,'message':str(error)}
            raw = (json.dumps(row,ensure_ascii=False)+'\n').encode()
            if os.fstat(fd).st_size+len(raw)>1024*1024:
                raise StateUnknown('archive_receipt_capacity_unknown')
            wrote = os.write(fd,raw)
            if wrote != len(raw):
                raise StateUnknown('archive_receipt_possible_write_unknown')
            os.fsync(fd)
            current = self.path.lstat()
            if (current.st_dev, current.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
                raise StateUnknown('archive_receipt_possible_write_unknown')
            # Durability of first creation also needs the containing directory.
            directory = os.open(self.path.parent,os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
