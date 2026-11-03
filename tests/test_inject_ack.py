"""Behavioral regression coverage."""
import asyncio
import json

from feishu_bridge import app_frontend


class _W:
    """只记下写了什么; 够验帧形状与顺序。"""

    def __init__(self, boom=False):
        self.lines, self.boom = [], boom

    def write(self, b):
        if self.boom:
            raise OSError("对端已断")
        self.lines.append(json.loads(b.decode()))


def _front(validator=None):
    f = app_frontend.AppFrontend.__new__(app_frontend.AppFrontend)
    f.chat_id = "backend"
    f._log = lambda *a, **k: None
    f._bg = set()
    f._gateway_writers = set()
    f._peer_report = None
    f._inject = lambda text, client: asyncio.sleep(0)
    if validator:
        f.set_chat_validator(validator)
    return f


def _send_frame(f, w, req):
    # ⚠ 用自建 loop 而不是 `asyncio.run`: `_handle_frame` 会 `ensure_future` 派后台任务,
    # `asyncio.run` 收尾时会把它取消并告警, 噪声盖住真失败。也不用 `get_event_loop`
    # (3.12+ 无运行 loop 时直接抛)。
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(f._handle_frame(req, w))
        # 把派出去的后台任务收干净 —— 不收的话 pytest 会吐
        # 「Task was destroyed but it is pending!」。**测试输出里的噪声会掩盖真失败**,
        # 这条今天已经吃过亏 (脚本退出码 0 + 零输出看起来完全正常)。
        if f._bg:
            loop.run_until_complete(asyncio.gather(*f._bg, return_exceptions=True))
    finally:
        loop.close()


def _send(f, w, chat_id):
    _send_frame(f, w, {"type": "user_message", "text": "hi", "chat_id": chat_id})


def _send_cmd(f, w, chat_id):
    _send_frame(f, w, {"type": "command", "text": "/compact", "chat_id": chat_id})


def _acks(w):
    return [x for x in w.lines if x.get("type") == "inject_ack"]


def test_ack_reports_the_group_when_allowed():
    f = _front(lambda c: (c == "group:fleet-product", ""))
    w = _W(); _send(f, w, "group:fleet-product")
    assert _acks(w) == [{"type": "inject_ack", "chat_id": "group:fleet-product"}]


def test_ack_reports_private_when_bridge_silently_falls_back():
    """靶心: 要求进群但被退回私聊时, 回执必须说**私聊**, 不能顺着请求说。"""
    f = _front(lambda c: (False, "not_member"))          # 任何域都不放行
    w = _W(); _send(f, w, "group:finance")
    assert _acks(w) == [{"type": "inject_ack", "chat_id": "backend",
                         "reason": "not_member"}], \
        "被静默退回私聊时, ack 仍报了群 —— 那就是把假成功搬到了新字段里"


def test_ack_for_plain_private_injection():
    f = _front()
    w = _W(); _send(f, w, None)
    assert _acks(w) == [{"type": "inject_ack", "chat_id": "backend"}]


def test_ack_is_written_before_the_turn_is_spawned():
    """⚠ 必须在派任务**之前**写: 注入方半关闭后只 drain 3 秒。

    等整轮跑完再写 = 它永远看不到, 而那又是一次"看起来正常的沉默"。
    """
    order = []
    f = _front()
    f._inject = lambda text, client: (order.append("spawn"), asyncio.sleep(0))[1]
    w = _W()
    real = w.write
    w.write = lambda b: (order.append("ack"), real(b))[1]
    _send(f, w, None)
    assert order[:2] == ["ack", "spawn"], f"顺序不对: {order}"


def test_ack_write_failure_does_not_break_the_turn():
    """Behavioral regression coverage."""
    f = _front()
    spawned = []
    f._inject = lambda text, client: (spawned.append(1), asyncio.sleep(0))[1]
    _send(f, _W(boom=True), None)        # 写就抛
    assert spawned, "写 ack 失败把这一轮也带倒了"









def _cmd_front(validator=None):
    f = _front(validator)
    f._command = lambda text: asyncio.sleep(0)
    return f


def test_command_ack_reports_the_group_when_allowed():
    f = _cmd_front(lambda c: (c == "group:fleet-product", ""))
    w = _W(); _send_cmd(f, w, "group:fleet-product")
    assert _acks(w) == [{"type": "inject_ack", "chat_id": "group:fleet-product"}]


def test_command_ack_reports_private_when_silently_falling_back():
    """靶心: guard 要压群, 桥退回私聊 —— 回执必须说私聊, 否则它照旧压错还以为压对了。"""
    f = _cmd_front(lambda c: (False, "not_member"))
    w = _W(); _send_cmd(f, w, "group:finance")
    assert _acks(w) == [{"type": "inject_ack", "chat_id": "backend",
                         "reason": "not_member"}], \
        "命令被退回私聊时 ack 仍报了群 —— 假成功换了个字段继续存在"


def test_command_ack_is_written_before_the_command_is_spawned():
    """同样只有 3 秒窗口: 等 /compact 跑完再写, guard 永远读不到。"""
    order = []
    f = _cmd_front()
    f._command = lambda text: (order.append("spawn"), asyncio.sleep(0))[1]
    w = _W()
    real = w.write
    w.write = lambda b: (order.append("ack"), real(b))[1]
    _send_cmd(f, w, None)
    assert order[:2] == ["ack", "spawn"], f"顺序不对: {order}"


def test_command_ack_write_failure_does_not_break_the_command():
    f = _cmd_front()
    spawned = []
    f._command = lambda text: (spawned.append(1), asyncio.sleep(0))[1]
    _send_cmd(f, _W(boom=True), None)
    assert spawned, "写 ack 失败把这条命令也带倒了"
