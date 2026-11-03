"""Behavioral regression coverage."""
import tempfile
from pathlib import Path

from feishu_bridge import fleet_watchdog as W


def _seen(line_body: str) -> bool:
    """这一行会不会被 **`_last_log_event` 本人** 当成「还在动」。

    ⚠ 早一版这里是 `any(k in line_body for k in W._ACTIVITY_MARKS)` —— **重新实现了判据**,
    于是把调用点退回 `startswith` 时这些测试照样全绿。**测桩位不等于测供给。**
    (跑控制组时当场发现的, 控制① 只红了一条源码断言。)
    现在写一份真的 bridge.log 进临时目录, 调真函数, 看它认不认这一行。
    """
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        (home / "bridge.log").write_text(
            f"[2026-09-14T12:00:00+00:00] [oc_x] {line_body}\n", encoding="utf-8")
        ts, _what = W._last_log_event(home)
        return ts > 0


def test_retry_decorated_tool_line_counts_as_activity():
    """靶心: 重试期那行必须算活动。"""
    assert _seen("(重试中) 工具: Bash"), "重试期的工具行被当成静默"


def test_app_mode_incoming_message_counts_as_activity():
    """App 模式收信行不以「收到」开头, 原来也漏了。

    后果: 一个「只思考不调工具」的回合**从第一秒就被算静默**。
    """
    assert _seen("💬 消息 (client=unknown): 📨 [来自 backend bot]")


def test_plain_forms_still_count():
    for body in ("工具: Bash", "收到文本", "收到图片", "✅ bridge 启动 (纯App模式)"):
        assert _seen(body), body


def test_unrelated_noise_does_not_count():
    """别宽到什么都算 —— 那样 watchdog 就永远不救人了。"""
    for body in ("lark ws 心跳", "⚠ 网关连接断开, 5s 后重连", "写 runs state 失败"):
        assert not _seen(body), body


def test_criterion_is_containment_not_prefix():
    """Behavioral regression coverage."""
    import inspect
    src = inspect.getsource(W._last_log_event)
    assert "startswith" not in src, "判据退回前缀匹配了 —— 带装饰的行会再次被当成静默"
    assert "_ACTIVITY_MARKS" in src, "判据没走标记表"


def test_threshold_is_not_tight_enough_to_kill_long_tool_calls():
    """Behavioral regression coverage."""
    assert W.STUCK_S >= 900, (
        f"STUCK_S={W.STUCK_S} 太紧: 跑长命令的 bot (灰度/测试套件) 会被当成卡死掐掉。"
        "硬卡死由心跳那条规则兜, 本条可以保守。")
