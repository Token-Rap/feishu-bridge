"""app_frontend (Web 前端接入层) 的契约测试。

验三件事: ① TeeTransport 仍满足 Transport 协议 (核心无感知);
② 双写语义 — 飞书为主路 (返回值), Web 广播为旁路 (fail-soft);
③ socket 端到端 — 注入回调收到消息, 广播能到达连接的客户端。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

# 同 test_transport_contract: bridge import 期读 sys.argv[1] 当 HOME, 必须先接管
_TMP_HOME = tempfile.mkdtemp(prefix="bridge-test-home-")
_argv = sys.argv
sys.argv = [_argv[0], _TMP_HOME]
try:
    from feishu_bridge.bridge import fleet_bridge as b  # noqa: E402
    from feishu_bridge.bridge import transport as t  # noqa: E402
    from feishu_bridge.bridge.app_frontend import AppFrontend, TeeTransport, _TeeStream  # noqa: E402
finally:
    sys.argv = _argv


def _public_methods(cls) -> set[str]:
    return {n for n, _ in inspect.getmembers(cls, callable) if not n.startswith("_")}


@pytest.fixture
def front(tmp_path):
    return AppFrontend(tmp_path, log=lambda m: None)


@pytest.fixture
def tee(front):
    feishu = MagicMock()
    front.broadcast = MagicMock(wraps=front.broadcast)
    return TeeTransport(feishu, front)


# ── ① 协议不被 Tee 破坏 ─────────────────────────────────────────────
def test_tee_satisfies_transport_protocol(tee):
    missing = [m for m in _public_methods(t.Transport) if not hasattr(tee, m)]
    assert not missing, f"TeeTransport 缺 Transport 方法: {missing}"


def test_tee_stream_satisfies_protocol():
    missing = _public_methods(t.StreamSession) - _public_methods(_TeeStream)
    assert not missing, f"_TeeStream 缺 StreamSession 方法: {sorted(missing)}"


# ── ② 双写语义 ──────────────────────────────────────────────────────
def test_send_tees_to_both(tee, front):
    tee.send("chat1", "hello")
    tee.feishu.send.assert_called_once_with("chat1", "hello")
    front.broadcast.assert_called_once_with({"ev": "notice", "text": "hello"})


def test_send_reply_returns_feishu_mid_and_broadcasts(tee, front):
    tee.feishu.send_reply.return_value = "mid_1"
    assert tee.send_reply("chat1", "答案", ["🚀 任务A"], "ref_1") == "mid_1"
    ev = front.broadcast.call_args[0][0]
    assert ev["ev"] == "reply" and ev["text"] == "答案" and ev["thinking"] == ["🚀 任务A"]


def test_media_broadcast_carries_path_and_result(tee, front):
    tee.feishu.send_media_file.return_value = True
    assert tee.send_media_file("chat1", "/tmp/a.png", "image") is True
    ev = front.broadcast.call_args[0][0]
    assert ev == {"ev": "media", "kind": "image", "path": "/tmp/a.png", "ok": True}


def test_passthrough_methods_hit_feishu_directly(tee, front):
    """非输出类方法直通飞书, 不广播 (Web 不需要这些概念)。"""
    tee.fetch_message_text("m1")
    tee.begin_busy("m1")
    tee.feishu.fetch_message_text.assert_called_once()
    tee.feishu.begin_busy.assert_called_once()
    front.broadcast.assert_not_called()


def test_tee_stream_mirrors_inner_state_and_broadcasts():
    from unittest.mock import AsyncMock
    inner = AsyncMock()                  # start/put/finish 都是协程
    inner.ok = True
    inner.message_id = "m9"
    front = MagicMock()
    s = _TeeStream(inner, front)
    assert s.ok is True and s.message_id == "m9"

    async def run():
        await s.put_answer("部分", force=True)      # force 绕过节流
        await s.finish("最终", ["🚀 A"])
    asyncio.run(run())
    inner.put_answer.assert_called_once()
    inner.finish.assert_called_once()
    evs = [c[0][0] for c in front.broadcast.call_args_list]
    assert {"ev": "stream", "text": "部分"} in evs
    assert {"ev": "reply", "text": "最终"} in evs


def test_broadcast_without_start_is_noop(front):
    """网关没起时 (未 start / 无连接) 广播必须静默 —— 飞书主路不受影响。"""
    front.broadcast({"ev": "notice", "text": "x"})   # 不抛即通过


def test_on_feishu_user_persists_chat_and_mirrors(front, tmp_path):
    front.on_feishu_user("oc_abc", "你好")
    assert front.chat_id == "oc_abc"
    assert (tmp_path / "app_chat").read_text(encoding="utf-8") == "oc_abc"
    # 重建实例 → chat_id 从盘上恢复 (bridge 重启后 Web 仍可注入)
    assert AppFrontend(tmp_path, log=lambda m: None).chat_id == "oc_abc"


# ── ③ socket 端到端 ─────────────────────────────────────────────────
def test_socket_roundtrip():
    """网关连上 → 注入消息到回调; bridge 广播 → 网关收到。

    不用 pytest tmp_path: macOS 的 AF_UNIX 路径上限 104 字节, pytest 的
    tmp_path 常超限 → 显式用 /tmp 短路径。
    """
    tmp_path = Path(tempfile.mkdtemp(prefix="af-", dir="/tmp"))
    front = AppFrontend(tmp_path, log=lambda m: None)
    got: list[str] = []

    async def run():
        async def inject(text, client="unknown"):   # 注入回调多了来源标识
            got.append(text)
        await front.start(inject)
        reader, writer = await asyncio.open_unix_connection(str(tmp_path / "app.sock"))
        writer.write(b'{"type": "user_message", "text": "web \xe6\xb6\x88\xe6\x81\xaf"}\n')
        await writer.drain()
        await asyncio.sleep(0.1)                      # 注入是异步处理
        front.broadcast({"ev": "notice", "text": "回执"})
        # 两行: ① inject_ack (收下 user_message 时就写, 在派任务之前) ② notice
        ack = await asyncio.wait_for(reader.readline(), timeout=2)
        line = await asyncio.wait_for(reader.readline(), timeout=2)
        writer.close()
        return json.loads(ack), json.loads(line)

    ack, ev = asyncio.run(run())
    assert got == ["web 消息"]
    # ⚠ **这是 `inject_ack` 唯一的真 socket 证据** (契约 fleet_api §2.4):
    # 别的测试验的是帧内容与顺序, 只有这条证明它**真的被对端读到了**。
    # 它还顺带钉住"ack 排在事件前面" —— 注入方半关闭后只 drain 3 秒, 排后面就等于没有。
    assert ack == {"type": "inject_ack", "chat_id": "app"}
    # 所有下行事件在唯一扇出点附上 chat_id。
    # (契约 fleet_api §2.5: 恒有; 私聊 = 桥内部的 chat_id)。
    # 这里连会话章一起断言, 而不是放宽比较 —— 放宽的话哪天章没盖上也测不出来。
    assert ev == {"ev": "notice", "text": "回执", "chat_id": "app"}


def test_tool_brief_extracts_readable_summary():
    from feishu_bridge.bridge.app_frontend import tool_brief
    assert tool_brief("Read", {"file_path": "/a/b/c/main.py"}) == "main.py"
    assert tool_brief("Bash", {"command": "ls -la\n  | head"}) == "ls -la | head"
    assert tool_brief("Task", {"description": "扫描日志", "prompt": "x" * 999}) == "扫描日志"
    assert tool_brief("Skill", {}) == ""                      # 无已知键 → 空
    assert len(tool_brief("Agent", {"prompt": "很长" * 100})) <= 80


def test_offline_events_buffered_and_replayed(tmp_path):
    """Behavioral regression coverage."""
    from feishu_bridge.bridge import fleet_bridge as _b  # noqa: F401  (确保模块可导入)
    from feishu_bridge.bridge.app_frontend import AppFrontend

    f = AppFrontend(tmp_path, lambda *_: None)
    f._loop = None                      # 模拟"网关没连上"
    f.broadcast({"ev": "reply", "text": "关键回复"})
    f.broadcast({"ev": "stream", "text": "中间态"})      # 过程量不该存
    kinds = [e["ev"] for _, e in f._pending]
    assert kinds == ["reply"], kinds

    # 超出上限只留最近的
    for i in range(80):
        f.broadcast({"ev": "notice", "text": str(i)})
    assert len(f._pending) <= AppFrontend._PENDING_MAX


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")
