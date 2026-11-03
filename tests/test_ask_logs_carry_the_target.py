"""Behavioral regression coverage."""
import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[1] / "fleet_bridge.py").read_text(encoding="utf-8")


def test_ask_sent_log_states_the_target():
    m = re.search(r'log\(f"\[\{chat_id\[:8\]\}\] ❓ 提问卡已发[^"]*"', _SRC)
    assert m, "找不到发卡日志 —— 先修这条测试的锚点, 别删断言"
    assert "落点 {ui_chat}" in m.group(0), (
        "发卡日志没写显示落点。前缀是**运行会话**, 群聊回合跑在私聊里, "
        "只看前缀可能误判卡片的显示会话")


def test_button_answer_log_states_the_target():
    m = re.search(r'log\(f"\[\{chat_id\[:8\]\}\] 🖲 按钮作答[^"]*"', _SRC)
    assert m, "找不到按钮作答日志 —— 先修锚点"
    assert "落点 {ASK_DISPLAY.get(ask_id) or chat_id}" in m.group(0), (
        "作答日志没写落点 —— 答案气泡画在卡当初显示的会话, 可能不是运行会话")


def test_the_target_matches_what_the_event_actually_carries():
    """⚠ 日志里的落点必须和 **broadcast 真正带的那个值** 是同一个表达式。

    否则就是新的一份真相: 日志说一个落点, 事件发去另一个, 而**日志看起来是对的**。
    这比没有日志更坏 —— 今天一整天都在抓这个形状。
    """
    seg = _SRC[_SRC.index("ASK_PENDING[chat_id] = (ask_id, fut)"):]
    seg = seg[:seg.index("🖲 按钮作答") if "🖲 按钮作答" in seg else 4000]
    assert '"chat_id": ui_chat' in seg, "发卡事件带的不是 ui_chat 了, 日志那行要跟着改"
