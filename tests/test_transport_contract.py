"""Transport 抽象的契约测试。

作用: 让 `transport.py` 的接口**真正承重**, 而不是一份没人校验的文档。
- 飞书实现漏方法 / 改签名 → 这里红, 而不是等某个 bot 半夜发图时才炸。
- 将来接第二个前端 (自研多端 app), 照 `test_transport_protocol_is_satisfied`
  的写法加一个用例即可, 缺什么一目了然。

不连网、不发消息: FeishuSender 被替身顶掉, 只验「接口形状 + 翻译逻辑」。
"""
from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

# bridge 在 import 时就拿 sys.argv[1] 当 HOME 并 mkdir(media 等)。不接管的话:
# 单跑本文件 → 拿测试文件路径去 mkdir 报错; 跑全量 → 在仓库里建出 tests/media/。
# 所以指到临时目录再 import (模块级一次性, 之后 argv 还原, 不影响其它测试)。
_TMP_HOME = tempfile.mkdtemp(prefix="bridge-test-home-")
_argv = sys.argv
sys.argv = [_argv[0], _TMP_HOME]
try:
    import fleet_bridge as b  # noqa: E402
    import transport as t  # noqa: E402
finally:
    sys.argv = _argv


def _public_methods(cls) -> set[str]:
    return {n for n, _ in inspect.getmembers(cls, callable) if not n.startswith("_")}


@pytest.fixture
def ft():
    """FeishuTransport 实例, 内层 sender 换成 MagicMock (不发真请求)。"""
    with patch.object(b, "FeishuSender", MagicMock()):
        obj = b.FeishuTransport("app", "secret", "feishu")
    obj._s = MagicMock()
    return obj


# ── 接口形状 ────────────────────────────────────────────────────────
def test_feishu_transport_satisfies_protocol(ft):
    """飞书实现必须覆盖 Transport 的每个方法 (漏一个就红)。"""
    missing = _public_methods(t.Transport) - _public_methods(type(ft))
    assert not missing, f"FeishuTransport 缺少 Transport 要求的方法: {sorted(missing)}"


def test_stream_satisfies_protocol():
    missing = _public_methods(t.StreamSession) - _public_methods(b._Stream)
    assert not missing, f"_Stream 缺少 StreamSession 要求的方法: {sorted(missing)}"


def test_signatures_match_protocol(ft):
    """参数名/顺序也要一致 —— 核心用关键字调用时才不会崩。"""
    for name in _public_methods(t.Transport):
        want = list(inspect.signature(getattr(t.Transport, name)).parameters)
        got = list(inspect.signature(getattr(type(ft), name)).parameters)
        assert want == got, f"{name}() 签名不符: 协议 {want} vs 实现 {got}"


# ── 翻译逻辑 (平台中立动作 → 飞书 API 序列) ──────────────────────────
def test_send_media_file_image_does_upload_then_send(ft):
    """图片: 必须「先 upload 拿 key 再 send」, 两步式不能漏。"""
    ft._s.upload_image.return_value = "img_key_1"
    ft._s.send_image.return_value = True
    assert ft.send_media_file("chat1", "/tmp/a.png", "image") is True
    ft._s.upload_image.assert_called_once_with("/tmp/a.png")
    ft._s.send_image.assert_called_once_with("chat1", "img_key_1")
    ft._s.upload_video.assert_not_called()


def test_send_media_file_video_uses_video_api(ft):
    ft._s.upload_video.return_value = "vid_key_1"
    ft._s.send_media.return_value = True
    assert ft.send_media_file("chat1", "/tmp/a.mp4", "video") is True
    ft._s.upload_video.assert_called_once_with("/tmp/a.mp4")
    ft._s.send_media.assert_called_once_with("chat1", "vid_key_1")
    ft._s.upload_image.assert_not_called()


def test_send_media_file_upload_failure_short_circuits(ft):
    """上传失败就不该再调发送 (拿 None 当 key 发过去会 400)。"""
    ft._s.upload_image.return_value = None
    assert ft.send_media_file("chat1", "/tmp/a.png", "image") is False
    ft._s.send_image.assert_not_called()


def test_busy_maps_to_typing_reaction(ft):
    """「忙碌指示」在飞书 = Typing 表情; 句柄就是 reaction_id。"""
    ft._s.add_reaction.return_value = "rid_9"
    assert ft.begin_busy("msg1") == "rid_9"
    ft._s.add_reaction.assert_called_once_with("msg1", "Typing")
    ft.end_busy("msg1", "rid_9")
    ft._s.remove_reaction.assert_called_once_with("msg1", "rid_9")


def test_open_stream_passes_inner_sender_not_self(ft):
    """_Stream 要拿到内层 FeishuSender —— 传 self 会绕回 __getattr__ 刷告警。"""
    s = ft.open_stream("chat1", "msg1", "问题预览")
    assert isinstance(s, b._Stream)
    assert s._s is ft._s, "open_stream 应把内层 sender 交给 _Stream"


# ── 抽象泄漏检测 ────────────────────────────────────────────────────
def test_unknown_attr_falls_back_and_warns(ft, monkeypatch):
    """兜底转发保命, 但要留下 WARN 日志, 泄漏点不能静默。"""
    logged: list[str] = []
    monkeypatch.setattr(b, "log", logged.append)
    ft._s.some_feishu_only_call = MagicMock(return_value="ok")
    assert ft.some_feishu_only_call() == "ok"
    assert any("抽象泄漏" in m for m in logged), "泄漏调用必须打 WARN"


def test_core_uses_no_feishu_specific_calls():
    """核心逻辑里不该再出现飞书专有方法名 —— 这是本次重构的验收线。"""
    src = (_REPO / "fleet_bridge.py").read_text("utf-8")
    forbidden = ["add_reaction", "remove_reaction", "upload_image", "send_image",
                 "upload_video", "download_resource", "pin_message",
                 "create_stream_card", "send_card_by_id", "stream_put", "stream_finish"]
    leaks = [f"SENDER.{m}" for m in forbidden if f"SENDER.{m}" in src]
    assert not leaks, f"核心仍直接调用飞书专有方法: {leaks}"


def test_null_transport_covers_card_methods():
    """Behavioral regression coverage."""
    null = b.NullTransport()
    for name in ("send", "send_text", "send_reply", "send_media_file", "open_stream",
                 "_post_card", "update_card", "begin_busy", "end_busy", "pin"):
        assert hasattr(null, name), f"NullTransport 缺 {name}"
    assert null._post_card("c", {}) is None      # 返回 None → 钩子走兜底分支, 仍阻塞
    assert null.update_card("m", {}) is True


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")
