"""Behavioral regression coverage."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

BRIDGE = str(Path(__file__).resolve().parents[2] / "src/feishu_bridge/bridge/fleet_bridge.py")
VENV = sys.executable

_PROBE = """
import sys, json
sys.argv = ['x', %r]
sys.path.insert(0, %r)
from feishu_bridge.bridge import fleet_bridge as b
print(json.dumps({"chats": list(b._TASK_TRACK), "v": b._TASK_TRACK_V}))
"""


def _load_with(home: Path, payload: dict) -> dict:
    (home / "task_track.json").write_text(json.dumps(payload), encoding="utf-8")
    out = subprocess.run([VENV, "-c", _PROBE % (str(home), str(Path(BRIDGE).parent))],
                         capture_output=True, text=True, timeout=60)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_v1_payload_is_discarded():
    home = Path(tempfile.mkdtemp(prefix="tt-v1-"))
    got = _load_with(home, {"chat-a": {"sid": "s", "items": [{"id": "1", "content": "旧"}]}})
    assert got["chats"] == []            # 没有 _v → 当 v1 丢弃


def test_v2_payload_is_kept():
    home = Path(tempfile.mkdtemp(prefix="tt-v2-"))
    got = _load_with(home, {"chat-a": {"sid": "s", "items": [{"id": "33", "content": "新"}]},
                            "_v": 2})
    assert got["chats"] == ["chat-a"]    # 版本相符 → 保留


def test_save_writes_version(tmp_path):
    """存盘一定带 _v, 否则下次启动会把自己写的数据当旧版丢掉。"""
    home = Path(tempfile.mkdtemp(prefix="tt-save-"))
    code = _PROBE % (str(home), str(Path(BRIDGE).parent))
    code += ("\nb._TASK_TRACK['c'] = {'sid': 's', 'items': []}\nb._save_task_track('c')\n"
             "print(json.dumps(json.loads((b._TASK_TRACK_FILE).read_text())))")
    out = subprocess.run([VENV, "-c", code], capture_output=True, text=True, timeout=60)
    saved = json.loads(out.stdout.strip().splitlines()[-1])
    assert saved.get("_v") == 2


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")
