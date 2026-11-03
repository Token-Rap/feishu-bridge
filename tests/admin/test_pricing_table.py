'Pricing table behavior and calibration checks.'
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from feishu_bridge.admin.fleet_admin import _PRICING, _PRICING_CALIBRATED, _price  # noqa: E402


def test_sonnet_5_is_the_cheaper_standard_price():
    """Behavioral regression coverage."""
    assert _price("claude-sonnet-5")[:2] == (2.0, 10.0)


def test_sonnet_46_did_not_get_dragged_along():
    """**这条是整档改 sonnet 会踩的坑**: 4.6 仍是 $3/$15, 跟着改就低估 33%。"""
    assert _price("claude-sonnet-4-6")[:2] == (3.0, 15.0)
    assert _price("claude-sonnet-4-5")[:2] == (3.0, 15.0)


def test_fable_51_cache_hit_is_a_quarter():
    """fable-5.1 / mythos-5.1 的 cache 命中是 0.025× input ($0.25), 全表唯一的例外 ——
    按 fable-5 的 $1 算就是**高估 4 倍**, 而缓存命中恰恰是用量大头。"""
    assert _price("claude-fable-5-1")[2] == 0.25
    assert _price("claude-mythos-5-1")[2] == 0.25


def test_fable_5_keeps_the_standard_cache_rate():
    assert _price("claude-fable-5")[2] == 1.00


def test_longest_key_wins_regardless_of_dict_order():
    """**匹配不许依赖书写顺序。** 顺序匹配是个不显眼的隐式约定: 谁重排一下 dict 就全错,
    而且不报错 —— 报告照出, 只是数字变了。这条把顺序打乱再验一次。
    """
    from feishu_bridge.admin import fleet_admin as fa
    old = fa._PRICING
    try:
        fa._PRICING = dict(reversed(list(old.items())))     # 顺序完全颠倒
        assert fa._price("claude-sonnet-5")[:2] == (2.0, 10.0)
        assert fa._price("claude-fable-5-1")[2] == 0.25
    finally:
        fa._PRICING = old


def test_unknown_model_estimates_high():
    """未知模型按最贵估 —— 低报消耗比高报危险 (用户据此决定要不要省着用)。"""
    assert _price("claude-something-new-9")[:2] == (10.0, 50.0)
    assert _price("")[:2] == (10.0, 50.0)


def test_report_shows_the_calibration_date():
    'Reports show the pricing table calibration date.'
    src = (Path(__file__).resolve().parents[2] / "src/feishu_bridge/admin/fleet_admin.py").read_text(encoding="utf-8")
    assert "价格表校准于 {_PRICING_CALIBRATED}" in src


def test_calibration_date_looks_like_a_date():
    assert len(_PRICING_CALIBRATED) == 10 and _PRICING_CALIBRATED.count("-") == 2


# ── DeepSeek: 真实 USD 扣费价, 分高峰/低谷; 认不出 = 未知, 不按最贵估 ──
from datetime import datetime, timezone  # noqa: E402

_PEAK = datetime(2026, 10, 19, 2, 30, tzinfo=timezone.utc)
_OFF = datetime(2026, 10, 19, 5, 0, tzinfo=timezone.utc)
_WEEKEND = datetime(2026, 10, 18, 2, 30, tzinfo=timezone.utc)   # 周日


def test_deepseek_flash_peak_and_offpeak():
    """来源 api-docs.deepseek.com/quick_start/pricing: 高峰 $0.3/$1.2, 命中 $0.006; 低谷减半。"""
    assert _price("deepseek-flash", _PEAK) == (0.30, 1.20, 0.006, 0.30)
    assert _price("deepseek-flash", _OFF) == (0.15, 0.60, 0.003, 0.15)
    assert _price("deepseek-flash", _WEEKEND) == (0.15, 0.60, 0.003, 0.15)


def test_deepseek_cli_suffix_and_legacy_alias_price_as_flash():
    assert _price("deepseek-flash[1m]", _PEAK)[:2] == (0.30, 1.20)
    assert _price("deepseek-v4-flash", _PEAK)[:2] == (0.30, 1.20)


def test_deepseek_v4_pro():
    assert _price("deepseek-v4-pro", _PEAK) == (1.32, 3.96, 0.044, 1.32)
    assert _price("deepseek-v4-pro", _OFF) == (0.66, 1.98, 0.022, 0.66)


def test_deepseek_without_time_is_priced_peak():
    assert _price("deepseek-flash")[:2] == (0.30, 1.20)


def test_unknown_deepseek_model_is_unknown_not_most_expensive():
    assert _price("deepseek-something-new") is None


def test_unpriced_tokens_are_reported_not_guessed(tmp_path):
    import json
    from feishu_bridge.admin import fleet_admin as fa
    proj = tmp_path / "p"
    proj.mkdir()
    now = datetime.now(timezone.utc).isoformat()
    rows = [{"type": "assistant", "timestamp": now,
             "message": {"model": "deepseek-mystery", "usage": {"input_tokens": 1000, "output_tokens": 10}}},
            {"type": "assistant", "timestamp": now,
             "message": {"model": "deepseek-flash", "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}}]
    (proj / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    agg = fa.aggregate_usage(tmp_path, 1)
    assert agg["total"]["unpriced_tokens"] == 1010
    assert agg["unpriced_models"] == ["deepseek-mystery"]
    assert agg["total"]["cost"] in (0.15, 0.30)          # 只算了 flash 那 1M 输入 (低谷/高峰)
    assert agg["total"]["deepseek_cost"] == agg["total"]["cost"]
