"""Schedule an idle session rollover after repeated compactions; preserve a hand-off note."""
from __future__ import annotations

import json
import hashlib
import math
import re
import time
from pathlib import Path

DEFAULT_COMPACTS = 3
WINDOW = (2, 6)            # local hours [start, end): the node's own clock, both nodes are BJT
QUIET_S = 3600             # transcript untouched this long = nobody is working in this session
NOTE_MIN_BYTES = 200       # a hand-off note shorter than this is treated as not written
_PENDING = "pending.json"


def codex_transcript_path(sid: str, codex_home: Path) -> Path | None:
    if not sid or not re.fullmatch(r"[A-Za-z0-9-]+", sid):
        return None
    try:
        matches = list((codex_home / "sessions").glob(f"**/rollout-*-{sid}.jsonl"))
        return matches[0] if len(matches) == 1 else None
    except OSError:
        return None


def fingerprint(path: Path) -> tuple[int, int, str]:
    """Catch content changes even when size and mtime were restored."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    st = path.stat()
    return st.st_mtime_ns, st.st_size, digest.hexdigest()


def count_compacts(transcript: Path, engine: str = "claude") -> int:
    """compact_boundary markers in one session transcript (every trigger: auto and manual)."""
    n = 0
    try:
        with open(transcript, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                marker = '"compacted"' if engine == "codex" else '"compact_boundary"'
                if marker not in line:
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        continue
                    if (row.get("type") == "compacted" if engine == "codex"
                            else row.get("subtype") == "compact_boundary"):
                        n += 1
                except ValueError:
                    continue
    except OSError:
        return 0
    return n


def settings(rt: dict) -> tuple[bool, int]:
    """runtime.json → (enabled, N). Opt-out with "auto_rollover": false, like "advisor"."""
    rt = rt if isinstance(rt, dict) else {}
    enabled = rt.get("auto_rollover", True) is not False
    try:
        n = int(rt.get("auto_rollover_compacts", DEFAULT_COMPACTS))
    except (TypeError, ValueError):
        n = DEFAULT_COMPACTS
    return enabled, max(1, n)


def in_window(now: float | None = None, window: tuple[int, int] = WINDOW) -> bool:
    hour = time.localtime(now if now is not None else time.time()).tm_hour
    return window[0] <= hour < window[1]


def note_dir(home: Path) -> Path:
    return Path(home) / "handoff"


def note_path(home: Path, chat_key: str) -> Path:
    """One file per session key; the next rollover of the same session overwrites it."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", chat_key).strip("_") or "chat"
    return note_dir(home) / f"{safe}.md"


def handoff_prompt(path: Path, compacts: int) -> str:
    return (
        f"[自动换新会话 · 交接] 这个会话已压缩 {compacts} 次, 马上要换成一个全新的会话。"
        f"新会话看不到这里的任何内容, 只能读你现在写的交接笔记。\n"
        f"请用 Write 工具把交接笔记写到这个文件 (覆盖写): {path}\n"
        "内容用中文, 只写接手的人需要的, 按这几节:\n"
        "1. 进行中 / 没做完的事 (各一句: 做到哪、下一步是什么)\n"
        "2. 在等谁 (等用户 / 等哪个 bot, 等的是什么; 没有就写「无」)\n"
        "3. 最近的关键决定和约定 (用户拍板的、跟别的 bot 定下的契约; 附文件路径)\n"
        "4. 接手前必须先知道的坑 (没有就写「无」)\n"
        "规则: 不要做任何别的事 —— 不回复用户、不给别的 bot 递话、不改代码、不提交。"
        "写完只回复一行: 已写好。"
    )


def codex_handoff_prompt(compacts: int) -> str:
    """The bridge writes the returned note; the fork needs no tools or write access."""
    return (
        f"[自动换新会话 · 交接] 源会话已压缩 {compacts} 次。只根据现有上下文，"
        "返回一份至少 200 UTF-8 字节的中文交接笔记正文，由桥接程序保存。\n"
        "写明：进行中事项及下一步；在等谁（没有写无）；关键决定和约定及文件路径；"
        "接手前的坑（没有写无）。不确定的内容明确标注，不编造。\n"
        "禁止调用任何工具、读写文件、执行命令、回复用户、发消息给其他 bot、"
        "继续原任务、提交或更改目标。只输出笔记正文。"
    )


def first_turn_note(path: Path, at: float, engine: str = "claude") -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(at))
    return (f"[自动换新会话] 上一个会话在 {stamp} 因多次压缩被自动换成了这个新会话。"
            f"先{'读取' if engine == 'codex' else '用 Read 读'}交接笔记 {path}, 再处理下面的消息。")


def _pending_file(home: Path) -> Path:
    return note_dir(home) / _PENDING


def _read_pending(home: Path, strict: bool = False) -> dict:
    try:
        d = json.loads(_pending_file(home).read_text(encoding="utf-8"))
        if strict and not isinstance(d, dict):
            raise ValueError("pending handoff must be an object")
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise
        return {}


def _validate_pending(got: object) -> dict:
    if (not isinstance(got, dict) or not isinstance(got.get("path"), str)
            or not Path(got["path"]).is_absolute() or not isinstance(got.get("prev"), str)
            or not got["prev"] or isinstance(got.get("at"), bool)
            or not isinstance(got.get("at"), (int, float))
            or not math.isfinite(got["at"]) or got["at"] <= 0):
        raise ValueError("malformed pending handoff entry")
    return got


def _write_pending(home: Path, d: dict) -> None:
    f = _pending_file(home)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)


def mark_pending(home: Path, chat_key: str, path: Path, at: float, prev: str) -> None:
    """Survives a restart: the note must reach the new session's first turn whenever it comes."""
    d = _read_pending(home, strict=True)  # never overwrite a broken handoff ledger
    d[chat_key] = {"path": str(path), "at": at, "prev": prev}
    _write_pending(home, d)


def take_pending(home: Path, chat_key: str, strict: bool = False) -> dict | None:
    """Pop the pending note for this chat (once). None if there is none."""
    d = _read_pending(home, strict=strict)
    if strict and chat_key in d:
        _validate_pending(d[chat_key])
    got = d.pop(chat_key, None)
    if got is not None:
        _write_pending(home, d)
    return got if isinstance(got, dict) else None


def peek_pending(home: Path, chat_key: str, strict: bool = False,
                 required: bool = False) -> dict | None:
    d = _read_pending(home, strict=strict)
    if required and chat_key not in d:
        raise ValueError("required pending handoff missing")
    got = d.get(chat_key)
    if strict and chat_key in d:
        return _validate_pending(got)
    return got if isinstance(got, dict) else None


def log_line(chat_key: str, compacts: int, path: Path, prev: str) -> str:
    return f"[{chat_key[:8]}] 🔁 auto-rollover: {compacts} compacts, note={path}, prev={prev[:8]}"
