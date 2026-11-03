"""Task 工具会话级清单 (修「任务卡冻结在 0/N」)。

根因: 旧版把清单放回合局部变量, 跨回合 TaskUpdate 对不上号被静默丢弃。
本组测试直接驱动 _session_tasks 的增删改语义 (跨调用共享 = 跨回合共享)。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv

CHAT = "task-track-test"


def _reset():
    b._TASK_TRACK.pop(CHAT, None)


def test_tasks_survive_across_calls():
    _reset()
    tasks = b._session_tasks(CHAT)
    tasks.append({"id": "1", "content": "任务A", "status": "pending"})
    # 模拟下一回合重新取 (旧 bug: 局部变量每回合清零)
    again = b._session_tasks(CHAT)
    assert again is tasks and len(again) == 1


def test_update_marks_completed_and_persists():
    _reset()
    tasks = b._session_tasks(CHAT)
    tasks.extend([{"id": "1", "content": "A", "status": "pending"},
                  {"id": "2", "content": "B", "status": "pending"}])
    b._save_task_track(CHAT)
    assert b._TASK_TRACK_FILE.is_file()
    tasks[0]["status"] = "completed"
    assert b._session_tasks(CHAT)[0]["status"] == "completed"


def test_session_switch_resets():
    _reset()
    b._session_tasks(CHAT).append({"id": "1", "content": "旧会话的", "status": "pending"})
    b._TASK_TRACK[CHAT]["sid"] = "old-session"     # 模拟换了会话 (当前 sid 为 "")
    assert b._session_tasks(CHAT) == []            # 新会话拿到干净清单


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")
