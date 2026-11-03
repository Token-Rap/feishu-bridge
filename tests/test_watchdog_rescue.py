"""Behavioral regression coverage."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
import fleet_watchdog as w  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_state(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "RESCUE_STATE", tmp_path / "rescue.json")


def test_first_failed_rescue_is_quiet():
    """第一次救不活不报 —— 单次 kickstart 之后没起来太常见 (还在启动中)。"""
    assert w.note_rescue("sample-a") == 1
    assert not w.rescue_exhausted("sample-a")


def test_alerts_after_threshold():
    """靶心: 连着救不活就得说话。"""
    for _ in range(w.RESCUE_ALERT_AT):
        w.note_rescue("sample-a")
    assert w.rescue_exhausted("sample-a")


def test_alerts_only_on_the_crossing_not_every_tick():
    """Behavioral regression coverage."""
    for _ in range(w.RESCUE_ALERT_AT):
        w.note_rescue("sample-a")
    assert w.rescue_exhausted("sample-a") is True
    w.note_rescue("sample-a")
    assert w.rescue_exhausted("sample-a") is False, "第二次不该再报"


def test_recovery_resets_the_streak():
    """救活了就清零 —— 下次再坏还要能再报 (状态翻转两个方向都要认)。"""
    for _ in range(w.RESCUE_ALERT_AT + 2):
        w.note_rescue("sample-a")
    w.clear_rescue("sample-a")
    assert w.note_rescue("sample-a") == 1
    assert not w.rescue_exhausted("sample-a")


def test_bots_are_counted_separately():
    """一台救不活不该让另一台跟着报。"""
    for _ in range(w.RESCUE_ALERT_AT):
        w.note_rescue("sample-a")
    assert w.rescue_exhausted("sample-a")
    assert w.note_rescue("sample-agent") == 1 and not w.rescue_exhausted("sample-agent")


def test_state_survives_process_restart():
    """watchdog 每次都是新进程跑的 —— 计数不落盘就永远数不到 2。"""
    w.note_rescue("sample-a")
    assert json.loads(w.RESCUE_STATE.read_text())["sample-a"] >= 1
    assert w.note_rescue("sample-a") == 2


def test_broken_state_file_does_not_crash():
    """状态文件坏了要能继续跑 —— watchdog 自己崩掉比不报警更糟。"""
    w.RESCUE_STATE.write_text("{ 坏的", encoding="utf-8")
    assert w.note_rescue("sample-a") == 1


def test_clear_on_unknown_bot_is_noop():
    w.clear_rescue("从没出现过的")     # 不抛就算过


if __name__ == "__main__":      # noqa: PLR0133
    # 这个文件**当脚本直接跑不会执行任何测试** —— 全是裸 `def test_*`, 定义完就退出,
    # 退出码 0、零输出, 看起来跟"全过"一模一样。
    # 避免把未执行测试的零退出码误判为通过。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")
