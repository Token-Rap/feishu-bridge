import contextlib
import json
from pathlib import Path

import pytest

import fleet_managed_map as managed
import fleet_sessions as sessions


@pytest.fixture
def selected(tmp_path, monkeypatch):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"other::claude": {"active": "other-old", "sessions": {
        "other-old": {"name": "preserve", "ts": "old"}}},
        "work::codex": {"active": None, "sessions": {"historical": {"name": "old", "ts": "old"}}}}))
    path.chmod(0o600)
    events = []

    @contextlib.contextmanager
    def transaction():
        events.append("begin-existing-coordinator")
        try:
            yield
        finally:
            events.append("end-existing-coordinator")

    monkeypatch.setattr(sessions, "SESSIONS_FILE", path)
    monkeypatch.setattr(sessions, "MANAGED_TRANSACTION", transaction)
    return path, events


def test_direct_stale_whole_map_write_is_not_admitted(selected):
    path, events = selected
    original = path.read_bytes()
    with pytest.raises(managed.Refused, match="managed_map_transaction_required"):
        sessions.write_sessions({})
    assert path.read_bytes() == original and events == []


def test_record_clear_and_name_preserve_old_sids_and_other_engines(selected):
    path, events = selected
    other = sessions.read_sessions()["other::claude"]
    sessions.record_session("work::codex", "genuine-new", "new work")
    sessions.name_active("work::codex", "named")
    sessions.clear_session("work::codex")
    sessions.set_active("work::codex", "genuine-new")
    result = sessions.read_sessions()
    assert result["other::claude"] == other
    assert set(result["work::codex"]["sessions"]) == {"historical", "genuine-new"}
    assert result["work::codex"]["active"] == "genuine-new"
    assert result["work::codex"]["sessions"]["genuine-new"]["name"] == "named"
    assert events == ["begin-existing-coordinator", "end-existing-coordinator"] * 4
    assert path.stat().st_mode & 0o777 == 0o600


def test_guard_refusal_is_before_read_modify_write(selected, monkeypatch):
    path, _ = selected
    old = path.read_bytes()

    @contextlib.contextmanager
    def refused():
        raise managed.Refused("existing_writer_unknown")
        yield

    monkeypatch.setattr(sessions, "MANAGED_TRANSACTION", refused)
    with pytest.raises(managed.Refused, match="existing_writer_unknown"):
        sessions.clear_session("work::codex")
    assert path.read_bytes() == old


@pytest.mark.parametrize("raw", [b"bad JSON", b"[]", b'{"work":1,"work":2}', b"\xff"])
def test_bad_managed_map_never_becomes_empty_first_session(selected, raw):
    path, _ = selected
    path.write_bytes(raw)
    with pytest.raises(managed.Refused, match="managed_map_unknown"):
        sessions.load_session("work::codex")
    assert path.read_bytes() == raw


def test_missing_nonprivate_and_symlink_map_refuse(selected):
    path, _ = selected
    path.chmod(0o644)
    with pytest.raises(managed.Refused):
        sessions.read_sessions()
    path.chmod(0o600)
    other = path.with_name("original.json")
    path.rename(other)
    path.symlink_to(other)
    with pytest.raises(managed.Refused):
        sessions.read_sessions()
    path.unlink()
    with pytest.raises(managed.Refused):
        sessions.read_sessions()


def test_replace_failure_retains_old_map_and_uncertain_artifact(selected, monkeypatch):
    path, events = selected
    old = path.read_bytes()
    replace = Path.replace

    def fail(self, target):
        if target == path:
            raise OSError("synthetic replace failure")
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(managed.Refused, match="managed_map_unknown"):
        sessions.clear_session("work::codex")
    assert path.read_bytes() == old
    assert len(list(path.parent.glob("sessions.json.managed-*.tmp"))) == 1
    assert events == ["begin-existing-coordinator", "end-existing-coordinator"]


def test_legacy_missing_map_policy_stays_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "MANAGED_TRANSACTION", None)
    monkeypatch.setattr(sessions, "SESSIONS_FILE", tmp_path / "missing.json")
    assert sessions.read_sessions() == {}
    sessions.record_session("work", "legacy")
    assert sessions.load_session("work") == "legacy"
