"""Tests for 飞书 bot 舰队管理 CLI (scripts/fleet_admin.py).

纯 stdlib 逻辑, 无 lark/sdk 依赖, 随主测试套件跑. 覆盖:
  - runtime.json 读写 + 还原默认 (set/clear)
  - 订阅用量聚合 (合成 transcript jsonl → token/成本分桶 + mtime/窗口过滤)
  - 价格匹配 / fleet 解析 / target 选择
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from feishu_bridge import fleet_admin as adm  # noqa: E402


# ── runtime.json 读写 ────────────────────────────────────────────────
def test_write_then_read_runtime(tmp_path):
    adm.write_runtime(tmp_path, model="sonnet", effort="high")
    rt = adm.read_runtime(tmp_path)
    assert rt["model"] == "sonnet"
    assert rt["effort"] == "high"
    assert "updated_ts" in rt


def test_write_runtime_merges_not_overwrites(tmp_path):
    adm.write_runtime(tmp_path, model="opus")
    adm.write_runtime(tmp_path, effort="low")   # 只动 effort, model 应留存
    rt = adm.read_runtime(tmp_path)
    assert rt["model"] == "opus" and rt["effort"] == "low"


def test_write_runtime_clear_restores_default(tmp_path):
    adm.write_runtime(tmp_path, model="haiku", effort="max")
    adm.write_runtime(tmp_path, model="default")   # 清 model
    rt = adm.read_runtime(tmp_path)
    assert "model" not in rt and rt["effort"] == "max"


def test_read_runtime_missing_is_empty(tmp_path):
    assert adm.read_runtime(tmp_path) == {}


# ── 价格匹配 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("model,idx,expected", [
    ("claude-opus-4-8", 0, 5.0),
    ("claude-opus-5", 0, 5.0),
    ("claude-fable-5", 0, 10.0),
    ("claude-sonnet-4-6", 0, 3.0),
    ("claude-haiku-4-5-20251001", 1, 5.0),
    ("totally-unknown", 0, 10.0),         # 未知 → 按最贵档 fable (保守)
])
def test_price_matching(model, idx, expected):
    assert adm._price(model)[idx] == expected


# ── 订阅用量聚合 ──────────────────────────────────────────────────────
def _assistant_line(ts: str, model: str, inp: int, out: int, cr: int = 0, cw: int = 0) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "message": {"model": model, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}},
    })


def test_aggregate_usage_sums_and_costs(tmp_path):
    proj = tmp_path / "proj-a"
    proj.mkdir()
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    yest = (now - timedelta(days=1)).date().isoformat()
    lines = [
        _assistant_line(now.isoformat(), "claude-opus-4-8", 1_000_000, 0),          # in $5
        _assistant_line(now.isoformat(), "claude-sonnet-4-6", 0, 1_000_000),         # out $15
        _assistant_line((now - timedelta(days=1)).isoformat(), "claude-opus-4-8", 0, 1_000_000),  # out $25
        # 非 assistant / 无 usage → 忽略
        json.dumps({"type": "user", "message": {"content": "hi"}}),
    ]
    (proj / "s.jsonl").write_text("\n".join(lines), encoding="utf-8")

    agg = adm.aggregate_usage(tmp_path, days=7)
    t = agg["total"]
    assert t["input"] == 1_000_000
    assert t["output"] == 2_000_000
    # 5 (opus in) + 15 (sonnet out) + 25 (opus out) = 45
    assert t["cost"] == pytest.approx(45.0, rel=1e-6)
    assert agg["by_day"][today]["cost"] == pytest.approx(20.0, rel=1e-6)
    assert agg["by_day"][yest]["cost"] == pytest.approx(25.0, rel=1e-6)


def test_aggregate_usage_excludes_outside_window(tmp_path):
    proj = tmp_path / "proj-b"
    proj.mkdir()
    old = datetime.now(timezone.utc) - timedelta(days=40)
    f = proj / "old.jsonl"
    f.write_text(_assistant_line(old.isoformat(), "claude-opus-4-8", 5_000_000, 0), encoding="utf-8")
    # mtime 也设成 40 天前 → 应被 mtime 粗筛跳过
    import os
    os.utime(f, (old.timestamp(), old.timestamp()))
    agg = adm.aggregate_usage(tmp_path, days=7)
    assert agg["total"]["input"] == 0
    assert agg["files_scanned"] == 0


def test_aggregate_usage_empty_projects_dir(tmp_path):
    agg = adm.aggregate_usage(tmp_path / "nope", days=7)
    assert agg["total"]["input"] == 0 and agg["files_scanned"] == 0


# ── fleet 解析 / target ───────────────────────────────────────────────
def test_load_fleet_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    fj = tmp_path / "fleet.json"
    fj.write_text(json.dumps({
        "qt": {"home": "~/.fleet-claude-bridge", "repo": "/r/qt"},
        "mgr": {"home": "~/.sample-manager-bridge", "repo": "/r/mgr", "self": True},
    }), encoding="utf-8")
    fleet = adm.load_fleet(fj)
    assert fleet["qt"]["home"] == tmp_path / ".fleet-claude-bridge"
    assert fleet["qt"]["self"] is False
    assert fleet["mgr"]["self"] is True


def test_targets_all_vs_name_vs_unknown():
    fleet = {"a": {"self": False}, "b": {"self": False}}
    assert len(adm._targets(fleet, "all")) == 2
    assert adm._targets(fleet, "a") == [("a", fleet["a"])]
    with pytest.raises(SystemExit):
        adm._targets(fleet, "zzz")


def test_set_effort_rejects_bad_level(tmp_path):
    fj = tmp_path / "fleet.json"
    fj.write_text(json.dumps({"a": {"home": str(tmp_path / "a"), "repo": "/r"}}), encoding="utf-8")
    args = adm.build_parser().parse_args(["--fleet", str(fj), "set-effort", "a", "turbo"])
    with pytest.raises(SystemExit):
        args.func(args)


def test_set_model_writes_runtime(tmp_path):
    home = tmp_path / "a"
    fj = tmp_path / "fleet.json"
    fj.write_text(json.dumps({"a": {"home": str(home), "repo": "/r"}}), encoding="utf-8")
    args = adm.build_parser().parse_args(["--fleet", str(fj), "set-model", "a", "sonnet"])
    args.func(args)
    assert adm.read_runtime(home)["model"] == "sonnet"


def test_set_effort_accepts_uppercase(tmp_path):
    home = tmp_path / "a"
    fj = tmp_path / "fleet.json"
    fj.write_text(json.dumps({"a": {"home": str(home), "repo": "/r"}}), encoding="utf-8")
    args = adm.build_parser().parse_args(["--fleet", str(fj), "set-effort", "a", "HIGH"])
    args.func(args)
    assert adm.read_runtime(home)["effort"] == "high"   # 规整成小写


def test_load_fleet_bad_entry_raises(tmp_path):
    fj = tmp_path / "fleet.json"
    fj.write_text(json.dumps({"bad": {"repo": "/r"}}), encoding="utf-8")   # 缺 home
    with pytest.raises(SystemExit):
        adm.load_fleet(fj)


def test_write_runtime_clear_with_none_and_empty(tmp_path):
    adm.write_runtime(tmp_path, model="opus", effort="high")
    adm.write_runtime(tmp_path, model=None, effort="")    # None / 空都还原默认
    rt = adm.read_runtime(tmp_path)
    assert "model" not in rt and "effort" not in rt


def test_read_ratelimit_roundtrip(tmp_path):
    (tmp_path / "ratelimit.json").write_text(
        json.dumps({"status": "allowed", "rate_limit_type": "five_hour"}), encoding="utf-8")
    assert adm.read_ratelimit(tmp_path)["status"] == "allowed"
    assert adm.read_ratelimit(tmp_path / "nope") is None


def test_fmt_ratelimit_renders_status_type_util():
    s = adm._fmt_ratelimit({"status": "allowed_warning", "rate_limit_type": "five_hour",
                            "resets_at": 1781014800, "utilization": 85})
    assert "🟡" in s and "five_hour" in s and "85%" in s and "BJT" in s
    assert "🔴" in adm._fmt_ratelimit({"status": "rejected"})








def _fleet_file(tmp_path, *, remote: bool = True) -> Path:
    entries = {"local-bot": {"home": str(tmp_path / "local-home")}}
    if remote:
        entries["sample-agent"] = {"home": "C:/fleet/sample-agent-home",
                              "addr": "192.0.2.16:8792"}
    f = tmp_path / "fleet.json"
    f.write_text(json.dumps(entries), encoding="utf-8")
    return f


def test_load_fleet_keeps_addr(tmp_path):
    """addr 是「这个 bot 不在本机」的唯一判据 —— 丢了它下游就无从区分。"""
    fleet = adm.load_fleet(_fleet_file(tmp_path))
    assert fleet["sample-agent"]["addr"] == "192.0.2.16:8792"
    assert fleet["local-bot"]["addr"] == ""


def test_is_remote_helper(tmp_path):
    fleet = adm.load_fleet(_fleet_file(tmp_path))
    assert adm.is_remote(fleet["sample-agent"]) is True
    assert adm.is_remote(fleet["local-bot"]) is False


def test_set_model_on_remote_refuses_and_creates_nothing(tmp_path, capsys, monkeypatch):
    """点名改远程 bot: 必须**明确失败**, 而不是造假目录后打勾。"""
    monkeypatch.chdir(tmp_path)          # 假目录会落在 cwd —— 就地验证它没被造出来
    args = type("A", (), {"fleet": _fleet_file(tmp_path), "target": "sample-agent",
                          "model": "opus"})()
    with pytest.raises(SystemExit):
        adm.cmd_set_model(args)
    out = capsys.readouterr().out
    assert "远程节点" in out and "192.0.2.16" in out
    assert not (tmp_path / "C:").exists()        # 关键: 一个字节都不该落盘
    assert "✅" not in out


def test_set_model_all_does_local_and_reports_skipped(tmp_path, capsys, monkeypatch):
    """all: 本机的照常改, 远程的**列出来说明没改** —— 沉默等于假装全做了。"""
    monkeypatch.chdir(tmp_path)
    args = type("A", (), {"fleet": _fleet_file(tmp_path), "target": "all",
                          "model": "opus"})()
    adm.cmd_set_model(args)
    out = capsys.readouterr().out
    assert "✅ local-bot" in out                  # 本机的真的改了
    assert adm.read_runtime(tmp_path / "local-home")["model"] == "opus"
    assert "sample-agent" in out and "远程节点" in out  # 远程的明说没改
    assert not (tmp_path / "C:").exists()


def test_set_effort_on_remote_also_refuses(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = type("A", (), {"fleet": _fleet_file(tmp_path), "target": "sample-agent",
                          "effort": "high"})()
    with pytest.raises(SystemExit):
        adm.cmd_set_effort(args)
    assert not (tmp_path / "C:").exists()


def test_list_marks_remote_instead_of_showing_stopped(tmp_path, capsys):
    """list 对远程节点原来显示 [停止] model=(默认) —— 那是**一个确定的假事实**,
    比空白更坏: 读的人会以为「它停了、用默认配置」。"""
    adm.cmd_list(type("A", (), {"fleet": _fleet_file(tmp_path)})())
    out = capsys.readouterr().out
    line = [ln for ln in out.splitlines() if "sample-agent" in ln][0]
    assert "远程节点" in line
    assert "停止" not in line and "(默认)" not in line


def test_context_marks_remote_instead_of_no_snapshot(tmp_path, capsys):
    """context 原来说「暂无快照 — 下一轮对话后可见」, 那是在暗示「它只是还没说话」。"""
    adm.cmd_context(type("A", (), {"fleet": _fleet_file(tmp_path)})())
    line = [ln for ln in capsys.readouterr().out.splitlines() if "sample-agent" in ln][0]
    assert "远程节点" in line and "暂无快照" not in line








def _home_with_beat(tmp_path, age_s: float | None):
    home = tmp_path / "h"
    home.mkdir(exist_ok=True)
    if age_s is not None:
        (home / "heartbeat").write_text(str(int(time.time() - age_s)), encoding="utf-8")
    return home


def test_running_when_heartbeat_fresh(tmp_path):
    assert adm._is_running(_home_with_beat(tmp_path, 5)) is True


def test_stopped_when_heartbeat_stale(tmp_path):
    """Behavioral regression coverage."""
    assert adm._is_running(_home_with_beat(tmp_path, 400)) is False


def test_stopped_when_heartbeat_missing(tmp_path):
    assert adm._is_running(_home_with_beat(tmp_path, None)) is False


def test_corrupt_heartbeat_does_not_raise(tmp_path):
    home = _home_with_beat(tmp_path, 5)
    (home / "heartbeat").write_text("不是数字", encoding="utf-8")
    assert adm._is_running(home) is False


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")



def _one_bot(tmp_path):
    home = tmp_path / "a"
    fj = tmp_path / "fleet.json"
    fj.write_text(json.dumps({"a": {"home": str(home), "repo": "/r"}}), encoding="utf-8")
    return home, fj


def test_set_advisor_writes_and_off_clears(tmp_path, capsys):
    home, fj = _one_bot(tmp_path)
    args = adm.build_parser().parse_args(["--fleet", str(fj), "set-advisor", "a", "claude-fable-5-1"])
    args.func(args)
    assert adm.read_runtime(home)["advisor"] == "claude-fable-5-1"
    assert "advisor → claude-fable-5-1" in capsys.readouterr().out
    args = adm.build_parser().parse_args(["--fleet", str(fj), "set-advisor", "a", "off"])
    args.func(args)
    assert "advisor" not in adm.read_runtime(home)


def test_set_advisor_keeps_model_and_effort(tmp_path):
    home, fj = _one_bot(tmp_path)
    adm.write_runtime(home, model="opus", effort="high")
    args = adm.build_parser().parse_args(["--fleet", str(fj), "set-advisor", "a", "claude-fable-5-1"])
    args.func(args)
    rt = adm.read_runtime(home)
    assert rt["model"] == "opus" and rt["effort"] == "high"


def test_set_advisor_rejects_junk(tmp_path):
    _, fj = _one_bot(tmp_path)
    for bad in ("fable; rm -rf /", "a b", "x" * 80):
        args = adm.build_parser().parse_args(["--fleet", str(fj), "set-advisor", "a", bad])
        with pytest.raises(SystemExit):
            args.func(args)


def test_config_shows_the_advisor(tmp_path, capsys):
    home, fj = _one_bot(tmp_path)
    adm.write_runtime(home, advisor="claude-fable-5-1")
    args = adm.build_parser().parse_args(["--fleet", str(fj), "config"])
    args.func(args)
    assert "advisor=claude-fable-5-1" in capsys.readouterr().out




def test_imports_and_writes_without_fcntl(tmp_path):
    import subprocess
    code = (
        "import sys, types\n"
        "sys.modules['fcntl'] = None\n"                              # ImportError, like Windows
        "calls = []\n"
        "sys.modules['msvcrt'] = types.SimpleNamespace(LK_LOCK=1, locking=lambda fd, m, n: calls.append(m))\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from feishu_bridge import fleet_admin as adm\n"
        f"rt = adm.write_runtime({str(tmp_path)!r}, model='claude-opus-5-5')\n"
        "assert rt['model'] == 'claude-opus-5-5' and calls == [1], (rt, calls)\n"
        "print('ok')\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr


def test_windows_node_treats_its_own_bots_as_local(tmp_path):
    home = tmp_path / "sample-home"
    home.mkdir()
    e = {"home": home, "addr": "192.0.2.16:8801", "node": "win"}
    assert adm.is_remote(e, windows=True) is False
    assert adm.is_remote(e, windows=False) is True, "Mac 上永远是远程 (Windows 路径在 POSIX 下是相对路径)"


def test_windows_node_still_refuses_what_is_not_here(tmp_path):
    assert adm.is_remote({"home": tmp_path / "missing", "addr": "x:1", "node": "win"}, windows=True) is True
    assert adm.is_remote({"home": tmp_path, "addr": "x:1", "node": ""}, windows=True) is True


def test_load_fleet_keeps_node(tmp_path):
    f = tmp_path / "fleet.json"
    f.write_text(json.dumps({"p": {"home": "D:/fleet/p-home", "addr": "a:1", "node": "win"}}), encoding="utf-8")
    assert adm.load_fleet(f)["p"]["node"] == "win"


def test_windows_node_treats_mac_bots_as_remote(tmp_path):
    """Behavioral regression coverage."""
    mac_bot = {"home": tmp_path / "does-not-matter", "addr": "", "node": ""}
    assert adm.is_remote(mac_bot, windows=True) is True
    assert adm.is_remote(mac_bot, windows=False) is False


def test_remote_note_for_a_mac_bot_names_no_empty_addr():
    note = adm._remote_note("backend", {"addr": "", "node": ""})
    assert "()" not in note and "Mac" in note
