"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-group-")]
try:
    from feishu_bridge import app_frontend as AF
    # ⚠ **fleet_bridge 也在这儿导入**, 不在函数里 `import`:
    # 函数里导入时 argv 是 pytest 的, 家目录解析会跑偏 ⇒ 这几条**单独跑红、全量跑绿**
    # (全量时别的测试模块已经用正确 argv 导入过了)。
    # **顺序依赖的绿比红更麻烦** —— 它只在有人单独跑这个文件时才现形。
    from feishu_bridge import fleet_bridge as FB
finally:
    sys.argv = _argv


def _front():
    f = AF.AppFrontend.__new__(AF.AppFrontend)
    f._writers = []
    f._loop = None
    f._pending = []
    f._gateway_writers = set()
    f._peer_report = None
    f.chat_id = "app"
    f._chat_validator = None
    return f


def test_broadcast_stamps_private_chat_by_default():
    f = _front()
    sent = []
    f._stash = sent.append
    ev = {"ev": "reply", "text": "hi"}
    out = f._with_chat(ev)
    assert out["chat_id"] == "app"
    assert "chat_id" not in ev, "不许就地改调用方的字典"


def test_broadcast_stamps_current_display_group():
    f = _front()
    tok = AF.DISPLAY_CHAT.set("group:fleet-product")
    try:
        assert f._with_chat({"ev": "reply"})["chat_id"] == "group:fleet-product"
    finally:
        AF.DISPLAY_CHAT.reset(tok)


def test_explicit_chat_id_wins():
    """已经带了就别盖掉 —— 补发路径会带上原来那条的会话。"""
    f = _front()
    assert f._with_chat({"ev": "reply", "chat_id": "group:x"})["chat_id"] == "group:x"


def test_unvalidated_chat_id_falls_back_to_private():
    """⚠ **没有校验器时一律退回私聊**, 不许放行。

    客户端送来的 chat_id 不校验 = 任何端可以在任何 bot 上开任意会话键
    ⇒ CLIENTS 无上限增长, 且跨域串台。**默认必须是安全的那一侧。**
    """
    f = _front()
    assert f.resolve_chat("group:随便编的") == "app"


def test_validator_is_consulted_and_can_allow():
    f = _front()
    f._chat_validator = lambda c: (c == "group:fleet-product", "not_member")
    assert f.resolve_chat("group:fleet-product") == "group:fleet-product"
    assert f.resolve_chat("group:别的域") == "app"


def test_empty_or_missing_chat_id_is_private():
    f = _front()
    f._chat_validator = lambda c: (True, "")
    assert f.resolve_chat(None) == "app"
    assert f.resolve_chat("") == "app"









def _handle_frame_src() -> str:
    src = Path(AF.__file__).read_text(encoding="utf-8")
    return src[src.index("async def _handle_frame"):src.index("async def _on_conn")]


def _checked_work_spawns(source: str) -> list[str]:
    """Conservative dominance: direct earlier resolution in this or an outer block.

    A conditional/loop/try body's resolver never leaks into a sibling or a later
    outer path. Nested definitions are not handler execution. This deliberately
    refuses routing it cannot prove; it does not search a file or a byte window.
    """
    import ast
    import textwrap

    def self_call(node, method):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == method and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self")

    def resolution(statement):
        if not isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr)):
            return False
        call = statement.value
        if not any(self_call(call, m) for m in
                   ("resolve_chat", "resolve_chat_ex", "resolve_work_display_ex")):
            return False
        if len(call.args) != 1 or call.keywords:
            return False
        arg = call.args[0]
        return (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
                and isinstance(arg.func.value, ast.Name) and arg.func.value.id == "req"
                and arg.func.attr == "get" and len(arg.args) == 1 and not arg.keywords
                and isinstance(arg.args[0], ast.Constant) and arg.args[0].value == "chat_id")

    def header_nodes(node):
        # Visit evaluated headers/expressions, never a nested statement body here.
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.stmt):
                yield child
                yield from header_nodes(child)

    checked = []

    def block(statements, resolved=False):
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                assert not any(self_call(n, "_spawn") for n in ast.walk(statement)), \
                    "nested work spawn requires explicit control-flow proof"
                continue
            for call in header_nodes(statement):
                if self_call(call, "_spawn"):
                    assert resolved, f"这条帧没先解会话: {ast.unparse(call)!r}"
                    checked.append(ast.unparse(call))
            # Mark only AFTER inspecting evaluation: a later resolver cannot count.
            resolved = resolved or resolution(statement)
            if isinstance(statement, (ast.If, ast.For, ast.AsyncFor, ast.While)):
                block(statement.body, resolved); block(statement.orelse, resolved)
            elif isinstance(statement, (ast.Try, ast.TryStar)):
                block(statement.body, resolved); block(statement.orelse, resolved)
                block(statement.finalbody, resolved)
                for handler in statement.handlers:
                    block(handler.body, resolved)
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                block(statement.body, resolved)
            elif isinstance(statement, ast.Match):
                for case in statement.cases:
                    block(case.body, resolved)
            elif isinstance(statement, (ast.Return, ast.Raise)):
                break

    handlers = [n for n in ast.parse(textwrap.dedent(source)).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_frame"]
    assert len(handlers) == 1, "必须检查唯一实际帧处理器"
    block(handlers[0].body)
    return checked


def test_every_frame_that_does_work_resolves_the_chat():
    """All real work spawns require preceding scoped resolution."""
    checked = _checked_work_spawns(_handle_frame_src())
    assert len(checked) >= 5, f"只扫到 {len(checked)} 条干活分支, 这不是通过是没验"


def test_routing_checker_accepts_resolution_in_dominating_outer_block():
    checked = _checked_work_spawns("""
        async def _handle_frame(self, req, writer):
            if req.get("type") == "user_message":
                work, display, why = self.resolve_work_display_ex(req.get("chat_id"))
                try:
                    if self._inject:
                        self._spawn(self._inject("SYNTHETIC", "test"))
                finally:
                    pass
    """)
    assert len(checked) == 1


def test_routing_checker_rejects_missing_resolution():
    import pytest
    with pytest.raises(AssertionError, match="没先解会话"):
        _checked_work_spawns("""
            async def _handle_frame(self, req, writer):
                if self._inject:
                    self._spawn(self._inject("SYNTHETIC", "test"))
        """)


def test_routing_checker_rejects_resolution_after_spawn():
    import pytest
    with pytest.raises(AssertionError, match="没先解会话"):
        _checked_work_spawns("""
            async def _handle_frame(self, req, writer):
                self._spawn(self._inject("SYNTHETIC", "test"))
                self.resolve_work_display_ex(req.get("chat_id"))
        """)


def test_routing_checker_rejects_resolution_only_in_sibling_branch():
    import pytest
    with pytest.raises(AssertionError, match="没先解会话"):
        _checked_work_spawns("""
            async def _handle_frame(self, req, writer):
                if req.get("type") == "other":
                    self.resolve_work_display_ex(req.get("chat_id"))
                else:
                    self._spawn(self._inject("SYNTHETIC", "test"))
        """)


def test_routing_checker_rejects_resolution_elsewhere_in_file():
    import pytest
    with pytest.raises(AssertionError, match="没先解会话"):
        _checked_work_spawns("""
            def unrelated(self, req):
                self.resolve_work_display_ex(req.get("chat_id"))
            async def _handle_frame(self, req, writer):
                self._spawn(self._inject("SYNTHETIC", "test"))
        """)


def test_injected_message_with_group_chat_id_runs_in_bot_session_but_displays_in_group():
    """群聊只是展示面: turn/context/compact 进 bot 自己, 事件显示在 group。"""
    import asyncio
    f = _front()
    f._chat_validator = lambda c: (c == "group:fleet-product", "not_member")
    seen = {}

    async def _fake(text, client):
        seen["chat"] = AF.CUR_CHAT.get()
        seen["display"] = AF.DISPLAY_CHAT.get()
        seen["text"] = text

    f._inject = _fake
    f._bg = set()

    async def run():
        await f._handle_frame({"type": "user_message", "text": "[group:fleet-product #T001] 干活",
                               "chat_id": "group:fleet-product", "client": "bot"}, None)
        await asyncio.sleep(0)
    asyncio.run(run())
    assert seen.get("chat") == "app", seen
    assert seen.get("display") == "group:fleet-product", seen


def test_internal_group_task_flag_reaches_the_injected_turn():
    """意图来自 session_inject 的机器字段，不能靠正文前缀猜。"""
    import asyncio
    f = _front()
    f._chat_validator = lambda c: (c == "group:fleet-product", "not_member")
    seen = {}

    async def _fake(_text, _client):
        seen["internal"] = AF.INTERNAL_GROUP_TASK.get()

    f._inject = _fake
    f._bg = set()

    async def run():
        await f._handle_frame({"type": "user_message", "text": "内部 brief",
                               "chat_id": "group:fleet-product",
                               "internal_group_task": True}, None)
        await asyncio.sleep(0)
    asyncio.run(run())
    assert seen == {"internal": True}


def test_injected_message_without_chat_id_stays_private():
    """今天的行为 (递话脚本不带 chat_id) —— 保留, 灰度期两种脚本并存不许炸。"""
    import asyncio
    f = _front()
    f._chat_validator = lambda c: (True, "")
    seen = {}

    async def _fake(text, client):
        seen["chat"] = AF.CUR_CHAT.get()

    f._inject = _fake
    f._bg = set()

    async def run():
        await f._handle_frame({"type": "user_message", "text": "老脚本递的话"}, None)
        await asyncio.sleep(0)
    asyncio.run(run())
    assert seen.get("chat") == "app", seen


def test_user_image_carries_the_group_into_the_turn():
    """Behavioral regression coverage."""
    import asyncio
    f = _front()
    f._chat_validator = lambda c: (c == "group:fleet-product", "not_member")
    f._inject_image = None
    seen = {}

    async def _fake(paths, caption):
        seen["chat"] = AF.CUR_CHAT.get()
        seen["display"] = AF.DISPLAY_CHAT.get()
        seen["paths"] = paths

    f._inject_image = _fake
    f._bg = set()

    async def run():
        await f._handle_frame({"type": "user_image", "paths": ["/tmp/a.png"],
                               "chat_id": "group:fleet-product"}, None)
        await asyncio.sleep(0)      # 让派出去的任务跑一轮
    asyncio.run(run())
    assert seen.get("chat") == "app", seen
    assert seen.get("display") == "group:fleet-product", seen


def test_user_image_with_bogus_chat_id_still_falls_back_to_private():
    """校验不过 ⇒ 退回私聊, 不是放行 (同 resolve_chat 的默认拒绝)。"""
    import asyncio
    f = _front()
    f._chat_validator = lambda c: (False, "not_member")
    seen = {}

    async def _fake(paths, caption):
        seen["chat"] = AF.CUR_CHAT.get()
        seen["display"] = AF.DISPLAY_CHAT.get()

    f._inject_image = _fake
    f._bg = set()

    async def run():
        await f._handle_frame({"type": "user_image", "paths": ["/tmp/a.png"],
                               "chat_id": "group:随便编的"}, None)
        await asyncio.sleep(0)
    asyncio.run(run())
    assert seen.get("chat") == "app", seen
    assert seen.get("display") == "app", seen


# 域校验覆盖含顶层元数据和 domains 的文件结构。

def _fake_home(tmp_path, mapping):
    """造一个假 HOME, 里面按**真文件形状**放 fleet_domains.json (顶层有元数据 + domains)。"""
    import json
    ops = tmp_path / ".fleet-ops"
    ops.mkdir(parents=True, exist_ok=True)
    (ops / "fleet_domains.json").write_text(
        json.dumps({"_v": 1, "_note": "元数据也在顶层", "domains": mapping}),
        encoding="utf-8")
    return tmp_path


def _ok(tmp_path, chat, mapping, bot="backend"):
    B = FB
    home = _fake_home(tmp_path, mapping)
    old_name, old_home = B._BOT_NAME, B.Path.home
    B._BOT_NAME = bot
    B.Path.home = staticmethod(lambda: home)
    try:
        return B._group_chat_ok(chat)[0]      # 只要放行与否; 原因码由下面几条单独钉
    finally:
        B._BOT_NAME, B.Path.home = old_name, old_home


_PROD = {"fleet-product": {"label": "FleetApp 产品",
                           "bots": ["sample-mobile", "sample-web", "backend"], "lead": "backend"}}


def test_validator_reads_domains_wrapper_not_top_level(tmp_path):
    assert _ok(tmp_path, "group:fleet-product", _PROD) is True


def test_validator_rejects_domain_without_lead(tmp_path):
    """没有 lead 的域不接群任务 (协议 groups/README.md) —— 缺 lead 就不是群。"""
    assert _ok(tmp_path, "group:career",
               {"career": {"label": "x", "bots": ["backend"]}}) is False


def test_validator_rejects_domain_i_am_not_in(tmp_path):
    assert _ok(tmp_path, "group:finance",
               {"finance": {"label": "x", "bots": ["fund-ops"], "lead": "fund-ops"}}) is False


def test_validator_rejects_when_bot_name_unknown(tmp_path):
    """⚠ `_BOT_NAME` 解不出来时**整个群聊禁用**, 不是"放行"。

    它是从 fleet.json 按 home 反查的, 查不到就是 None ——
    **不知道自己是谁的时候, 默认必须是拒绝那一侧。**
    """
    assert _ok(tmp_path, "group:fleet-product", _PROD, bot=None) is False


def test_validator_rejects_unreadable_domains_file(tmp_path):
    """读不到 = 不放行 (而不是"读不到就当没限制")。"""
    B = FB
    old_name, old_home = B._BOT_NAME, B.Path.home
    B._BOT_NAME = "backend"
    B.Path.home = staticmethod(lambda: tmp_path / "根本不存在")
    try:
        assert B._group_chat_ok("group:fleet-product")[0] is False
    finally:
        B._BOT_NAME, B.Path.home = old_name, old_home


def test_unreadable_domains_is_told_apart_from_no_such_domain(tmp_path):
    'An unreadable domain registry must be distinguished from a missing domain.'
    B = FB
    old_name, old_home = B._BOT_NAME, B.Path.home
    B._BOT_NAME = "backend"
    try:
        B.Path.home = staticmethod(lambda: tmp_path / "根本不存在")
        blind = B._group_chat_ok("group:fleet-product")
        B.Path.home = staticmethod(lambda: _fake_home(tmp_path, _PROD))
        missing = B._group_chat_ok("group:压根没有这个域")
    finally:
        B._BOT_NAME, B.Path.home = old_name, old_home
    assert blind == (False, "domains_unreadable")
    assert missing == (False, "no_such_domain")
    assert blind[1] != missing[1], "两者的原因码一样 = 又回到分不出的状态"


def test_not_member_and_no_lead_have_their_own_reasons(tmp_path):
    """其余几条也各有原因码 —— 派活方据此分得出「我的锅」还是「那台桥坏了」。"""
    B = FB
    old_name, old_home = B._BOT_NAME, B.Path.home
    try:
        B.Path.home = staticmethod(lambda: _fake_home(tmp_path, _PROD))
        B._BOT_NAME = "sample-project"
        assert B._group_chat_ok("group:fleet-product") == (False, "not_member")
        B.Path.home = staticmethod(
            lambda: _fake_home(tmp_path, {"无主域": {"bots": ["backend"]}}))
        B._BOT_NAME = "backend"
        assert B._group_chat_ok("group:无主域") == (False, "no_lead")
    finally:
        B._BOT_NAME, B.Path.home = old_name, old_home


def test_public_group_without_lead_allows_only_its_bots(tmp_path):
    """公共群故意没有默认 bot；不能把“没 lead”继续等同于“不是群”。"""
    B = FB
    old_name, old_home = B._BOT_NAME, B.Path.home
    domains = {"生活": {"public": True, "users": ["gzt", "sample-user"],
                         "bots": ["home-agent", "food-finder"]}}
    try:
        B.Path.home = staticmethod(lambda: _fake_home(tmp_path, domains))
        B._BOT_NAME = "food-finder"
        assert B._group_chat_ok("group:生活") == (True, "")
        B._BOT_NAME = "backend"
        assert B._group_chat_ok("group:生活") == (False, "not_member")
    finally:
        B._BOT_NAME, B.Path.home = old_name, old_home
