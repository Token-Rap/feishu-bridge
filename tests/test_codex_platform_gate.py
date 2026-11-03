'Tests for the cross-platform Codex capability gate.'
import app_frontend as AF
import fleet_bridge as FB


def test_windows_defaults_to_disabled():
    assert AF.codex_default_for("nt") is False, "Windows 上 Codex 必须默认关"


def test_posix_defaults_to_enabled():
    assert AF.codex_default_for("posix") is True, "Mac/Linux 不该被这道门误伤"


def test_explicit_env_still_wins_on_windows():
    """An explicit environment setting overrides the platform default."""
    assert AF.codex_enabled_from("0", "nt") is False
    assert AF.codex_enabled_from("1", "nt") is True
    assert AF.codex_enabled_from("", "posix") is True


def test_caps_and_runtime_gate_never_drift():
    """靶心: 两处独立读同一个 env, 一处关一处开 = 客户端看不到 GPT, 而 bot 照样能被打进 Codex。"""
    assert AF.CODEX_CAPABLE is FB.CODEX_ENABLED, (
        "caps 的开关和运行门漂开了 —— 客户端不显示 GPT, 但存量 runtime.json / 手工帧"
        "仍会把这个 bot 送进 _run_turn_codex, 而它在 Windows 上会静默卡死")
