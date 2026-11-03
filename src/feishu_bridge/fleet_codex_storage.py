"""Fleet-owned Codex storage. Read-only admission; never provision/login/migrate.

This module is a SOURCE proposal. Missing configuration is a refusal, never a
fallback to the desktop home. The marker records an independently reviewed local
namespace/configuration, not a human grant or proof of a valid account login.
"""
from __future__ import annotations

import base64
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tomllib
from feishu_bridge import fleet_managed_codex as _managed_codex

KEY = "FLEET_CODEX_STORAGE_HOME"
MARKER = "fleet-storage.json"
DESKTOP_KEY = "FLEET_CODEX_DESKTOP_HOME"
_RESERVED = ("CODEX_HOME", "CODEX_SQLITE_HOME", KEY)
# Reviewed public ChatGPT route, not an observation of a loaded native default.
# Exact equality is intentional: no URL normalization, host suffix, or wildcard.
_CHATGPT_BASE_URL = "https://chatgpt.com/backend-api/"


class StorageRefused(RuntimeError):
    """Fixed technical disposition; no credential or private transcript content."""


def _refuse(reason: str):
    raise StorageRefused("Fleet Codex storage: " + reason)


def _windows_private(paths: list[Path]) -> None:
    # Standard read-only Windows ACL inspection. Do not change ACLs or accept
    # broad Users/Everyone/Authenticated Users access. Never infer ACL from mode.
    script = r'''$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue';
$me=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;
$paths=ConvertFrom-Json __PATHS__; foreach($p in $paths) {
 $i=Get-Item -LiteralPath $p -Force; if(($i.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'reparse'};
 $a=Get-Acl -LiteralPath $p; $owner=$a.GetOwner([Security.Principal.SecurityIdentifier]).Value;
 if($owner -ne $me){throw 'foreign_owner'};
 foreach($r in $a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
  if($r.AccessControlType -eq 'Allow' -and $r.IdentityReference.Value -notin @($me,'S-1-5-18','S-1-5-32-544')){throw 'nonprivate_acl'}
 }
}; 'private' '''
    literal = "'" + json.dumps([str(p) for p in paths]).replace("'", "''") + "'"
    script = script.replace("__PATHS__", literal)
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run([str(exe), "-NoProfile", "-NonInteractive", "-EncodedCommand",
            base64.b64encode(script.encode("utf-16le")).decode()], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        _refuse("windows_acl_unknown")
    if result.returncode != 0 or result.stdout.decode(errors="replace").strip() != "private":
        _refuse("windows_acl_refused")


def _private(paths: list[Path], *, contained: bool = False) -> None:
    for p in paths:
        try:
            s = p.lstat()
        except OSError:
            _refuse("missing_private_input")
        if p.is_symlink() or p.resolve() != p.absolute():
            _refuse("linked_input")
        if not (stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode)):
            _refuse("nonregular_input")
        if stat.S_ISREG(s.st_mode) and s.st_nlink != 1:
            _refuse("hardlinked_input")
        # Codex may create readable descendants inside its inaccessible 0700
        # root. They must remain account-owned and not writable by other users.
        if os.name != "nt" and (s.st_uid != os.getuid() or s.st_mode & (0o022 if contained else 0o077)):
            _refuse("foreign_or_nonprivate_input")
    if os.name == "nt":
        _windows_private(paths)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents



def _account_route(config: dict, *, effective: bool) -> None:
    """Closed built-in ChatGPT route; no custom provider interpretation.

    Local omitted provider/store use the reviewed CLI defaults. Effective RPC
    must expose them positively. Only absent/null or an empty provider map is
    reviewed; even a seemingly benign selected-provider record needs review.
    Effective projection may expose only the exact reviewed public ChatGPT URL.
    Local overrides remain unreviewed even when they use that same URL. No nested
    auth/header/token/command value is read, stringified or exported.
    """
    provider = config.get("model_provider", None if effective else "openai")
    store = config.get("cli_auth_credentials_store", None if effective else "file")
    reason = "effective_account_route_unknown" if effective else "account_route_unreviewed"
    if (type(provider) is not str or provider != "openai"
            or type(store) is not str or store != "file"
            or type(config.get("forced_login_method")) is not str
            or config["forced_login_method"] != "chatgpt"):
        _refuse(reason)
    providers = config.get("model_providers")
    if providers is not None and (type(providers) is not dict or providers):
        _refuse(reason)
    chatgpt_url = config.get("chatgpt_base_url")
    if chatgpt_url is not None and not (
            effective and type(chatgpt_url) is str and chatgpt_url == _CHATGPT_BASE_URL):
        _refuse(reason)
    # No other route override has been reviewed for this storage scope.
    # Null projected optional fields mean absent; all non-null values refuse,
    # including empty strings/maps and unknown native representations.
    if any(config.get(k) is not None for k in (
            "openai_base_url", "auth", "base_url",
            "env_key", "http_headers", "env_http_headers", "experimental_bearer_token")):
        _refuse(reason)


def _desktop_home(fleet: Path) -> Path:
    """Explicit desktop reference or observed inherited/default home, read-only.

    A child inherits the explicit reference so a nested Fleet Codex launch does
    not mistake its projected CODEX_HOME for the desktop. No home is created.
    The reference is configuration, not proof against deliberate same-user
    reconfiguration or multiple unknown desktop homes.
    """
    raw = os.environ.get(DESKTOP_KEY)
    inherited = os.environ.get("CODEX_HOME")
    if raw is not None and inherited and inherited not in (raw, str(fleet)):
        _refuse("desktop_reference_conflict")
    if raw is None:
        raw = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        if raw == str(fleet):
            _refuse("desktop_home_unknown")
    if type(raw) is not str or not raw or raw != raw.strip():
        _refuse("desktop_home_unknown")
    personal = Path(raw)
    if not personal.is_absolute() or personal.resolve() != personal.absolute():
        _refuse("desktop_home_unknown")
    if _inside(fleet, personal) or _inside(personal, fleet):
        _refuse("desktop_home_overlap")
    # Also keep the ordinary default personal tree separate when a custom one
    # is declared. Existence/content of either desktop tree is never inspected.
    default = (Path.home() / ".codex").resolve()
    if _inside(fleet, default) or _inside(default, fleet):
        _refuse("desktop_home_overlap")
    return personal


def _policy(policy: object, home: str, desktop: str) -> None:
    if type(policy) is not dict:
        _refuse("shell_policy_unknown")
    if policy.get("experimental_use_profile", False) is not False:
        _refuse("shell_profile_unreviewed")
    includes = policy.get("include_only", [])
    filters = policy.get("filters", {})
    if type(includes) is not list or any(type(v) is not str for v in includes) or type(filters) is not dict:
        _refuse("shell_policy_unknown")
    if any(type(k) is not str or v not in ("include", "exclude") for k, v in filters.items()):
        _refuse("shell_policy_unknown")
    if filters and (includes or policy.get("exclude")):
        _refuse("mixed_shell_filters")
    allow = includes or [k for k, v in filters.items() if v == "include"]
    if allow and any(not any(fnmatch.fnmatchcase(k.lower(), pattern.lower()) for pattern in allow)
                     for k in (*_RESERVED, DESKTOP_KEY)):
        _refuse("shell_filter_drops_storage")
    values = policy.get("set", {})
    if (type(values) is not dict or any(values.get(k) != home for k in _RESERVED)
            or values.get(DESKTOP_KEY) != desktop):
        _refuse("shell_storage_not_bound")


@dataclass(frozen=True)
class Scope:
    home: Path
    namespace: str
    config_sha256: str
    identity: tuple
    desktop_home: Path

    def config(self) -> dict:
        # Flat config keys are the supported App Server override representation.
        return {"sqlite_home": str(self.home),
                "shell_environment_policy.set." + DESKTOP_KEY: str(self.desktop_home),
                **{"shell_environment_policy.set." + k: str(self.home) for k in _RESERVED},
                **_managed_codex.config(os.environ)}

    def env(self, parent: dict | None = None) -> dict:
        if select() != self:
            _refuse("namespace_changed")
        result = dict(os.environ if parent is None else parent)
        result.update({k: str(self.home) for k in _RESERVED})
        result[DESKTOP_KEY] = str(self.desktop_home)
        return _managed_codex.environment(result, os.environ)

    def command(self, argv: list[str]) -> list[str]:
        if select() != self:
            _refuse("namespace_changed")
        return [*_managed_codex.command(argv, os.environ), *[v for k, value in self.config().items()
                        for v in ("-c", k + "=" + json.dumps(value, ensure_ascii=False))]]

    def effective(self, config: object) -> None:
        if select() != self:
            _refuse("namespace_changed")
        if type(config) is not dict or config.get("sqlite_home") != str(self.home):
            _refuse("effective_state_home_unknown")
        _account_route(config, effective=True)
        _managed_codex.effective(config, os.environ)
        _policy(config.get("shell_environment_policy"), str(self.home), str(self.desktop_home))
        # A custom log path may not escape the reviewed namespace.
        logs = config.get("log_dir")
        if logs is not None and (type(logs) is not str or not _inside(Path(logs).resolve(), self.home)):
            _refuse("foreign_log_home")

    def transcript(self, sid: str) -> Path:
        if select() != self:
            _refuse("namespace_changed")
        if type(sid) is not str or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", sid):
            _refuse("invalid_thread_id")
        root = self.home / "sessions"
        if not root.exists():
            _refuse("thread_not_in_fleet_namespace")
        _private([root], contained=True)
        try:
            # Do not traverse a symlink/reparse subtree or read the desktop home.
            matches = []
            for directory, dirs, files in os.walk(root, followlinks=False):
                p = Path(directory)
                _private([p, *[p / d for d in dirs]], contained=True)
                for name in files:
                    if fnmatch.fnmatchcase(name, "rollout-*-" + sid + ".jsonl"):
                        matches.append(p / name)
            if len(matches) != 1:
                _refuse("thread_not_in_fleet_namespace")
            path = matches[0]
            _private([path], contained=True)
            with path.open("rb") as stream:
                line = stream.readline(16385)
            if len(line) > 16384:
                _refuse("thread_header_unknown")
            meta = json.loads(line)
            if (type(meta) is not dict or meta.get("type") != "session_meta"
                    or type(meta.get("payload")) is not dict or meta["payload"].get("id") != sid):
                _refuse("thread_header_unknown")
            return path
        except (OSError, ValueError, UnicodeError):
            _refuse("thread_header_unknown")


def select() -> Scope:
    raw = os.environ.get(KEY)
    if type(raw) is not str or not raw.strip() or raw != raw.strip():
        _refuse("not_configured")
    if any(os.environ.get(k) for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
                                      "OPENAI_BASE_URL", "CHATGPT_BASE_URL")):
        _refuse("account_environment_unreviewed")
    home = Path(raw)
    if not home.is_absolute() or home.resolve() != home.absolute():
        _refuse("noncanonical_home")
    personal = _desktop_home(home)
    marker, config, auth = home / MARKER, home / "config.toml", home / "auth.json"
    _private([home, marker, config, auth])
    try:
        if marker.stat().st_size > 2048 or config.stat().st_size > 262144 or auth.stat().st_size == 0:
            _refuse("input_size_or_login_unknown")
        data = json.loads(marker.read_bytes())
        content = config.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if (type(data) is not dict or set(data) != {"version", "namespace", "config_sha256"}
                or type(data["version"]) is not int or data["version"] != 1
                or type(data["namespace"]) is not str or not re.fullmatch(r"[a-f0-9]{32}", data["namespace"])
                or data["config_sha256"] != digest):
            _refuse("namespace_or_config_unreviewed")
        cfg = tomllib.loads(content.decode("utf-8"))
        # Current per-device file-backed ChatGPT login only. Credentials are never
        # opened here. Keyring/provider continuity needs its own concrete review.
        _account_route(cfg, effective=False)
        # Persist these reserved values in the independently reviewed dedicated
        # config as well: a tool-launched Codex reads this same home recursively.
        if cfg.get("sqlite_home") != str(home):
            _refuse("configured_state_home_unbound")
        _policy(cfg.get("shell_environment_policy"), str(home), str(personal))
        s = home.stat()
        return Scope(home, data["namespace"], digest, (s.st_dev, s.st_ino), personal)
    except (OSError, ValueError, UnicodeError, tomllib.TOMLDecodeError):
        _refuse("namespace_or_config_unknown")


def runtime_binding() -> tuple:
    try:
        scope = select()
        return (str(scope.home), scope.namespace, scope.config_sha256, scope.identity, str(scope.desktop_home))
    except StorageRefused as exc:
        return (os.environ.get(KEY), str(exc))


@dataclass(frozen=True)
class Launch:
    """Unused legacy exec helper must return its env with argv; no iterable fallback."""
    argv: list[str]
    env: dict
    scope: Scope


def session_mapping(path: Path, key: str, loaded: str, scope: Scope) -> None:
    """Distinguish an intentional missing active ID from a swallowed read error."""
    try:
        if path.is_symlink():
            _refuse("session_mapping_linked")
        if not path.exists():
            if loaded:
                _refuse("session_mapping_unknown")
            return
        _private([path], contained=True)
        if path.stat().st_size > 2 * 1024 * 1024:
            _refuse("session_mapping_unknown")
        value = json.loads(path.read_bytes())
        if type(value) is not dict:
            _refuse("session_mapping_unknown")
        entry = value.get(key)
        active = entry.get("active") if type(entry) is dict else entry
        if active is not None and type(active) is not str:
            _refuse("session_mapping_unknown")
        if (active or "") != loaded:
            _refuse("session_mapping_changed")
        if loaded:
            sessions = entry.get("sessions") if type(entry) is dict else None
            metadata = sessions.get(loaded) if type(sessions) is dict else None
            expected = {"home": str(scope.home), "namespace": scope.namespace}
            if type(metadata) is not dict or metadata.get("fleet_codex_namespace") != expected:
                _refuse("session_namespace_unbound")
    except (OSError, ValueError, UnicodeError):
        _refuse("session_mapping_unknown")


def record_session(module, key: str, sid: str, scope: Scope, preview: str = "") -> None:
    """Same per-bot map/ID/history; new originals acquire their storage namespace.

    Never annotate an old unbound ID. Its exact migration needs separate owner
    review. Existing new/reset/rename/resume operations retain this row metadata.
    The write is the existing module's ordinary atomic file replacement; no new
    task/journal, no copying a personal transcript, and no context fallback.
    """
    if select() != scope or not sid:
        _refuse("namespace_changed")
    path = module.SESSIONS_FILE
    try:
        if path.is_symlink() or (path.exists() and path.stat().st_size > 2 * 1024 * 1024):
            _refuse("session_mapping_unknown")
        if path.exists():
            _private([path], contained=True)
        data = json.loads(path.read_bytes()) if path.exists() else {}
        if type(data) is not dict:
            _refuse("session_mapping_unknown")
        e = module.chat_entry(data, key)
        sessions = dict(e["sessions"])
        binding = {"home": str(scope.home), "namespace": scope.namespace}
        if sid in sessions and sessions[sid].get("fleet_codex_namespace") != binding:
            _refuse("session_namespace_unbound")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row = sessions.get(sid, {"name": (preview or "").strip().replace("\n", " ")[:24]})
        sessions[sid] = {**row, "ts": now, "fleet_codex_namespace": binding}
        module.write_sessions({**data, key: {"active": sid, "sessions": sessions}})
    except (OSError, ValueError, UnicodeError, TypeError, KeyError, AttributeError):
        _refuse("session_mapping_write_unknown")


async def terminal_preflight(scope: Scope, executable: str, cwd: Path) -> None:
    """Config-only public RPC, no thread/model/tool calls; before opening a PTY.

    CLI overrides bind both the final TUI and this read-only configuration check.
    Unknown effective shell filtering refuses instead of starting with a default
    desktop home. This function is not invoked by SOURCE fixture preparation.
    """
    argv = scope.command([executable, "app-server", "--listen", "stdio://",
        "-c", "features.apps=false", "-c", "features.plugins=false",
        "-c", "features.hooks=false", "-c", "notify=[]"])
    proc = await asyncio.create_subprocess_exec(*argv, cwd=str(cwd), env=scope.env(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=1024 * 1024)

    async def request(frame):
        proc.stdin.write((json.dumps(frame) + "\n").encode())
        await proc.stdin.drain()
        for _ in range(64):
            raw = await proc.stdout.readline()
            if not raw:
                _refuse("terminal_config_unknown")
            msg = json.loads(raw)
            if type(msg) is not dict or "method" in msg:
                _refuse("terminal_config_unexpected_frame")
            if msg.get("id") != frame["id"]:
                continue
            if "error" in msg:
                _refuse("terminal_config_refused")
            return msg
        _refuse("terminal_config_unknown")

    try:
        await asyncio.wait_for(request({"method": "initialize", "id": 1,
            "params": {"clientInfo": {"name": "fleet-storage-check", "version": "1.0"},
                       "capabilities": {"experimentalApi": True}}}), 10)
        proc.stdin.write(b'{"method":"notifications/initialized"}\n')
        await proc.stdin.drain()
        msg = await asyncio.wait_for(request({"method": "config/read", "id": 2,
            "params": {"cwd": str(cwd), "includeLayers": False}}), 10)
        scope.effective(msg.get("result", {}).get("config"))
    except (asyncio.TimeoutError, OSError, ValueError, AttributeError):
        _refuse("terminal_config_unknown")
    finally:
        if proc.stdin:
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.terminate()  # Only this newly owned config child; no bot signal.
            await proc.wait()
