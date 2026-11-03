"""Selected native bridge startup is closed until full lifecycle wiring exists.

This prevents a partial component installation from accepting real work through
the legacy Feishu/frontend/PTY/helper paths. Default homes are unchanged. The
future trusted loader hook needs its own exact SOURCE and attestation review.
"""
from pathlib import Path

from feishu_bridge.fleet_managed_ingress import Refused, Selection


def require_startup_binding(environ, argv):
    keys = ("FLEET_TARGET_CONTROL_REQUIRED", "FLEET_TARGET_CONTROL_DIR",
            "FLEET_TARGET_CONTROL_ID")
    if all(environ.get(key) is None for key in keys):
        return
    home = environ.get("FLEET_BRIDGE_HOME")
    if type(home) is not str or not home:
        raise Refused("managed_bootstrap_home_unknown")
    Selection.from_env(environ, Path(home))
    # No boolean, raw frame flag, .env marker or reused diagnostic can supply the
    # absent writer/child/PTY/question/loader implementation.
    raise Refused("managed_full_lifecycle_unbound")
