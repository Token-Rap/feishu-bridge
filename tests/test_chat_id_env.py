"""Behavioral regression coverage."""
import asyncio

from feishu_bridge import fleet_bridge as fb


def _stub(monkeypatch):
    """把 _get_client 的外部依赖全换成桩, 只留下"传给 SDK 的 options"这条真路径。"""
    captured = {}

    class _Opts:
        def __init__(self, **kw):
            captured.update(kw)

    class _Client:
        def __init__(self, options=None):
            pass

        async def connect(self):
            pass

    monkeypatch.setattr(fb, "ClaudeAgentOptions", _Opts)
    monkeypatch.setattr(fb, "ClaudeSDKClient", _Client)
    monkeypatch.setattr(fb, "_thinking_opt", lambda: {})   # 假 Opts 不是 dataclass
    monkeypatch.setattr(fb, "_load_session", lambda c: None)
    monkeypatch.setattr(fb, "_read_runtime", lambda: {})
    monkeypatch.setattr(fb, "log", lambda *a, **k: None)
    monkeypatch.setattr(fb, "CLIENTS", {})
    monkeypatch.setattr(fb, "CLIENT_SIG", {})
    monkeypatch.setattr(fb, "FORK_PENDING", set())
    return captured


def test_group_display_does_not_replace_the_bot_session_env(monkeypatch):
    cap = _stub(monkeypatch)
    tok = fb.app_frontend.DISPLAY_CHAT.set("group:fleet-product")
    try:
        asyncio.run(fb._get_client("app"))
    finally:
        fb.app_frontend.DISPLAY_CHAT.reset(tok)
    assert cap["env"]["FLEET_CHAT_ID"] == "app"


def test_private_session_env_carries_its_own_chat_id(monkeypatch):
    """靶心: 两个会话各拿各的。写死成私聊 = 群会话的工具报出私聊身份, 又一次静默投错。"""
    cap = _stub(monkeypatch)
    asyncio.run(fb._get_client("app"))
    assert cap["env"]["FLEET_CHAT_ID"] == "app", \
        "私聊会话报出了别的会话键 —— 那就是把误投从发送方搬到了桥里"


def test_env_does_not_replace_the_inherited_environment(monkeypatch):
    """SDK 是 {**os.environ, **options.env} 合并 (subprocess_cli.py:689)。

    这条守的是**别往 env 里塞多余的东西**: 只放这一个键。哪天有人顺手把整个
    os.environ 拷进来, PATH/凭证就会被这份快照冻住, 而表现是"某天起工具找不到命令"。
    """
    cap = _stub(monkeypatch)
    asyncio.run(fb._get_client("app"))
    assert set(cap["env"]) == {"FLEET_CHAT_ID"}, f"env 里多了东西: {sorted(cap['env'])}"
