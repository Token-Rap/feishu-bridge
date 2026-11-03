"""Exact managed session-map I/O; authority is supplied by the coordinator.

No lock/fence store is created here.  The caller holds the coordinator's existing
writer transaction for the complete read/modify/replace/readback operation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import uuid


class Refused(OSError):
    pass


def _refuse():
    raise Refused("managed_map_unknown")


def _physical(path: Path):
    try:
        st = path.lstat()
        if (path != path.resolve() or not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.getuid() or st.st_nlink != 1
                or stat.S_IMODE(st.st_mode) != 0o600):
            _refuse()
        return st
    except OSError:
        _refuse()


def read(path: Path) -> dict:
    before = _physical(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            _refuse()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            _refuse()
        result = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique)
        if type(result) is not dict:
            _refuse()
        after = _physical(path)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            _refuse()
        return result
    except (OSError, ValueError, UnicodeError):
        _refuse()
    finally:
        os.close(fd)


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            _refuse()
        out[key] = value
    return out


def write(path: Path, data: dict) -> None:
    if type(data) is not dict:
        _refuse()
    before = _physical(path)
    try:
        raw = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        if len(raw) > 16 * 1024 * 1024:
            _refuse()
        # A distinct exclusive temporary file avoids clobbering another writer's
        # .tmp. Any uncertain write keeps its artifact for operator inspection.
        tmp = path.with_name(path.name + ".managed-" + uuid.uuid4().hex + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        current = _physical(path)
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            _refuse()
        tmp.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _physical(path)
        if path.read_bytes() != raw:
            _refuse()
    except (OSError, ValueError, TypeError, UnicodeError):
        _refuse()
