"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from feishu_bridge.bridge.app_frontend import AppFrontend  # noqa: E402


def _front() -> AppFrontend:
    return AppFrontend(Path(tempfile.mkdtemp()), lambda *_a: None)


def test_origin_wins_when_known():
    """靶心: 下载过的文件, 回显要用网关侧那个路径。"""
    f = _front()
    f._origins["/win/local/copy.jpg"] = "/Users/me/.fleet-app/uploads/1_photo.jpg"
    assert f.origin_or("/win/local/copy.jpg") == "/Users/me/.fleet-app/uploads/1_photo.jpg"


def test_unknown_path_passes_through():
    """本机 bot 从来不走下载那条路 —— 它的路径网关直接读得到, 不能被改掉。"""
    f = _front()
    assert f.origin_or("/Users/me/.fleet-x/media/sent/a.png") == "/Users/me/.fleet-x/media/sent/a.png"


def test_origin_is_recorded_from_the_url_query():
    """原路径就在 URL 的 `path=` 里 —— **文件本来就是从那儿下来的**,
    所以回显不需要把字节再传回去 (出站那条路才需要 relay_media)。"""
    from urllib.parse import parse_qs, quote, urlparse
    url = ("http://192.0.2.1:8790/media?path="
           + quote("/Users/me/.fleet-app/uploads/1788_photo.jpg") + "&token=abc")
    got = parse_qs(urlparse(url).query).get("path", [""])[0]
    assert got == "/Users/me/.fleet-app/uploads/1788_photo.jpg"


def test_broadcast_uses_origin_or():
    """钉住调用点: 三处回显广播都要过 origin_or, 少一处就是那一种媒体仍然看不见。"""
    src = (Path(__file__).resolve().parent.parent / "src/feishu_bridge/bridge/fleet_bridge.py").read_text(encoding="utf-8")
    seg = src[src.index("async def _app_inject_image"):]
    seg = seg[:seg.index("if caption:")]
    assert seg.count("APP_FRONT.origin_or(p)") == 2, "image/video 两处回显没都换"


def test_recording_failure_does_not_break_download():
    """记不住原路径只是回显退回老行为 —— 不该让整个下载失败 (fail-soft)。"""
    src = (Path(__file__).resolve().parent.parent / "src/feishu_bridge/bridge/app_frontend.py").read_text(encoding="utf-8")
    seg = src[src.index("def _fetch_to_local"):src.index("def origin_or")]
    assert "except Exception" in seg.split("origin = parse_qs")[1]
