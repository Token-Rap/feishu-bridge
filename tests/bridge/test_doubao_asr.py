'Tests for the configured speech recognition engine and failure handling.'
from __future__ import annotations

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


def test_doubao_fails_soft_without_network():
    # 文件不存在 → ffmpeg 转码失败 → None (不碰网络, 不抛)
    assert b._transcribe_doubao("/nonexistent/voice.opus") is None


def test_transcribe_skips_doubao_without_key(monkeypatch):
    def boom(path):
        raise AssertionError("未配 Key 不该调豆包")
    monkeypatch.setattr(b, "_DOUBAO_ASR_KEY", "")
    monkeypatch.setattr(b, "_transcribe_doubao", boom)
    # 关键是 boom 没被触发 (没 Key 就不该去调豆包)
    assert b._transcribe("/nonexistent/voice.opus") is None


def test_transcribe_prefers_doubao_with_key(monkeypatch):
    monkeypatch.setattr(b, "_DOUBAO_ASR_KEY", "test-key")
    monkeypatch.setattr(b, "_transcribe_doubao", lambda p: "你好，舰队")
    assert b._transcribe("/whatever.opus") == "你好，舰队"








def test_no_key_now_means_no_transcription_at_all(monkeypatch):
    """靶心: 没配 Key 时直接失败, **不再有任何本地回落**。"""
    monkeypatch.setattr(b, "_DOUBAO_ASR_KEY", "")
    assert b._transcribe("/nonexistent/voice.opus") is None


def test_doubao_failure_no_longer_falls_back(monkeypatch):
    """豆包失败也直接失败 —— 原来这里会去加载 faster-whisper。"""
    monkeypatch.setattr(b, "_DOUBAO_ASR_KEY", "test-key")
    monkeypatch.setattr(b, "_transcribe_doubao", lambda p: None)
    assert b._transcribe("/whatever.opus") is None


def test_faster_whisper_is_not_imported_anymore():
    """回落删干净了没有 —— 留着 import 的话它会在运行时才炸 (包已从 requirements 移除)。"""
    src = Path(__file__).resolve().parents[2].joinpath(
        "src/feishu_bridge/bridge/fleet_bridge.py").read_text(encoding="utf-8")
    assert "faster_whisper" not in src, "还有 faster_whisper 的引用没拆干净"
    assert "WhisperModel" not in src, "还有 WhisperModel 的引用没拆干净"


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")
