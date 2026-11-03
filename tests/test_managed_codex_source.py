"""Exact managed command/projection checks; no native child or personal reads."""
import json

import pytest

import fleet_managed_codex as policy
from fleet_managed_ingress import Refused


def selected(tmp_path):
    return {"FLEET_TARGET_CONTROL_REQUIRED": "1", "FLEET_TARGET_CONTROL_DIR": str(tmp_path),
            "FLEET_TARGET_CONTROL_ID": "shopping", "FLEET_BRIDGE_HOME": str(tmp_path)}


def test_legacy_command_environment_and_projection_are_unchanged():
    argv = ["codex", "app-server", "--listen", "stdio://"]
    assert policy.command(argv, {}) is argv
    assert policy.config({}) == {}
    original = {"KEY": "value"}
    assert policy.environment(original, {}) is original
    assert original == {"KEY": "value"}
    policy.effective({}, {})


def test_selected_native_and_terminal_argv_always_bind_projectless(tmp_path):
    env = selected(tmp_path)
    for argv in (["codex", "app-server", "--listen", "stdio://"],
                 ["codex", "resume", "same-sid"], ["codex", "exec", "same prompt"]):
        bound = policy.command(argv, env)
        assert bound == [argv[0], "--strict-config", *argv[1:]]
        assert argv[1] != "--strict-config"
    assert policy.config(env)["project_root_markers"] == []
    assert policy.environment({}, env) == env | {"CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED": "1"}


@pytest.mark.parametrize("markers", [None, False, "[]", (), {}, [".git"], [None]])
def test_unknown_effective_projection_has_no_fallback(tmp_path, markers):
    with pytest.raises(Refused, match="effective_project_selection_unknown"):
        policy.effective({"project_root_markers": markers}, selected(tmp_path))


def test_effective_exact_empty_array_and_missing_field(tmp_path):
    env = selected(tmp_path)
    policy.effective({"project_root_markers": [], "shell_environment_policy": {
        "set": policy.bindings(env)}}, env)
    with pytest.raises(Refused):
        policy.effective({}, selected(tmp_path))


@pytest.mark.parametrize("key", ["FLEET_TARGET_CONTROL_REQUIRED", "FLEET_TARGET_CONTROL_DIR",
                                "FLEET_TARGET_CONTROL_ID", "FLEET_BRIDGE_HOME"])
def test_partial_selection_refuses_before_any_command(tmp_path, key):
    env = selected(tmp_path); env.pop(key)
    with pytest.raises(Refused):
        policy.command(["codex", "app-server"], env)


def test_actual_scope_methods_bind_same_scope_without_replacing_account_checks(tmp_path, monkeypatch):
    import fleet_codex_storage as storage
    env = selected(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    scope = storage.Scope(tmp_path, "namespace", "hash", (), tmp_path / "desktop")
    monkeypatch.setattr(storage, "select", lambda: scope)
    argv = scope.command(["codex", "app-server", "--listen", "stdio://"])
    assert argv[:3] == ["codex", "--strict-config", "app-server"]
    assert "project_root_markers=[]" in argv
    assert scope.env({})["CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED"] == "1"
    config = {"sqlite_home": str(tmp_path), "project_root_markers": [],
              "model_provider": "openai", "forced_login_method": "chatgpt",
              "cli_auth_credentials_store": "file", "shell_environment_policy": {"set": {
                  "CODEX_HOME": str(tmp_path), "CODEX_SQLITE_HOME": str(tmp_path),
                  storage.KEY: str(tmp_path), storage.DESKTOP_KEY: str(tmp_path / "desktop"),
                  **policy.bindings(env)}}}
    scope.effective(config)
    config["forced_login_method"] = None
    with pytest.raises(storage.StorageRefused, match="effective_account_route_unknown"):
        scope.effective(config)


def test_missing_or_changed_descendant_binding_refuses(tmp_path):
    env = selected(tmp_path)
    config = {"project_root_markers": [], "shell_environment_policy": {"set": policy.bindings(env)}}
    for key in policy.bindings(env):
        old = config["shell_environment_policy"]["set"][key]
        config["shell_environment_policy"]["set"][key] = "wrong"
        with pytest.raises(Refused, match="effective_managed_shell_unknown"):
            policy.effective(config, env)
        config["shell_environment_policy"]["set"][key] = old
