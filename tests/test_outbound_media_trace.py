"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
_argv = sys.argv
_HOME = tempfile.mkdtemp(prefix="bridge-media-trace-")
sys.argv = [_argv[0], _HOME]
try:
    from feishu_bridge import fleet_bridge as b
    from feishu_bridge.app_frontend import AppFrontend, TeeTransport
finally:
    sys.argv = _argv


@pytest.fixture
def sender(monkeypatch):
    s = MagicMock()
    s.send_media_file.return_value = True
    monkeypatch.setattr(b, "SENDER", s)
    return s


# ── ① 文件不在/是空的 → 不发, 而且要说出来 ──────────────────────────
def test_missing_file_is_not_sent(sender, tmp_path):
    """纯 App 模式下这条以前是「静默成功」: 广播一个取不到的路径, 用户对着转圈。"""
    b._send_local_media("chat1", "video", str(tmp_path / "没有这个.mp4"))
    sender.send_media_file.assert_not_called()


def test_missing_file_tells_the_user_which_one(sender, tmp_path):
    b._send_local_media("chat1", "video", str(tmp_path / "没有这个.mp4"))
    said = " ".join(c[0][1] for c in sender.send_text.call_args_list)
    assert "没有这个.mp4" in said, said


def test_empty_file_is_treated_as_missing(sender, tmp_path):
    """0 字节的 mp4 上传上去也是坏的 —— 与其发出去再失败, 不如当场说清楚。"""
    f = tmp_path / "空.mp4"
    f.write_bytes(b"")
    b._send_local_media("chat1", "video", str(f))
    sender.send_media_file.assert_not_called()


def test_normal_file_still_goes_through(sender, tmp_path):
    """前置校验只拦死路径, 正常文件的行为一个字不变。"""
    f = tmp_path / "好的.mp4"
    f.write_bytes(b"x" * 1024)
    b._send_local_media("chat1", "video", str(f))
    sender.send_media_file.assert_called_once_with("chat1", str(f), "video")


def test_send_failure_still_warns(sender, tmp_path):
    """原有的失败提示不能被新逻辑吃掉。"""
    f = tmp_path / "好的.mp4"
    f.write_bytes(b"x" * 1024)
    sender.send_media_file.return_value = False
    b._send_local_media("chat1", "video", str(f))
    said = " ".join(c[0][1] for c in sender.send_text.call_args_list)
    assert "发送失败" in said and "好的.mp4" in said


# ── ② 每次出站都留一行 ──────────────────────────────────────────────
@pytest.fixture
def tee(tmp_path):
    lines: list[str] = []
    front = AppFrontend(tmp_path, log=lines.append)
    t = TeeTransport(MagicMock(), front)
    t.feishu.send_media_file.return_value = True
    return t, front, lines


def _media_lines(lines: list[str]) -> list[str]:
    return [l for l in lines if "出站媒体" in l]


def test_outbound_media_logs_once(tee, tmp_path):
    t, _front, lines = tee
    f = tmp_path / "片子.mp4"
    f.write_bytes(b"x" * 2048)
    t.send_media_file("chat1", str(f), "video")
    assert len(_media_lines(lines)) == 1, lines


def test_log_carries_name_size_and_audience(tee, tmp_path):
    """文件名/大小是「发的是哪个」, 在线连接数是「有没有人接得住」。

    在线 0 不等于失败 (客户端上线后会从历史补), 但**它和「发给了 2 个客户端」是两回事**,
    分不开就没法回答用户那句「我这边什么都没看到」。
    """
    t, _front, lines = tee
    f = tmp_path / "片子.mp4"
    f.write_bytes(b"x" * 2048)
    t.send_media_file("chat1", str(f), "video")
    line = _media_lines(lines)[0]
    for want in ("片子.mp4", "2.0KB", "video"):
        assert want in line, f"{want!r} 不在: {line}"
    assert "0" in line, f"没说有几个客户端在线: {line}"


def test_log_says_when_file_is_gone(tee, tmp_path):
    """路径取不到大小时不许瞎报 —— 「-」和「0.0KB」是两回事。"""
    t, _front, lines = tee
    t.send_media_file("chat1", str(tmp_path / "没了.mp4"), "video")
    line = _media_lines(lines)[0]
    assert "-" in line, line


def test_logging_never_breaks_sending(tee, tmp_path, monkeypatch):
    """留痕是旁路: 它自己炸了也不许把这条媒体拦下来。"""
    t, front, _lines = tee
    monkeypatch.setattr(front, "_log", MagicMock(side_effect=RuntimeError("boom")))
    f = tmp_path / "片子.mp4"
    f.write_bytes(b"x" * 16)
    assert t.send_media_file("chat1", str(f), "video") is True


def test_media_event_shape_unchanged(tee, tmp_path):
    """协议面一个字段都不许动 (客户端不用改) —— 加日志不是改协议。"""
    t, front, _lines = tee
    front.relay_media = lambda _p: None
    evs = []
    front.broadcast = evs.append
    f = tmp_path / "片子.mp4"
    f.write_bytes(b"x" * 16)
    t.send_media_file("chat1", str(f), "video")
    assert evs[0] == {"ev": "media", "kind": "video", "path": str(f), "ok": True}


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")
