"""多会话持久 — 纯 stdlib 会话登记 (read/write/active/record/resolve/迁移).

从 fleet_bridge 抽出便于单测 (无 lark / claude-agent-sdk 依赖, 任意 venv 可测)。
import 后由 bridge 设 `SESSIONS_FILE = <home>/sessions.json`; `LOG` 可选注入日志回调;
`PROJECTS_DIR` 可覆盖 (测试 session_exists)。

sessions.json: {chat_id: {active: sid|null, sessions: {sid: {name, ts}}}}
兼容旧扁平 {chat_id: sid} (chat_entry 迁移成单会话)。FORK_PENDING 等运行态留在 bridge, 不在此。
"""
from __future__ import annotations

import json
import hashlib
import os
import contextlib
import contextvars
import functools
from datetime import datetime, timezone
from pathlib import Path

# bridge 在 import 后覆盖这两个;默认值仅兜底/便于独立跑。
SESSIONS_FILE: Path = Path.home() / ".fleet-claude-bridge" / "sessions.json"
PROJECTS_DIR: Path = Path.home() / ".claude" / "projects"   # session_exists 用 (可测试覆盖)
LOG = None                                                  # 可注入 LOG(msg:str)
MANAGED_TRANSACTION = None  # Selected-home coordinator context; never a second lock.
_MAP_TRANSACTION = contextvars.ContextVar("managed_map_transaction", default=False)


@contextlib.contextmanager
def mutation():
    """Hold the SAME coordinator writer transaction across read/modify/write."""
    if MANAGED_TRANSACTION is None:
        yield
        return
    if _MAP_TRANSACTION.get() is not False:
        # Nested helpers remain inside the original same-coordinator scope.
        yield
        return
    with MANAGED_TRANSACTION() as guard:
        token = _MAP_TRANSACTION.set((True, guard))
        try:
            yield
        finally:
            _MAP_TRANSACTION.reset(token)


def _transactional(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with mutation():
            return fn(*args, **kwargs)
    return wrapped


def _log(msg: str) -> None:
    if LOG:
        LOG(msg)


def read_sessions() -> dict:
    """读不到返回 {}。**文件不存在是正常的** (首次运行), 但读/解析失败要留痕:

    这个 {} 会一路喂给 `_load_session()` → `resume_sid=None` → bridge **开一个新会话**,
    表现是"用户的上下文凭空没了", 而日志里一个字都没有 —— 跟"这个 chat 本来就没聊过"
    完全无法区分。
    """
    if MANAGED_TRANSACTION is not None:
        import fleet_managed_map
        return fleet_managed_map.read(SESSIONS_FILE)
    if not SESSIONS_FILE.exists():
        return {}                       # 首次运行, 不是异常
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _log(f"⚠ 会话映射读失败 ({exc}) —— 本次会当成「无历史会话」, "
             f"下一条消息将开新会话而不是续接")
        return {}


def write_sessions(data: dict) -> None:
    if MANAGED_TRANSACTION is not None:
        import fleet_managed_map
        context = _MAP_TRANSACTION.get()
        if type(context) is not tuple or len(context) != 2 or context[0] is not True:
            raise fleet_managed_map.Refused("managed_map_transaction_required")
        if context[1] is not None:
            # The actual Runtime yields its scoped validator; isolated physical
            # map fixtures may use an explicit trusted context without one.
            context[1](SESSIONS_FILE, data)
        fleet_managed_map.write(SESSIONS_FILE, data)
        return
    try:
        tmp = SESSIONS_FILE.with_suffix(".tmp")     # 原子写
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SESSIONS_FILE)
    except Exception as exc:  # noqa: BLE001
        _log(f"⚠ 写 session 失败: {exc}")


def chat_entry(data: dict, chat_id: str) -> dict:
    """取某 chat 的 {active, sessions}; 兼容旧扁平 str 格式 (迁移成单会话)."""
    e = data.get(chat_id)
    if isinstance(e, str):                          # 旧格式 {chat_id: sid}
        return {"active": e, "sessions": {e: {"name": "", "ts": ""}}}
    if isinstance(e, dict):
        return {"active": e.get("active"), "sessions": e.get("sessions", {})}
    return {"active": None, "sessions": {}}


def load_session(chat_id: str) -> str | None:
    """当前 active session_id (给 resume); 无则 None (开新)."""
    return chat_entry(read_sessions(), chat_id).get("active")


@_transactional
def record_session(chat_id: str, session_id: str, preview: str = "") -> None:
    """每轮记录 active session_id (新会话首次记则用 preview 首段当默认名)."""
    if not session_id:
        return
    data = read_sessions()
    e = chat_entry(data, chat_id)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sess = dict(e["sessions"])
    if session_id not in sess:                      # 新会话 → 默认名=首条消息片段
        sess[session_id] = {"name": (preview or "").strip().replace("\n", " ")[:24], "ts": now}
    else:
        sess[session_id] = {**sess[session_id], "ts": now}
    write_sessions({**data, chat_id: {"active": session_id, "sessions": sess}})


@_transactional
def clear_session(chat_id: str) -> None:
    """/new: active 置空 (开新会话), 旧会话仍留 list 可 resume."""
    data = read_sessions()
    e = chat_entry(data, chat_id)
    write_sessions({**data, chat_id: {"active": None, "sessions": e["sessions"]}})


def sessions_revision() -> tuple | None:
    """Observe a stable registry file version; unknown versions cannot be replaced."""
    def identity(st):
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns
    try:
        with SESSIONS_FILE.open("rb") as source:
            before = identity(os.fstat(source.fileno()))
            raw = source.read()
            after = identity(os.fstat(source.fileno()))
        if before != after or after != identity(SESSIONS_FILE.stat()):
            return None
        if not isinstance(json.loads(raw), dict):
            return None
        return after, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError):
        return None


@_transactional
def discard_session(chat_id: str, session_id: str) -> None:
    """Deactivate only the matching ID; retain all historical metadata."""
    data = read_sessions()
    if chat_id not in data:
        return
    e = chat_entry(data, chat_id)
    if e["active"] != session_id:
        return
    entry = dict(data[chat_id]) if isinstance(data[chat_id], dict) else e
    entry["active"] = None
    write_sessions({**data, chat_id: entry})


@_transactional
def record_replacement(chat_id: str, session_id: str, *, expected: str,
                       revision: tuple, preview: str = "", codex_namespace: dict | None = None) -> bool:
    """Register a fresh replacement without dropping history or overwriting drift.

    This synchronous bridge-instance check does not exclude unrelated external
    writers; any observed file-version change refuses the replacement.
    """
    if not revision or sessions_revision() != revision or not session_id:
        return False
    if codex_namespace is not None and (
            type(codex_namespace) is not dict or set(codex_namespace) != {"home", "namespace"}
            or any(type(value) is not str or not value for value in codex_namespace.values())):
        return False
    data = read_sessions()
    e = chat_entry(data, chat_id)
    if e["active"] != expected or session_id in e["sessions"]:
        return False
    entry = dict(data[chat_id]) if isinstance(data[chat_id], dict) else e
    entry["sessions"] = {**e["sessions"], session_id: {
        "name": preview.strip().replace("\n", " ")[:24],
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **({"fleet_codex_namespace": dict(codex_namespace)} if codex_namespace is not None else {}),
    }}
    entry["active"] = session_id
    if sessions_revision() != revision:
        return False
    updated = {**data, chat_id: entry}
    write_sessions(updated)
    return read_sessions() == updated and sessions_revision() is not None


@_transactional
def set_active(chat_id: str, session_id: str) -> None:
    data = read_sessions()
    e = chat_entry(data, chat_id)
    write_sessions({**data, chat_id: {"active": session_id, "sessions": e["sessions"]}})


@_transactional
def name_active(chat_id: str, name: str) -> bool:
    data = read_sessions()
    e = chat_entry(data, chat_id)
    sid = e.get("active")
    if not sid or sid not in e["sessions"]:
        return False
    sess = {**e["sessions"], sid: {**e["sessions"][sid], "name": name[:40]}}
    write_sessions({**data, chat_id: {"active": sid, "sessions": sess}})
    return True


def list_sessions(chat_id: str):
    """→ (active_sid, [(sid, name, ts), ...] 按 ts 倒序)."""
    e = chat_entry(read_sessions(), chat_id)
    items = sorted(e["sessions"].items(), key=lambda kv: kv[1].get("ts", ""), reverse=True)
    return e.get("active"), [(sid, v.get("name", ""), v.get("ts", "")) for sid, v in items]


def resolve_session(chat_id: str, token: str) -> list[str]:
    """token → 匹配 sid 列表. 精确 id 命中→只返它(唯一); 否则前缀 id + 名字子串全收,
    >1 由调用方提示用更长前缀 (防静默切错会话)."""
    token = (token or "").strip()
    if not token:
        return []
    e = chat_entry(read_sessions(), chat_id)
    if token in e["sessions"]:                      # 精确 id → 唯一, 优先于前缀歧义
        return [token]
    hits: list[str] = []
    for sid in e["sessions"]:                        # 前缀 id
        if sid.startswith(token):
            hits.append(sid)
    for sid, v in e["sessions"].items():             # 名字子串 (不重复)
        if sid not in hits and v.get("name") and token.lower() in v["name"].lower():
            hits.append(sid)
    return hits


def session_exists(session_id: str) -> bool:
    """transcript 还在才 resume — 防 session 被清/裁剪后 resume 失败致 bot 不回复."""
    if not session_id:
        return False
    try:
        return any(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    except Exception:  # noqa: BLE001
        return False


def transcript_path(session_id: str) -> Path | None:
    """The session's transcript file, or None (auto-rollover counts compacts in it)."""
    if not session_id:
        return None
    try:
        return next(iter(PROJECTS_DIR.glob(f"*/{session_id}.jsonl")), None)
    except Exception:  # noqa: BLE001
        return None


@_transactional
def migrate_sessions_once() -> None:
    """启动时把旧扁平 {chat: sid} 一次性规范成 {chat: {active, sessions}} 落盘 (免每次 read 重迁)."""
    data = read_sessions()
    if not data or not any(isinstance(v, str) for v in data.values()):
        return
    norm = {chat: ({"active": v, "sessions": {v: {"name": "", "ts": ""}}} if isinstance(v, str) else v)
            for chat, v in data.items()}
    write_sessions(norm)
    _log("已迁移旧扁平 sessions.json → 多会话结构")
