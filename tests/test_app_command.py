"""App 静默斜杠命令 (command 帧) 的行为测试。

要点: 不回显用户气泡 (无 ev:user 广播)、不镜像飞书 ("你: ..."), 命令回复照常;
非斜杠文本拒收 (普通消息必须走会回显的 user_message)。
"""
from __future__ import annotations

import asyncio
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

CHAT = "app-command-test"


class StubFeishu:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, chat, text):
        self.sent.append(text)


class StubSender:
    def __init__(self):
        self.texts: list[str] = []
        self.feishu = StubFeishu()

    def send(self, chat, text):
        self.texts.append(text)

    def send_text(self, chat, text):
        self.texts.append(text)


class StubFront:
    chat_id = CHAT

    def __init__(self):
        self.events: list[dict] = []

    def broadcast(self, ev):
        self.events.append(ev)


def _setup():
    sender, front = StubSender(), StubFront()
    b.SENDER, b.APP_FRONT = sender, front
    b.CLIENTS.pop(CHAT, None)
    return sender, front


def test_command_runs_without_echo():
    sender, front = _setup()
    asyncio.run(b._app_command("/compact"))
    # 无活动会话 → 命令的正常回复照常发出
    assert any("无可压缩" in t for t in sender.texts)
    # 静默: 无用户气泡广播、无飞书镜像
    assert not any(ev.get("ev") == "user" for ev in front.events)
    assert sender.feishu.sent == []


def test_non_slash_text_rejected():
    sender, front = _setup()
    asyncio.run(b._app_command("你好"))
    assert sender.texts == [] and sender.feishu.sent == []
    assert not any(ev.get("ev") == "user" for ev in front.events)


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")
