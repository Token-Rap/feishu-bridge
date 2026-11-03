"""Behavioral regression coverage."""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv

CHAT = "task-id-bind-test"


def _bind(text: str) -> None:
    """复刻 run_turn 里的绑定逻辑 (工具结果 → 填最早一条未绑定的 id)。"""
    m = re.search(r"Task #(\d+) created", text)
    if m:
        pend = next((t for t in b._session_tasks(CHAT) if not t.get("id")), None)
        if pend is not None:
            pend["id"] = m.group(1)


def _create(content: str) -> None:
    b._session_tasks(CHAT).append({"id": "", "content": content, "status": "pending"})


def _update(tid: str, status: str) -> bool:
    tasks = b._session_tasks(CHAT)
    hit = next((t for t in tasks if t.get("id") == tid), None)
    if hit is None and tid.isdigit():
        base = next((int(t["id"]) for t in tasks if str(t.get("id", "")).isdigit()), None)
        i = int(tid) - base if base is not None else int(tid) - 1
        hit = tasks[i] if 0 <= i < len(tasks) else None
    if hit is None:
        return False
    hit["status"] = status
    return True


def test_global_task_ids_bind_and_update():
    b._TASK_TRACK.pop(CHAT, None)
    for i, name in enumerate(["领试用", "写中继", "验证"]):
        _create(name)
        _bind(f"Task #{10 + i} created successfully: {name}")   # harness 全局号从 10 起
    assert [t["id"] for t in b._session_tasks(CHAT)] == ["10", "11", "12"]
    assert _update("11", "completed")                            # 精确命中
    assert b._session_tasks(CHAT)[1]["status"] == "completed"


def test_fallback_when_binding_missing():
    """结果文本没绑上 → 用「全局号 - 首条号」推算, 仍能命中。"""
    b._TASK_TRACK.pop(CHAT, None)
    _create("A"); _bind("Task #7 created successfully: A")
    _create("B")                                                # 这条没绑上
    assert _update("8", "completed")                            # 8 - 7 = 序号 1
    assert b._session_tasks(CHAT)[1]["status"] == "completed"


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")
