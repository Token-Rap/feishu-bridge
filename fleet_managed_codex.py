"""Managed-only project discovery policy for the pinned Codex loader.

No native invocation or configuration write. Empty markers select only the
actual CWD's project layer, retaining system/forced managed requirements.
This does not exclude ordinary cloud/model-catalog startup or attest readiness.
"""
from pathlib import Path

from fleet_managed_ingress import Refused, Selection


def selected(environ):
    keys = ("FLEET_TARGET_CONTROL_REQUIRED", "FLEET_TARGET_CONTROL_DIR",
            "FLEET_TARGET_CONTROL_ID")
    if all(environ.get(key) is None for key in keys):
        return False
    home = environ.get("FLEET_BRIDGE_HOME")
    legacy = environ.get("FEISHU_BRIDGE_HOME")
    if (type(home) is not str or not home or (legacy is not None and legacy != home)):
        raise Refused("managed_codex_home_unknown")
    Selection.from_env(environ, Path(home))
    return True


def bindings(environ):
    if not selected(environ):
        return {}
    return {key: environ[key] for key in ("FLEET_TARGET_CONTROL_REQUIRED",
            "FLEET_TARGET_CONTROL_DIR", "FLEET_TARGET_CONTROL_ID", "FLEET_BRIDGE_HOME")} | {
            "CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED": "1"}


def config(environ):
    values = bindings(environ)
    return ({"project_root_markers": [],
             **{"shell_environment_policy.set." + key: value for key, value in values.items()}}
            if values else {})


def command(argv, environ):
    if not selected(environ):
        return argv
    if type(argv) is not list or not argv or any(type(s) is not str for s in argv):
        raise Refused("managed_codex_command_unknown")
    # Pinned CLI supports the root flag for ordinary TUI/exec/app-server only.
    # Actual callers are reviewed separately; this does not permit extra RPCs.
    return [argv[0], "--strict-config", *argv[1:]]


def environment(result, selection_env):
    # Explicit selected policy also survives a fresh bounded parent environment.
    # The reviewed shell-policy projection binds these references for descendants.
    result.update(bindings(selection_env))
    return result


def effective(config_value, environ):
    if selected(environ):
        markers = config_value.get("project_root_markers")
        if type(markers) is not list or markers:
            raise Refused("effective_project_selection_unknown")
        policy = config_value.get("shell_environment_policy")
        values = policy.get("set") if type(policy) is dict else None
        if type(values) is not dict or any(type(values.get(key)) is not str or values[key] != value
                for key, value in bindings(environ).items()):
            raise Refused("effective_managed_shell_unknown")
