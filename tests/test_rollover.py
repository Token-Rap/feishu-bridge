"""Pure half of the nightly auto-rollover (fleet_rollover.py). Wiring tests: test_rollover_bridge.py."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fleet_rollover as ro  # noqa: E402


def _transcript(tmp_path, markers):
    f = tmp_path / "s.jsonl"
    lines = [json.dumps({"type": "user", "message": {"content": "hi"}})]
    for trig in markers:
        lines.append(json.dumps({"type": "system", "subtype": "compact_boundary",
                                 "compactMetadata": {"trigger": trig}}))
    lines.append(json.dumps({"type": "assistant", "message": {"content": "the word compact_boundary in text"}}))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def test_counts_every_trigger_and_ignores_the_word_in_text(tmp_path):
    assert ro.count_compacts(_transcript(tmp_path, ["manual", "auto", "manual"])) == 3


def test_missing_transcript_counts_zero(tmp_path):
    assert ro.count_compacts(tmp_path / "nope.jsonl") == 0


def test_settings_default_opt_out_and_override():
    assert ro.settings({}) == (True, 3)
    assert ro.settings({"auto_rollover": False}) == (False, 3)
    assert ro.settings({"auto_rollover_compacts": 5}) == (True, 5)
    assert ro.settings({"auto_rollover_compacts": "x"}) == (True, 3)
    assert ro.settings({"auto_rollover_compacts": 0}) == (True, 1)
    assert ro.settings(None) == (True, 3)


def test_window_is_local_2_to_6():
    def at(h):
        return time.mktime((2026, 9, 30, h, 30, 0, 0, 0, -1))
    assert [ro.in_window(at(h)) for h in (1, 2, 5, 6)] == [False, True, True, False]


def test_note_path_is_safe_per_session(tmp_path):
    assert ro.note_path(tmp_path, "group:fleet-product").name == "group_fleet-product.md"
    assert ro.note_path(tmp_path, "app").parent == tmp_path / "handoff"
    assert "/" not in ro.note_path(tmp_path, "../../x").name


def test_pending_survives_and_is_taken_once(tmp_path):
    p = ro.note_path(tmp_path, "app")
    ro.mark_pending(tmp_path, "app", p, 100.0, "abcdef123")
    ro.mark_pending(tmp_path, "group:x", p, 101.0, "zzz")
    got = ro.take_pending(tmp_path, "app")
    assert got["path"] == str(p) and got["prev"] == "abcdef123"
    assert ro.take_pending(tmp_path, "app") is None
    assert ro.take_pending(tmp_path, "group:x") is not None


def test_prompts_name_the_path_and_forbid_side_effects(tmp_path):
    p = ro.note_path(tmp_path, "app")
    hp = ro.handoff_prompt(p, 4)
    assert str(p) in hp and "4" in hp and "不回复用户" in hp and "不给别的 bot 递话" in hp
    assert str(p) in ro.first_turn_note(p, time.time())
