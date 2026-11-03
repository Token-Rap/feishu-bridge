'测试并发更新任务卡时的冲突与重试。'
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import task_card

# ⚠ 桥启动时读 argv[1] 当 home —— 照本仓既有测试的写法给它一个临时目录,
# 否则 import 就 NotADirectoryError。
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-taskrace-home-")]
try:
    import fleet_bridge
finally:
    sys.argv = _argv


def _write(home, data):
    (home / "task_track.json").write_text(json.dumps(data, ensure_ascii=False),
                                          encoding="utf-8")


def _read(home):
    return json.loads((home / "task_track.json").read_text(encoding="utf-8"))


def test_save_one_keeps_other_chats_from_disk(tmp_path):
    """靶心: 我拿着陈旧底稿落盘, 也不能把别人**刚写进磁盘**的那格抹掉。"""
    _write(tmp_path, {"app": {"sid": "s1", "items": [{"content": "我的"}]}, "_v": 2})
    # 另一个进程在这中间写进了它自己那格
    cur = _read(tmp_path)
    cur["group:x"] = {"sid": "s2", "items": [{"content": "群的"}]}
    _write(tmp_path, cur)
    # 我只改我自己那格 (注意: 我手上的 track 里压根没有 group:x)
    task_card.save_one(tmp_path, "app", {"sid": "s1", "items": [{"content": "我的-新"}]})
    back = _read(tmp_path)
    assert back["app"]["items"][0]["content"] == "我的-新"
    assert back["group:x"]["items"][0]["content"] == "群的", \
        "另一个会话刚写的那格被盖掉了"


def test_save_one_can_delete_only_its_own_slot(tmp_path):
    _write(tmp_path, {"app": {"sid": "s1", "items": [{"content": "a"}]},
                      "group:x": {"sid": "s2", "items": [{"content": "b"}]}, "_v": 2})
    task_card.save_one(tmp_path, "app", None)
    back = _read(tmp_path)
    assert "app" not in back
    assert back["group:x"]["items"][0]["content"] == "b"


def test_bridge_and_script_share_one_implementation():
    """⚠ 两边各写一份合并/落盘逻辑迟早漂开, 而漂开的表现就是又丢一次条目。"""
    import ast
    import inspect
    src = inspect.getsource(fleet_bridge._save_task_track)
    # ⚠ **先剥掉 docstring 再查** —— 那段文档里引用了旧写法 `{**_TASK_TRACK, ...}`,
    # 直接 `in src` 会匹到自己的注释然后报"又写整份了"。
    # 扫描源码时排除散文，避免注释导致误报。
    import textwrap
    fn = ast.parse(textwrap.dedent(src)).body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]                      # 去掉 docstring
    code = ast.unparse(fn)
    assert "task_card.save_one" in code, "桥没走共用的 save_one —— 又有两份落盘逻辑了"
    assert "**_TASK_TRACK" not in code, "桥又在整份写回内存了"


def test_bridge_save_requires_a_chat():
    """没有 chat 参数 = 又回到"写整份"那个形状, 类型上就该挡住。"""
    import inspect
    sig = inspect.signature(fleet_bridge._save_task_track)
    assert "chat_id" in sig.parameters, "_save_task_track 丢了 chat_id 参数"
    assert sig.parameters["chat_id"].default is inspect.Parameter.empty, \
        "chat_id 有默认值 = 调用方可以省略 = 迟早又有人写整份"
