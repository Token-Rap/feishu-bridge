"""Behavioral regression coverage."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app_frontend import AppFrontend  # noqa: E402

# ⚠ fleet_bridge 必须在**模块级**导, 且临时换掉 argv: 在函数里 `import fleet_bridge` 时
# argv 是 pytest 的, 家目录解析会跑偏 (`tests/test_x.py/media`)。
# 今天已经在 test_group_chat_id 栽过同一次并写了注释, 这里又犯 ——
# **写在别处的教训救不了这里**, 所以两处都留一句。
_argv_bak = sys.argv
sys.argv = [_argv_bak[0], tempfile.mkdtemp(prefix="bridge-ask-")]
try:
    import fleet_bridge as FB  # noqa: E402
finally:
    sys.argv = _argv_bak


def _front() -> AppFrontend:
    return AppFrontend(Path(tempfile.mkdtemp(prefix="front-test-")), lambda *_a: None)


def test_ask_is_stashed_while_gateway_offline():
    f = _front()          # 没有 writers = 网关未连
    f.broadcast({"ev": "ask", "ask_id": "a1", "questions": [{"question": "选哪个?"}]})
    assert [e["ev"] for _ts, e in f._pending] == ["ask"]


def test_ask_cancel_also_stashed():
    f = _front()
    f.broadcast({"ev": "ask_cancel", "ask_id": "a1", "reason": "answered"})
    assert [e["ev"] for _ts, e in f._pending] == ["ask_cancel"]


def test_transient_events_still_dropped():
    """流式/工具这类高频瞬时事件不该攒着 (攒了也没意义, 还会撑爆队列)。"""
    f = _front()
    for ev in ({"ev": "stream", "text": "半句"}, {"ev": "tool", "name": "Bash"},
               {"ev": "todos", "items": []}, {"ev": "ctx", "used": 1}):
        f.broadcast(ev)
    assert f._pending == []


def test_compact_is_stashed_while_gateway_offline():
    """Behavioral regression coverage."""
    f = _front()
    f.broadcast({"ev": "compact", "phase": "start", "used": 120000, "max": 200000})
    f.broadcast({"ev": "compact", "phase": "done", "used": 36000, "max": 200000,
                 "before_used": 120000})
    assert [e["phase"] for _ts, e in f._pending] == ["start", "done"]


def test_stashed_ask_is_json_serializable():
    f = _front()
    f.broadcast({"ev": "ask", "ask_id": "a1",
                 "questions": [{"question": "q", "options": [{"label": "A"}]}]})
    json.dumps([e for _ts, e in f._pending])   # 补发时要能序列化, 不能抛


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")



def _ask_env():
    FB.ASK_PENDING.clear()
    FB.ASK_DISPLAY.clear()
    return FB


def test_answer_matches_group_display_ask_in_the_bot_session():
    """群里发的卡显示在 group, 但等待的 future 属于 bot 自己的 session。"""
    import asyncio
    B = _ask_env()

    async def run():
        loop = asyncio.get_running_loop()
        B.MAIN_LOOP = loop
        sent: list = []
        B.APP_FRONT.broadcast = sent.append
        fut = loop.create_future()
        B.ASK_PENDING["app"] = ("aa11bb22", fut)
        B.ASK_DISPLAY["aa11bb22"] = "group:fleet-product"
        ok = B._app_answer_ask("三个都建", "aa11bb22")
        await asyncio.sleep(0)
        return ok, fut, sent
    ok, fut, sent = asyncio.run(_wrap(run))
    assert ok is True, "群卡的作答没被认出来"
    assert fut.result() == "三个都建", "future 没被兑现"
    # ⚠ 回执也要落在群里 —— 同一个函数里的第二半, 修一处只修好一半
    assert sent and sent[0].get("chat_id") == "group:fleet-product", sent


def test_shared_answer_echo_keeps_actor_structured_and_text_clean():
    import asyncio
    B = _ask_env()

    async def run():
        loop = asyncio.get_running_loop()
        B.MAIN_LOOP = loop
        sent: list = []
        B.APP_FRONT.broadcast = sent.append
        fut = loop.create_future()
        B.ASK_PENDING["app"] = ("public-answer", fut)
        B.ASK_DISPLAY["public-answer"] = "group:life"
        tok_user = B.app_frontend.APP_USER.set("sample-user")
        tok_text = B.app_frontend.APP_USER_TEXT.set("选: A")
        try:
            ok = B._app_answer_ask("[sample-user] 选: A", "public-answer")
            await asyncio.sleep(0)
        finally:
            B.app_frontend.APP_USER_TEXT.reset(tok_text)
            B.app_frontend.APP_USER.reset(tok_user)
        return ok, sent

    ok, sent = asyncio.run(_wrap(run))
    assert ok is True
    assert sent == [{"ev": "user", "origin": "app", "text": "选: A",
                     "chat_id": "group:life", "user": "sample-user"}]


async def _wrap(fn):
    return await fn()


def test_answer_with_wrong_ask_id_is_not_consumed():
    """Behavioral regression coverage."""
    import asyncio
    B = _ask_env()

    async def run():
        fut = asyncio.get_running_loop().create_future()
        B.ASK_PENDING["group:x"] = ("real-id", fut)
        return B._app_answer_ask("乱答", "other-id")
    assert asyncio.run(_wrap(run)) is False
