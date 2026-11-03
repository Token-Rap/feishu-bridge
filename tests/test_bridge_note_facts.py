"""Bridge guidance must reflect the available runtime capabilities."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# _BRIDGE_HOME 在 import 时从 sys.argv[1] 定死 (见模块 74 行), pytest 下那是测试文件路径
# → mkdir 炸。仓库其他测试都用这个手法, 照抄。
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-note-test-")]
try:
    from fleet_bridge import _build_bridge_note  # noqa: E402
finally:
    sys.argv = _argv

_OUTBOX = Path("/tmp/bot-home/media/outbox")


_CARD_CMD = "python3 /repo/task_card.py --home /tmp/bot-home"


def _note(*, app_only: bool, lessons: Path | None,
          task_card_cmd: str | None = _CARD_CMD, bot_name: str | None = "backend",
          fleet_state: Path | None = None, fleet_api: Path | None = None) -> str:
    return _build_bridge_note(app_only, _OUTBOX, lessons, task_card_cmd, bot_name,
                              fleet_state, fleet_api)


def test_app_only_note_never_mentions_feishu(tmp_path):
    """纯 App 模式下飞书端整个不存在 —— 提一句都是在教 bot 一件假事。"""
    note = _note(app_only=True, lessons=None)
    assert "你在App中运行" in note
    assert "飞书" not in note


def test_non_app_only_still_describes_feishu(tmp_path):
    """非纯 App 的实例仍是两侧都在 —— 别为了修今天的问题把另一种配置写坏。"""
    note = _note(app_only=False, lessons=None)
    assert "飞书聊天" in note
    assert "飞书端回文字" in note        # 提问卡在飞书侧确实是回文字


def test_report_delivery_guidance_is_present_in_both_claude_transports():
    for app_only in (True, False):
        note = _note(app_only=app_only, lessons=None)
        assert "关键结论、当前状态、阻塞" in note
        assert "Mac/Windows/UNC/file://" in note
        assert "不声称它已作为文件送达" in note
        assert "不绕过隐藏目录来源限制" in note
        assert "正常 http/https 网页链接" in note


def test_lessons_line_dropped_when_file_absent(tmp_path):
    """Windows 节点没有 ~/.fleet-ops/lessons.md —— 让它去读就是每次静默失败一次。"""
    note = _note(app_only=True, lessons=tmp_path / "nope" / "lessons.md")
    assert "lessons" not in note
    assert "踩坑库" not in note


def test_lessons_line_present_when_file_exists(tmp_path):
    f = tmp_path / "lessons.md"
    f.write_text("# 坑", encoding="utf-8")
    note = _note(app_only=True, lessons=f)
    assert str(f) in note and "参考当前环境提供的文档" in note


def test_unreadable_lessons_path_does_not_raise(tmp_path):
    """渲染提示词不该因为一个路径读不了就掀翻进程。"""
    class _Boom(type(tmp_path)):
        def is_file(self):
            raise OSError("permission denied")
    note = _note(app_only=True, lessons=_Boom(tmp_path / "x.md"))
    assert "踩坑库" not in note          # 读不出就当没有, 但不炸


def test_does_not_teach_tools_that_no_longer_exist(tmp_path):
    """Verify the task-card guidance does not recommend unavailable tools."""
    note = _note(app_only=True, lessons=None)
    assert "task_card.py" in note                    # 教现在真能用的那条路
    assert "TodoWrite 把计划" not in note             # 第一代, 早没了
    assert "用 TaskCreate 建条目" not in note
    assert "别再去 ToolSearch 找" in note              # 明说别找, 不是默默不提


def test_no_task_card_no_instructions(tmp_path):
    """Windows 节点的同步只覆盖 fleet_bridge.py —— 那边没有 task_card.py。
    教它跑一个不存在的脚本, 跟这个文件从前教 TodoWrite 是同一个错。"""
    note = _note(app_only=True, lessons=None, task_card_cmd=None)
    assert "task_card.py" not in note
    assert "任务清单" not in note


def test_command_carries_no_backslashes_and_no_guessed_interpreter():
    """Use the active interpreter and paths compatible with shell invocation."""
    import sys as _sys
    from fleet_bridge import _TASK_CARD_CMD  # noqa: PLC0415
    assert _TASK_CARD_CMD, "本机上这个命令该是有的 (task_card.py 就在仓根)"
    assert "\\" not in _TASK_CARD_CMD, f"命令里有反斜杠, Git Bash 会吞掉: {_TASK_CARD_CMD}"
    assert not _TASK_CARD_CMD.startswith("python3 "), "别用名字猜解释器"
    assert _TASK_CARD_CMD.startswith(Path(_sys.executable).as_posix()), _TASK_CARD_CMD


def test_task_card_command_is_per_instance(tmp_path):
    """--home 是每实例的, 跟 outbox 同理 —— 别写死成某一台的路径。"""
    note = _build_bridge_note(True, _OUTBOX, None,
                              "python3 /x/task_card.py --home /tmp/other-home")
    assert "--home /tmp/other-home" in note


def test_task_card_command_is_scoped_to_the_routed_chat():
    note = _note(app_only=True, lessons=None)
    assert "--chat <chat_id>" in note
    assert "会话路由里的真实值" in note
    assert "多会话时 `--chat` 必填" in note
    assert "群聊任务也必须维护这张卡" in note
    assert "派活/汇报不会自动更新它" in note


# Reminder guidance.

_API_DOC = Path.home() / ".fleet-ops" / "fleet_api.md"


def test_reminder_capability_is_announced():
    """The optional API guide is referenced when provided."""
    note = _note(app_only=True, lessons=None, fleet_api=Path("/tmp/fleet_api.md"))
    assert "可用接口" in note and "/tmp/fleet_api.md" in note


def test_reminder_syntax_survived_the_move():
    """**搬家最容易出的事是东西在半路掉了。** 这条验它真的到了对面。

    (文件不在就跳过 —— 远程节点/CI 上没有它, 那不算失败。)
    """
    if not _API_DOC.is_file():
        return
    doc = _API_DOC.read_text(encoding="utf-8")
    assert "/api/reminders" in doc and "X-Fleet-Token" in doc


def test_group_contract_is_marked_live():
    """群聊已上线后, 共用契约不能继续把整章标成未实现。"""
    if not _API_DOC.is_file():
        return
    doc = _API_DOC.read_text(encoding="utf-8")
    start = doc.index("## 群聊 (group chat)")
    section = doc[start:]
    assert "已上线" in doc[start:doc.find("\n## ", start + 1)]
    assert "状态: 群聊已上线" in section


def test_response_shape_is_stated():
    """Behavioral regression coverage."""
    if not _API_DOC.is_file():
        return
    assert '{"reminders"' in _API_DOC.read_text(encoding="utf-8")


def test_bot_name_is_filled_in_when_known():
    """Behavioral regression coverage."""
    note = _note(app_only=True, lessons=None, bot_name="backend",
                 fleet_api=Path("/tmp/fleet_api.md"))
    assert "`backend`" in note


def test_bot_name_falls_back_to_a_placeholder():
    """查不到就给占位符让模型自己填 —— **不猜**。填错名字只影响归属显示, 比编一个假名强。"""
    note = _note(app_only=True, lessons=None, bot_name=None,
                 fleet_api=Path("/tmp/fleet_api.md"))
    assert "<你自己的 bot 名>" in note and "`backend`" not in note


def test_outbox_path_is_per_instance(tmp_path):
    """outbox 是每实例算的 (Windows 上是 C:\\fleet\\...), 别写死成 Mac 路径。"""
    note = _build_bridge_note(True, Path("/tmp/other-home/media/outbox"), None)
    assert "/tmp/other-home/media/outbox" in note


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")


def test_reminders_and_artifacts_follow_environment_and_user_preferences():
    note = _note(app_only=True, lessons=None)
    assert "确认当前环境提供的工具、用户意愿和适用的保留规则" in note
    assert "~/.fleet-app/docs" not in note


# 提交署名由 harness 注入。

def test_co_authored_by_trailer_is_owned_by_the_harness():
    """模型名和邮箱由 harness 注入, 提示词不应再钉死一份可能过期的字面值。"""
    note = _note(app_only=True, lessons=None)
    assert "保留 harness 已注入的 `Co-Authored-By` trailer" in note
    assert "不要手改它的模型名或邮箱" in note


def test_co_authored_by_does_not_carry_the_bot_name():
    """harness 的署名不应再被 bot 名或第二条 Claude trailer 污染。"""
    note = _note(app_only=True, lessons=None, bot_name="backend")
    assert "Co-Authored-By: backend" not in note
    assert "Claude <backend" not in note
    assert "不要再额外添加另一条 Claude trailer" in note


def test_bot_trailer_coexists_rather_than_being_replaced():
    """Behavioral regression coverage."""
    note = _note(app_only=True, lessons=None)
    assert "Bot: <名字>" in note and "两行并存" in note


def test_signing_rule_does_not_license_extra_commits():
    """加署名 ≠ 鼓励多提交。"用户没要求就别主动 commit"那条不变 —— 这半句被精简掉的话,
    读到的 bot 会把"提交时怎么做"理解成"该去提交"。"""
    note = _note(app_only=True, lessons=None)
    assert "你自己执行 git commit 时" in note
    assert "用户没要求就别主动 commit" in note


# Task-card updates are as important as creation.

def test_updating_is_as_loud_as_creating():
    """Task-card guidance emphasizes updates as well as creation."""
    note = _note(app_only=True, lessons=None)
    assert "**先把计划列成任务清单再动手**" in note
    assert "**每完成一条当场划掉**" in note, "更新这件事必须和「先列」一样加粗"


def test_says_the_cost_of_batching_updates():
    """光说"要更新"没有力量 —— 得说清攒着不更新会发生什么。

    **比不列更糟**这个判断是有依据的: 不列, 用户知道自己没有进度可看; 列了不更新,
    他以为看到的是进度, 而那是假的。这跟这仓反复吃亏的形状同一个 ——
    **看起来正常的错误比明显的缺席更贵**。
    """
    note = _note(app_only=True, lessons=None)
    assert "比不列更糟" in note


def test_both_update_dialects_are_allowed():
    """Behavioral regression coverage."""
    note = _note(app_only=True, lessons=None)
    assert "done 2" in note and "重发整张 set" in note


def test_clear_is_not_a_progress_update():
    """Behavioral regression coverage."""
    note = _note(app_only=True, lessons=None)
    assert "别用 `clear` 代替标完成" in note










def test_teaches_the_key_syntax():
    note = _note(app_only=True, lessons=None)
    assert "[[置顶:key]]" in note and "顶替" in note


def test_warns_against_rotating_keys():
    """每期换一个新 key 会一期堆一条, 几周后挤掉用户手动置顶的 —— 而挤掉这件事
    他只在"我钉的那条不见了"时才发现。这半句是这条建议里唯一有代价的部分, 别被精简掉。"""
    assert "别每期换一个新 key" in _note(app_only=True, lessons=None)


def test_warns_about_key_forking_across_paths():
    """Behavioral regression coverage."""
    note = _note(app_only=True, lessons=None)
    assert "同一件事只用**一个** key" in note and "并排挂两份" in note








def test_points_at_the_state_file_when_present():
    note = _note(app_only=True, lessons=None, fleet_state=Path("/tmp/fs.md"))
    assert "/tmp/fs.md" in note and "舰队现状" in note


def test_keeps_the_ask_fleet_manager_half():
    """Behavioral regression coverage."""
    note = _note(app_only=True, lessons=None, fleet_state=Path("/tmp/fs.md"))
    assert "再向负责管理的人员确认" in note


def test_no_state_file_no_line():
    """Behavioral regression coverage."""
    assert "舰队现状" not in _note(app_only=True, lessons=None, fleet_state=None)


def test_windows_path_is_picked_by_existence_not_by_platform(tmp_path, monkeypatch):
    """Resolve the available configuration file without a platform branch."""
    import fleet_bridge as B
    home = tmp_path / "h"
    (home / ".fleet-ops").mkdir(parents=True)
    monkeypatch.setattr(B.Path, "home", staticmethod(lambda: home))
    assert B._fleet_ops_file("x.md") is None            # 两处都没有 → None
    (home / ".fleet-ops" / "x.md").write_text("hi", encoding="utf-8")
    assert B._fleet_ops_file("x.md") == home / ".fleet-ops" / "x.md"


def test_no_platform_branch_in_the_fallback():
    """不许出现平台判断 —— `Path("C:/fleet/…")` 在 Mac 上自然 is_file() 为假。"""
    import inspect

    import fleet_bridge as B
    src = inspect.getsource(B._fleet_ops_file)
    assert "C:/fleet/" in src
    assert "platform" not in src and "os.name" not in src


def test_every_fleet_ops_data_file_goes_through_the_fallback():
    """Behavioral regression coverage."""
    src = (Path(__file__).resolve().parent.parent / "fleet_bridge.py").read_text(encoding="utf-8")
    for name in ("fleet.json", "fleet_domains.json", "fleet_state.md", "fleet_api.md"):
        assert f'_fleet_ops_file("{name}")' in src, f"{name} 没走两路回落"
        # 且不许再有绕过它的直连写法
        assert f'".fleet-ops" / "{name}"' not in src, \
            f"{name} 还有一处直读 ~/.fleet-ops/ —— 那条路在 Windows 上恒失败"


# ── 命名与语言 ──────────────────────────────────


def test_ascii_identifiers_rule():
    note = _note(app_only=True, lessons=None)
    assert "会被机器读的标识符一律 ASCII" in note


def test_ascii_rule_excludes_ui_text():
    """⚠ 边界: UI 文案与数据值不在此列 —— 要求的是"被机器 switch 或拼进 URL 的东西",
    不是"用户看得见的字"。少了这半句, 下一个 bot 会把中文界面也一起改掉。"""
    assert "UI 文案与数据值除外" in _note(app_only=True, lessons=None)


def test_english_comments_rule_and_its_limits():
    """Behavioral regression coverage."""
    note = _note(app_only=True, lessons=None)
    assert "新写的代码注释与标识符用英文" in note
    assert "存量" in note and "commit message" in note


def test_the_prompt_itself_stays_chinese():
    """Check the stated scope of the language guidance."""
    assert "这份提示词本身照旧中文" in _note(app_only=True, lessons=None)








def test_criterion_is_contract_not_complexity():
    """Behavioral regression coverage."""
    note = _note(app_only=True, lessons=None)
    assert "要不要定一个双方都得遵守的契约" in note and "不是复杂度" in note


def test_alignment_is_bounded_and_produces_a_file():
    """Behavioral regression coverage."""
    note = _note(app_only=True, lessons=None)
    assert "限一次往返" in note and "写进文件的契约" in note


def test_receiver_can_escalate():
    """发起方通常低估别人那侧的改动量 —— 所以升级权不能只在发起方手里。"""
    assert "收方可以要求升级" in _note(app_only=True, lessons=None)


def test_retraction_goes_first():
    """Behavioral regression coverage."""
    note = _note(app_only=True, lessons=None)
    assert "作废我上一条" in note and "第一句" in note


def test_collaboration_language_follows_context():
    note = _note(app_only=True, lessons=None)
    assert "language appropriate to the participants and task" in note
    assert "must be written in English" not in note










def test_says_claude_md_is_a_per_turn_cost():
    """靶心: 说清它是**常驻成本**, 不是仓库文档。"""
    note = _note(app_only=True, lessons=None)
    assert "每一轮都整份进系统提示" in note and "12KB" in note


def test_says_what_belongs_by_how_fast_it_changes():
    """Behavioral regression coverage."""
    note = _note(app_only=True, lessons=None)
    assert "多久会变" in note and "一周内会变" in note


def test_points_somewhere_else_to_write():
    """只说"别写"没有出路 —— 会话产生的信息确实需要落盘, 得给它一个地方。"""
    note = _note(app_only=True, lessons=None)
    assert "NOTES.md" in note


def test_oversize_note_carries_the_real_numbers():
    """Behavioral regression coverage."""
    import fleet_bridge as fb
    s = fb._claude_md_cost_note(34227)
    assert "33KB" in s and "11409" in s, s


def test_no_note_when_within_limit():
    """**只在超限时出现。** 每轮都出现的提示会被当背景音 —— 而一个具体的
    "你每轮多花 11409 tokens" 不是背景音。"""
    import fleet_bridge as fb
    assert fb._claude_md_cost_note(6061) == ""
    assert fb._claude_md_cost_note(12 * 1024) == ""


def test_limit_is_a_parameter_not_a_magic_number():
    """上限可调 —— 12KB 是当下的判断, 不是物理常数。"""
    import fleet_bridge as fb
    assert fb._claude_md_cost_note(9000, limit=8 * 1024) != ""
