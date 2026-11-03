"""每个 bot 可通过 runtime.json 配置 advisor；无效配置应记录并安全回退。"""
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("claude_default")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-advisor-")]
try:
    import fleet_bridge as fb
finally:
    sys.argv = _argv

_CLI_LINE = 'Error: The model "claude-bogus-9" cannot be used as an advisor.'


class _ProcessError(Exception):
    pass


def _stub(monkeypatch, runtime: dict, reject: set[str] = frozenset()):
    built: list[dict] = []
    logs: list[str] = []

    class _Opts:
        def __init__(self, **kw):
            self.kw = kw

    class _Client:
        def __init__(self, options=None):
            self.kw = options.kw
            built.append(self.kw)

        async def connect(self):
            adv = (self.kw.get("extra_args") or {}).get("advisor")
            if adv in reject:
                cb = self.kw.get("stderr")
                if cb:
                    cb(f'Error: The model "{adv}" cannot be used as an advisor.')
                raise _ProcessError("Command failed with exit code 1\nError output: Check stderr output for details")

        async def disconnect(self):
            pass

        async def set_model(self, m):
            self.kw["model"] = m

    monkeypatch.setattr(fb, "ClaudeAgentOptions", _Opts)
    monkeypatch.setattr(fb, "ClaudeSDKClient", _Client)
    monkeypatch.setattr(fb, "_thinking_opt", lambda: {})
    monkeypatch.setattr(fb, "_load_session", lambda c: None)
    monkeypatch.setattr(fb, "_read_runtime", lambda: dict(runtime))
    monkeypatch.setattr(fb, "log", lambda s, *a, **k: logs.append(s))
    monkeypatch.setattr(fb, "CLIENTS", {})
    monkeypatch.setattr(fb, "CLIENT_SIG", {})
    monkeypatch.setattr(fb, "CLIENT_ADVISOR", {})
    monkeypatch.setattr(fb, "ADVISOR_REJECTED", {})
    monkeypatch.setattr(fb, "FORK_PENDING", set())
    monkeypatch.setattr(fb, "IDLE_WATCH", {})
    return built, logs


def test_no_advisor_key_means_no_advisor_arg(monkeypatch):
    built, _ = _stub(monkeypatch, {})
    asyncio.run(fb._get_client("c"))
    assert "advisor" not in (built[-1].get("extra_args") or {})
    assert built[-1].get("stderr") is None, "没开 advisor 时不许改默认的 stderr 处理"


def test_advisor_is_passed_to_the_cli(monkeypatch):
    built, logs = _stub(monkeypatch, {"advisor": "claude-fable-5-1"})
    asyncio.run(fb._get_client("c"))
    assert built[-1]["extra_args"]["advisor"] == "claude-fable-5-1"
    assert fb.CLIENT_ADVISOR["c"] == "claude-fable-5-1"
    assert any("advisor=claude-fable-5-1" in s for s in logs), logs


@pytest.mark.parametrize("off", ["off", "none", "null", "", "  "])
def test_off_words_disable_it(monkeypatch, off):
    built, _ = _stub(monkeypatch, {"advisor": off})
    asyncio.run(fb._get_client("c"))
    assert "advisor" not in (built[-1].get("extra_args") or {})


def test_rejected_advisor_falls_back_and_logs_the_clis_own_reason(monkeypatch):
    """Behavioral regression coverage."""
    built, logs = _stub(monkeypatch, {"advisor": "claude-bogus-9"}, reject={"claude-bogus-9"})
    client = asyncio.run(fb._get_client("c"))
    assert client is not None and fb.CLIENTS["c"] is client
    assert "advisor" not in (built[-1].get("extra_args") or {}), "回落后的 client 不该再带 advisor"
    assert "cannot be used as an advisor" in fb.ADVISOR_REJECTED["claude-bogus-9"]
    assert any("cannot be used as an advisor" in s and "claude-bogus-9" in s for s in logs), logs


def test_a_rejected_advisor_is_not_retried_every_turn(monkeypatch):
    built, _ = _stub(monkeypatch, {"advisor": "claude-bogus-9"}, reject={"claude-bogus-9"})
    asyncio.run(fb._get_client("c"))
    n = len(built)
    fb.CLIENTS.clear(); fb.CLIENT_SIG.clear()
    asyncio.run(fb._get_client("c"))
    assert len(built) == n + 1, "被拒过的 advisor 又试了一次"


def test_turning_it_off_clears_the_rejection_memory(monkeypatch):
    """set-advisor off 再 on = 显式重试的办法 (比如开了 extra usage 之后)。"""
    _stub(monkeypatch, {"advisor": "off"})
    fb.ADVISOR_REJECTED["claude-fable-5-1"] = "credits"
    asyncio.run(fb._get_client("c"))
    assert fb.ADVISOR_REJECTED == {}


def test_connect_failure_without_advisor_still_raises(monkeypatch):
    """回落只为 advisor 存在; 别的启动失败照旧往上抛, 不许被这层吞掉。"""
    built, _ = _stub(monkeypatch, {})

    async def boom(self):
        raise _ProcessError("other")
    monkeypatch.setattr(fb.ClaudeSDKClient, "connect", boom)
    with pytest.raises(_ProcessError):
        asyncio.run(fb._get_client("c"))


def test_changing_the_advisor_rebuilds_instead_of_live_set_model(monkeypatch):
    built, _ = _stub(monkeypatch, {"advisor": "off"})
    asyncio.run(fb._get_client("c"))
    monkeypatch.setattr(fb, "_read_runtime", lambda: {"advisor": "claude-fable-5-1"})
    asyncio.run(fb._get_client("c"))
    assert len(built) == 2 and built[-1]["extra_args"]["advisor"] == "claude-fable-5-1"


@pytest.mark.parametrize("err,expect", [
    ("API Error: 400 The configured advisor model is not compatible", True),
    ("Advisor calls bill to usage credits, which are off", True),
    ("usage credits exhausted", True),
    ("API Error: Connection error", False),
])
def test_mid_turn_advisor_errors_are_recognised_only_when_an_advisor_is_on(monkeypatch, err, expect):
    monkeypatch.setattr(fb, "CLIENT_ADVISOR", {"c": "claude-fable-5-1"})
    assert fb._advisor_turn_error("c", err) is expect
    monkeypatch.setattr(fb, "CLIENT_ADVISOR", {})
    assert fb._advisor_turn_error("c", err) is False


def test_turn_error_branch_swaps_the_client_before_the_continuation():
    """回合出错那段: advisor 错要算进自动续跑, 且续跑前换成不带 advisor 的 client。"""
    src = fb.Path(fb.__file__).read_text(encoding="utf-8")
    i = src.index("⚠ 本轮判定为出错")
    seg = src[i:src.index('await client.query("重试")', i)]
    assert "_advisor_turn_error(" in seg and "_drop_client(chat_id)" in seg and "_get_client(chat_id)" in seg
