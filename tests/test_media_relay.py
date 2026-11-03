"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-relay-test-")]
try:
    from feishu_bridge.app_frontend import AppFrontend, TeeTransport
finally:
    sys.argv = _argv


@pytest.fixture
def front(tmp_path):
    return AppFrontend(tmp_path, log=lambda m: None)


def _gateway_env(monkeypatch, on: bool = True):
    for k, v in (("FEISHU_BRIDGE_GATEWAY", "http://127.0.0.1:8790"),
                 ("FEISHU_BRIDGE_GATEWAY_TOKEN", "tkn")):
        monkeypatch.setenv(k, v if on else "")


def _capture_upload(monkeypatch, front, img: Path, reply: bytes = b'{"path": "/gw/x.gif"}'):
    """跑一次 relay_media, 返回它实际发出的请求 (body, headers)。"""
    sent = {}

    class _Resp:
        def read(self): return reply
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=None):
        sent["body"] = req.data
        sent["headers"] = req.headers
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    out = front.relay_media(str(img))
    return out, sent


# ── ① 真实 content-type ────────────────────────────────────────────
@pytest.mark.parametrize("name,ctype", [
    ("a.gif", "image/gif"), ("a.png", "image/png"), ("a.jpg", "image/jpeg"),
    ("a.jpeg", "image/jpeg"), ("a.webp", "image/webp"),
    ("a.mp4", "video/mp4"), ("a.mov", "video/quicktime"),
])
def test_relay_sends_real_content_type(monkeypatch, front, tmp_path, name, ctype):
    _gateway_env(monkeypatch)
    img = tmp_path / name
    img.write_bytes(b"x")
    _, sent = _capture_upload(monkeypatch, front, img)
    assert f"Content-Type: {ctype}".encode() in sent["body"]
    assert b"application/octet-stream" not in sent["body"]


def test_unknown_suffix_falls_back_to_octet_stream(monkeypatch, front, tmp_path):
    """认不出来就照旧 —— 网关那侧还有后缀兜底, 不该在这里把文件拦下来。"""
    _gateway_env(monkeypatch)
    f = tmp_path / "a.weird"
    f.write_bytes(b"x")
    _, sent = _capture_upload(monkeypatch, front, f)
    assert b"application/octet-stream" in sent["body"]


def test_uppercase_suffix_still_matched(monkeypatch, front, tmp_path):
    """Windows 上 .PNG/.JPG 很常见 —— 大小写不该决定它能不能送达。"""
    _gateway_env(monkeypatch)
    f = tmp_path / "A.PNG"
    f.write_bytes(b"x")
    _, sent = _capture_upload(monkeypatch, front, f)
    assert b"Content-Type: image/png" in sent["body"]


def test_relay_returns_gateway_path(monkeypatch, front, tmp_path):
    _gateway_env(monkeypatch)
    f = tmp_path / "a.gif"
    f.write_bytes(b"x")
    out, _ = _capture_upload(monkeypatch, front, f)
    assert out == "/gw/x.gif"


def test_no_gateway_configured_returns_none(monkeypatch, front, tmp_path):
    """本机模式: 不需要中转, 返回 None 是正常路径不是失败。"""
    _gateway_env(monkeypatch, on=False)
    f = tmp_path / "a.gif"
    f.write_bytes(b"x")
    assert front.relay_media(str(f)) is None
    assert front.relays_to_gateway is False


def test_relays_to_gateway_true_when_configured(monkeypatch, front):
    _gateway_env(monkeypatch)
    assert front.relays_to_gateway is True


# ── ② 中转失败不再冒充成功 ──────────────────────────────────────────
@pytest.fixture
def tee():
    f = MagicMock(spec=AppFrontend)
    t = TeeTransport(MagicMock(), f)
    t.feishu.send_media_file.return_value = True
    return t


def _evs(front_mock) -> list[dict]:
    return [c[0][0] for c in front_mock.broadcast.call_args_list]


def test_relay_failure_tells_the_user(tee):
    """配了网关却没中转成功 → 广播的本地路径 App 必然取不到, 不能假装发出去了。"""
    tee._front.relay_media.return_value = None
    tee._front.relays_to_gateway = True
    tee.send_media_file("chat1", "/tmp/a.png", "image")
    evs = _evs(tee._front)
    assert [e["ev"] for e in evs] == ["media", "notice"]
    assert "未能送达" in evs[1]["text"] and "a.png" in evs[1]["text"]


def test_local_mode_stays_silent(tee):
    """本机模式返回 None 是正常的 —— 这里冒出一条警告就是天天狼来了。"""
    tee._front.relay_media.return_value = None
    tee._front.relays_to_gateway = False
    tee.send_media_file("chat1", "/tmp/a.png", "image")
    evs = _evs(tee._front)
    assert [e["ev"] for e in evs] == ["media"]
    assert evs[0]["path"] == "/tmp/a.png"


def test_successful_relay_stays_silent_and_uses_gateway_path(tee):
    tee._front.relay_media.return_value = "/gw/a.png"
    tee._front.relays_to_gateway = True
    tee.send_media_file("chat1", "/tmp/a.png", "image")
    evs = _evs(tee._front)
    assert [e["ev"] for e in evs] == ["media"]
    assert evs[0]["path"] == "/gw/a.png"


def test_media_event_shape_unchanged(tee):
    """协议面不变 (客户端不用改): media 事件字段与改前一致。"""
    tee._front.relay_media.return_value = None
    tee._front.relays_to_gateway = False
    tee.send_media_file("chat1", "/tmp/a.png", "image")
    assert _evs(tee._front)[0] == {"ev": "media", "kind": "image",
                                   "path": "/tmp/a.png", "ok": True}










from urllib.error import HTTPError, URLError  # noqa: E402


def _img(tmp_path, name="a.gif"):
    f = tmp_path / name
    f.write_bytes(b"GIF89a")
    return f


def _attempts(monkeypatch, front, img, side_effects):
    """按 side_effects 逐次响应 (异常就抛, 否则当成功), 返回实际尝试次数与结果。"""
    calls = {"n": 0}
    slept = []

    class _Resp:
        def read(self): return b'{"path": "/gw/ok.gif"}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake(req, timeout=None):
        i = calls["n"]
        calls["n"] += 1
        eff = side_effects[i] if i < len(side_effects) else None
        if isinstance(eff, Exception):
            raise eff
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    out = front.relay_media(str(img))
    return calls["n"], out, slept


def test_transient_error_is_retried_and_can_succeed(monkeypatch, front, tmp_path):
    """Behavioral regression coverage."""
    _gateway_env(monkeypatch)
    n, out, _ = _attempts(monkeypatch, front, _img(tmp_path),
                          [OSError("[WinError 10054] 远程主机强迫关闭了一个现有的连接")])
    assert n == 2 and out == "/gw/ok.gif"


def test_gives_up_after_three_attempts(monkeypatch, front, tmp_path):
    _gateway_env(monkeypatch)
    boom = URLError("connection reset")
    n, out, slept = _attempts(monkeypatch, front, _img(tmp_path), [boom, boom, boom])
    assert n == 3 and out is None
    assert len(slept) == 2          # 最后一次失败后不必再睡


def test_backoff_between_attempts(monkeypatch, front, tmp_path):
    """退避要真的递增 —— 立刻重连三次多半撞同一个瞬断。"""
    _gateway_env(monkeypatch)
    boom = URLError("x")
    _, _, slept = _attempts(monkeypatch, front, _img(tmp_path), [boom, boom, boom])
    assert slept == sorted(slept) and slept[0] < slept[-1]


def test_http_error_is_not_retried(monkeypatch, front, tmp_path):
    """Behavioral regression coverage."""
    _gateway_env(monkeypatch)
    err = HTTPError("http://x/upload", 415, "Unsupported Media Type", {}, None)
    n, out, _ = _attempts(monkeypatch, front, _img(tmp_path), [err, err, err])
    assert n == 1 and out is None


def test_unreadable_file_is_not_retried(monkeypatch, front, tmp_path):
    """文件读不出来 (已被消费/权限) 重试也没用, 别白等两轮退避。"""
    _gateway_env(monkeypatch)
    n, out, slept = _attempts(monkeypatch, front, tmp_path / "不存在.gif", [])
    assert n == 0 and out is None and slept == []









def _http_err(code: str | int, msg: str = ""):
    return HTTPError("http://x/upload", int(code), msg or str(code), {}, None)


@pytest.mark.parametrize("code", [502, 503, 504])
def test_gateway_5xx_is_retried(monkeypatch, front, tmp_path, code):
    """Behavioral regression coverage."""
    _gateway_env(monkeypatch)
    err = _http_err(code)
    n, out, _ = _attempts(monkeypatch, front, _img(tmp_path), [err, err, err])
    assert n == 3 and out is None


def test_transient_502_then_success(monkeypatch, front, tmp_path):
    """Behavioral regression coverage."""
    _gateway_env(monkeypatch)
    n, out, _ = _attempts(monkeypatch, front, _img(tmp_path), [_http_err(502)])
    assert n == 2 and out == "/gw/ok.gif"


@pytest.mark.parametrize("code", [400, 401, 413, 415, 404])
def test_4xx_still_not_retried(monkeypatch, front, tmp_path, code):
    """4xx 是**这个请求本身**的问题, 重试三次结果一模一样, 只是慢三倍。"""
    _gateway_env(monkeypatch)
    err = _http_err(code)
    n, out, _ = _attempts(monkeypatch, front, _img(tmp_path), [err, err, err])
    assert n == 1 and out is None


def test_500_not_retried(monkeypatch, front, tmp_path):
    """Behavioral regression coverage."""
    _gateway_env(monkeypatch)
    err = _http_err(500)
    n, out, _ = _attempts(monkeypatch, front, _img(tmp_path), [err, err, err])
    assert n == 1 and out is None


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")
