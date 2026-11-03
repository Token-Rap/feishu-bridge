"""DeepSeek 作为 Claude Code 的后端 (backend/T-deepseek-model)。

纯逻辑测 fleet_providers; fleet_bridge 的接线用源码形状钉住 (导入它要飞书/SDK 全套)。
⚠ 全程用假 key + 临时文件, 绝不碰 ~/.fleet-app/secrets 下的真 key。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from feishu_bridge import fleet_providers as P  # noqa: E402
from feishu_bridge.app_frontend import AppFrontend  # noqa: E402

SRC = (ROOT / "src/feishu_bridge/fleet_bridge.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for k in ("DEEPSEEK_API_KEY", "FLEET_DEFAULT_MODEL", "FLEET_CROSS_PROVIDER_RESUME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FLEET_DEEPSEEK_KEY_FILE", str(tmp_path / "no-such-key"))


def test_provider_classification():
    assert P.provider_for_model("deepseek-flash") == "deepseek"
    assert P.provider_for_model("deepseek-flash[1m]") == "deepseek"
    assert P.provider_for_model("gpt-6-astra") == "openai"
    assert P.provider_for_model("claude-opus-5") == "anthropic"
    assert P.provider_for_model("opus") == "anthropic"


def test_cli_model_adds_1m_only_for_deepseek_and_app_model_strips_it():
    assert P.cli_model("deepseek-flash") == "deepseek-flash[1m]"
    assert P.cli_model("deepseek-flash[1m]") == "deepseek-flash[1m]"
    assert P.cli_model("claude-opus-5") == "claude-opus-5"
    assert P.app_model("deepseek-flash[1m]") == "deepseek-flash"
    assert P.app_model("claude-opus-5") == "claude-opus-5"


def test_anthropic_env_is_empty_so_behaviour_is_unchanged():
    assert P.provider_env("claude-opus-5") == {}
    assert P.provider_env("opus") == {}


def test_deepseek_without_key_raises_not_crashes():
    with pytest.raises(P.ProviderNotConfigured):
        P.provider_env("deepseek-flash")
    assert P.deepseek_configured() is False


def test_deepseek_env_from_key_file(monkeypatch, tmp_path):
    f = tmp_path / "k"
    f.write_text("sk-fake\n", encoding="utf-8")
    monkeypatch.setenv("FLEET_DEEPSEEK_KEY_FILE", str(f))
    assert P.deepseek_configured() is True
    env = P.provider_env("deepseek-flash")
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-fake"
    assert env["ANTHROPIC_MODEL"] == "deepseek-flash[1m]"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "deepseek-flash"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "deepseek-flash"
    # Anthropic 订阅凭证不许跟着请求发到第三方
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "" and env["ANTHROPIC_API_KEY"] == ""


def test_env_var_key_wins_over_file(monkeypatch, tmp_path):
    f = tmp_path / "k"
    f.write_text("sk-file", encoding="utf-8")
    monkeypatch.setenv("FLEET_DEEPSEEK_KEY_FILE", str(f))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    assert P.provider_env("deepseek-v4-pro")["ANTHROPIC_AUTH_TOKEN"] == "sk-env"
    assert P.provider_env("deepseek-v4-pro")["ANTHROPIC_MODEL"] == "deepseek-v4-pro[1m]"


def test_empty_key_file_is_not_configured(monkeypatch, tmp_path):
    f = tmp_path / "k"
    f.write_text("", encoding="utf-8")
    monkeypatch.setenv("FLEET_DEEPSEEK_KEY_FILE", str(f))
    assert P.deepseek_configured() is False


def test_default_model(monkeypatch):
    for configured in (True, False, None):
        assert P.resolve_default_model(configured=configured) == "gpt-6.1-sol"
    monkeypatch.setenv("FLEET_DEFAULT_MODEL", "claude-opus-5")
    assert P.resolve_default_model(configured=True) == "claude-opus-5"


def test_cross_provider_resume_guard(monkeypatch, tmp_path):
    m = tmp_path / "session_providers.json"
    # 没登记过的老会话 = anthropic
    assert P.can_resume(m, "old-sid", "claude-opus-5") is True
    assert P.can_resume(m, "old-sid", "deepseek-flash") is False
    P.tag_session(m, "ds-sid", "deepseek")
    assert P.can_resume(m, "ds-sid", "deepseek-v4-pro") is True
    assert P.can_resume(m, "ds-sid", "claude-opus-5") is False
    monkeypatch.setenv("FLEET_CROSS_PROVIDER_RESUME", "1")
    assert P.can_resume(m, "ds-sid", "claude-opus-5") is True


def test_tag_session_survives_corrupt_file(tmp_path):
    m = tmp_path / "session_providers.json"
    m.write_text("{坏", encoding="utf-8")
    P.tag_session(m, "x", "deepseek")
    assert P.session_provider(m, "x") == "deepseek"


def test_caps_report_deepseek_only_with_key(monkeypatch, tmp_path):
    front = AppFrontend(tmp_path, log=lambda m: None)
    assert "deepseek" not in front.caps()
    f = tmp_path / "k"
    f.write_text("sk-fake", encoding="utf-8")
    monkeypatch.setenv("FLEET_DEEPSEEK_KEY_FILE", str(f))
    assert "deepseek" in front.caps()


# ── fleet_bridge 接线 (源码形状) ─────────────────────────────────────────────
def test_default_model_comes_from_provider_resolution():
    assert "DEFAULT_MODEL = _prov.resolve_default_model()" in SRC


def test_client_env_and_model_go_through_provider():
    seg = SRC[SRC.index("async def _get_client"):SRC.index("_ADVISOR_OFF =")]
    assert 'env={"FLEET_CHAT_ID": chat_id, **prov_env}' in seg
    assert "model=_prov.cli_model(model)" in seg
    assert "await client.set_model(_prov.cli_model(model))" in seg
    # 换 provider 不能活切 (环境变量只在起子进程时生效)
    assert "same_provider" in seg and "and same_provider" in seg
    assert "_prov.can_resume(_SESSION_PROVIDERS_FILE, resume_sid, model)" in seg
    # 不跨提供方续聊时发的 session_reset 带新模型的 App 侧 id
    assert '"model": _prov.app_model(model)' in seg


def test_new_sessions_are_tagged_with_provider():
    assert "_prov.tag_session(_SESSION_PROVIDERS_FILE, new_sid," in SRC


def test_set_runtime_model_denies_deepseek_without_key_and_flags_provider_switch():
    seg = SRC[SRC.index("def _set_runtime_model"):SRC.index("def _bjt_short")]
    assert "_prov.deepseek_configured()" in seg
    assert '"hint": "provider_switched"' in seg


def test_rollover_note_uses_provider_env():
    seg = SRC[SRC.index("async def _rollover_write_note"):][:2500]
    assert '**prov_env}' in seg and "_prov.can_resume(" in seg


# ── runtime.json 重置迁移 (两个租户) ─────────────────────────────────────────
def _mk_home(root, name, rt):
    h = root / name
    h.mkdir()
    if rt is not None:
        (h / "runtime.json").write_text(json.dumps(rt), encoding="utf-8")
    return h


def test_runtime_migration_covers_both_tenants_dry_run_then_apply(tmp_path):
    from scripts import migrate_runtime_deepseek as M
    a = _mk_home(tmp_path, "gzt-bot", {"model": "gpt-6.1-sol", "effort": "high"})
    b = _mk_home(tmp_path, "sample-user-bot", {"model": "claude-sonnet-5"})
    c = _mk_home(tmp_path, "ds-bot", {"model": "deepseek-v4-pro"})
    d = _mk_home(tmp_path, "plain", None)
    fj = tmp_path / "fleet.json"
    fj.write_text(json.dumps({"gzt-bot": {"home": str(a)}, "ds-bot": {"home": str(c)},
                              "plain": {"home": str(d)},
                              "win": {"home": "D:/x", "addr": "1.2.3.4:1"}}), encoding="utf-8")
    fd = tmp_path / "fleets"
    fd.mkdir()
    (fd / "sample-user.json").write_text(json.dumps({"sample-user-bot": {"home": str(b), "app_only": True}}),
                                 encoding="utf-8")
    owners = {(o, n) for o, n, _h, _a in M.registries(fj, fd)}
    assert ("gzt", "gzt-bot") in owners and ("sample-user", "sample-user-bot") in owners
    M.main(["--fleet-json", str(fj), "--fleets-dir", str(fd)])          # dry-run
    assert json.loads((a / "runtime.json").read_text())["model"] == "gpt-6.1-sol"
    assert not list(a.glob("runtime.json.bak-*"))
    M.main(["--fleet-json", str(fj), "--fleets-dir", str(fd), "--apply"])
    ra = json.loads((a / "runtime.json").read_text())
    assert "model" not in ra and ra["effort"] == "high", "只删 model, 别的覆盖保留"
    assert "model" not in json.loads((b / "runtime.json").read_text())
    assert json.loads((c / "runtime.json").read_text())["model"] == "deepseek-v4-pro"
    assert len(list(a.glob("runtime.json.bak-*"))) == 1
    assert not (d / "runtime.json").exists()


def test_runtime_migration_remote_only_when_home_not_on_this_machine(tmp_path):
    from scripts import migrate_runtime_deepseek as M
    here = _mk_home(tmp_path, "tcp-bot", {"model": "gpt-6.1-sol"})
    lan = {"localhost", "127.0.0.1", "::1", "192.0.2.16"}
    # 没 addr → 本机
    assert not M.is_remote(here, "", lan)
    # 带 addr 但 home 就在本机 (Windows 节点在自己机器上跑) → 本机
    assert not M.is_remote(here, "192.0.2.9:8792", lan)
    # home 不在本机, addr 是本机地址 → 本机
    assert not M.is_remote(tmp_path / "missing", "192.0.2.16:8792", lan)
    assert not M.is_remote(tmp_path / "missing", "[::1]:8792", lan)
    # home 不在本机, addr 是别的机器 → 远程
    assert M.is_remote(tmp_path / "missing", "192.0.2.9:8792", lan)
    # Mac 上的 Windows 路径不是绝对路径, 不能因为 cwd 里碰巧有同名目录就当本机
    assert M.is_remote(M._home("D:/fleet/x"), "192.0.2.9:8792", lan)


def test_runtime_migration_applies_to_local_addr_bots_and_counts_in_summary(tmp_path, capsys, monkeypatch):
    from scripts import migrate_runtime_deepseek as M
    monkeypatch.setattr(M, "_local_hosts", lambda: {"localhost", "127.0.0.1"})
    a = _mk_home(tmp_path, "win-local", {"model": "gpt-6.1-sol"})
    b = _mk_home(tmp_path, "win-local2", {"model": "claude-opus-5"})
    fj = tmp_path / "fleet.json"
    fj.write_text(json.dumps({"win-local": {"home": str(a), "addr": "192.0.2.9:8792"},
                              "win-local2": {"home": str(b), "addr": "127.0.0.1:8793"},
                              "far": {"home": "D:/nope", "addr": "192.0.2.8:8794"}}),
                  encoding="utf-8")
    fd = tmp_path / "fleets"
    fd.mkdir()
    M.main(["--fleet-json", str(fj), "--fleets-dir", str(fd)])
    out = capsys.readouterr().out
    assert "[dry-run] 将改 2 个" in out, out
    assert "far" in out and "远程节点" in out
    assert "win-local " in out and "远程节点 192.0.2.9" not in out
    M.main(["--fleet-json", str(fj), "--fleets-dir", str(fd), "--apply"])
    out = capsys.readouterr().out
    assert "已改 2 个" in out, out
    assert "model" not in json.loads((a / "runtime.json").read_text())
    assert "model" not in json.loads((b / "runtime.json").read_text())
