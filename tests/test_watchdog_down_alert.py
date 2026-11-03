'测试 watchdog 告警与哨兵；必须模拟所有发送通道，避免发送真实告警。'
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from feishu_bridge.watchdog import fleet_watchdog as w  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_sends(monkeypatch, tmp_path):
    """挡住两条真实发送通道 + 把哨兵挪到 tmp。"""
    sent = []
    monkeypatch.setenv("FLEET_WATCHDOG_ALERT_BOT", "test-agent")
    monkeypatch.setattr(w.Path, "home", lambda: tmp_path)
    (tmp_path / ".fleet-app").mkdir()
    (tmp_path / ".fleet-app" / "token").write_text("test-token")
    monkeypatch.setattr(w, "_WD_DOWN_FLAG", tmp_path / "down")

    class _R:
        returncode = 1          # 第一层默认失败, 好让第二层被走到
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: _R())

    def _fake_urlopen(req, timeout=0):
        sent.append(req)
        raise RuntimeError("网关也不通")   # 默认两层都不通, 单条测试再放开
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return sent


def test_alerts_and_leaves_a_sentinel():
    """靶心: 两条通道都不通时, 至少留下哨兵 —— 不能一起静默。"""
    w._alert_watchdog_down("测试原因")
    assert w._WD_DOWN_FLAG.exists()
    assert "测试原因" in w._WD_DOWN_FLAG.read_text(encoding="utf-8")


def test_second_time_is_quiet():
    """Behavioral regression coverage."""
    w._alert_watchdog_down("第一次")
    before = w._WD_DOWN_FLAG.read_text(encoding="utf-8")
    w._alert_watchdog_down("第二次")
    assert w._WD_DOWN_FLAG.read_text(encoding="utf-8") == before, "第二次不该重写/重报"


def test_recovery_clears_it():
    """恢复即清零 —— 下次再坏还要能再报 (状态翻转两个方向都要认)。"""
    w._alert_watchdog_down("坏了")
    w._clear_watchdog_down()
    assert not w._WD_DOWN_FLAG.exists()
    w._alert_watchdog_down("又坏了")
    assert w._WD_DOWN_FLAG.exists()


def test_gateway_channel_is_tried_when_notify_fails(_no_real_sends):
    """The gateway is attempted if the optional hook fails or is absent."""
    w._alert_watchdog_down("测试")
    assert _no_real_sends, "网关通道没被尝试 —— 第一层的成功判据可能又骗了我们"


def test_missing_registry_triggers_the_alert(monkeypatch, tmp_path):
    """端到端: 注册表读不到 → 走告警, 而不是 print 完就 return。"""
    called = []
    monkeypatch.setattr(w, "FLEET", tmp_path / "nope.json")
    monkeypatch.setattr(w, "check_gateway", lambda: None)
    monkeypatch.setattr(w, "_alert_watchdog_down", lambda why: called.append(why))
    w.main()
    assert called and "读不到 bot 注册表" in called[0], called


def test_gateway_alert_without_target_stays_local(monkeypatch, _no_real_sends):
    monkeypatch.delenv("FLEET_WATCHDOG_ALERT_BOT", raising=False)
    w._alert_watchdog_down("test")
    assert not _no_real_sends
    assert w._WD_DOWN_FLAG.exists()


def test_gateway_restart_requires_explicit_label(monkeypatch):
    monkeypatch.delenv("FLEET_GATEWAY_LAUNCHD_LABEL", raising=False)
    calls = []
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: calls.append(a))
    w.check_gateway()
    assert not calls


def test_configured_notify_hook_receives_arguments(monkeypatch):
    calls = []
    monkeypatch.setenv("FLEET_NOTIFY_HOOK", "/example/notify.py")
    monkeypatch.setattr(w.subprocess, "run", lambda args, **kw: calls.append(args))
    w._notify_crash(Path("/example/node"), "sample-agent", 1)
    assert calls and calls[0][:2] == ["python3", "/example/notify.py"]
    assert "--home" in calls[0] and "--title" in calls[0] and "--text" in calls[0]


def test_unconfigured_notify_hook_is_skipped(monkeypatch):
    calls = []
    monkeypatch.delenv("FLEET_NOTIFY_HOOK", raising=False)
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **kw: calls.append(a))
    w._notify_crash(Path("/example/node"), "sample-agent", 1)
    w._notify_rescue_failed(Path("/example/node"), "sample-agent", 2)
    assert calls == []
