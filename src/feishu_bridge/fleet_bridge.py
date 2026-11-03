#!/usr/bin/env python3
'Feishu bridge for managing agent conversations and client events.'
from __future__ import annotations

import os
import sys
from feishu_bridge.fleet_managed_bootstrap import require_startup_binding as _managed_startup_binding
_managed_startup_binding(dict(os.environ), sys.argv)

import asyncio
import contextlib
import contextvars
try:
    import fcntl                      # Unix 文件锁
except ImportError:                   # Windows 节点无 fcntl → msvcrt 等价实现
    fcntl = None
    import msvcrt
import itertools
from feishu_bridge import codex_approvals
from feishu_bridge import codex_questions
from feishu_bridge import question_defaults_producer
from feishu_bridge import question_defaults_intents
from feishu_bridge import peer_reports
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from feishu_bridge.fleet_risk import classify_risk  # noqa: E402  (同目录纯逻辑模块)

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (  # noqa: E501
    P2CardActionTrigger, P2CardActionTriggerResponse)

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

# 环境变量双轨读取 (改名迁移): 新名 FLEET_BRIDGE_* 优先, 回落旧名。
# ⚠ **这一行必须在下面 _BRIDGE_HOME 之前** —— 那是导入期第一个用到 _env 的地方,

from feishu_bridge.fleet_env import env as _env  # noqa: E402
from feishu_bridge.fleet_common import (  # noqa: E402  (T022: leaf helpers; log stays local for import-time AST guard)
    ctx_tokens as _ctx_tokens, mu_total as _mu_total, chunks as _chunks)
from feishu_bridge.fleet_failures import (  # noqa: E402  (T022: failure/retry wording)
    _NET_ERR_KEYS, _AUTH_ERR_KEYS, _TLS_ERR_KEYS, _RESULT_FIELDS_MAX,
    _retry_summary, _retry_user_note, looks_like_api_error,
    _error_suffix, _failure_meta, _result_fields_line, _failure_tip,
    turn_failed)

# ── 配置 ──────────────────────────────────────────────────────────
# 多实例: FLEET_BRIDGE_HOME (旧名 FEISHU_BRIDGE_HOME 仍可用) 指向配置目录
# (各自 .env + media + app 凭证 + repo), 同一份代码/venv 可跑多个 bot。
_BRIDGE_HOME = Path(
    (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "")
    or _env("HOME")
    or str(Path.home() / ".fleet-claude-bridge")
)
load_dotenv(_BRIDGE_HOME / ".env")

# 用同一环境变量解析器读取 CLI 目录。
if _claude_dir := _env("CLAUDE_DIR"):
    # Windows 服务以 SYSTEM 跑时 PATH 没有用户目录 → 从 .env 补 claude CLI 所在目录
    os.environ["PATH"] = _claude_dir + os.pathsep + os.environ.get("PATH", "")

_MEDIA_DIR = _BRIDGE_HOME / "media"     # 下载的图片/语音落盘处
_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
_OUTBOX_DIR = _MEDIA_DIR / "outbox"     # 待发图片暂存 — 发送成功后即删
_OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
_MEDIA_KEEP_DAYS = 7                    # media/ 只保留最近 N 天, 启动时清理

# 舰队管理: 管理器 bot (fleet_admin.py) 跟本 bot 走文件 IPC ——
#   runtime.json: 管理器写 model/effort 覆盖, 本 bot 每轮读 (变更则重建 client 生效)
#   status.json:  本 bot 每轮写上下文 token/轮数/成本, 管理器读
_RUNTIME_FILE = _BRIDGE_HOME / "runtime.json"
_STATUS_FILE = _BRIDGE_HOME / "status.json"
_RATELIMIT_FILE = _BRIDGE_HOME / "ratelimit.json"   # 真·订阅额度 (RateLimitEvent 落盘)

APP_ONLY = _env("APP_ONLY").strip() == "1"   # 纯 App 模式: 无飞书前端 (多用户租户的 bot)
APP_ID = _env("APP_ID").strip()
APP_SECRET = _env("APP_SECRET").strip()
DOMAIN = _env("DOMAIN", "feishu").strip() or "feishu"
ALLOWED_OPEN_ID = _env("OPEN_ID").strip()
REPO = _env("REPO", str(Path.cwd())).strip()
OAUTH_TOKEN = os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip()



# 为同步副本 (改飞书转换逻辑两边都要同步)。硬 import — 缺了直接 ImportError 启动即暴露,
# 不再静默回落 identity (那会让 ##/表格 变裸文本却不报错).
from feishu_bridge.fleet_markdown import md_to_feishu  # noqa: E402  (同目录, parent 已在 line 37 入 path)
from feishu_bridge.transport import Transport  # noqa: E402  (前端抽象接口, FeishuTransport 是其飞书实现)
from feishu_bridge import app_frontend  # noqa: E402  (群聊要读 CUR_CHAT 这个 contextvar)
from feishu_bridge import fleet_codex_storage as _codex_storage
from feishu_bridge import task_card    # noqa: E402  (共用 merged(): 卡上的序号两边必须是同一套)
from feishu_bridge.app_frontend import AppFrontend, TeeTransport, tool_brief  # noqa: E402  (Web 前端: socket 事件源 + 回复双写)



# `_load_claude_md()` 的 except 分支里调 log(), 而它原本定义在更后面 —— 那一刻 log 不存在,
# 于是**降级路径自己抛 NameError**, 把「读不到就空着继续跑」变成硬崩溃, launchd 疯狂重试
# 全撞在同一行。触发源是 iCloud 在磁盘紧张时驱逐本地文件, read_text 抛 OSError(11)。
# 编码设置又必须排在 log 之前: log 打中文/emoji, Windows 上 stdout 默认 GBK 会直接崩。
# 顺序 = 编码 → log → 其它。tests/test_import_time_safety.py 用 AST 钉住, 别挪回去。

# Windows 控制台/重定向默认 GBK, emoji 直接 UnicodeEncodeError 崩进程 → 强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001  (非常规流对象没有 reconfigure, 忽略)
    pass


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)

def _load_claude_md() -> str:
    """读仓库 CLAUDE.md, 注入 system prompt 给 bot 仓库上下文.

    走 system_prompt append 而非 setting_sources — 后者会连带载入 permissions
    allowlist 绕过 PreToolUse 审批门. 这样既有上下文又不破坏护栏.
    """
    md = Path(REPO) / "CLAUDE.md"
    if not md.exists():
        return ""                       # 该仓没有项目说明, 正常
    try:
        return md.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        # 静默返回 "" = 这个 bot 这一轮起**没有项目指令**了, 但它照样能流利地答话,
        # 只是不再遵守本仓的任何约定 —— 最像"正常"的一种坏
        log(f"⚠ CLAUDE.md 存在但读不出 ({exc}) —— 本进程将**不带项目指令**运行")
        return ""


_CLAUDE_MD = _load_claude_md()



# 仓库标签从当前路径计算，避免提示词引用错误的仓库。
_REPO_NAME = Path(REPO).name or REPO

# 注入 system prompt 的桥接能力说明 (所有 bot 通用): 发图/发视频约定
#
# 提示词中的环境事实应尽量从运行时计算；无法自动推导的工具名称须随环境升级核对。
def _safe_is_file(p: Path) -> bool:
    """路径不可访问也算"没有" —— 提示词渲染不该因为一次 OSError 就掀翻进程。"""
    try:
        return p.is_file()
    except OSError:
        return False


_NOTE_LESSONS = Path.home() / ".fleet-ops" / "lessons.md"
# 查找可选的舰队状态和接口说明文件。
def _fleet_ops_file(name: str) -> Path | None:
    """`~/.fleet-ops/<name>` 或 `C:/fleet/<name>`, **取存在的那个**; 都没有 → None。

    不写平台分支 —— `Path("C:/fleet/…")` 在 Mac 上只是个不存在的相对路径, `is_file()`
    自然为假; Windows 上 `Path.home()/".fleet-ops"` 不存在。**让"哪台机器"由文件在不在回答。**

    所有使用这些文件的调用方应共用回落逻辑；文件仍须由部署流程提供。
    """
    for q in (Path.home() / ".fleet-ops" / name, Path(f"C:/fleet/{name}")):
        if _safe_is_file(q):
            return q
    return None


_NOTE_FLEET_STATE = _fleet_ops_file("fleet_state.md")
# 接口速查: 提醒/表格/灰度进度的 curl 语法从提示词正文搬到这里 —— 一周用几次的东西
# 不该每回合每 bot 读一遍 (_BRIDGE_NOTE 每回合都进上下文)。
_NOTE_FLEET_API = _fleet_ops_file("fleet_api.md")


_REPLY_DELIVERY_NOTE = (
    "回复交付：关键结论、当前状态、阻塞和待批准的具体范围必须完整写在消息正文中，"
    "不要只让用户点击报告链接才能知道。Mac/Windows/UNC/file:// 本机路径只作可复制的引用，"
    "不写成普通可点击 Markdown 链接，也不声称它已作为文件送达。需要实际文件时，"
    "遵守现有附件入库、鉴权下载与文档查看契约；不把账号 token 放进 URL，"
    "不绕过隐藏目录来源限制或自动复制私有报告出去。正常 http/https 网页链接及已经支持的"
    "文档、file_id 附件和分享入口保持可用。"
)

_CODEX_QUESTION_NOTE = (
    "Fleet Codex 提问卡：选择类问题用 request_user_input_async。每次只能有 1–3 题，"
    "每题必须提供完整、非空的 title 和 2–8 个非空、互不重复的字符串 options；"
    "不得省略 options，也不要把开放式问题交给此工具。开放式问题用普通文字询问。"
    "需要更多题时，先等当前问题的真实回答，再提后续独立问题。保留完整范围与风险说明，"
    "不替用户选择，不自动重发或复活已取消的问题；提交或卡片显示不等于已收到回答。"
)


def _build_bridge_note(app_only: bool, outbox: Path, lessons: Path | None,
                       task_card_cmd: str | None = None,
                       bot_name: str | None = None,
                       fleet_state: Path | None = None,
                       fleet_api: Path | None = None) -> str:
    """渲染桥接能力说明；可选资源不可用时省略相应提示。"""
    # 纯 App 模式下飞书端整个不存在; 非纯 App 的实例仍是"飞书+App"两侧都在
    where = "App" if app_only else "飞书聊天 (同时镜像到 App)"
    ask_ui = "App 端点按钮" if app_only else "App 端点按钮, 飞书端回文字"
    try:
        has_lessons = lessons is not None and lessons.is_file()
    except OSError:      # 路径不可访问也算没有 —— 提示词渲染不该因此掀翻进程
        has_lessons = False
    return (
    _REPLY_DELIVERY_NOTE + "\n\n"
    + f"桥接能力: 你在{where}中运行, 回复里单独一行写 [[图片: /绝对路径]] 即可把该本地图片"
    "作为图片消息发给用户 (标记会从文字回复中剥离, 图片随后送达; 支持 png/jpg/gif/webp, ≤10MB)。"
    "同理单独一行 [[视频: /绝对路径]] 可发本地 mp4 视频 (≤30MB)。"
    "单独一行 [[文件: /绝对路径]] 可把 xlsx/docx/pdf 等文档发给用户 (≤50MB): 用户在 App 里点下载, "
    "也能生成分享链接发给别人, **保留 7 天**。文件放在你的仓目录下最稳 (Mac 上 /tmp 也行); "
    "`.` 开头的目录一律不许发, Mac 上 tempfile 默认的 /var/folders 也不行。"
    # A stable pin key replaces the previous version of recurring content.
    "回复里单独一行写 [[置顶]] 可把这条回复 Pin 到会话顶部 (用户回头能在 Pin 列表里找到); "
    "只在用户明确要求置顶、或内容确实值得长期留存时用。"
    "**每天/每期刷新的同一份东西** (今日清单、当周计划) 写 [[置顶:key]] —— 同 key 的新的"
    "**顶替**旧的, 不同 key 共存。别每期换一个新 key: 那会一期堆一条, 几周后把用户手动"
    "置顶的挤掉, 而他只会在「我钉的那条不见了」时才发现。"
    
    # 标记置顶的是"我这条回复", 前提是**正好在回复**; 而脚本产出的东西 (每日清单、
    # 晨报) 根本不在对话轮次里, 走的是 /notify 或 外部通知工具。
    # 两条路都对、各管一段 —— 但**同一件事用了两个 key 就会并排挂两份, 互不顶替**,
    # 而那个"两份"看起来完全正常, 只有用户会觉得"怎么有两个今日清单"。
    "⚠ 同一件事只用**一个** key: 其他通知方式若也置顶同一份东西, "
    "两边的 key 必须一模一样 —— 不一样就会并排挂两份且互不顶替。"
    f"临时生成/下载的图片视频请放 {outbox}/ 目录, 发送成功后会自动删除, 不占存储。"
    # 远程节点 (Windows) 上没有这个文件 —— 那边读一次失败一次, 同样是静默的
    + (f"如需排查外部集成，可参考当前环境提供的文档 {lessons}。"
       if has_lessons else "")
    + "向用户提**选择类**问题 (二选一/多选/要不要) 时, 必须用 AskUserQuestion 工具而不是"
    f"在文本里问 —— 用户会收到可点选的卡片 ({ask_ui}), 你会真正等到答案再继续; "
    "开放式问题 (答案不是有限选项) 才用文本问。"
    + (("接到**需要多步**的活 (3 步以上 / 涉及多文件 / 有先后阶段) 时, **先把计划列成"
        "任务清单再动手**: `" + task_card_cmd + " --chat <chat_id> set \"读代码|in_progress\" \"改代码\" "
        "\"跑测试\"`；把 `<chat_id>` 换成上方会话路由里的真实值。多会话时 `--chat` 必填, "
        "只有确认这个 bot 只有一个会话时才可省略。清单实时显示给用户 (App 上是任务卡), 让他看得见你要做什么、"
        "进行中的**群聊任务也必须维护这张卡**；`groups/*/tasks.jsonl` 的派活/汇报不会自动更新它，"
        "完成群任务后要在当前路由对应的 task_card 条目里同步 `done`。"
        # `_stale_card_note()` 在卡片过期时提醒，避免每轮重复提示。
        "**每完成一条当场划掉** (`done 2` / `start 3`, 或重发整张 set 把状态写进条目) —— "
        "攒到最后划 = 用户全程盯着一张冻结的卡, **比不列更糟**。"
        "**别用 `clear` 代替标完成**。简单的一两步活别列。"
        # 不给 task_card 加「等谁」字段 —— 每个 bot 都要
        # 维护它, 而**没维护的那些看起来跟维护了的一样**(又是同一个形状)。
        # 用现成的状态表达: 等别人 = 不是我在做 = pending。提醒只看 in_progress,
        # 于是"合理地闲置"自动不打扰, 不需要第二个信号。
        "**在等别人的条目标 `pending` 并把「等谁」写进条目文字**"
        "(例:「等其他成员完成对照实验」) —— `in_progress` 只留给**你自己正在做**的。"
        "⚠ **别再去 ToolSearch 找 TaskCreate / TaskUpdate / TaskList** —— 当前 "
        "工具可用性取决于当前环境；任务清单可用上面的 "
        "task_card.py。")
       if task_card_cmd else "")
    + ("如需设置提醒或保存长期产物，先确认当前环境提供的工具、用户意愿和适用的保留规则。")
    # Treat fleet state as a snapshot and verify missing or stale information.
    + (f"不确定舰队现状时 (有哪些 bot / 谁管什么 / 谁在哪台机器), **先读 {fleet_state}** "
       "—— 内容可能是生成时的"
       "快照。**那里没有的、或你有理由怀疑它过期的, 再向负责管理的人员确认。**"
       if fleet_state else "")
# Include the bot name when describing the shared API.
    + (f"需要了解可用接口时, 查看 {fleet_api} ("
       f"里面 `bot` 字段填 `{bot_name or '<你自己的 bot 名>'}`)。"
       if fleet_api else "")
    
    
    # Coordinate shared contracts before independent implementation.
    + ("跟别的 bot 协作分两种, **判据是「要不要定一个双方都得遵守的契约」, 不是复杂度** "
       "(复杂度各判各的, 判错那次最贵): ① 一方实现、别人只读结果 → 直接做, 递话只用来"
       "要事实; ② 两端以上同时改, 或要定字段名/协议/职责边界 → **先对齐再动手**, 且"
       "**限一次往返、产出一份写进文件的契约**, 之后各自实现不再互相问。"
       "发起方先判是哪种; **收方可以要求升级为 ②** —— 它更清楚自己那侧要改多少。"
       "⚠ 递话改规格时**第一句就写「作废我上一条」** —— 你的消息可能正躺在对方队列里, "
       "而它已经按旧规格动手了。"
       "Use the language appropriate to the participants and task.")
    # 会话路由: work session 与 display chat 故意分开, 所以不能
    # 让模型从 FLEET_CHAT_ID 猜是不是群聊。每条消息的 `_stamp` 都会附一行机器字段。
    # 这里说明字段的**行为含义**, 字段本身逐消息提供, 两者缺一不可。
    + ("每条用户消息开头都有一行 `[会话路由 {...}]`: `is_group` 是是否群聊, `chat_id` "
       "是本条回复的展示会话, `group` 是群域名 (私聊为空)。**普通回复无需选路由**, 桥会自动"
       "回到该 `chat_id`; 使用已配置的跨会话工具时按它决定: "
       "`is_group=true` 且这件事应留在当前群里就带 `--group <group>`, 私聊则不带。")
    # Keep machine-readable identifiers portable across clients.
    + ("**会被机器读的标识符一律 ASCII**: URL、JSON 键、枚举值、表名、文件名 "
       "(UI 文案与数据值除外 —— 那是给人看的)。**新写的代码注释与标识符用英文**, 存量"
       "不追、改到哪顺手改哪; commit message、markdown 文档、这份提示词本身照旧中文。"
       )
    
    
    # Keep per-turn instructions stable and concise.
    + ("**本仓 CLAUDE.md 每一轮都整份进系统提示** (约 1 token/3 字符) —— 它是常驻成本, "
       "不是仓库文档。**只放身份/命令/约定, 控制在 12KB 以内**; 判据是**多久会变**: "
       "一周内会变的 (进度/现状/复盘/记录) 不进, 能进的是几个月不变的 "
       "(身份与边界/命令/不变量/协作规矩)。会话里产生的重要信息写进"
       "本仓 `NOTES.md` 或别的文件 (要用时再读), 别往 CLAUDE.md 里堆。"
       "(会话可能重置, 文件不会 —— 但\"落进文件\"不等于\"落进 CLAUDE.md\"。)")
    + ("提交署名: **你自己执行 git commit 时**, 保留 harness 已注入的 `Co-Authored-By` trailer；"
       "不要手改它的模型名或邮箱, 也不要再额外添加另一条 Claude trailer。"
       "若本仓另有 `Bot: <名字>` trailer (管「哪个 bot 干的」, 跟这条管「Claude 参与了」"
       "是两件事), 那条照旧, **两行并存**; trailer 之间不空行, 都在 message 末尾。"
       "⚠ 这条只改「提交时怎么署名」, **不改「用户没要求就别主动 commit」** —— "
       "加署名不等于鼓励多提交。")
    )


_TASK_CARD = Path(__file__).resolve().parent / "task_card.py"
try:
    # 远程节点 (Windows) 的同步只覆盖 fleet_bridge.py 一个文件 → 那边没有它。
    # 判断放在**导入期**且不许抛: 这一段过时/出错的后果是静默的 (bot 只是"没那么做")。
    # Use the current interpreter and shell-compatible paths across platforms.
    _TASK_CARD_CMD = (f"{Path(sys.executable).as_posix()} {_TASK_CARD.as_posix()}"
                      f" --home {_BRIDGE_HOME.as_posix()}"
                      if _TASK_CARD.is_file() else None)
except OSError:
    _TASK_CARD_CMD = None
try:
    # Resolve the bot name from the configured home directory.
    _fleet_json = _fleet_ops_file("fleet.json")     # 两路回落, 见 _fleet_ops_file
    _BOT_NAME = next(
        (n for n, c in json.loads(_fleet_json.read_text(encoding="utf-8")).items()
         if Path(str(c.get("home", "")).replace("~", str(Path.home()), 1)) == _BRIDGE_HOME),
        None) if _fleet_json else None
except Exception:  # noqa: BLE001  (导入期不许因为读不到这个文件就掀翻进程)
    _BOT_NAME = None
_BRIDGE_NOTE = _build_bridge_note(APP_ONLY, _OUTBOX_DIR, _NOTE_LESSONS, _TASK_CARD_CMD,
                                  _BOT_NAME, _NOTE_FLEET_STATE, _NOTE_FLEET_API)


def _claude_md_cost_note(size: int, limit: int = 12 * 1024) -> str:
    """CLAUDE.md 超限时追加的一句 —— **给能改它的那个人看, 带具体数字**。

    Include the actual size in the per-turn warning so the recipient can act.
    """
    if size <= limit:
        return ""
    return (f"\n\n⚠ **本仓 CLAUDE.md 现在 {size // 1024}KB, 每一轮都整份进系统提示 "
            f"(约 {size // 3} tokens/轮, 上限建议 {limit // 1024}KB)。**"
            f"请把非「身份/命令/约定」的内容挪进 NOTES.md 等文件 —— 那些用时再读, "
            f"不必每轮付费。")


try:
    if _CLAUDE_MD:
        _CM_SIZE = len(_CLAUDE_MD.encode("utf-8"))
        _BRIDGE_NOTE += _claude_md_cost_note(_CM_SIZE)
        if _CM_SIZE > 12 * 1024:
            log(f"⚠ 本仓 CLAUDE.md {_CM_SIZE // 1024}KB (>12KB): 它每轮整份进系统提示, "
                f"约 {_CM_SIZE // 3} tokens/轮 —— 已在提示词里提醒本 bot")
except Exception:  # noqa: BLE001  (算不出来不该拦启动)
    pass

_OPEN_BASE = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}
_WS_DOMAIN = {"feishu": lark.FEISHU_DOMAIN, "lark": lark.LARK_DOMAIN}
_FEISHU_MSG_MAX = 4000          # 单条飞书 text 分片上限
_THINKING_MAX_LINES = 40        # 折叠"思维链"面板最多展示几行工具活动
# 流式输出 (CardKit 打字机): 默认关; 需后台加 cardkit:card:write scope + 客户端≥7.20.
# 失败自动降级回一次性整卡, 零回归. 开: 在该实例 .env 加 FEISHU_BRIDGE_STREAMING=1.
STREAMING_ON = _env("STREAMING").strip() in ("1", "true", "yes")
CHROME_ON = _env("CHROME").strip() in ("1", "true", "yes")  # Claude in Chrome
_STREAM_MIN_INTERVAL = 0.2      # 两次流式 PUT 最小间隔 (≤5/秒, 避开 10/秒红线)
_STREAM_MIN_DELTA = 24          # answer 增量 ≥ 此字符才 flush
_APPROVAL_TIMEOUT_S = 300       # 审批等待上限, 超时默认拒绝
_FEISHU_RETRY_MAX = 3           # 飞书 API 连接层瞬断的重试次数 (**只此一份**)
_YES = {"y", "yes", "是", "批准", "同意", "ok", "好", "可以", "允许"}
_NO = {"n", "no", "否", "拒绝", "不", "取消", "stop"}
# 中断当前 turn 的词 (在 ACTIVE 拦截之前处理, 否则会被"上一条还在处理"挡掉)
_INTERRUPT = {"/stop", "停", "停止", "中断", "stop", "取消", "cancel", "！停", "!停"}


def _read_runtime() -> dict:
    """读 <home>/runtime.json 的 model/effort 覆盖 (管理器 bot 写); 缺失 → {}.

    **损坏要留痕**: 静默 {} = 静默回落默认模型/强度, 而用户明明切过 —— 表现是
    默认值；应记录读取错误。
    """
    if not _RUNTIME_FILE.exists():
        return {}                       # 从没设过覆盖, 正常
    try:
        return json.loads(_RUNTIME_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ runtime.json 存在但读不出 ({exc}) —— 本次回落默认模型/强度")
        return {}


def _write_status(chat_id: str, result_msg, ctx: dict | None = None) -> None:
    """每轮把上下文/用量快照写 <home>/status.json (管理器 bot 读). 失败不影响对话.

    ctx = client.get_context_usage() 的**权威**用量 (totalTokens/maxTokens/percentage/model);
    缺失则回落 ResultMessage.usage 估算. sessions 用 dict spread 合并 (不写死字段).
    """
    try:
        rt = _read_runtime()
        usage = getattr(result_msg, "usage", None) or {}
        mu = getattr(result_msg, "model_usage", None) or {}
        if ctx:
            ctx_tokens = int(ctx.get("totalTokens", 0) or 0)
            max_tokens = int(ctx.get("maxTokens", 0) or 0)
            pct = round(float(ctx.get("percentage", 0.0) or 0.0), 1)
            eff_model = _prov.app_model(ctx.get("model") or "") or DEFAULT_MODEL
        else:
            ctx_tokens = _ctx_tokens(usage)
            max_tokens, pct = 0, 0.0
            eff_model = ((rt.get("model") or "").strip()
                         or (max(mu, key=lambda k: _mu_total(mu[k])) if mu else "")
                         or DEFAULT_MODEL)
        eff_effort = (rt.get("effort") or "").strip() or DEFAULT_EFFORT
        prev: dict = {}
        try:
            prev = json.loads(_STATUS_FILE.read_text(encoding="utf-8")).get("sessions", {})
        except Exception:  # noqa: BLE001
            prev = {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        sessions = {**prev, chat_id: {
            "context_tokens": ctx_tokens,
            "max_tokens": max_tokens,
            "context_pct": pct,
            "num_turns": int(getattr(result_msg, "num_turns", 0) or 0),
            "last_cost_usd": round(float(getattr(result_msg, "total_cost_usd", 0.0) or 0.0), 4),
            "model_usage": mu,        # 权威 per-model 用量拆分 (SDK ResultMessage.model_usage)
            "ts": now,
        }}
        tmp = _STATUS_FILE.with_suffix(".tmp")    # 临时文件 + rename, 原子写防管理器读到半截
        tmp.write_text(json.dumps(
            {"ts": now, "repo": REPO, "model": eff_model, "effort": eff_effort,
             "defaults": {"model": DEFAULT_MODEL, "effort": DEFAULT_EFFORT},
             "sessions": sessions}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_STATUS_FILE)
        # 把这一轮**实际跑的**模型报给网关: 短名 bot (model=opus/sonnet) 的
        # 完整版本号只有这里知道 —— SDK 解析后经 ctx.model 报回来, 而网关那侧还原不出。
        # 失败不许影响这一轮: 它只是让 chip 少一个版本号。
        with contextlib.suppress(Exception):
            APP_FRONT.broadcast_runtime(eff_model)
        # 权威用量流水: 每轮追加一行 (SDK total_cost_usd + model_usage), 供管理器用量
        # 监测按窗口聚合真实成本 (status.json 只留最新一轮, 无法回看历史). 失败静默.
        try:
            cost = round(float(getattr(result_msg, "total_cost_usd", 0.0) or 0.0), 4)
            if cost or mu:
                line = json.dumps({"ts": now, "cost_usd": cost, "model_usage": mu},
                                  ensure_ascii=False)
                with (_BRIDGE_HOME / "usage_ledger.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ 写 status 失败: {exc}")


def _persist_ratelimit(rate_limit_info) -> None:
    """RateLimitEvent → <home>/ratelimit.json (账户级真·订阅额度, 管理器 usage 读)."""
    try:
        info = rate_limit_info
        payload = {
            "status": getattr(info, "status", None),                 # allowed / allowed_warning / rejected
            "rate_limit_type": getattr(info, "rate_limit_type", None),  # five_hour / seven_day[_opus/_sonnet] / overage
            "resets_at": getattr(info, "resets_at", None),
            "utilization": getattr(info, "utilization", None),
            "overage_status": getattr(info, "overage_status", None),
            "overage_resets_at": getattr(info, "overage_resets_at", None),
            "overage_disabled_reason": getattr(info, "overage_disabled_reason", None),
            "raw": getattr(info, "raw", None),                       # 服务端原始 payload (utilization 为 null 时值可能藏这)
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        tmp = _RATELIMIT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_RATELIMIT_FILE)
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ 写 ratelimit 失败: {exc}")


# ── 多会话持久 (resume/fork/切换) → 抽到 fleet_sessions.py 便于单测 (无 lark/sdk 依赖) ──
from feishu_bridge import fleet_sessions as _fs  # noqa: E402  (同目录, parent 已在 line 37 入 path)
_fs.SESSIONS_FILE = _BRIDGE_HOME / "sessions.json"
# DeepSeek 等 provider: engine 仍是 claude, 只换环境变量。见 fleet_providers.py
from feishu_bridge import fleet_providers as _prov  # noqa: E402
_SESSION_PROVIDERS_FILE = _BRIDGE_HOME / "session_providers.json"   # sid → provider (跨提供方 resume 护栏)
from feishu_bridge.fleet_quote import pick_quote_ref, remember_sent, load_sent_cache, save_sent_cache, load_notify_entry  # noqa: E402  (引用 ref 判定 + 出站正文缓存, 纯逻辑)
_fs.LOG = log
# 别名: bridge 内部仍用 _ 前缀名 (调用点不变)
_read_sessions = _fs.read_sessions
_load_session = _fs.load_session
_record_session = _fs.record_session
_clear_session = _fs.clear_session
_set_active = _fs.set_active
_name_active = _fs.name_active
_list_sessions = _fs.list_sessions
_resolve_session = _fs.resolve_session
_session_exists = _fs.session_exists
_migrate_sessions_once = _fs.migrate_sessions_once


_QUOTE_MAX = 2000   # 引用正文截断上限 (防止引一整条长回复把 prompt 撑爆)


def _msg_body_to_text(msg_type: str, content_raw: str) -> str:
    """把被引消息的 body.content 解析成可读文本. 覆盖 text / post / interactive."""
    try:
        c = json.loads(content_raw)
    except Exception:  # noqa: BLE001
        return (content_raw or "").strip()
    if msg_type == "text":
        return c.get("text", "").strip()
    if msg_type == "post":              # 富文本: {title, content:[[{tag,text}...]...]}
        parts: list[str] = []
        for line in c.get("content", []):
            for seg in line:
                if isinstance(seg, dict) and seg.get("text"):
                    parts.append(seg["text"])
        return " ".join(parts).strip()
    if msg_type == "interactive":       # 卡片 (含 bot 回复/转发的各种卡) → 广谱抽文字
        out: list[str] = []
        seen: set[str] = set()          # 去重 (同串多处出现只留一次)

        def _add(v) -> None:
            if isinstance(v, str):
                s = v.strip()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)

        def walk(node) -> None:
            if isinstance(node, dict):
                # 广谱: 任意节点的 content / text / (title 里的) 文字都抓, 不再限 tag 种类
                _add(node.get("content") if isinstance(node.get("content"), str) else None)
                _add(node.get("text") if isinstance(node.get("text"), str) else None)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(c)
        return "\n".join(out).strip()
    return f"[{msg_type} 消息]"


def _post_to_text(content: dict) -> str:
    """收到的富文本(post) → 纯文本. content 已解析: {title, content:[[{tag,text/href/user_name}...]...]}.

    段落间保留换行; text/a(链接) 取 text, at(@) 取名字, img/media 忽略. 供 on_message 用。
    """
    parts: list[str] = []
    title = (content.get("title") or "").strip()
    if title:
        parts.append(title)
    for para in content.get("content", []) or []:
        line = ""
        for run in para or []:
            if not isinstance(run, dict):
                continue
            tag = run.get("tag")
            if tag in ("text", "a"):
                line += run.get("text", "") or ""
            elif tag == "at":
                line += "@" + (run.get("user_name") or run.get("user_id") or "")
        parts.append(line)
    return "\n".join(parts).strip()


# ── 飞书发送 (tenant token 缓存 + im/v1/messages) ──────────────────
class FeishuSender:
    def __init__(self, app_id: str, app_secret: str, domain: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._base = _OPEN_BASE.get(domain, _OPEN_BASE["feishu"])
        self._token = ""
        self._token_exp = 0.0

    def _tenant_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        req = Request(
            f"{self._base}/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": self._app_id, "app_secret": self._app_secret}).encode(),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urlopen(req, timeout=10) as resp:
            res = json.loads(resp.read().decode())
        if res.get("code") != 0:
            raise RuntimeError(f"飞书 token 获取失败: {res}")
        self._token = res["tenant_access_token"]
        self._token_exp = time.time() + int(res.get("expire", 7200))
        return self._token

    def send(self, chat_id: str, text: str) -> None:
        """发到指定 chat_id (p2p), 长文分片. 用 markdown 卡片渲染."""
        for chunk in _chunks(text, _FEISHU_MSG_MAX):
            self._send_one(chat_id, chunk)

    def send_text(self, chat_id: str, text: str) -> None:
        """轻量纯文本 (msg_type=text) — 给"处理中/工具进度"这类状态用, 不走卡片."""
        token = self._tenant_token()
        req = Request(
            f"{self._base}/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=json.dumps({
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            }).encode(),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with urlopen(req, timeout=10) as resp:
                res = json.loads(resp.read().decode())
            if res.get("code") != 0:
                log(f"⚠ 状态发送失败: {res}")
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ 状态发送异常: {exc}")

    def upload_image(self, path: str) -> str | None:
        """上传本地图片 → image_key (POST im/v1/images, multipart). 失败返 None."""
        try:
            raw = Path(path).read_bytes()
            if len(raw) > 10 * 1024 * 1024:          # 飞书图片上限 10MB
                log(f"⚠ 图片超 10MB, 不上传: {path}")
                return None
            boundary = f"----feishubridge{int(time.time() * 1000)}"
            name = Path(path).name.replace('"', "")
            body = b"".join([
                (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="image_type"\r\n\r\nmessage\r\n').encode(),
                (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="image"; filename="{name}"\r\n'
                 f"Content-Type: application/octet-stream\r\n\r\n").encode(),
                raw,
                f"\r\n--{boundary}--\r\n".encode(),
            ])
            token = self._tenant_token()
            req = Request(
                f"{self._base}/open-apis/im/v1/images",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                         "Authorization": f"Bearer {token}"},
            )
            with urlopen(req, timeout=30) as resp:
                res = json.loads(resp.read().decode())
            if res.get("code") == 0:
                return res.get("data", {}).get("image_key")
            log(f"⚠ 图片上传失败: {res}")
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ 图片上传异常: {exc}")
        return None

    def send_image(self, chat_id: str, image_key: str) -> bool:
        """发图片消息 (msg_type=image). 成功返 True."""
        token = self._tenant_token()
        req = Request(
            f"{self._base}/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=json.dumps({
                "receive_id": chat_id,
                "msg_type": "image",
                "content": json.dumps({"image_key": image_key}),
            }).encode(),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with urlopen(req, timeout=10) as resp:
                res = json.loads(resp.read().decode())
            if res.get("code") == 0:
                return True
            log(f"⚠ 图片消息发送失败: {res}")
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ 图片消息发送异常: {exc}")
        return False

    def upload_video(self, path: str) -> str | None:
        """上传本地 mp4 → file_key (POST im/v1/files, multipart, file_type=mp4). 失败返 None."""
        try:
            raw = Path(path).read_bytes()
            if len(raw) > 30 * 1024 * 1024:          # 飞书文件上限 30MB
                log(f"⚠ 视频超 30MB, 不上传: {path}")
                return None
            boundary = f"----feishubridge{int(time.time() * 1000)}"
            name = Path(path).name.replace('"', "")
            body = b"".join([
                (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="file_type"\r\n\r\nmp4\r\n').encode(),
                (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="file_name"\r\n\r\n{name}\r\n').encode(),
                (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="file"; filename="{name}"\r\n'
                 f"Content-Type: application/octet-stream\r\n\r\n").encode(),
                raw,
                f"\r\n--{boundary}--\r\n".encode(),
            ])
            token = self._tenant_token()
            req = Request(
                f"{self._base}/open-apis/im/v1/files",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                         "Authorization": f"Bearer {token}"},
            )
            with urlopen(req, timeout=60) as resp:
                res = json.loads(resp.read().decode())
            if res.get("code") == 0:
                return res.get("data", {}).get("file_key")
            log(f"⚠ 视频上传失败: {res}")
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ 视频上传异常: {exc}")
        return None

    def send_media(self, chat_id: str, file_key: str) -> bool:
        """发视频消息 (msg_type=media, 聊天内可直接播放). 成功返 True."""
        token = self._tenant_token()
        req = Request(
            f"{self._base}/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=json.dumps({
                "receive_id": chat_id,
                "msg_type": "media",
                "content": json.dumps({"file_key": file_key}),
            }).encode(),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with urlopen(req, timeout=15) as resp:
                res = json.loads(resp.read().decode())
            if res.get("code") == 0:
                return True
            log(f"⚠ 视频消息发送失败: {res}")
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ 视频消息发送异常: {exc}")
        return False

    def pin_message(self, message_id: str) -> bool:
        """Pin 一条消息到会话顶部 (POST im/v1/pins). 成功返 True."""
        if not message_id:
            return False
        res = self._req_json("POST", f"{self._base}/open-apis/im/v1/pins",
                             {"message_id": message_id})
        if res and res.get("code") == 0:
            return True
        log(f"⚠ Pin 失败: {res}")
        return False

    def unpin_message(self, message_id: str) -> bool:
        """撤销置顶 (DELETE im/v1/pins/{mid}). 成功返 True; 已不在置顶列表也算成功。"""
        if not message_id:
            return False
        res = self._req_json("DELETE", f"{self._base}/open-apis/im/v1/pins/{message_id}")
        if res and res.get("code") == 0:
            return True
        log(f"⚠ 撤销置顶失败 (不影响本轮): {res}")
        return False

    def add_reaction(self, message_id: str, emoji_type: str = "Typing") -> str | None:
        """给消息加表情 (处理中指示), 返回 reaction_id (撤销时用). 失败返 None."""
        if not message_id:
            return None
        try:
            token = self._tenant_token()
            req = Request(
                f"{self._base}/open-apis/im/v1/messages/{message_id}/reactions",
                data=json.dumps({"reaction_type": {"emoji_type": emoji_type}}).encode(),
                headers={"Content-Type": "application/json; charset=utf-8",
                         "Authorization": f"Bearer {token}"},
            )
            with urlopen(req, timeout=10) as resp:
                res = json.loads(resp.read().decode())
            if res.get("code") == 0:
                return res.get("data", {}).get("reaction_id")
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ 加表情失败: {exc}")
        return None

    def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        if not (message_id and reaction_id):
            return
        try:
            token = self._tenant_token()
            req = Request(
                f"{self._base}/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}",
                method="DELETE",
                headers={"Authorization": f"Bearer {token}"},
            )
            urlopen(req, timeout=10).read()
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ 撤表情失败: {exc}")

    def download_resource(self, message_id: str, key: str, res_type: str, suffix: str) -> str | None:
        """下载消息资源 → 存盘, 返回路径. res_type: image (图片) | file (语音/文件).

        GET im/v1/messages/{message_id}/resources/{key}?type=... 返回二进制.
        """
        try:
            token = self._tenant_token()
            url = (f"{self._base}/open-apis/im/v1/messages/{message_id}"
                   f"/resources/{key}?type={res_type}")
            req = Request(url, headers={"Authorization": f"Bearer {token}"})
            with urlopen(req, timeout=30) as resp:
                raw = resp.read()
            path = _MEDIA_DIR / f"{message_id}{suffix}"
            path.write_bytes(raw)
            return str(path)
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ 资源下载失败 ({res_type}): {exc}")
            return None

    def get_message(self, message_id: str) -> str | None:
        """拉取单条消息正文 (解析"回复引用"的被引气泡). GET im/v1/messages/{id}.

        返回可读文本; 取不到/空返 None.
        """
        if not message_id:
            return None
        res = self._req_json(
            "GET", f"{self._base}/open-apis/im/v1/messages/{message_id}", None)
        if not (res and res.get("code") == 0):
            log(f"⚠ 取引用消息失败: {res}")
            return None
        items = res.get("data", {}).get("items", [])
        if not items:
            return None
        body = items[0].get("body", {})
        text = _msg_body_to_text(items[0].get("msg_type", ""), body.get("content", ""))
        if not text:
            return None
        return text[:_QUOTE_MAX] + " …(截断)" if len(text) > _QUOTE_MAX else text

    # ── CardKit 流式 (打字机) ──────────────────────────────────────
    def _req_json(self, method: str, url: str, body: dict | None) -> dict | None:
        # 连接层瞬断 (SSL UNEXPECTED_EOF / 代理抖动) 会让回复发不出去 → 重试.
        # ⚠ 次数只由 `_FEISHU_RETRY_MAX` 说了算: 收口前 range(3) / attempt < 2 / 文案里的
        # "3 次" 是**三个独立字面量**, 改一个漏两个。最险的漏法是 range 改大而
        # `attempt < 2` 没跟上 ⇒ **多出来那几次完全不退避, 连续打上游**, 而日志照旧说 3 次。
        # HTTPError (服务端已应答的 4xx/5xx) 不重试: 那是真 API 错, 且可能已生效.
        last_exc = None
        for attempt in range(_FEISHU_RETRY_MAX):
            try:
                token = self._tenant_token()
                data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
                req = Request(url, data=data, method=method,
                              headers={"Content-Type": "application/json; charset=utf-8",
                                       "Authorization": f"Bearer {token}"})
                with urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode())
            except HTTPError as exc:
                try:                           # 飞书错误体含 code/msg, 不打出来查不了根因
                    detail = exc.read().decode()[:300]
                except Exception:  # noqa: BLE001
                    detail = ""
                log(f"⚠ API {method} 失败: {exc} · {url.split('open-apis/')[-1]} · {detail}")
                return None                     # 服务端已应答 → 不重试
            except Exception as exc:  # noqa: BLE001  连接层错误 → 可安全重试 (未提交)
                last_exc = exc
                if attempt < _FEISHU_RETRY_MAX - 1:
                    time.sleep(0.6 * (attempt + 1))
        log(f"⚠ API {method} 失败 (重试 {_FEISHU_RETRY_MAX} 次): {last_exc}")
        return None

    def create_stream_card(self, card: dict) -> str | None:
        """建流式卡片实体 → card_id. data 必须二次 json.dumps (string-in-string)."""
        res = self._req_json("POST", f"{self._base}/open-apis/cardkit/v1/cards",
                             {"type": "card_json", "data": json.dumps(card, ensure_ascii=False)})
        if res and res.get("code") == 0:
            return res.get("data", {}).get("card_id")
        log(f"⚠ 建流式卡失败: {res}")
        return None

    def _post_interactive(self, chat_id: str, content_str: str,
                          reply_to: str | None = None) -> str | None:
        """发 interactive 消息 → message_id. reply_to 设则走原生「回复」接口
        (把回复挂到该消息的气泡上, 迟到也认得清答的是哪条); 回复失败回落普通发送."""
        if reply_to:
            res = self._req_json(
                "POST", f"{self._base}/open-apis/im/v1/messages/{reply_to}/reply",
                {"content": content_str, "msg_type": "interactive"})
            if res and res.get("code") == 0:
                return res.get("data", {}).get("message_id")
            log(f"⚠ 回复式发送失败, 回落普通发送: {res}")
        res = self._req_json(
            "POST", f"{self._base}/open-apis/im/v1/messages?receive_id_type=chat_id",
            {"receive_id": chat_id, "msg_type": "interactive", "content": content_str})
        if res and res.get("code") == 0:
            return res.get("data", {}).get("message_id")
        log(f"⚠ 飞书发送失败: {res}")
        return None

    def send_card_by_id(self, chat_id: str, card_id: str,
                        reply_to: str | None = None) -> str | None:
        """发消息绑卡片实体 → message_id (reply_to 设则回复式)."""
        return self._post_interactive(
            chat_id, json.dumps({"type": "card", "data": {"card_id": card_id}}), reply_to)

    def stream_put(self, card_id: str, element_id: str, content: str, seq: int) -> bool:
        """流式更新某元素的**全量**内容 (服务端算增量打字机渲染). sequence 严格递增."""
        res = self._req_json(
            "PUT", f"{self._base}/open-apis/cardkit/v1/cards/{card_id}/elements/{element_id}/content",
            {"content": content, "sequence": seq})
        return bool(res and res.get("code") == 0)

    def stream_finish(self, card_id: str, seq: int, summary: str = "") -> bool:
        """定稿: 关流式 (否则卡片不能转发 + 10min 后自动关, 交互回调也才正常).
        summary 设则同时把列表/推送预览更新成答案摘要 (免得永远停在「生成中」)."""
        cfg: dict = {"streaming_mode": False}
        if summary:
            cfg["summary"] = {"content": summary}
        res = self._req_json(
            "PATCH", f"{self._base}/open-apis/cardkit/v1/cards/{card_id}/settings",
            {"settings": json.dumps({"config": cfg}), "sequence": seq})
        return bool(res and res.get("code") == 0)

    def update_card(self, message_id: str, card: dict) -> bool:
        """原地更新一张已发的交互卡 (答完题把按钮换成结果, 消除「点了没反应」错觉)。"""
        r = self._req_json("PATCH", f"{self._base}/open-apis/im/v1/messages/{message_id}",
                           {"content": json.dumps(card, ensure_ascii=False)})
        return bool(r) and r.get("code") == 0

    def _post_card(self, chat_id: str, card: dict, reply_to: str | None = None) -> str | None:
        """发任意 interactive 卡片 dict → 返 message_id (reply_to 设则回复式, 失败回落)."""
        return self._post_interactive(chat_id, json.dumps(card, ensure_ascii=False), reply_to)

    def _send_one(self, chat_id: str, text: str) -> None:
        """发单段 markdown 卡片 (markdown 元素渲染)."""
        self._post_card(chat_id, {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "markdown", "content": md_to_feishu(text)}],
        })

    def send_reply(self, chat_id: str, answer_md: str, thinking_lines: list[str],
                   reply_to: str | None = None) -> str | None:
        """最终回复: 答案 markdown (永远可见) + 可选的默认折叠"思维链"面板.

        collapsible_panel 是 Card 1.0 原生组件 (无需 schema 2.0); 答案在面板外,
        旧客户端 (<7.9) 也至少能看到答案. thinking_lines 空则不带面板.
        reply_to 设则回复式 (挂到触发消息气泡, 消除"迟到看着像答上一条"的错觉).
        """
        ans = md_to_feishu(answer_md or "")
        if len(ans) > _FEISHU_MSG_MAX:
            ans = ans[:_FEISHU_MSG_MAX] + "\n\n…(已截断)"
        elements: list[dict] = [{"tag": "markdown", "content": ans or "(无内容)"}]

        if False and thinking_lines:  # Retained for compatibility with the broadcast interface.
            shown = thinking_lines[:_THINKING_MAX_LINES]
            extra = len(thinking_lines) - len(shown)
            tmd = "\n".join(shown)
            if extra > 0:
                tmd += f"\n… 还有 {extra} 步省略"
            if len(tmd) > 6000:
                tmd = tmd[:6000] + "\n…(已截断)"
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {
                    "title": {"tag": "markdown", "content": "**🤖 子代理活动**(点击展开)"},
                    "vertical_align": "center",
                    "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined",
                             "color": "grey", "size": "16px 16px"},
                    "icon_position": "right",
                    "icon_expanded_angle": -180,
                },
                "border": {"color": "grey", "corner_radius": "5px"},
                "padding": "8px 8px 8px 8px",
                "elements": [{"tag": "markdown", "content": tmd}],
            })
        return self._post_card(chat_id, {"config": {"wide_screen_mode": True}, "elements": elements},
                               reply_to=reply_to)


# ── 全局状态 ──────────────────────────────────────────────────────
MAIN_LOOP: asyncio.AbstractEventLoop | None = None
SENDER: Transport | None = None   # 核心只依赖抽象接口; 换前端在这里换实现即可
APP_FRONT = AppFrontend(_BRIDGE_HOME, log)   # Web 前端接入 (fleet-app 网关经 app.sock 连入)
_MANAGED_ENTRY = None  # Installed only by the future exact trusted bootstrap.
# 默认模型可由环境变量覆盖。
# FLEET_DEFAULT_MODEL 仍可显式覆盖。启动时算一次，变更后需走灰度重载。
DEFAULT_MODEL = _prov.resolve_default_model()


def _thinking_opt() -> dict:
    'Request summarized thinking when supported by the installed SDK.'
    try:
        import dataclasses as _dc  # noqa: PLC0415
        if "thinking" in {f.name for f in _dc.fields(ClaudeAgentOptions)}:
            return {"thinking": {"type": "adaptive", "display": "summarized"}}
    except Exception:  # noqa: BLE001  (探测失败也不许掀翻; 见上面那段)
        pass
    return {}
DEFAULT_EFFORT = "high"          # 舰队默认思考强度 (单独覆盖仍优先)
# 合法思考强度 —— **只此一份**。`fleet_env.py` 的模块头早点过名:
# 「effort 合法值散在五处三仓、其中两份不一致」。这次收的是本文件内那两处
# (`/effort` 斜杠命令 与 App 静默通道), 它们原来各写一遍同样的字面量。
# ⚠ 没有全收, 且是**故意的**:
# · `fleet_admin.py` 的 EFFORTS 少 default/默认 —— 那是 CLI, 要求显式档位是有意的
# · 网关 `server/main.py` 那份少「默认」—— **跨仓**, 改它要先递规格 (铁律 4)。
#   眼下无害: 客户端的 set_effort 先过网关校验, 「默认」在那儿就被拦, 到不了下面那个分支;
#   用户手打 `/effort 默认` 才走得到 —— 而那条路本来就该认中文。
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")   # 真实档位
EFFORT_ALIASES = ("default", "默认")                          # 「回到默认」的两种写法
EFFORT_ACCEPTED = frozenset(EFFORT_LEVELS + EFFORT_ALIASES)
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
# 路径判据只在 `_prov.codex_bin()`, 别在这里再读 env。`or "codex"` 只为 exec 报错时有个看得懂的名字;
# 有没有 Codex 由 CODEX_ENABLED 判。
CODEX_BIN = _prov.codex_bin() or "codex"
# Operators can disable Codex for a broken local installation.
# ⚠ **单一真相**: 跟 caps 那个开关共用同一处判断, 不各读各的 env。
# 原来两处独立读、默认值各写死一份 ⇒ 改一处漏一处的话:
# caps 关了而运行门开着 ⇒ 客户端不显示 GPT, 但**存量 runtime.json / 手工帧照样能把这个 bot
# 打进 `_run_turn_codex`** —— 否则 Windows 上可能静默卡死。
CODEX_ENABLED = app_frontend.CODEX_CAPABLE
# Choose the sandbox explicitly for the deployment; default to workspace access.
CODEX_SANDBOX = (os.getenv("FLEET_CODEX_SANDBOX", "workspace-write").strip()
                 or "workspace-write")
CODEX_REASONING_SUMMARY = (os.getenv("FLEET_CODEX_REASONING_SUMMARY", "detailed").strip()
                           or "detailed")
CODEX_LEDGER_FILE = _BRIDGE_HOME / "codex_ledger.jsonl"
# Opt-in only; preserve existing never/default sandbox and all managed constraints.
CODEX_APPROVAL_POLICY = os.getenv("FLEET_CODEX_APPROVAL_POLICY", "never")
CODEX_APPROVALS_REVIEWER = os.getenv("FLEET_CODEX_APPROVALS_REVIEWER", "")
CODEX_PERMISSIONS_PROBE = os.getenv("FLEET_CODEX_PERMISSIONS_PROBE", "")
CODEX_APPROVALS: dict[str, codex_approvals.Approvals] = {}
CODEX_QUESTIONS = codex_questions.Registry(lambda ev: APP_FRONT.broadcast(ev), lambda s: log(s))
from feishu_bridge import codex_archived_resume as _codex_archived  # exact native resume refusal only
_CODEX_ARCHIVED = _codex_archived.Store(_BRIDGE_HOME)
_QUESTION_CONTINUATION = contextvars.ContextVar("codex_question_continuation", default=None)
_QUESTION_TASKS: set[asyncio.Task] = set()


def _question_runtime_binding() -> str:
    value = (_read_runtime(), _runtime_snapshot(), CODEX_BIN, CODEX_SANDBOX,
             CODEX_APPROVAL_POLICY, CODEX_APPROVALS_REVIEWER, CODEX_PERMISSIONS_PROBE,
             _codex_storage.runtime_binding())
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _question_valid(card) -> bool:
    b = card.origin.binding
    try:
        _codex_storage.session_mapping(_fs.SESSIONS_FILE, _session_chat_key(b.work, "codex"),
                                       b.thread, _codex_storage.select())
    except _codex_storage.StorageRefused:
        return False
    if (card.origin.failed or b.work in ROLLOVER_HOLD or b.work in IDLE_ACTIVE
            or b.protocol != codex_questions.PROTOCOL
            or _runtime_snapshot()[2] != "codex" or _question_runtime_binding() != b.runtime
            or _load_session(_session_chat_key(b.work, "codex")) != b.thread):
        return False
    want = b.display if b.display.startswith("group:") else b.work
    work, display, why = APP_FRONT.resolve_work_display_ex(want)
    canonical = display if display.startswith("group:") else _BOT_NAME
    if why or work != b.work or canonical != b.display:
        return False
    if not card.origin.normal:
        control = CODEX_TURNS.get(b.work)
        return (not card.origin.disconnected and CODEX_PROCS.get(b.work) is card.origin.process
                and control is not None and control.proc is card.origin.process
                and control.thread_id == b.thread and control.turn_id == b.turn)
    return True  # Logical card lifetime only: no exited-process adoption.


def _question_origin(chat_id, thread, turn_id, proc, protocol, runtime):
    if protocol is None or _question_runtime_binding() != runtime:
        return None
    requested = _display_chat_for(chat_id)
    work, display, why = APP_FRONT.resolve_work_display_ex(requested)
    if why or work != chat_id:
        return None
    display = display if display.startswith("group:") else _BOT_NAME
    if not display or _load_session(_session_chat_key(chat_id, "codex")) != thread:
        return None
    return codex_questions.Origin(codex_questions.Binding(
        chat_id, display, thread, turn_id, runtime, protocol, os.urandom(16).hex()), proc)


# Managed local owner authoring only; these handles are never transport fields.
_QUESTION_DEFAULT_OWNER = object()
_QUESTION_DEFAULT_PRODUCER = None
_QUESTION_DEFAULT_INTENTS = None


def _question_defaults_bootstrap():
    global _QUESTION_DEFAULT_PRODUCER, _QUESTION_DEFAULT_INTENTS
    if type(_BOT_NAME) is not str or not _BOT_NAME.strip():
        return  # Unknown configured owner remains on unchanged legacy input.
    _QUESTION_DEFAULT_PRODUCER = question_defaults_producer.BridgeOwnedQuestionProducer(
        CODEX_QUESTIONS, owner=_QUESTION_DEFAULT_OWNER, bot=_BOT_NAME, valid=_question_valid)
    _QUESTION_DEFAULT_INTENTS = question_defaults_intents.OwnerIntentInbox(
        _BRIDGE_HOME / question_defaults_intents.DIRECTORY, bot=_BOT_NAME, audit=lambda s: log(s))


_question_defaults_bootstrap()


def _observe_question_event(origin, event, *, process, current):
    if _QUESTION_DEFAULT_INTENTS is not None and _QUESTION_DEFAULT_INTENTS.try_event(event, origin=origin, process=process,
            current=current, adapter=_QUESTION_DEFAULT_PRODUCER, owner=_QUESTION_DEFAULT_OWNER):
        return  # Matched owner intent consumed once; no possible-write fallback retry.
    CODEX_QUESTIONS.observe(origin, event, process=process, current=current)


async def _question_expiry_loop() -> None:
    while True:
        await asyncio.sleep(1)
        CODEX_QUESTIONS.sweep(_question_valid)
        if PEER_REPORTS is not None:
            for work in {r["work"] for r in PEER_REPORTS.held()}:
                if work not in _PEER_DRAINS:
                    _PEER_DRAINS.add(work)
                    task = asyncio.create_task(_drain_peer_reports(work))
                    _PEER_TASKS.add(task)
                    task.add_done_callback(_PEER_TASKS.discard)
                    task.add_done_callback(lambda _, w=work: _PEER_DRAINS.discard(w))


PEER_REPORTS = None
_PEER_DRAINS: set[str] = set()
_PEER_TASKS: set[asyncio.Task] = set()
_QUESTION_DRAIN_WORKS: dict[str, int] = {}


def _question_task_done(work, task):
    _QUESTION_TASKS.discard(task)
    remaining = _QUESTION_DRAIN_WORKS.get(work, 1) - 1
    if remaining:
        _QUESTION_DRAIN_WORKS[work] = remaining
    else:
        _QUESTION_DRAIN_WORKS.pop(work, None)


def _peer_qualified(value, display):
    root = Path.home() / ".fleet-ops"
    return peer_reports.qualify(value, receiver=_BOT_NAME, display=display,
                                registry=root / "fleet.json", domains=root / "fleet_domains.json")


def _defer_peer_report(work, display, text, arrived, origin) -> bool:
    if PEER_REPORTS is None or _peer_qualified(origin, display) is None:
        return False
    CODEX_QUESTIONS.sweep(_question_valid)
    held = [c for c in CODEX_QUESTIONS.cards.values() if c.origin.binding.work == work
            and c.state in {"pending", "received", "queued", "dispatching"}]
    old = PEER_REPORTS.lookup(origin["id"])
    if not held and old is None:
        return False
    try:
        row, fresh = PEER_REPORTS.hold(origin, work=work, display=display, text=text,
            arrived=arrived, visible=not app_frontend.INTERNAL_GROUP_TASK.get(),
            cards=[c.ask_id for c in held], expires=min((c.expires_at for c in held), default=arrived))
    except (OSError, ValueError, KeyError, TypeError):
        # Refusal before an atomic journal commit uses the existing unrelated-input
        # path. No accepted-but-lost report or exemption from cancellation.
        log("Peer report deferral refused journal=unavailable_or_conflict; ordinary input path")
        return False
    log(f"Peer report id={origin['id']} state={row['state']} fresh={fresh} work={work} display={display}")
    return True


async def _drain_peer_reports(work):
    """One claimed original at a time; never steer, retry, or restore a card."""
    if PEER_REPORTS is None:
        return
    for row in PEER_REPORTS.held(work):
        if (work in ACTIVE or work in ROLLOVER_HOLD
                or _QUESTION_DRAIN_WORKS.get(work, 0)):
            return
        CODEX_QUESTIONS.sweep(_question_valid)
        # Admission is current-work scoped, not authority rebound to old rows.
        if any(c.origin.binding.work == work
               and c.state in {"pending", "received", "queued", "dispatching"}
               for c in CODEX_QUESTIONS.cards.values()):
            return
        if any(aid in CODEX_QUESTIONS.cards for aid in row["cards"]):
            return
        ident = row["origin"]["id"]
        try:
            if _peer_qualified(row["origin"], row["display"]) is None:
                PEER_REPORTS.transition(ident, "blocked", "source_or_route_revoked")
                log(f"Peer report id={ident} state=blocked; original retained for review")
                continue
            # Persist at-most-once claim before any possible model write. A failed
            # claim never dispatches; later attempts are storage-only, not retries.
            PEER_REPORTS.transition(ident, "claimed")
        except (OSError, ValueError, KeyError, TypeError):
            log(f"Peer report id={ident} state=held journal_error; original retained")
            return
        ACTIVE.add(work)
        toks = APP_FRONT._push_chat_context(work, row["display"])
        ok = False
        try:
            # Keep the original arrival and display. This is now an ordinary turn;
            # its normal new_turn cancellation and all control handlers still apply.
            await run_turn(work, _stamp(row["text"], row["arrived"], row["display"]))
            ok = True
            await _drain_coalesce(work)
        except asyncio.CancelledError:
            raise
        except Exception:
            log(f"Peer report id={ident} delivery_failed_or_unknown; no retry")
        finally:
            ACTIVE.discard(work)
            APP_FRONT._pop_chat_context(toks)
            try:
                PEER_REPORTS.transition(ident, "completed" if ok else "delivery_unknown")
            except (OSError, ValueError):
                log(f"Peer report id={ident} terminal_journal_error; persisted claim prevents replay")


def _question_dispatch_guard(card) -> bool:
    if (CODEX_QUESTIONS.cards.get(card.ask_id) is not card or card.state != "dispatching"
            or not _question_valid(card) or time.monotonic() >= card.deadline):
        raise RuntimeError("Conversational card binding expired or changed")
    return True


def _question_live_dispatch_guard(card, control) -> bool:
    _question_dispatch_guard(card)
    if card.origin.normal:
        raise codex_questions.CompletedBeforeWrite("Origin completed before steering write")
    b = card.origin.binding
    if CODEX_TURNS.get(b.work) is not control or CODEX_PROCS.get(b.work) is not card.origin.process:
        raise RuntimeError("Conversational turn owner changed")
    return True


async def _dispatch_question_answer(card) -> None:
    b = card.origin.binding
    try:
        if not CODEX_QUESTIONS.transition(card, "queued", _question_valid):
            return
        if not card.origin.normal:
            control = CODEX_TURNS.get(b.work)
            if not CODEX_QUESTIONS.transition(card, "dispatching", _question_valid):
                return
            # Guard runs after the existing write lock, immediately before writing.
            try:
                await control.steer(card.answer, guard=lambda: _question_live_dispatch_guard(card, control))
            except codex_questions.CompletedBeforeWrite:
                # The guard ran BEFORE stdin.write: this is normal scheduling,
                # never a retry/fallback after a possibly delivered steering RPC.
                if not CODEX_QUESTIONS.requeue_completed_before_write(card, _question_valid):
                    return
            else:
                CODEX_QUESTIONS.delivered(card, acknowledged=True)
                return
        while b.work in ACTIVE:
            CODEX_QUESTIONS.sweep(_question_valid)
            if CODEX_QUESTIONS.cards.get(card.ask_id) is not card:
                return
            await asyncio.sleep(0.05)
        if not CODEX_QUESTIONS.transition(card, "dispatching", _question_valid):
            return
        # Occupy the ordinary scheduling slot atomically; no generic message fallback.
        ACTIVE.add(b.work)
        token = _QUESTION_CONTINUATION.set(card)
        toks = APP_FRONT._push_chat_context(b.work, b.display if b.display.startswith("group:") else b.work)
        try:
            # Ephemeral card answers must not enter durable inflight recovery/replay.
            await run_turn(b.work, card.answer)
            _QUESTION_CONTINUATION.reset(token)
            token = None
            await _drain_coalesce(b.work)
        finally:
            ACTIVE.discard(b.work)
            APP_FRONT._pop_chat_context(toks)
            if token is not None:
                _QUESTION_CONTINUATION.reset(token)
    except asyncio.CancelledError:
        CODEX_QUESTIONS.cancel(card, "dispatch_cancelled")
        raise
    except Exception:
        CODEX_QUESTIONS.delivered(card, acknowledged=False)
    finally:
        # turn/start acknowledgement removes it earlier; a failed start cannot retry.
        CODEX_QUESTIONS.delivered(card, acknowledged=False)


def _app_answer_codex_question(req: dict) -> None:
    card = CODEX_QUESTIONS.claim(req, _question_valid)
    if card is None:
        log("Codex question answer refused reason=invalid_or_stale")
        return  # Dedicated namespace is always consumed, never flattened/injected.
    if len(_QUESTION_TASKS) >= codex_questions.MAX_PENDING:
        CODEX_QUESTIONS.cancel(card, "dispatch_limit")
        return
    task = asyncio.create_task(_dispatch_question_answer(card))
    _QUESTION_TASKS.add(task)
    work = card.origin.binding.work
    _QUESTION_DRAIN_WORKS[work] = _QUESTION_DRAIN_WORKS.get(work, 0) + 1
    task.add_done_callback(lambda done: _question_task_done(work, done))


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


CODEX_STREAM_LIMIT = _positive_int_env("FLEET_CODEX_STREAM_LIMIT_BYTES", 16 * 1024 * 1024)
CODEX_COMPACT_TIMEOUT = _positive_int_env("FLEET_CODEX_COMPACT_TIMEOUT_SECONDS", 300)


def _engine_for_model(model: str) -> str:
    return "codex" if str(model or "").strip().lower().startswith("gpt-") else "claude"


def _runtime_snapshot() -> tuple[str, str, str]:
    rt = _read_runtime()
    model = (rt.get("model") or "").strip() or DEFAULT_MODEL
    effort = (rt.get("effort") or "").strip() or DEFAULT_EFFORT
    return model, effort, _engine_for_model(model)


def _session_chat_key(chat_id: str, engine: str | None = None) -> str:
    got = (engine or _runtime_snapshot()[2]).strip().lower()
    return f"{chat_id}::codex" if got == "codex" else chat_id


def _codex_usage_int(usage: dict, *keys: str) -> int:
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in keys:
        try:
            total += int(usage.get(key, 0) or 0)
        except Exception:  # noqa: BLE001
            continue
    return total


def _codex_usage_total(usage: dict) -> int:
    """Cached input and reasoning output are subsets, not additional tokens."""
    for key in ("totalTokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return (_codex_usage_int(usage, "input_tokens", "inputTokens")
            + _codex_usage_int(usage, "output_tokens", "outputTokens"))


class _CodexTurnUsage:
    """One turn's delta of cumulative snapshots; never sum replayed snapshots."""
    fields = {
        "inputTokens": "input_tokens", "outputTokens": "output_tokens",
        "cachedInputTokens": "cached_input_tokens",
        "cacheWriteInputTokens": "cache_write_input_tokens",
        "reasoningOutputTokens": "reasoning_output_tokens", "totalTokens": "total_tokens",
    }

    def __init__(self, thread: str, turn: str, *, new_thread: bool):
        self.thread, self.turn = thread, turn
        self.baseline = {key: 0 for key in self.fields} if new_thread else None
        self.baseline_source = "new-thread" if new_thread else "unknown"
        self.latest = None
        self.result = {}
        self.complete = True

    @classmethod
    def counts(cls, value):
        if not isinstance(value, dict):
            return None
        out = {}
        for key, alias in cls.fields.items():
            raw = value.get(key, value.get(alias, 0))
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                return None
            out[key] = raw
        if "totalTokens" not in value and "total_tokens" not in value:
            out["totalTokens"] = out["inputTokens"] + out["outputTokens"]
        if not any(key in value or alias in value for key, alias in cls.fields.items()):
            return None
        return out

    def feed(self, ev: dict, *, before_turn: bool = False) -> bool:
        params = ev.get("params") if isinstance(ev.get("params"), dict) else {}
        tid = params.get("threadId") or ev.get("thread_id")
        nested = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        turn = params.get("turnId") or nested.get("id")
        if tid and str(tid) != self.thread:
            return False
        token = params.get("tokenUsage")
        total = self.counts(token.get("total")) if isinstance(token, dict) else None
        last = self.counts(token.get("last")) if isinstance(token, dict) else None
        if before_turn or (turn and str(turn) != self.turn):
            # Only startup snapshots can establish the previous turn's baseline.
            if before_turn and total is not None:
                self.baseline, self.baseline_source = total, "before-turn"
            return False
        if total is not None:
            if self.baseline is None:
                if last is None or any(total[k] < last[k] for k in self.fields):
                    self.complete = False
                    return False  # Never charge an entire resumed thread to this turn.
                self.baseline = {k: total[k] - last[k] for k in self.fields}
                self.baseline_source = "first-update-minus-last"
            if (any(total[k] < self.baseline[k] for k in self.fields)
                    or self.latest is not None and any(total[k] < self.latest[k] for k in self.fields)):
                self.complete = False  # Reset/out-of-order data cannot safely be added.
                return False
            self.latest = total
            self.result = {k: total[k] - self.baseline[k] for k in self.fields}
            return True
        got = self.counts(_codex_usage_from(ev))
        if got is not None:
            # Legacy last-only events cannot prove full multi-call coverage.
            if not self.result:
                self.result = got
            elif isinstance(token, dict):
                for k in self.fields:
                    self.result[k] = max(self.result[k], got[k])
            else:
                self.result = got
            self.complete = False
            return True
        return False

    def usage(self) -> dict:
        if not self.result:
            return {}
        return {**self.result, "_fleetAccounting": {
            "version": 2, "complete": self.complete,
            "method": "thread-total-delta", "baseline": self.baseline_source,
        }}


def _codex_thread_id(ev: dict, fallback: str = "") -> str:
    for key in ("thread_id", "session_id", "id"):
        val = ev.get(key)
        if val:
            return str(val)
    thread = ev.get("thread")
    if isinstance(thread, dict):
        for key in ("id", "thread_id", "session_id"):
            val = thread.get(key)
            if val:
                return str(val)
    return fallback


def _codex_usage_from(ev: dict, fallback: dict | None = None) -> dict:
    got = ev.get("usage")
    if isinstance(got, dict):
        return got
    turn = ev.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("usage"), dict):
        return turn["usage"]
    params = ev.get("params")
    if isinstance(params, dict):
        token_usage = params.get("tokenUsage")
        if isinstance(token_usage, dict):
            latest = token_usage.get("last") or token_usage.get("total")
            if isinstance(latest, dict):
                return latest
    return fallback or {}


def _codex_text_value(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for key in ("text", "content", "message", "output"):
            got = _codex_text_value(v.get(key))
            if got:
                return got
    if isinstance(v, list):
        return "".join(_codex_text_value(x) for x in v)
    return ""


def _codex_tool_brief(ev: dict) -> tuple[str, str] | None:
    params = ev.get("params") if isinstance(ev.get("params"), dict) else {}
    item = (params.get("item") if isinstance(params.get("item"), dict)
            else ev.get("item") if isinstance(ev.get("item"), dict) else ev)
    name = str(item.get("name") or item.get("tool_name") or item.get("type") or "").strip()
    if not name:
        return None
    low = name.lower()
    if not any(k in low for k in ("tool", "exec", "command", "patch", "file", "shell")):
        return None
    brief = _codex_text_value(item.get("command") or item.get("input")
                              or item.get("description"))[:240]
    return name, brief


def _codex_reasoning_text(ev: dict) -> tuple[str, bool]:
    """Return a displayable reasoning summary and whether it is a delta.

    `codex exec --json` currently emits completed reasoning items. App Server
    exposes the same summary as text deltas, so accept both shapes without ever
    falling back to encrypted/raw reasoning fields.
    """
    typ = str(ev.get("type") or ev.get("event") or ev.get("method") or "")
    item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
    if typ == "item.completed" and str(item.get("type") or "") == "reasoning":
        return _codex_text_value(item.get("text") or item.get("summary")), False
    if typ in ("item/reasoning/summaryTextDelta",
               "response.reasoning_summary_text.delta"):
        params = ev.get("params") if isinstance(ev.get("params"), dict) else ev
        return _codex_text_value(params.get("delta")), True
    return "", False


def _codex_agent_text(ev: dict) -> str:
    """Extract one completed assistant message from Codex JSONL output."""
    typ = str(ev.get("type") or ev.get("event") or "")
    item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
    if typ != "item.completed" or str(item.get("type") or "") != "agent_message":
        return ""
    return _codex_text_value(item.get("text") or item.get("content")
                             or item.get("message"))


def _codex_error_event(ev: dict, thread: str = "", turn: str = "") -> tuple[str, bool] | None:
    """Return a provider error and whether App Server says it will retry.

    Current App Server emits an ``error`` notification. Older ``codex exec --json``
    builds exposed the same failure as a completed item whose type was ``error``;
    keep accepting that shape so a CLI upgrade cannot turn a visible failure silent.
    """
    method = str(ev.get("method") or ev.get("type") or ev.get("event") or "")
    params = ev.get("params") if isinstance(ev.get("params"), dict) else {}
    if method == "error":
        got_thread = str(params.get("threadId") or params.get("thread_id") or "")
        got_turn = str(params.get("turnId") or params.get("turn_id") or "")
        if thread and got_thread and got_thread != thread:
            return None
        if turn and got_turn and got_turn != turn:
            return None
        error = params.get("error") if isinstance(params.get("error"), dict) else {}
        detail = _codex_text_value(error) or "Codex reported an error"
        return detail, bool(params.get("willRetry") or params.get("will_retry"))

    if method not in ("item/completed", "item.completed"):
        return None
    item = params.get("item") if isinstance(params.get("item"), dict) else ev.get("item")
    if not isinstance(item, dict) or str(item.get("type") or "").lower() != "error":
        return None
    detail = _codex_text_value(item.get("error") or item) or "Codex reported an error"
    return detail, False


# Codex 以 error 条目的形状发出、但**只是提示**的已知文本 (小写子串匹配)。
# ⚠ 这是**白名单**, 不是黑名单 —— 方向是故意选的:
#   · 名单外的一律照旧告警并判失败 ⇒ 新出现的提示会误报, 但**看得见**, 再加进来就是
#   · 真失败不会因为名单而漏过 (除非正文恰好就是这句提示)
# ⚠ **不许改成"同轮有正常回答 / 有成功命令就放过"**: 契约 §2.18⑬ 当初堵的正是
#   "工具被拦, 模型照样流畅作答, turn.completed, exit 0" —— 那种判据会把洞重开。
# Known informational messages must not be reported as failed turns.
_CODEX_INFO_ERRORS = (
    "skill descriptions were shortened to fit the skills context budget",
)


def _codex_is_informational(detail: str) -> bool:
    low = (detail or "").lower()
    return any(k in low for k in _CODEX_INFO_ERRORS)


class _CodexActiveTurn:
    """Writable side of one App Server turn; stdout stays owned by run_turn."""

    def __init__(self, proc: asyncio.subprocess.Process, thread_id: str, turn_id: str, *, writer=None):
        self.proc = proc
        self.thread_id = thread_id
        self.turn_id = turn_id
        self._writer = writer  # Trusted actual child object, never a frame flag.
        self._next_id = 100
        self._pending: dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()

    async def request(self, method: str, params: dict, timeout: float = 15.0, *, guard=None) -> dict:
        if self.proc.stdin is None or self.proc.returncode is not None:
            raise RuntimeError("Codex App Server is not writable")
        async with self._write_lock:
            if self._writer is None and guard is not None:
                guard()
            request_id = self._next_id
            self._next_id += 1
            future = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
            message = {"method": method, "id": request_id, "params": params}
            if self._writer is not None:
                try:
                    raw = (json.dumps(message, ensure_ascii=False, allow_nan=False)
                           + "\n").encode("utf-8", "strict")
                    await self._writer.write(self.proc, raw, guard=guard)
                except BaseException:
                    self._pending.pop(request_id, None)
                    future.cancel()
                    self._writer._hold()
                    raise
            else:
                self.proc.stdin.write((json.dumps(message, ensure_ascii=False)
                                       + "\n").encode("utf-8"))
                await self.proc.stdin.drain()
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, message: dict) -> bool:
        if "method" in message:  # server requests have their own id namespace
            return False
        request_id = message.get("id")
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        if isinstance(message.get("error"), dict):
            err = message["error"]
            future.set_exception(RuntimeError(str(err.get("message") or err)))
        else:
            future.set_result(message)
        return True

    async def steer(self, text: str, *, guard=None) -> None:
        response = await self.request("turn/steer", {
            "threadId": self.thread_id,
            "expectedTurnId": self.turn_id,
            "input": [{"type": "text", "text": text}],
        }, guard=guard)
        # Selected 0.159.2 TurnSteerResponse requires a STRING turnId. A
        # matching RPC id alone cannot establish acknowledgement of this turn.
        result = response.get("result") if isinstance(response, dict) else None
        if (not isinstance(response, dict) or "error" in response
                or not isinstance(result, dict)
                or not isinstance(result.get("turnId"), str)
                or result["turnId"] != self.turn_id):
            raise RuntimeError("Codex steering acknowledgement is missing or mismatched")

    def close(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("Codex turn ended before steering completed"))
        self._pending.clear()


async def _codex_rpc_send(proc: asyncio.subprocess.Process, message: dict, *, guard=None, writer=None) -> None:
    if writer is not None:
        try:
            raw = (json.dumps(message, ensure_ascii=False, allow_nan=False)
                   + "\n").encode("utf-8", "strict")
            await writer.write(proc, raw, guard=guard)
        except BaseException:
            writer._hold()
            raise
        return
    if proc.stdin is None:
        raise RuntimeError("Codex App Server stdin unavailable")
    if guard is not None:
        guard()
    proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    await proc.stdin.drain()


class _CodexRpcError(RuntimeError):
    """An error response correlated to one process and one outgoing RPC."""
    def __init__(self, proc, request_id, error, method=None, thread=None):
        super().__init__(str(error.get("message") or error))
        self.proc, self.request_id = proc, request_id
        self.method, self.thread, self.error = method, thread, error


async def _codex_rpc_response(proc: asyncio.subprocess.Process, request_id: int,
                              on_event=None, on_error=None, reader=None, *, method=None, thread=None) -> dict:
    if proc.stdout is None:
        raise RuntimeError("Codex App Server stdout unavailable")
    while True:
        if reader is not None:
            message = await reader.read_frame(proc)
            if message is None:
                break
        else:
            raw = await proc.stdout.readline()
            if not raw:
                break
            try:
                message = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                continue
        if not isinstance(message, dict):
            raise RuntimeError("Codex App Server returned a malformed frame")
        # A server request can also have an id; it is not our RPC response. Rollover
        # must inspect notifications immediately, including those preceding the ack.
        if type(message.get("id")) is not int or message["id"] != request_id or "method" in message:
            if on_event is not None:
                on_event(message)
            continue
        if reader is not None:
            reader.require_response(message, request_id)
        if isinstance(message.get("error"), dict):
            if on_error is not None:
                on_error(message)
            if reader is not None:
                reader.response_error()  # Fixed refusal; no raw native error export.
            raise _CodexRpcError(proc, request_id, message["error"], method, thread)
        return message
    raise RuntimeError("Codex App Server exited before responding")


def _codex_unusable_session(error: Exception, proc, thread: str) -> bool:
    """Recognize only an exact original-SID failure of this resume request."""
    if (not isinstance(error, _CodexRpcError) or error.proc is not proc
            or error.request_id != 2 or error.method != "thread/resume"
            or error.thread != thread or not thread):
        return False
    raw = error.error
    if type(raw.get("code")) is not int or raw["code"] not in {-32600, -32603, -32000}:
        return False
    data = raw.get("data")
    if data is not None:
        if not isinstance(data, dict):
            return False
        for key in ("threadId", "thread_id", "sessionId", "session_id"):
            if key in data and data[key] != thread:
                return False
    sid = re.escape(thread)
    patterns = (
        rf"session {sid} (?:is|has been) archived(?:\. Run codex unarchive {sid} first)?\.?",
        rf"session {sid} is archived\. Run codex unarchive {sid} to unarchive it first\.",
        rf"(?:thread|session|conversation) {sid} (?:not found|does not exist|doesn't exist)\.?",
        rf"no rollout found for thread id {sid}\.?",
        rf"(?:unknown|missing) (?:thread|session|conversation) {sid}\.?",
    )
    return any(re.fullmatch(pattern, str(raw.get("message") or "")) for pattern in patterns)


def _codex_resume_guard(chat_id: str, session_key: str, binding: tuple, proc) -> None:
    sid, revision, runtime, display = binding
    if (revision is None or CODEX_PROCS.get(chat_id) is not proc
            or _load_session(session_key) != sid or _fs.sessions_revision() != revision
            or _question_runtime_binding() != runtime or _display_chat_for(chat_id) != display):
        raise RuntimeError("Codex resume binding changed; recovery stopped without overwriting current state")


def _codex_cmd(prompt: str, model: str, effort: str, thread: str | None) -> tuple[_codex_storage.Launch, Path]:
    out_file = Path("/tmp") / f"fleet_codex_last_{os.getpid()}_{int(time.time() * 1000)}.txt"
    if thread:
        cmd = [CODEX_BIN, "exec", "resume",
               "-c", f"sandbox_mode={CODEX_SANDBOX}",
               "--json", "-o", str(out_file),
               "-m", model, "-c", f"model_reasoning_effort={effort}",
               "-c", f"model_reasoning_summary={CODEX_REASONING_SUMMARY}",
               thread, prompt]
    else:
        cmd = [CODEX_BIN, "exec",
               "-s", CODEX_SANDBOX,
               "--json", "-o", str(out_file),
               "-m", model, "-c", f"model_reasoning_effort={effort}",
               "-c", f"model_reasoning_summary={CODEX_REASONING_SUMMARY}",
               prompt]
    storage = _codex_storage.select()
    if thread:
        storage.transcript(thread)
    return _codex_storage.Launch(storage.command(cmd), storage.env(), storage), out_file


async def _codex_compact_thread(thread: str) -> None:
    """Run Codex's native manual compaction for an existing persisted thread."""
    storage = _codex_storage.select()
    storage.transcript(thread)
    proc = await asyncio.create_subprocess_exec(
        *storage.command([CODEX_BIN, "app-server", "--listen", "stdio://"]),
        cwd=REPO, env=storage.env(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=CODEX_STREAM_LIMIT)
    stderr_task = asyncio.create_task(proc.stderr.read() if proc.stderr else asyncio.sleep(0, b""))

    async def send(msg: dict) -> None:
        if proc.stdin is None:
            raise RuntimeError("Codex app-server stdin unavailable")
        proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def read_for(request_id: int) -> dict:
        if proc.stdout is None:
            raise RuntimeError("Codex app-server stdout unavailable")
        while raw := await proc.stdout.readline():
            try:
                msg = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                continue
            if msg.get("id") != request_id:
                continue
            if isinstance(msg.get("error"), dict):
                err = msg["error"]
                raise RuntimeError(str(err.get("message") or err))
            return msg
        err_raw = await stderr_task
        detail = err_raw.decode("utf-8", errors="replace").strip() if isinstance(err_raw, bytes) else ""
        raise RuntimeError(detail or "Codex app-server exited before responding")

    try:
        await send({"method": "initialize", "id": 1, "params": {
            "clientInfo": {"name": "fleet-bridge", "version": "1.0"},
            "capabilities": {"experimentalApi": True},
        }})
        await read_for(1)
        await send({"method": "notifications/initialized"})
        await send({"method": "config/read", "id": 21, "params": {"includeLayers": False, "cwd": REPO}})
        storage.effective((await read_for(21)).get("result", {}).get("config"))
        await send({"method": "thread/resume", "id": 2,
                    "params": {"threadId": thread, "excludeTurns": True, "config": storage.config()}})
        await read_for(2)
        await send({"method": "thread/compact/start", "id": 3,
                    "params": {"threadId": thread}})

        accepted = False
        completed = False
        if proc.stdout is None:
            raise RuntimeError("Codex app-server stdout unavailable")
        while not (accepted and completed):
            raw = await proc.stdout.readline()
            if not raw:
                err_raw = await stderr_task
                detail = (err_raw.decode("utf-8", errors="replace").strip()
                          if isinstance(err_raw, bytes) else "")
                raise RuntimeError(detail or "Codex app-server exited during compaction")
            try:
                msg = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                continue
            if msg.get("id") == 3:
                if isinstance(msg.get("error"), dict):
                    err = msg["error"]
                    raise RuntimeError(str(err.get("message") or err))
                accepted = True
                continue
            if msg.get("method") != "turn/completed":
                continue
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if str(params.get("threadId") or "") != thread:
                continue
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            status = str(turn.get("status") or "")
            if status != "completed":
                err = turn.get("error") if isinstance(turn.get("error"), dict) else {}
                raise RuntimeError(str(err.get("message") or f"compaction {status or 'failed'}"))
            completed = True
    finally:
        if proc.stdin:
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.terminate()
            await proc.wait()
        if not stderr_task.done():
            stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await stderr_task


def _append_codex_ledger(chat_id: str, model: str, effort: str, thread: str,
                         usage: dict, exit_code: int | None, secs: float,
                         error: str = "", *, writer=None) -> None:
    observed = None
    if writer is not None:
        from feishu_bridge.fleet_managed_persistence import begin
        observed = begin(writer, "ledger", chat_id, thread, CODEX_LEDGER_FILE)
    try:
        if observed is not None:
            observed.append_ledger(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "chat_id": chat_id,
                "repo": REPO,
                "engine": "codex",
                "model": model,
                "effort": effort,
                "thread": thread,
                "usage": usage if isinstance(usage, dict) else {},
                "exit": exit_code,
                "secs": round(secs, 1),
                "error": error[:500],
            }, ensure_ascii=False) + "\n")
        else:
            CODEX_LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
            with CODEX_LEDGER_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "chat_id": chat_id,
                    "repo": REPO,
                    "engine": "codex",
                    "model": model,
                    "effort": effort,
                    "thread": thread,
                    "usage": usage if isinstance(usage, dict) else {},
                    "exit": exit_code,
                    "secs": round(secs, 1),
                    "error": error[:500],
                }, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        if observed is not None:
            observed.refuse()
        log(f"⚠ 写 Codex ledger 失败: {exc}")
    except BaseException:
        if observed is not None:
            observed.refuse()
        raise
    else:
        if observed is not None:
            observed.observe()


def _write_codex_status(chat_id: str, model: str, effort: str, thread: str,
                        usage: dict, exit_code: int | None, *, writer=None) -> None:
    observed = None
    if writer is not None:
        from feishu_bridge.fleet_managed_persistence import begin
        observed = begin(writer, "status", chat_id, thread, _STATUS_FILE)
    try:
        prev: dict = {}
        try:
            if observed is not None:
                prev = observed.previous_sessions()
            else:
                prev = json.loads(_STATUS_FILE.read_text(encoding="utf-8")).get("sessions", {})
        except Exception:  # noqa: BLE001
            if observed is not None:
                observed.refuse()
            prev = {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        used = _codex_usage_total(usage)
        sessions = {**prev, chat_id: {
            "context_tokens": used,
            "max_tokens": 0,
            "context_pct": 0.0,
            "num_turns": 0,
            "last_cost_usd": 0.0,
            "model_usage": {model: usage if isinstance(usage, dict) else {}},
            "engine": "codex",
            "thread": thread,
            "exit": exit_code,
            "ts": now,
        }}
        tmp = _STATUS_FILE.with_suffix(".tmp")
        if observed is not None:
            observed.write_status(json.dumps({
            "ts": now,
            "repo": REPO,
            "model": model,
            "effort": effort,
            "engine": "codex",
            "defaults": {"model": DEFAULT_MODEL, "effort": DEFAULT_EFFORT},
            "sessions": sessions,
        }, ensure_ascii=False, indent=2))
        else:
            tmp.write_text(json.dumps({
                "ts": now,
                "repo": REPO,
                "model": model,
                "effort": effort,
                "engine": "codex",
                "defaults": {"model": DEFAULT_MODEL, "effort": DEFAULT_EFFORT},
                "sessions": sessions,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(_STATUS_FILE)
        if observed is not None:
            APP_FRONT.broadcast_runtime(model)
        else:
            with contextlib.suppress(Exception):
                APP_FRONT.broadcast_runtime(model)
    except Exception as exc:  # noqa: BLE001
        if observed is not None:
            observed.refuse()
        log(f"⚠ 写 Codex status 失败: {exc}")
    except BaseException:
        if observed is not None:
            observed.refuse()
        raise
    else:
        if observed is not None:
            observed.observe()

# 只读工具: 本轮若只用到这些, 网络中断可安全整轮重试 (重跑无副作用)。
# 写类 (Write/Edit/Bash/发消息/下单) 不在内 → 重试可能重复执行, 只提示手动。
_READONLY_TOOLS = {"Read", "Grep", "Glob", "WebSearch", "WebFetch", "TodoWrite",
                   "NotebookRead", "TodoRead", "Task",
                   # Session task tools have no external side effects; retain for compatibility.
                   "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput"}
CLIENTS: dict[str, ClaudeSDKClient] = {}            # chat_id → 持久 Claude 会话 client
IDLE_WATCH: dict[str, "asyncio.Task"] = {}  # chat_id → 轮间空闲监听
IDLE_ACTIVE: set[str] = set()                       # 监听器正处于一个后台回合中 (交接要等它播完)
IDLE_STOP: set[str] = set()                         # 请求监听器在当前回合结束后退出
IDLE_SEEN: dict[str, float] = {}                    # chat_id → 后台回合最后一次产出时刻
CLIENT_SIG: dict[str, tuple] = {}                   # chat_id → (model, effort, advisor) 该 client 建时签名
CLIENT_ADVISOR: dict[str, str] = {}                 # chat_id → advisor the live client was built with ('' = none)
# advisor model → the CLI's reason for refusing it. Not retried until runtime.json turns the
# advisor off (that clears this) — otherwise every turn would pay a failed CLI start first.
ADVISOR_REJECTED: dict[str, str] = {}
ACTIVE: set[str] = set()                            # chat_id 正在跑一个 turn
CLAUDE_STEERABLE: set[str] = set()                  # 已进入响应流、可接同轮追加消息
INTERRUPTED: set[str] = set()                       # chat_id 本 turn 被用户主动中断 (抑制 error 回复)
CODEX_PROCS: dict[str, asyncio.subprocess.Process] = {}  # chat_id → 正在跑的 Codex CLI 子进程
CODEX_TURNS: dict[str, _CodexActiveTurn] = {}       # chat_id → 可 turn/steer 的 App Server turn
STEER_LOCKS: dict[str, asyncio.Lock] = {}           # 同 chat 串行确认队列移除
ASK_CUT: set[str] = set()  # 提问卡结束后先定稿上一段
_TASK_TRACK: dict[str, dict] = {}                   # chat_id → {"sid", "items"}: Task 工具的会话级任务清单
_TASK_TRACK_FILE = _BRIDGE_HOME / "task_track.json"
# Version 2 stores actual task IDs; discard incompatible persisted state.
_TASK_TRACK_V = 2
try:
    _raw = json.loads(_TASK_TRACK_FILE.read_text(encoding="utf-8"))
    if int(_raw.pop("_v", 1)) == _TASK_TRACK_V:
        _TASK_TRACK.update(_raw)
except Exception:  # noqa: BLE001
    pass


_BLOCK_START = ("```", "#", "|", ">", "- ", "* ", "1. ")


def _join_blocks(buf: str, text: str) -> str:
    """拼接文本块，必要时添加换行或空行，保留 Markdown 块级结构。"""
    if buf and not buf.endswith("\n"):
        buf += "\n\n" if text.lstrip("\n").startswith(_BLOCK_START) else "\n"
    return buf + text


def _read_status_ctx(chat_id: str) -> dict:
    """从 status.json 取该会话的上下文用量 (压缩前后对比用)。取不到 → {}。"""
    try:
        s = json.loads(_STATUS_FILE.read_text(encoding="utf-8"))["sessions"][chat_id]
        return {"used": int(s.get("context_tokens") or 0),
                "max": int(s.get("max_tokens") or 0)}
    except Exception:  # noqa: BLE001
        return {}


def _session_tasks(chat_id: str) -> list:
    """返回会话级任务清单；会话切换时重置，持久化以便进程重启后恢复。"""
    sid = _load_session(chat_id) or ""
    st = _TASK_TRACK.get(chat_id)
    if st is None or st.get("sid") != sid:
        # Clear persisted and displayed tasks when switching sessions.
        had = bool((st or {}).get("items"))
        st = _TASK_TRACK[chat_id] = {"sid": sid, "items": []}
        if had:      # 只在真顶掉了一份非空清单时才播 —— 否则开机/首次问询都在制造噪音
            _save_task_track(chat_id)
            _broadcast_todos([])
    return st["items"]


_TASK_TRACK_SEEN_MTIME = 0.0     # 最后一次「我们已知」的文件 mtime (自己写的也算已知)


def _task_items_snapshot() -> str:
    """当前内存清单的可比较快照 —— 用来判断"真的变了"而不是"文件被 touch 了"。"""
    return json.dumps(_TASK_TRACK, ensure_ascii=False, sort_keys=True)


def _reload_task_track_if_changed() -> bool:
    '文件被外部修改时重新加载；坏文件或版本不符不清空内存。跳过自身写入，内容未变化时不广播。轮询不能消除并发写入窗口。'
    global _TASK_TRACK_SEEN_MTIME
    try:
        mtime = _TASK_TRACK_FILE.stat().st_mtime
    except OSError:
        return False                      # 文件不在 = 没有外部改动可认, 内存照旧
    if mtime == _TASK_TRACK_SEEN_MTIME:
        return False
    _TASK_TRACK_SEEN_MTIME = mtime        # 无论加载成功与否都记下, 免得坏文件每轮重试刷日志
    try:
        raw = json.loads(_TASK_TRACK_FILE.read_text(encoding="utf-8"))
        if int(raw.pop("_v", 1)) != _TASK_TRACK_V:
            return False
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ task_track.json 读不动, 保留内存里的清单: {exc}")
        return False
    before = _task_items_snapshot()
    _TASK_TRACK.clear()
    _TASK_TRACK.update(raw)
    return _task_items_snapshot() != before


def _save_task_track(chat_id: str) -> None:
    '仅更新当前会话的任务卡，借助 task_card.save_one 从磁盘最新状态合并。并发读改写的限制见该函数。'
    global _TASK_TRACK_SEEN_MTIME
    try:
        # ⚠ 路径从 `_TASK_TRACK_FILE` 派生, **别另写一遍 `_BRIDGE_HOME / "task_track.json"`**:
        # 那是第二个来源, 两处能漂开 —— 而漂开时测试里打的桩就失效了.
        track = task_card.save_one(_TASK_TRACK_FILE.parent, chat_id,
                                   _TASK_TRACK.get(chat_id))
        # 把别人那几格的最新内容收进内存 —— 不收的话下一次落盘又是拿陈旧的底稿去写
        _TASK_TRACK.clear()
        _TASK_TRACK.update({k: v for k, v in track.items() if not k.startswith("_")})
        # 记下自己写出来的 mtime: 监视器据此跳过**我们自己的**那次改动,
        # 否则每次工具调用都会多播一轮清单 (自己制造噪音)。
        _TASK_TRACK_SEEN_MTIME = _TASK_TRACK_FILE.stat().st_mtime
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ 落盘任务清单失败 (不影响这一轮): {exc}")
PLAN_ACTIVE: set[str] = set()                       # chat_id 本 turn 处于 /plan 计划模式 (can_use_tool 才门 ExitPlanMode)
FORK_PENDING: set[str] = set()                      # chat_id 下次建 client 要 fork 当前会话 (/fork)
PENDING: dict[str, asyncio.Future] = {}             # chat_id → 等待中的审批 Future (同时只一个审批流: plan 或 pretool)
QueuedInput = tuple[str, float] | tuple[str, float, str] | tuple[str, float, str, bool]
COALESCE: dict[str, list[QueuedInput]] = {}
ASK_PENDING: dict[str, tuple[str, asyncio.Future]] = {}  # chat_id → (ask_id, 等答案的 future)                 # chat_id → 忙时暂存的消息 (当前 turn 跑完后合并成一条接着跑)
ASK_DISPLAY: dict[str, str] = {}                         # ask_id → UI 落点; 群聊问答显示在 group, future 仍按 bot 会话等
# work chat → display of the turn now running. Written by run_turn (which runs in the
# triggering message's context); read by code the SDK calls from its own task, where the
# DISPLAY_CHAT contextvar is whatever it was when that task was born.
TURN_DISPLAY: dict[str, str] = {}

# ── 未完成回合落盘 ──────────────────────────────────────
# 进程被杀时, 正在跑的那一轮**没有任何地方记着它的输入是什么**, COALESCE 又是纯内存 ——
# 于是每次重启都静默吃掉「被打断的活」+「排在它后面的消息」, 而 bot 重启后一脸空闲。
# Notify the user about interrupted work; do not automatically replay side effects.
_INFLIGHT_FILE = _BRIDGE_HOME / "inflight.json"
_INFLIGHT_TEXT_MAX = 500        # 原始输入可能是贴进来的长日志, 提示里放不下也没必要


def _inflight_load() -> dict:
    try:
        return json.loads(_INFLIGHT_FILE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _inflight_save(data: dict) -> None:
    """写盘; 空了就删文件 —— **残留文件本身就是判据**, 留个空壳会让判据失真。"""
    try:
        if not data:
            _INFLIGHT_FILE.unlink(missing_ok=True)
            return
        tmp = _INFLIGHT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_INFLIGHT_FILE)       # 原子替换: 断电时要么旧的要么新的, 不会半截
    except Exception:  # noqa: BLE001
        pass          # fail-soft: 落盘失败不影响回合


def inflight_mark(chat_id: str, text: str) -> None:
    """一轮开跑 → 记下来。与 ACTIVE.add 同生共死 (见 handle_message 的 try/finally)。"""
    d = _inflight_load()
    d[chat_id] = {"text": (text or "")[:_INFLIGHT_TEXT_MAX], "ts": time.time(), "queued": []}
    _inflight_save(d)


def inflight_queue(chat_id: str, items: list[str]) -> None:
    """忙时暂存的消息一并落盘。没有在跑的回合就不建条目 (排队只在 ACTIVE 时发生)。"""
    d = _inflight_load()
    if chat_id not in d:
        return
    d[chat_id]["queued"] = [str(i)[:_INFLIGHT_TEXT_MAX] for i in items]
    _inflight_save(d)


def inflight_clear(chat_id: str) -> None:
    """一轮收尾 → 抹掉。放在 finally 里, 所以正常结束/异常/中断都覆盖到。"""
    d = _inflight_load()
    if d.pop(chat_id, None) is not None:
        _inflight_save(d)


def inflight_take_pending() -> dict:
    """启动时取残留并**立刻删掉** —— 提示只发一次, 否则每次重启都再提示同一件事。"""
    d = _inflight_load()
    try:
        _INFLIGHT_FILE.unlink(missing_ok=True)   # 坏文件也在这里被清掉, 别每次启动都撞
    except Exception:  # noqa: BLE001
        pass
    return d


def inflight_notice(entry: dict) -> str:
    """残留 → 给用户的一句话。

    **不能说「已恢复」「正在继续」** —— 我们没有重跑它。说了就是第二个假成功信号,
    而这个功能存在的全部理由就是消灭第一个。
    """
    when = time.strftime("%H:%M", time.localtime(entry.get("ts") or time.time()))
    text = (entry.get("text") or "").strip()
    head = f"⚠ 上一轮在 {when} 被打断了（进程重启），内容是：\n{text}"
    n = len(entry.get("queued") or [])
    tail = f"\n\n另有 {n} 条消息还在排队，也一并没跑。" if n else ""
    return head + tail + "\n\n**我没有自动重跑**（怕把已经做过的事做第二次）。要继续的话跟我说一声。"

ASK_WAIT_SEC = 600                  # 提问卡等答案上限 (对用户口径「10 分钟」)
# 交给 CLI 的钩子超时: 必须比 ASK_WAIT_SEC 长。CLI 对 SDK callback 钩子的默认超时正好
# 也是 600s, 且它从下发 hook_callback 开始计时、我们从发完卡才开始 —— 撞在同一秒时
# 必然是 CLI 先到期, 模型收到 "PreToolUse hook did not respond before its timeout"
# 而不是我们的「按默认继续, 别重复提问」→ 无人值守时它会原样再问一遍
# 避免无人值守时重复提问。
ASK_HOOK_TIMEOUT_SEC = ASK_WAIT_SEC + 60
_LAST_QUOTE_REF: dict[str, str] = {}  # 保留兼容；不用于去重
_SENT_CARD_MAX = 300
_SENT_CARD_PATH = _BRIDGE_HOME / "sent_card_cache.json"   # 出站正文缓存落盘处 → 重启不丢
_NOTIFY_CARD_PATH = _BRIDGE_HOME / "notify_card_cache.json"   # cron 推送脚本写, 本进程只读 (现读现取)
# message_id → bot 出站正文 (引用 bot 卡片回复时兜底; API 取卡片只返占位符). 启动从磁盘载入.
_SENT_CARD_TEXT: "OrderedDict[str, str]" = load_sent_cache(_SENT_CARD_PATH, _SENT_CARD_MAX)


def _remember(message_id, text) -> None:
    """记 bot 出站正文 + 立即落盘 (引用回复兜底, 重启不丢)."""
    remember_sent(_SENT_CARD_TEXT, message_id, text, _SENT_CARD_MAX)
    save_sent_cache(_SENT_CARD_TEXT, _SENT_CARD_PATH)


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def make_pretool_hook(chat_id: str):
    """PreToolUse hook 当审批门 (比 can_use_tool 可靠: 每次工具必触发, 不看 permission_mode).

    非高危 → 放行 (返回 {}); 高危 → 飞书 y/n 审批, 超时/拒绝 → deny.
    """
    async def hook(input_data, tool_use_id, context):
        tool = input_data.get("tool_name", "")
        inp = input_data.get("tool_input", {}) or {}
        high, reason, summary = classify_risk(tool, inp, REPO)
        if not high:
            return {}                       # 放行
        SENDER.send(chat_id,
                    f"🔐 **审批请求** · {reason}\n```\n{summary}\n```\n"
                    f"回复 **y** 批准 / **n** 拒绝 ({_APPROVAL_TIMEOUT_S//60} 分钟无回复自动拒绝)")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        PENDING[chat_id] = fut
        try:
            approved = await asyncio.wait_for(fut, timeout=_APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            SENDER.send(chat_id, "⏱ 审批超时, 已拒绝该操作。")
            return _deny("用户未在时限内批准")
        finally:
            PENDING.pop(chat_id, None)
        if approved:
            return {}                       # 批准 → 放行
        SENDER.send(chat_id, "🚫 已拒绝该操作。")
        return _deny("用户拒绝")
    return hook


def make_can_use_tool(chat_id: str):
    """can_use_tool 回调 — **只**门 ExitPlanMode (计划模式批准), 其它工具一律放行.

    bypassPermissions 下本回调不触发; set_permission_mode("plan")
    后才对 ExitPlanMode 触发. 故平时无感, /plan 时才弹「计划待批准」, 复用 PENDING y/n.
    """
    async def cb(tool_name, tool_input, ctx):
        # 仅在 /plan 计划模式且工具是 ExitPlanMode 才阻塞审批; 其余一律放行.
        # PLAN_ACTIVE 守卫: 万一 SDK 未来在非 plan 路径触发本回调, 也不会卡死 5 分钟.
        if tool_name != "ExitPlanMode" or chat_id not in PLAN_ACTIVE:
            return PermissionResultAllow()              # bypass 取向: 其它工具全放行
        plan = (tool_input or {}).get("plan", "") or "(空计划)"
        SENDER.send(chat_id, f"📋 **计划待批准**\n\n{plan}\n\n回复 **y** 执行 / **n** 改计划"
                             f"（{_APPROVAL_TIMEOUT_S // 60} 分钟无回复自动取消）")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        PENDING[chat_id] = fut
        try:
            approved = await asyncio.wait_for(fut, timeout=_APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            SENDER.send(chat_id, "⏱ 计划审批超时, 已取消。")
            return PermissionResultDeny(message="用户未在时限内批准计划")
        finally:
            PENDING.pop(chat_id, None)
        if approved:
            SENDER.send(chat_id, "✅ 计划已批准, 开始执行。")
            return PermissionResultAllow()
        SENDER.send(chat_id, "🚫 计划未批准, 请重新描述需求或调整。")
        return PermissionResultDeny(message="用户要求修改计划")
    return cb


async def _get_client(chat_id: str) -> ClaudeSDKClient:
    """每会话一个**持久** ClaudeSDKClient.

    一次性 query() 跨多轮会让 can_use_tool 审批控制流 'Stream closed' (输入流结束
    即拆); 持久连接保活整个会话, 审批/工具稳定. 进程内上下文由 client 维持; **跨重启**
    则靠 resume=持久 session_id (见下) 续接, 故重启不丢记忆.

    model/effort 由 <home>/runtime.json 覆盖 (管理器 bot 写); 运行中变更则丢弃旧
    client 重建; 重建时 resume 同一 session → 记忆保留. 缺失 → 沿用 SDK 默认.
    """
    rt = _read_runtime()
    # 未覆盖时显式用 opus-5: 之前传 None = 跟着 SDK 默认走,
    # 各端界面显示的「默认」与实际跑的模型对不上
    sig = _runtime_sig(rt)
    model, effort, advisor = sig

    client = CLIENTS.get(chat_id)
    if client is not None and CLIENT_SIG.get(chat_id) != sig:
        old = CLIENT_SIG.get(chat_id) or (None, None, None)
        # 只 model 变 → 活体 set_model, 不重建不丢记忆 (advisor 是启动参数, 变了得重建)。
        # ⚠ 换 provider (Anthropic↔DeepSeek) 不能活切: base_url/key 是**子进程环境变量**,
        # 只在起 CLI 时生效 —— 活切只会把 deepseek-flash 这个名字发给 Anthropic。必须重建。
        same_provider = (_prov.provider_for_model(old[0] or DEFAULT_MODEL)
                         == _prov.provider_for_model(model))
        if old[1:] == (effort, advisor) and same_provider:
            try:
                await client.set_model(_prov.cli_model(model))   # None → 还原默认
                CLIENT_SIG[chat_id] = sig
                log(f"[{chat_id[:8]}] 模型活体切换 → {model or '默认'}")
                return client
            except Exception as exc:  # noqa: BLE001
                log(f"[{chat_id[:8]}] set_model 失败 ({exc}), 回落重建")
        # 切换静默生效, 不再发「⚙️ 已切换/已更新」提醒卡, 只记日志
        log(f"[{chat_id[:8]}] runtime 变更 {old} → {sig}, 重建 client")
        await _drop_client(chat_id)
        client = None
    if client is None:
        # 审批门走 PreToolUse hook (可靠); 不加 setting_sources — 否则仓库/全局
        # permissions allowlist 会预放行工具, 绕过审批门. cwd 已绑仓库, CLAUDE.md
        # 按需自读.
        # 审批门已关 (跟主 Claude Code session 一样 bypass 全放行).
        # 仅靠发送人白名单 (ALLOWED_OPEN_ID) 防越权. 要恢复"高危审批"门:
        # 去掉 permission_mode, 加回 hooks={"PreToolUse":[HookMatcher(hooks=[make_pretool_hook(chat_id)])]}.
        resume_sid = _load_session(chat_id)        # 跨重启续接 active 会话
        if resume_sid and not _session_exists(resume_sid):
            log(f"[{chat_id[:8]}] active session {resume_sid[:8]} transcript 不存在, 开新会话")
            resume_sid = None
        provider = _prov.provider_for_model(model)
        # provider 专属环境变量 (DeepSeek: base_url / key / 模型映射)。没 key → 抛,
        # 这一轮以错误收尾 (run_turn 的 except 会给 App 发 ev=error), 进程不倒。
        prov_env = _prov.provider_env(model)
        if provider != "anthropic":
            advisor = ""                           # --advisor 是 Anthropic 专有, 别带给第三方
        if resume_sid and not _prov.can_resume(_SESSION_PROVIDERS_FILE, resume_sid, model):
            # 跨提供方续聊默认不做 (理由见 fleet_providers 「会话归属」一节): 开新会话,
            # 旧会话留在 /sessions, 切回原模型后 /resume 能接上。
            old_prov = _prov.session_provider(_SESSION_PROVIDERS_FILE, resume_sid)
            log(f"[{chat_id[:8]}] 会话 {resume_sid[:8]} 属于 {old_prov}, 当前模型走 {provider} "
                f"→ 不跨提供方续聊, 开新会话")
            with contextlib.suppress(Exception):
                APP_FRONT.broadcast({"ev": "session_reset", "engine": "claude",
                                     "prev": resume_sid, "prev_provider": old_prov,
                                     "provider": provider, "model": _prov.app_model(model),
                                     "hint": "provider_switched", "ts": time.time()})
            resume_sid = None
            FORK_PENDING.discard(chat_id)
        fork = chat_id in FORK_PENDING             # /fork: 从 active 分叉; 提交后 (run_turn 成功 record) 才清, 防中断/异常丢 fork
        _cli_err: deque = deque(maxlen=20)         # CLI stderr tail, captured only when an advisor is on
        opts = ClaudeAgentOptions(
            cwd=REPO,
            # 这一轮属于哪个会话 —— 交给**该会话自己的** CLI 子进程, 工具进程继承得到
            # (SDK 是 {**os.environ, **options.env} 合并, 不是替换, subprocess_cli.py:689;
            # client 又是每会话一个, 所以两个会话不会串)。
            # 传入当前会话标识，供工具进程使用。
            # ⚠ 只放这一个键: 往里塞 os.environ 快照会把 PATH/凭证冻在建 client 那一刻。
            # 再叠加 provider 专属变量 (anthropic 时为空 dict, 行为不变)。
            # 同理只放**这次要覆盖的键**, 不塞 os.environ 快照。
            env={"FLEET_CHAT_ID": chat_id, **prov_env},
            permission_mode="bypassPermissions",
            can_use_tool=make_can_use_tool(chat_id),   # bypass 下不触发; /plan 切 plan 后门 ExitPlanMode
            # 提问卡阻塞等答案 + 主树守卫 (bypass 下 hook 仍生效)
            hooks={"PreToolUse": _pretool_matchers(chat_id)},
            model=_prov.cli_model(model),   # 恒有值; DeepSeek 补 [1m] (见 fleet_providers.cli_model)
            effort=effort,      # 恒有值: runtime 未覆盖则 DEFAULT_EFFORT
            **_thinking_opt(),  # 显式要摘要式思考, 别吃模型默认值 (见该函数的注释)
            resume=resume_sid or None,   # None → 全新会话; 有 → 重放该 session 历史
            fork_session=fork,           # True → 从 resume 的会话分叉出新 sid (原会话保留)
            system_prompt=(
                {"type": "preset", "preset": "claude_code",
                 "append": _BRIDGE_NOTE + (
                     f"\n\n以下是本仓库 ({_REPO_NAME}) 的 CLAUDE.md 项目说明, 作为背景知识:\n\n{_CLAUDE_MD}"
                     if _CLAUDE_MD else "")}
            ),
            include_partial_messages=True,   # 逐字流式 (打字机): 消费 StreamEvent 增量
            max_buffer_size=16 * 1024 * 1024,   
            # Leave cli_path unset so the SDK can select its bundled CLI.
            cli_path=None,
            extra_args={**({"chrome": None} if CHROME_ON else {}),   # --chrome: 使用浏览器扩展
                        **({"advisor": advisor} if advisor else {})},
            # Only with an advisor: a refused advisor makes the CLI exit at startup, and the SDK's
            # ProcessError says just "Check stderr output for details" — the reason is only here.
            **({"stderr": _cli_err.append} if advisor else {}),
        )
        client = ClaudeSDKClient(options=opts)
        try:
            await client.connect()
        except Exception as exc:  # noqa: BLE001
            if not advisor:
                raise
            with contextlib.suppress(Exception):
                await client.disconnect()
            reason = next((ln.strip() for ln in reversed(_cli_err) if "advisor" in ln.lower()),
                          f"{type(exc).__name__}: {str(exc)[:160]}")
            _reject_advisor(advisor, reason)
            return await _get_client(chat_id)         # rebuilt without it (now in ADVISOR_REJECTED)
        CLIENTS[chat_id] = client
        CLIENT_SIG[chat_id] = sig
        CLIENT_ADVISOR[chat_id] = advisor
        log(f"[{chat_id[:8]}] 新建持久会话 client (model={model or '默认'}, provider={provider}, effort={effort or '默认'}, "
            f"{'advisor=' + advisor + ', ' if advisor else ''}"
            f"{'fork ' if fork else ''}{'resume ' + resume_sid[:8] if resume_sid else '新会话'})")
    return client


_ADVISOR_OFF = {"", "off", "none", "null", "default", "no", "false"}
# Words that, while an advisor is on, mark a failed turn as the advisor's doing (API 400
# "configured advisor model is not compatible", advisor calls billed to usage credits that are off).
_ADVISOR_TURN_ERR_KEYS = ("advisor", "usage credit")


def _effective_advisor(rt: dict) -> str:
    """runtime.json "advisor" → the model to pass as --advisor, or '' for none.

    Off-words clear the rejection memory: `set-advisor off` then on again is the explicit retry
    (e.g. after extra usage gets enabled).
    """
    val = str(rt.get("advisor") or "").strip()
    if val.lower() in _ADVISOR_OFF:
        ADVISOR_REJECTED.clear()
        return ""
    return "" if val in ADVISOR_REJECTED else val


def _reject_advisor(advisor: str, reason: str) -> None:
    ADVISOR_REJECTED[advisor] = reason
    log(f"⚠ advisor {advisor} 不可用, 已回落为不带 advisor: {reason} "
        f"(runtime.json 把 advisor 设成 off 再设回来才会重试)")


def _advisor_turn_error(chat_id: str, err: str) -> bool:
    """A failed turn that the advisor caused — only meaningful while this client has one."""
    return bool(CLIENT_ADVISOR.get(chat_id)) and any(k in err.lower() for k in _ADVISOR_TURN_ERR_KEYS)


def _runtime_sig(rt: dict) -> tuple[str, str, str]:
    """(model, effort, advisor) a client is built with — the ONE place this tuple is defined.

    Tests that plant a stub client must set CLIENT_SIG from this too: a stale hand-written
    tuple makes _get_client swap the stub for a REAL client, and the test then runs real
    model turns (widening this from 2 to 3 fields did exactly that to 6 tests).
    """
    model = (rt.get("model") or "").strip() or DEFAULT_MODEL
    effort = (rt.get("effort") or "").strip() or DEFAULT_EFFORT
    return model, effort, _effective_advisor(rt)


async def _drop_client(chat_id: str) -> None:
    task = IDLE_WATCH.pop(chat_id, None)   # client 要没了, 陪跑的监听器一起走
    if task is not None:
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001
            pass
        IDLE_STOP.discard(chat_id)
        IDLE_ACTIVE.discard(chat_id)
    CLIENT_SIG.pop(chat_id, None)
    CLIENT_ADVISOR.pop(chat_id, None)
    FORK_PENDING.discard(chat_id)        # 切换/重建都清待分叉意图 (覆盖 /resume//new//reset; /fork 自己先 drop 后 add)
    client = CLIENTS.pop(chat_id, None)
    if client is not None:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def _idle_watch(chat_id: str) -> None:
    """轮与轮之间监听事件流。

    CLI 2.1.224+ 在后台任务完成时会自动唤醒模型续跑一轮 (task-notification), 但那时
    run_turn 早已结束、流上没有消费者 —— 该回合的输出积压, 直到下一条用户消息的
    receive_response 把它当成"本轮回复"吐出去: 拿旧答案回新问题, 且此后逐轮错位。
    这里把后台回合当场消费并推送给用户; 顺带清扫 resume 残留的陈旧终止符。
    """
    client = CLIENTS.get(chat_id)
    if client is None:
        return
    try:
        while chat_id not in IDLE_STOP:
            buf = ""
            tools = 0
            async for msg in client.receive_response():
                if isinstance(msg, (AssistantMessage, StreamEvent)):
                    # 只有模型真在产出才算「后台回合进行中」—— 此前任何消息 (含 /model
                    # 切换的系统事件) 都置位, 一次误判就永久占住, 下条用户消息白等 10 分钟
                    
                    IDLE_ACTIVE.add(chat_id)
                    IDLE_SEEN[chat_id] = time.time()
                if isinstance(msg, AssistantMessage) and not getattr(msg, "parent_tool_use_id", None):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            buf += block.text
                        elif isinstance(block, ToolUseBlock):
                            tools += 1
                            log(f"[{chat_id[:8]}] (后台回合) 工具: {block.name}")
                            APP_FRONT.broadcast({"ev": "tool", "name": block.name, "sub": False,
                                                 "brief": tool_brief(block.name, block.input or {})})
            INTERRUPTED.discard(chat_id)   # 「停」掐的是后台回合 → 标志就地消化, 不能漏给下一真回合
            text = _strip_leaked_toolcall(buf).strip()
            text, out_media = _extract_media_markers(text)
            text, _, _ = _extract_pin_marker(text)
            if text:
                mid = SENDER.send_reply(chat_id, "⏳ **后台任务续报**\n\n" + text, [])
                _remember(mid, text)
                log(f"[{chat_id[:8]}] 后台回合已推送 ({len(text)} 字, {tools} 工具)")
            else:
                log(f"[{chat_id[:8]}] 清扫空后台回合 ({tools} 工具, 无正文)")
            for kind, mpath in out_media:
                await asyncio.to_thread(_send_local_media, chat_id, kind, mpath)
            IDLE_ACTIVE.discard(chat_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log(f"[{chat_id[:8]}] 空闲监听异常退出: {type(exc).__name__}: {exc}")
    finally:
        IDLE_ACTIVE.discard(chat_id)


def _start_idle_watch(chat_id: str) -> None:
    if chat_id in IDLE_WATCH or CLIENTS.get(chat_id) is None:
        return
    IDLE_STOP.discard(chat_id)
    IDLE_WATCH[chat_id] = asyncio.get_event_loop().create_task(_idle_watch(chat_id))


async def _stop_idle_watch(chat_id: str) -> None:
    """新回合开始前的交接: 后台回合播到一半就等它播完 (硬掐会让残余事件再次污染
    下一轮 —— 那正是要修的病); 空等状态则直接取消 (无消费即无损失)。"""
    task = IDLE_WATCH.pop(chat_id, None)
    if task is None:
        return
    IDLE_STOP.add(chat_id)
    fresh = time.time() - IDLE_SEEN.get(chat_id, 0) < 90    # 停摆超过 90 秒 = 不是活回合, 直接掐
    if chat_id in IDLE_ACTIVE and fresh:
        SENDER.send_text(chat_id, "⏳ 一个后台回合正在收尾, 完了马上处理你这条…")
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=180)
        except BaseException:  # noqa: BLE001  超时/异常 → 只能掐了, 自愈重试兜底
            task.cancel()
    else:
        task.cancel()
    try:
        await task
    except BaseException:  # noqa: BLE001
        pass
    IDLE_STOP.discard(chat_id)
    IDLE_ACTIVE.discard(chat_id)
    IDLE_SEEN.pop(chat_id, None)


ASK_PARTIAL: dict[str, dict] = {}   # ask_id → {问题序号: "header: label"} (多问逐个点完才算答完)


def _ask_button_card(ask_id: str, qs: list) -> dict:
    """AskUserQuestion → 飞书按钮卡 (点按钮即作答, 走 WS card.action.trigger 回调)。

    多选题按钮只能单点 (飞书按钮无多选态), 卡上注明可回文字多选。
    """
    elements: list[dict] = []
    for qi, q in enumerate(qs):
        title = q.get("question", "")
        if len(qs) > 1:
            title = f"{qi + 1}/{len(qs)} · {title}"
        elements.append({"tag": "markdown", "content": f"**{title}**"})
        actions = []
        for o in (q.get("options") or []):
            desc = (o.get("description") or "").strip()
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": o.get("label", "")[:30]},
                "type": "default",
                "value": {"fleet_ask": ask_id, "qi": qi, "qn": len(qs),
                          "header": q.get("header", ""), "label": o.get("label", "")},
            })
            if desc:
                elements_desc = desc[:60]
        if actions:
            elements.append({"tag": "action", "actions": actions})
        descs = [f"▸ {o.get('label','')}: {o.get('description','').strip()[:60]}"
                 for o in (q.get("options") or []) if (o.get("description") or "").strip()]
        if descs:
            elements.append({"tag": "note", "elements": [
                {"tag": "plain_text", "content": "\n".join(descs)}]})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                     "content": "点按钮作答 · 也可直接回复文字 (多选/自定义) · 10 分钟有效"}]})
    return {"config": {"wide_screen_mode": True}, "elements": elements}


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """飞书按钮点击回调 (跑在 lark ws 线程)。识别我们的提问卡, resolve 等待中的 future。"""
    try:
        v = (data.event.action.value or {})
    except Exception:  # noqa: BLE001
        return P2CardActionTriggerResponse({})
    ask_id = v.get("fleet_ask")
    if not ask_id:
        return P2CardActionTriggerResponse({})
    for chat_id, (aid, fut) in list(ASK_PENDING.items()):
        if aid != ask_id or fut.done():
            continue
        header, label = v.get("header", ""), v.get("label", "")
        ans = f"{header}: {label}" if header else label
        qn = int(v.get("qn") or 1)
        if qn <= 1:
            answer = ans
        else:                                   # 多问: 攒齐才算答完
            partial = ASK_PARTIAL.setdefault(ask_id, {})
            partial[int(v.get("qi") or 0)] = ans
            if len(partial) < qn:
                return P2CardActionTriggerResponse({"toast": {
                    "type": "info", "content": f"已选 {label} ({len(partial)}/{qn}), 还差 {qn - len(partial)} 题"}})
            answer = "; ".join(partial[k] for k in sorted(partial))
            ASK_PARTIAL.pop(ask_id, None)
        if MAIN_LOOP:
            MAIN_LOOP.call_soon_threadsafe(lambda f=fut, a=answer: (not f.done()) and f.set_result(a))
        APP_FRONT.broadcast(app_frontend.user_event(          # App 端也看到答了什么
            answer, origin="feishu", chat_id=ASK_DISPLAY.get(ask_id) or chat_id))
        # 落点同上一行的 chat_id —— 答案气泡画在**卡当初显示的那个会话**, 不是运行会话
        log(f"[{chat_id[:8]}] 🖲 按钮作答 → 落点 {ASK_DISPLAY.get(ask_id) or chat_id}: {answer[:50]}")
        return P2CardActionTriggerResponse({"toast": {"type": "success", "content": f"已选择: {label}"}})
    return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "这张提问卡已过期"}})


def make_ask_hook(chat_id: str):
    """AskUserQuestion 的 PreToolUse 钩子：阻塞等待用户作答。

    飞书发文字卡 / App 发结构化 ask 事件, 任一端回答都行; 拿到答案后 deny 工具并把
    答案放进 reason → 模型在同一轮里按答案继续。超时/取消 → allow, 回落旧行为
    (工具返回「未回答」, 模型按假设走)。
    """
    async def _hook(input_data, tool_use_id, context):  # noqa: ANN001, ARG001
        tin = (input_data or {}).get("tool_input") or {}
        qs = tin.get("questions") or []
        if not qs:
            return {}
        import uuid as _uuid
        ask_id = _uuid.uuid4().hex[:12]
        # ⚠ TURN_DISPLAY first: the SDK calls this hook from its own task, whose contextvar
        # is from whenever that task was born (a group:ops turn put its card in DM).
        ui_chat = TURN_DISPLAY.get(chat_id) or app_frontend.DISPLAY_CHAT.get() or chat_id
        fut: asyncio.Future = MAIN_LOOP.create_future()
        ASK_PENDING[chat_id] = (ask_id, fut)
        ASK_DISPLAY[ask_id] = ui_chat
        # ⚠ **显式盖会话章, 别靠 contextvar 兜底**: 这个 `_hook` 是 SDK
        # 调进来的, 它在哪个 task/context 里跑**是 SDK 的实现细节** —— 我从没验过
        # `CUR_CHAT` 在这儿还在不在。而 `chat_id` 就在闭包里, 用它是确定的。
        # 赌一个没验过的继承关系, 输了的表现是: 卡发在群里、撤卡的帧落在私聊,
        # 于是**群里那张永远挂着** 。
        APP_FRONT.broadcast({"ev": "ask", "ask_id": ask_id,
                             "chat_id": ui_chat, "questions": qs})
        _post = getattr(SENDER.feishu, "_post_card", None)   # 防御: 缺方法也不许掀翻提问流程
        card_mid = await asyncio.to_thread(
            _post, chat_id, _ask_button_card(ask_id, qs)) if _post else None
        if not card_mid:                        # 按钮卡失败 → 文字卡兜底 (纯文字回答仍可用)
            await asyncio.to_thread(SENDER.feishu.send, chat_id, _render_ask(tin))
        # Record both work session and display destination.
        log(f"[{chat_id[:8]}] ❓ 提问卡已发 (ask={ask_id}) → 落点 {ui_chat}, 等待作答…")
        try:
            answer = await asyncio.wait_for(fut, timeout=ASK_WAIT_SEC)
        except asyncio.TimeoutError:
            if card_mid:
                await asyncio.to_thread(SENDER.feishu.update_card, card_mid,
                    {"config": {"wide_screen_mode": True}, "elements": [
                        {"tag": "markdown", "content": "⌛ **提问已超时** (10 分钟未回答, 已按默认继续)"}]})
            APP_FRONT.broadcast({"ev": "ask_cancel", "ask_id": ask_id,
                                 "chat_id": ui_chat, "reason": "timeout"})
            await asyncio.to_thread(SENDER.feishu.send, chat_id,
                                    "⌛ 提问 10 分钟未回答, 我按默认假设继续。")
            # 必须 deny 并说清「别再问」: 放行的话 SDK 回一句「用户未回答」, 模型
            
            ASK_CUT.add(chat_id)        # 超时继续跑也算段落边界 (同下)
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "用户 10 分钟未回答 (人可能不在)。请**不要重复提问**, 按最合理的默认假设"
                    "继续执行, 并在最终回复里用一句话说明你假设了什么、用户想改可以直接说。")}}
        finally:
            ASK_PENDING.pop(chat_id, None)
            ASK_PARTIAL.pop(ask_id, None)
            ASK_DISPLAY.pop(ask_id, None)
        if card_mid:                            # 答完把按钮卡原地换成结果 (点了没反应的错觉来源)
            done_card = {"config": {"wide_screen_mode": True}, "elements": [
                {"tag": "markdown", "content": f"✅ **已回答**: {answer if answer else '(已取消)'}"}]}
            await asyncio.to_thread(SENDER.feishu.update_card, card_mid, done_card)
        if answer is None:                      # 用户发「停」等 → 取消提问
            APP_FRONT.broadcast({"ev": "ask_cancel", "ask_id": ask_id,
                                 "chat_id": ui_chat, "reason": "cancelled"})
            return {}
        # 正常作答也要广播清除 —— 否则其他在线客户端 (及网关的未决缓存) 的卡片一直挂着。
        # ⚠ **这条会和网关那条重复** (可能在短时间内收到相同的两帧):
        # 网关收到作答时**自己也 fanout 一条** (server/main.py 的 ws answer 分支与 /api/answer,
        # 注释写着「别等 bridge 回 ask_cancel」—— 那是为了 bridge 已重启/超时的情况兜底)。
        # **两条都保留**: 各自覆盖的失效场景不同 (它兜 bridge 死了, 这条兜网关没参与的路径,
        # 如飞书卡按钮/超时/取消), 去掉任何一条都会丢一半覆盖。
        # 代价写进协议头: ask_cancel **可能重复到达, 客户端必须按 ask_id 幂等处理**。
        APP_FRONT.broadcast({"ev": "ask_cancel", "ask_id": ask_id,
                             "chat_id": ui_chat, "reason": "answered"})
        # 提问是段落边界: 让 run_turn 在下一段文本前把提问前的内容定稿成独立消息,
        # 避免作答后重复发送旧内容。
        ASK_CUT.add(chat_id)
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"用户已通过卡片回答:\n{answer}\n"
                "请按该回答继续执行, 不要再次调用 AskUserQuestion 重复确认同一问题。")}}
    return _hook


try:
    from feishu_bridge import fleet_worktree as _fleet_wt
except Exception:  # noqa: BLE001 - the guard is optional; a broken import must not take the bridge down
    _fleet_wt = None


def _main_tree_guard_matcher() -> HookMatcher:
    """Deny Edit/Write into a worktree-enabled main tree (contracts/task_worktree.md).

    The bridge main tree is the live code of every bot, and fleet-app's is three bots' shared
    tree: one relative-path Edit there ships on the next restart. Fails open — a bug in the
    guard must cost a missed denial, never a broken turn (fail-soft rule).
    """
    async def _hook(input_data, tool_use_id, context):  # noqa: ANN001, ARG001
        try:
            why = _fleet_wt.main_tree_denial(str((input_data or {}).get("tool_name") or ""),
                                             (input_data or {}).get("tool_input") or {}, REPO)
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ 主树守卫出错, 本次放行: {type(exc).__name__}: {exc}")
            return {}
        if why:
            log(f"⛔ 主树守卫拦下 {(input_data or {}).get('tool_name')}: "
                f"{((input_data or {}).get('tool_input') or {}).get('file_path', '')}")
            return _deny(why)
        return {}
    return HookMatcher(matcher="Edit|Write|MultiEdit|NotebookEdit", hooks=[_hook])


def _pretool_matchers(chat_id: str) -> list:
    return [_ask_hook_matcher(chat_id)] + ([_main_tree_guard_matcher()] if _fleet_wt else [])


def _ask_hook_matcher(chat_id: str) -> HookMatcher:
    """提问卡钩子的 matcher —— timeout 必须显式给, 不给就回落 CLI 默认 600s 撞车。"""
    return HookMatcher(matcher="AskUserQuestion", hooks=[make_ask_hook(chat_id)],
                       timeout=ASK_HOOK_TIMEOUT_SEC)


def _display_chat_for(chat_id: str) -> str:
    """当前回合的 UI 落点; 没有 group/display 上下文时就是真实 bot 会话。"""
    return app_frontend.DISPLAY_CHAT.get() or chat_id


def _render_ask(inp: dict | None) -> str:
    """AskUserQuestion 的问题+选项 → 飞书卡片文本。

    SDK 环境无交互 UI, 该工具立即返回「用户未回答」, 模型会按假设继续 —— 所以这张卡
    是**知情用**: 让用户看到它问了什么、想选什么, 用户可直接下一条消息回答。
    """
    qs = (inp or {}).get("questions") or []
    if not qs:
        return "❓ 我有个问题想问你, 但没取到内容。"
    out = ["❓ **我需要你定一下**（直接回消息作答即可, 我会等你 10 分钟）", ""]
    for i, q in enumerate(qs, 1):
        out.append(f"**{i}. {q.get('question', '')}**")
        for o in q.get("options", []) or []:
            desc = (o.get("description") or "").strip()
            out.append(f"   ▸ **{o.get('label', '')}**" + (f" — {desc[:70]}" if desc else ""))
        out.append("")
    out.append("_(10 分钟没回复的话, 我按最合理的方案继续。)_")
    return "\n".join(out)


_TASK_STATUS_EMOJI = {"completed": "✅", "failed": "❌", "cancelled": "🛑",
                      "error": "❌", "timeout": "⌛"}


def _task_done_brief(msg) -> str:
    """子代理完成 (TaskNotificationMessage) → 一行收尾摘要."""
    status = getattr(msg, "status", "") or "?"
    emoji = _TASK_STATUS_EMOJI.get(status, "•")
    summary = (getattr(msg, "summary", "") or "").strip()
    if len(summary) > 120:
        summary = summary[:120] + "…"
    tid = (getattr(msg, "task_id", "") or "")[:6]
    return f"{emoji} 子代理[{tid}] {status}" + (f": {summary}" if summary else "")


_DOUBAO_ASR_KEY = _env("DOUBAO_ASR_KEY").strip()   # 火山引擎 API Key; 空=不用豆包


def _transcribe_doubao(audio_path: str) -> str | None:
    """豆包·大模型录音文件识别极速版 (免费试用 20h): 同步一次调用秒回。
    任一步失败返回 None → 上层直接失败 (本地 无本地转写回落)。"""
    import base64
    import subprocess
    import urllib.request
    import uuid
    try:
        # ffmpeg 统一转 16k 单声道 wav (飞书语音是 opus, 直传格式参数容易踩坑)
        wav = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", audio_path, "-ar", "16000", "-ac", "1",
             "-f", "wav", "pipe:1"],
            capture_output=True, timeout=30).stdout
        if len(wav) < 100:
            raise RuntimeError("ffmpeg 转码输出为空")
        body = {"audio": {"data": base64.b64encode(wav).decode(),
                          "format": "wav", "rate": 16000, "bits": 16, "channel": 1},
                "request": {"model_name": "bigmodel",
                            "enable_itn": True, "enable_punc": True}}
        req = urllib.request.Request(
            "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "X-Api-Key": _DOUBAO_ASR_KEY,
                     "X-Api-Resource-Id": "volc.bigasr.auc_turbo",
                     "X-Api-Request-Id": str(uuid.uuid4()),
                     "X-Api-Sequence": "-1"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            code = resp.headers.get("X-Api-Status-Code", "")
            data = json.loads(resp.read())
        if code and code != "20000000":
            raise RuntimeError(f"status={code} {resp.headers.get('X-Api-Message', '')}")
        text = ((data.get("result") or {}).get("text") or "").strip()
        return text or None
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ 豆包转写失败 ({exc})")   # 无本地转写回落
        return None


def _transcribe(audio_path: str) -> str | None:
    """使用豆包语音识别；未配置密钥或识别失败时返回 None，不使用本地回落。"""
    if not _DOUBAO_ASR_KEY:
        log("⚠ 未配置豆包 ASR Key, 无法转写语音 (无本地转写回落)")
        return None
    text = _transcribe_doubao(audio_path)
    if text:
        log(f"🎙 豆包转写 {len(text)} 字")
        return text
    log("⚠ 豆包转写失败, 且已无本地回落")
    return None


def _streaming_card(summary: str = "") -> dict:
    """CardKit 2.0 流式卡骨架: answer (可见) + 折叠 thinking 面板, 三个 element_id.

    summary = 会话列表/推送里显示的预览文本 (设成本轮问题, 免得列表只见「生成中」)。
    """
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "streaming_config": {"print_frequency_ms": {"default": 70},
                                 "print_step": {"default": 2}, "print_strategy": "fast"},
            "summary": {"content": (f"💬 {summary}" if summary else "[生成中…]")},
        },
        "body": {"elements": [
            {"tag": "markdown", "element_id": "answer", "content": ""},
        ]},
    }


class _Stream:
    """一个 turn 的 CardKit 流式会话. 任一步失败 → ok=False, 上层回退一次性整卡."""

    def __init__(self, sender: "FeishuSender", chat_id: str,
                 reply_to: str | None = None, summary: str = "") -> None:
        self._s = sender
        self._chat = chat_id
        self._reply_to = reply_to        # 触发消息 id → 流式卡挂到它气泡上 (回复式)
        self._summary = summary          # 会话列表/推送预览文本 (本轮问题)
        self.ok = False
        self.card_id: str | None = None
        self.message_id: str | None = None
        self._seq = itertools.count(1)
        self._last = 0.0
        self._putlen = -1

    async def start(self) -> bool:
        self.card_id = await asyncio.to_thread(
            self._s.create_stream_card, _streaming_card(self._summary))
        if not self.card_id:
            return False
        self.message_id = await asyncio.to_thread(
            self._s.send_card_by_id, self._chat, self.card_id, self._reply_to)
        if not self.message_id:              # 绑卡消息偶发 400 → 等半秒重试一次
            await asyncio.sleep(0.5)
            self.message_id = await asyncio.to_thread(
                self._s.send_card_by_id, self._chat, self.card_id, self._reply_to)
        self.ok = bool(self.message_id)
        return self.ok

    @property
    def delivered_chars(self) -> int:
        """已成功写进卡片的答案长度 (卡死时据此判断内容是否基本已送达)。"""
        return max(self._putlen, 0)

    async def put_answer(self, full: str, force: bool = False) -> None:
        if not self.ok:
            return
        now = time.monotonic()
        if not force and (now - self._last < _STREAM_MIN_INTERVAL
                          or len(full) - self._putlen < _STREAM_MIN_DELTA):
            return
        if await asyncio.to_thread(self._s.stream_put, self.card_id, "answer",
                                   md_to_feishu(full), next(self._seq)):
            self._last = now
            self._putlen = len(full)
        else:
            self.ok = False

    async def put_thinking(self, lines: list[str]) -> None:
        # 飞书卡不显示子代理面板。
        return

    async def finish(self, full: str, lines: list[str]) -> bool:
        """定稿最终内容。返回是否真送达 —— 卡失效 (长回合/久等提问超 TTL) 时终写会
        静默失败。返回 False
        让上层走整卡兜底重发。"""
        if not self.ok:
            return False
        if not await asyncio.to_thread(self._s.stream_put, self.card_id, "answer",
                                       md_to_feishu(full or "(无内容)"), next(self._seq)):
            self.ok = False
            return False               # 答案区写失败 = 卡已死, 别再往下写, 交上层重发
        # (子代理面板已移除, 不再写 thinking 元素)
        gist = _answer_gist(full)        # 定稿把预览从「生成中」更新成答案摘要
        await asyncio.to_thread(self._s.stream_finish, self.card_id, next(self._seq), gist)
        return True


class FeishuTransport:
    """`transport.Transport` 的飞书实现 —— 平台专有调用全部收口在这一层。

    只做「翻译」不做逻辑: 平台中立动作 → 飞书 API 序列 (如 send_media_file 内部完成
    upload 拿 key + send 的两步式)。核心逻辑不再出现 image_key / reaction / card_id
    这类飞书名词; 换前端只需另写一个同接口的类, 核心一行不改。

    `__getattr__` 将未显式适配的调用转发给 FeishuSender 并记录警告。
    """

    def __init__(self, app_id: str, app_secret: str, domain: str) -> None:
        self._s = FeishuSender(app_id, app_secret, domain)

    def __getattr__(self, name: str):
        attr = getattr(self._s, name)     # 无此属性时照常抛 AttributeError
        log(f"⚠ Transport 抽象泄漏: 核心直接调用了飞书专有方法 `{name}` (应收进 Transport)")
        return attr

    # ── 发送 ──────────────────────────────────────────────────────
    def send(self, chat: str, text: str) -> None:
        self._s.send(chat, text)

    def send_text(self, chat: str, text: str) -> None:
        self._s.send_text(chat, text)

    def send_reply(self, chat: str, answer_md: str, thinking_lines: list[str],
                   reply_to: str | None = None, extra: dict | None = None) -> str | None:
        return self._s.send_reply(chat, answer_md, thinking_lines, reply_to)

    def send_media_file(self, chat: str, path: str, kind: str) -> bool:
        """发本地图片/视频。飞书要求先上传拿 key 再发, 两步都藏在这里。"""
        if kind == "file":
            # 原来只分 video 与"其他", "其他"一律**当图片上传** —— xlsx 会被当图片传,
            # 失败后报"发送失败"。飞书这条 sender 本来就发不了文件; 说一声去哪儿取。
            # 返回成功: App 那侧照常由网关收库 (§2.24)。
            self._s.send_text(chat, f"📎 {Path(path).name} (飞书发不了文件, 到 App 里下载)")
            return True
        if kind == "video":
            key = self._s.upload_video(path)
            return bool(key) and self._s.send_media(chat, key)
        key = self._s.upload_image(path)
        return bool(key) and self._s.send_image(chat, key)

    # ── 接收侧辅助 ────────────────────────────────────────────────
    def download_incoming(self, message_id: str, key: str, kind: str,
                          suffix: str) -> str | None:
        return self._s.download_resource(message_id, key, kind, suffix)

    def fetch_message_text(self, message_id: str) -> str | None:
        return self._s.get_message(message_id)

    # ── 状态指示 ──────────────────────────────────────────────────
    def begin_busy(self, message_id: str) -> str | None:
        """飞书用「Typing 表情」表示处理中; 返回 reaction_id 当句柄。"""
        return self._s.add_reaction(message_id, "Typing")

    def end_busy(self, message_id: str, handle: str) -> None:
        self._s.remove_reaction(message_id, handle)

    def pin(self, message_id: str) -> bool:
        return self._s.pin_message(message_id)

    def unpin(self, message_id: str) -> bool:
        return self._s.unpin_message(message_id)

    # ── 流式 ──────────────────────────────────────────────────────
    def open_stream(self, chat: str, reply_to: str | None = None,
                    summary: str = "") -> "_Stream":
        return _Stream(self._s, chat, reply_to, summary)

    def _post_card(self, chat_id: str, card: dict, reply_to: str | None = None) -> str | None:
        return self._s._post_card(chat_id, card, reply_to)   # 提问按钮卡 (正名转发, 免 __getattr__ 告警)

    def update_card(self, message_id: str, card: dict) -> bool:
        return self._s.update_card(message_id, card)


class _NullStream:
    """纯 App 模式的流式桩: ok=True 让核心照常流式, 输出全靠 Tee 广播给网关。"""

    ok = True
    message_id = None

    async def start(self) -> bool:
        return True

    async def put_answer(self, full: str, force: bool = False) -> None:
        pass

    async def put_thinking(self, lines: list[str]) -> None:
        pass

    async def finish(self, full: str, lines: list[str]) -> None:
        pass


class NullTransport:
    """纯 App 模式的前端桩 (Transport 实现): 没有飞书, 所有平台输出都是 no-op。

    真实出口是 TeeTransport 的广播旁路 — App 是唯一前端时, 旁路就是主路。
    """

    def send(self, chat: str, text: str, kind: str = "") -> None: ...
    def send_text(self, chat: str, text: str) -> None: ...
    # 提问卡走飞书交互卡; 纯 App 模式没有那一端, 但**必须存在**这两个方法 ——
    # 缺了会让提问钩子抛 AttributeError → 不阻塞 → 工具当场返回「用户未作答」,
    
    def _post_card(self, chat_id: str, card: dict, reply_to: str | None = None) -> None:
        return None            # 无 message_id → 钩子走「发卡失败」兜底分支, 照常阻塞等答案

    def update_card(self, message_id: str, card: dict) -> bool:
        return True

    def send_reply(self, chat: str, answer_md: str, thinking_lines: list[str],
                   reply_to: str | None = None, extra: dict | None = None) -> str | None:
        return None

    def send_media_file(self, chat: str, path: str, kind: str) -> bool:
        return True

    def download_incoming(self, message_id: str, key: str, kind: str, suffix: str) -> str | None:
        return None

    def fetch_message_text(self, message_id: str) -> str | None:
        return None

    def begin_busy(self, message_id: str) -> str | None:
        return None

    def end_busy(self, message_id: str, handle: str) -> None: ...

    def pin(self, message_id: str) -> bool:
        return False

    def open_stream(self, chat: str, reply_to: str | None = None, summary: str = "") -> _NullStream:
        return _NullStream()


_TODO_EMOJI = {"completed": "✅", "in_progress": "🔄", "pending": "⬜"}

# 模型偶发把工具调用「说」成文本 (malformed function calling) → 泄漏进回复.
# 匹配 harness 工具调用起始标记 (antml/invoke/function/parameter), 截掉其后全部.
_LEAK_RE = re.compile(r"<\s*/?\s*(?:antml:)?(?:invoke|function|parameter|tool_call)\b", re.I)


def _strip_leaked_toolcall(text: str) -> str:
    """截掉回复里泄漏的原始工具调用语法 (标记及其后全是废内容). 无泄漏则原样返回."""
    if not text:
        return text
    m = _LEAK_RE.search(text)
    if not m:
        return text
    cut = text[:m.start()].rstrip()
    # 泄漏标记前常有一行残词 (如 "count") → 仅当末行是纯 ASCII 短裸词才去掉 (保留中文正文)
    lines = cut.split("\n")
    tail = lines[-1].strip() if lines else ""
    if 0 < len(tail) <= 12 and " " not in tail and tail.isascii():
        lines.pop()
    return "\n".join(lines).rstrip()


# 回复里的发图/发视频标记: 单独一行 [[图片: /绝对路径]] / [[视频: /绝对路径]] (也认 image/video).
# 提取后从文本剥离, 转成图片/视频消息.
_MEDIA_MARK_RE = re.compile(
    r"^[ \t]*\[\[(图片|image|视频|video|文件|file)[:：]\s*(.+?)\s*\]\][ \t]*$", re.M | re.I)


def _file_source_refusal(p: Path) -> str:
    """`[[文件:]]` 的来源路径规则 (契约 §2.24) —— **只在桥判**, 因为只有桥见过原始路径。

    须在 `$HOME`、`/tmp` 或**这个 bot 自己的仓目录** (`REPO`) 下, 且路径里**不含 `.` 开头的段**。
    bot 可能被提示注入,
    这道挡的是 `~/.ssh` `~/.aws` `~/.claude` `~/.fleet-*` 这类地方。
    ⚠ 不放宽到 Mac 的 `/var/folders` (tempfile 默认位置): 网关本机链路只认 $HOME 与 /tmp,
      两边不一致就会"桥放行了、网关又拒了"。改在给 bot 的说明里讲清放哪儿。
    ⚠ 类型/大小**不在这里判** —— 那份白名单只住网关 (files.py), 两份迟早分叉。
    返回空串 = 放行; 否则是给人看的拒绝原因。
    """
    try:
        rp = p.resolve()
    except Exception:  # noqa: BLE001
        return "路径无法解析"
    bases = []
    for raw in (Path.home(), Path("/tmp"), Path(REPO) if REPO else None):
        try:
            if raw is not None:
                bases.append(raw.resolve())
        except Exception:  # noqa: BLE001
            pass
    for base in bases:
        if rp.is_relative_to(base):
            if any(part.startswith(".") for part in rp.relative_to(base).parts):
                return "在 . 开头的目录里"
            return ""
    return "不在 $HOME、/tmp 或你的仓目录下"


def _extract_media_markers(text: str) -> tuple[str, list[tuple[str, str]]]:
    """抽出媒体标记 → (剥离后的文本, [(kind, path)]). kind ∈ {image, video, file}; 路径无效原地提示."""
    items: list[tuple[str, str]] = []

    def _sub(m: re.Match) -> str:
        tag = m.group(1).lower()
        kind = "video" if tag in ("视频", "video") else "file" if tag in ("文件", "file") else "image"
        p = Path(m.group(2)).expanduser()
        if kind == "file" and p.is_file():
            why = _file_source_refusal(p)
            if why:     # 拒绝要看得见: 原地换成一句说明, 不许静默吞掉
                log(f"⚠ 文件标记被拒 ({why}): {p}")
                return f"(⚠ 这个文件不允许发送: {p.name} —— {why})"
        if p.is_file():
            items.append((kind, str(p)))
            return ""
        return f"(⚠ 文件不存在: {m.group(2)})"

    return _MEDIA_MARK_RE.sub(_sub, text or ""), items


# 置顶标记: 回复里单独一行 [[置顶]] (也认 pin) → 该条回复发出后 Pin 到会话顶部.
# [[置顶:<key>]] → **同 key 顶替、不同 key 共存** (与 fleet_notify 的 --pin-key 同语义)。
# 无 key 的 [[置顶]] **保持原样**: 纯 pin 不撤旧 —— 现有拿它钉多条重要记录的用法不受影响,

# 无 key 连发已经在顶部堆了 4 条, 那类才需要 key)。
_PIN_MARK_RE = re.compile(r"^[ \t]*\[\[(?:置顶|pin)(?::([^\]\s]{1,40}))?\]\][ \t]*$",
                          re.M | re.I)


def _extract_pin_marker(text: str) -> tuple[str, bool, str]:
    """抽出 [[置顶]] / [[置顶:key]] → (剥离后的文本, 是否置顶, key)。无 key 时 key=""。"""
    if not text:
        return text, False, ""
    m = _PIN_MARK_RE.search(text)
    if not m:
        return text, False, ""
    return _PIN_MARK_RE.sub("", text), True, (m.group(1) or "")


def _pin_state_path(key: str) -> Path:
    """key → state 文件。命名遵循共享通知状态约定 (同目录同规则), 这样两条
    置顶路径的「谁是活的」是同一份登记, 孤儿清理不会互相误伤。"""
    safe = "".join(c for c in key if c.isalnum() or c in "-_") or "default"
    return _BRIDGE_HOME / f"pin_state_{safe}.json"


def _pin_keyed(mid: str, key: str) -> None:
    """置顶 mid; 带 key 时先登记再撤同 key 上一条。全程 fail-soft, 绝不掀翻本轮。

    顺序是 **pin 新 → 写登记 → 撤旧**: 中间任何一步炸掉, 最坏是顶部多留一条旧的
    (看得见, 用户/下次运行能收拾), 而不是新的没上去或者两条都没了。
    """
    try:
        if not SENDER.pin(mid):
            return
        if not key:                    # 无 key: 历史行为, 纯 pin 不撤旧
            return
        sp = _pin_state_path(key)
        try:
            prev = json.loads(sp.read_text(encoding="utf-8")).get("message_id")
        except Exception:  # noqa: BLE001
            prev = None                # 登记坏了当没有旧的, 不阻断本次置顶
        try:
            tmp = sp.with_name(sp.name + ".tmp")
            tmp.write_text(json.dumps({"message_id": mid}), encoding="utf-8")
            tmp.replace(sp)
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ 写置顶登记失败 (不影响本轮): {exc}")
        if prev and prev != mid:
            SENDER.unpin(prev)
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ 置顶处理异常 (不影响本轮): {exc}")


def _send_local_media(chat_id: str, kind: str, path: str) -> None:
    """发送一个本地图片/视频; outbox 里的临时文件发完即删 (其他路径不动)."""
    what = {"video": "视频", "file": "文件"}.get(kind, "图片")
    # Reject missing or empty media before either transport reports success.
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size == 0:
            log(f"⚠ 出站媒体未发送 (文件不存在或为空): {path}")
            SENDER.send_text(chat_id, f"⚠ {what}发送失败 (文件不存在或为空): {p.name}")
            return
    except Exception as exc:  # noqa: BLE001  (校验本身出错不该把媒体拦下来)
        log(f"⚠ 出站媒体前置校验异常, 照常发送: {exc}")
    ok = SENDER.send_media_file(chat_id, path, kind)
    if not ok:
        SENDER.send_text(chat_id, f"⚠ {what}发送失败: {Path(path).name}")
    try:
        p = Path(path).resolve()
        if ok and p.is_relative_to(_OUTBOX_DIR.resolve()):
            p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _cleanup_media(days: int = _MEDIA_KEEP_DAYS) -> None:
    """启动时清理 media/ (含 outbox) 里超过 N 天的文件, 防止收发图长期攒盘."""
    cutoff = time.time() - days * 86400
    n = 0
    try:
        for p in _MEDIA_DIR.rglob("*"):
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                n += 1
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ media 清理异常: {exc}")
    if n:
        log(f"🧹 media 清理: 删除 {n} 个超过 {days} 天的文件")


_COMPACT_DONE_TEXT = "🗜 上下文压缩完成 (旧细节折叠为摘要, 落盘内容不受影响)。"


def _merge_reply_meta(fail: dict, compact_done: bool) -> dict:
    """复制回复元数据；压缩完成时标记 kind，保留 reply 作为回合终结事件。"""
    return {**fail, "kind": "compact_done"} if compact_done else dict(fail)


def _app_seal(text: str, extra: dict | None = None) -> None:
    """流式卡终写失败时补发 App reply 事件，确保客户端结束进行中状态。"""
    try:
        APP_FRONT.broadcast({"ev": "reply", "text": text, **(extra or {})})
    except Exception:  # noqa: BLE001  (收尾广播失败不能反过来掀翻回合)
        pass


def _app_pin_last(want_pin: bool, key: str, mid: str | None, text: str = "x") -> None:
    """向 App 广播置顶意图。网关根据最后一条回复定位；空回复不置顶，避免误指上一条。"""
    if not want_pin:
        return
    if not (text or "").strip():
        # 空正文 → 网关不会把它落进 history → `history[-1]` 是**上一轮**的东西,
        
        
        # 而 **pin 错了用户看到的是一条莫名其妙的置顶, 更可能被当成「App 抽风」而不报**。
        # 所以宁可不 pin, 且留一行。
        log(f"[[置顶]] 跳过: 这轮正文是空的 (key={key or '-'}) —— "
            f"置顶空回复会钉到上一轮那条")
        return
    try:
        APP_FRONT.broadcast({"ev": "pin_last", "key": key})
        log(f"已请求 App 置顶上一条回复 (key={key or '-'}, 飞书 mid={mid or '无'})")
    except Exception as exc:  # noqa: BLE001  (置顶是附加动作, 不许反过来掀翻这一轮)
        # **必须留痕**: 这次 bug 躺着没被发现的全部原因就是"失败时一个字都没有"。
        log(f"App 置顶广播失败 ({type(exc).__name__}: {exc}) —— 这条回复没能置顶")


def _thinking_signal(ev: dict, active_idx: int | None) -> tuple[dict | None, int | None]:
    """根据 thinking 块的开始和结束事件生成状态信号；沿用 reasoning 事件以兼容旧客户端。"""
    kind = ev.get("type")
    if kind == "content_block_start" and (ev.get("content_block") or {}).get("type") == "thinking":
        if active_idx is not None:
            return None, active_idx          # 已经亮着就别再发一遍 (客户端会闪)
        return {"ev": "reasoning", "text": "", "active": True}, ev.get("index")
    if kind == "content_block_stop" and active_idx is not None and ev.get("index") == active_idx:
        return {"ev": "reasoning", "text": "", "active": False}, None
    return None, active_idx


_EMPTY_THINKING_NOTED = False    # 空思考块只提示一次 (每块都说 = 刷屏 = 等于没说)


def _note_empty_thinking() -> None:
    """首次收到空 thinking 块时记录提示，说明前端没有可显示的文本。"""
    global _EMPTY_THINKING_NOTED
    if _EMPTY_THINKING_NOTED:
        return
    _EMPTY_THINKING_NOTED = True
    try:
        
        # 同课)。写死「本模型不给明文」是当下的**断言** —— 哪天模型给了明文, 这行不再触发,
        # 但如果它曾把解释写成事实, 读旧日志的人会被一句过期的断言带偏。
        log("💭 收到 thinking 块但文本为空 (只有 signature) —— 前端的思考面板不会有内容, "
            "不是没实现也不是坏了。当前的解释: 这个模型的思考不出明文")
    except Exception:  # noqa: BLE001  (留痕是旁路)
        pass


def _broadcast_todos(todos: list, replay: bool = False, chat: str = "",
                     ts: int = 0) -> None:
    """广播任务清单。replay 标记状态对齐；ts 表示清单更新时间而非广播时间。"""
    try:
        ev = {"ev": "todos", "items": [
            {"status": t.get("status"), "content": t.get("content")}
            for t in todos if isinstance(t, dict)]}
        if replay:
            ev["replay"] = True
        if ts:
            # **这张卡最后更新的时刻**, 不是广播时刻:
            # 重连会重发同一张卡, 拿广播时刻当更新时刻 ⇒ 一份早做完的旧清单
            # **永远刚刚更新**, 会让重播的全完成清单凭空造 live，属于
            # 机制原样重来, 只是换了个字段。
            # ⚠ 拿不到就**不发这个字段**, 别塞 0 —— 收方的规则是"没有就不猜",
            # 塞 0 会变成一张 1970 年的卡, 而那看起来完全正常。
            ev["ts"] = ts
        if chat:
            # 显式指定会话 —— 扇出点见到已有的 chat_id 就不再盖章 (app_frontend._with_chat)。
            # 任务卡是**一张共享卡**: 同一份合并清单播给每个活着的会话, 所以这里必须逐个指定,
            # 不能靠 CUR_CHAT (轮询循环在回合之外跑, 那时它是空的 ⇒ 恒落私聊)。
            ev["chat_id"] = chat
        APP_FRONT.broadcast(ev)
    except Exception:  # noqa: BLE001
        pass


def _card_chats() -> list[str]:
    """返回所有活跃或持久化会话，以便广播和清理共享任务卡。"""
    chats = set(CLIENTS) | set(_TASK_TRACK) | _known_session_chats()
    if APP_FRONT.chat_id:
        chats.add(APP_FRONT.chat_id)
    return sorted(c for c in chats if c and not c.startswith("_"))


def _known_session_chats() -> set[str]:
    '读取持久化会话以清理断连会话的任务卡。读取失败时返回空集合。'
    try:
        raw = json.loads(_fs.SESSIONS_FILE.read_text(encoding="utf-8"))
        return {k for k in raw if isinstance(k, str)} if isinstance(raw, dict) else set()
    except Exception:  # noqa: BLE001
        return set()


def _task_track_mtime() -> int:
    """任务卡最后更新的时刻 (epoch 秒)。读不到 ⇒ 0 ⇒ 帧上不带这个字段。

    ⚠ 用**文件 mtime**, 不是 `time.time()`: 重连时这张卡会被原样重发, 拿广播时刻
    当更新时刻等于让一份早就做完的旧清单"永远刚刚更新"。
    """
    try:
        return int(_TASK_TRACK_FILE.stat().st_mtime)
    except Exception:  # noqa: BLE001
        return 0


def _push_task_card() -> bool:
    """把**合并后的**清单播给每个活着的会话。fail-soft, 任何异常只影响这一次推送。

    合并顺序用 `task_card.merged` —— **跟写卡的脚本共用同一个函数**,
    因为用户看到的序号和 `done N` 认的序号必须是同一套 (各写一份迟早漂开,
    而漂开的表现是"我说 done 5, 划掉的是第 6 条")。
    """
    try:
        # `_chat` 只服务 CLI 与 start/done 的写回, **不发给客户端**: 合并已按内容去重,
        # 一条可能同时来自两个会话, 再标「私聊」/「# 求职」就是假的归属。
        # 用户 「任务的状态是跟着 bot 走的, 和私聊还是群无关。」
        items = [{k: v for k, v in it.items() if k != "_chat"}
                 for it in task_card.merged(_TASK_TRACK)]
        ts = _task_track_mtime()      # 一次算好 —— 同一张卡播给几个会话, ts 必须一样
        for c in _card_chats():
            _broadcast_todos(items, replay=True, chat=c, ts=ts)
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ 推任务卡失败 (不影响这一轮): {exc}")
        return False


def _render_todos(todos: list) -> str:
    """任务清单 → 飞书勾选卡文本 (✅完成/🔄进行/⬜待办)。

    兼容两种来源: 旧 TodoWrite 的 content/activeForm, 与当前 Task* 系列
    (TaskCreate 的 subject 也归一化成 content, 见采集处)。
    """
    if not todos:
        return ""
    lines = []
    for t in todos:
        if not isinstance(t, dict):
            continue
        st = t.get("status", "pending")
        # in_progress 优先显示 activeForm (进行时态), 否则 content
        text = (t.get("activeForm") if st == "in_progress" else None) or t.get("content") or ""
        lines.append(f"{_TODO_EMOJI.get(st, '⬜')} {text}")
    total = sum(1 for t in todos if isinstance(t, dict))
    done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
    return f"📋 **任务清单** ({done}/{total})\n" + "\n".join(lines)


_STALE_CARD_AFTER_S = 30 * 60      # 卡多久没动过算"放旧了"
_STALE_CARD_MARK = "[任务卡提醒]"   # 单独一行, 与 [消息时间 ] 一样要从消息标签里滤掉


def _stale_card_note() -> str:
    """任务卡长时间未更新且仍有进行中条目时提示检查。依据文件 mtime，带上条目内容；异常时返回空串。"""
    try:
        age = time.time() - _TASK_TRACK_FILE.stat().st_mtime
        if age < _STALE_CARD_AFTER_S:
            return ""
        # ⚠ 键是 `content` —— `task_card.py` 写盘就是这个键:
        # 原来只读 `text` ⇒ `running` 恒为 [""], **而列表非空所以提醒照发**,
        # 只是「」里什么都没有。坏掉的正是下面 docstring 那条边界(带上原文),
        # 而它坏了三天没人发现: 空引号看起来就像正常排版。
        # `text` 留作兜底, 万一别处还有旧格式。
        running = [t.get("content") or t.get("text") or "" for st in _TASK_TRACK.values()
                   for t in (st or {}).get("items", [])
                   if isinstance(t, dict) and t.get("status") == "in_progress"]
        if not running:
            return ""
        head = running[0][:40]
        more = f" (另有 {len(running) - 1} 条也挂着)" if len(running) > 1 else ""
        # ⚠ 问的是**整张卡**不是那一条:
        # 几天来收方都只答"这一条准不准"然后补一条状态进去 ⇒ mtime 更新、提醒消停,
        # 而**整张作废的框架没人重看**。问句决定了收方去看什么。
        return (f"\n{_STALE_CARD_MARK} 这张卡还是当前这件事吗? "
                f"已 {int(age // 60)} 分钟没更新, 仍挂着「{head}」(进行中){more} —— "
                f"不是了就整张重列; 还是的话把进度划到位。")
    except Exception:  # noqa: BLE001  (提醒失败绝不能影响这一轮)
        return ""


def _route_marker(display_chat: str) -> str:
    """Model-visible per-message route fields; display_chat was already validated upstream."""
    is_group = display_chat.startswith("group:")
    payload = {
        "is_group": is_group,
        "chat_id": display_chat,
        "group": display_chat[6:] if is_group else "",
    }
    return "[会话路由 " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "]"


def _stamp(text: str, arrived: float | None = None, display_chat: str | None = None) -> str:
    """给用户消息打上**送达时刻**的时间戳 (BJT), 让 Claude 知道你什么时候发的。

    用送达时刻而非处理时刻 —— 否则 bot 忙 20min 后才处理, 就丢掉了「用户已等 20min」
    这个关键信息 (用户常因为久等没回复才追发一条, bot 得能看出来)。
    每条都带 → 模型可自行比较相邻消息算出间隔。
    """
    ts = datetime.fromtimestamp(arrived or time.time(),
                                timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    route = display_chat if display_chat is not None else app_frontend.DISPLAY_CHAT.get()
    # 飞书/旧调用方没有 display context 时, chat_id 的私聊键由 handle_message 显式传入;
    # 单测直接调 _stamp 则留空, 仍明确表达 private, 不编一个会话名。
    return (f"[消息时间 {ts} BJT]{_stale_card_note()}\n"
            f"{_route_marker(route)}\n{text}")


def _msg_label(prompt: str, limit: int = 24) -> str:
    """从触发 prompt 取一句人类可读标签 (图片/引用包装文已处理). 空则空串。"""
    if not prompt:
        return ""
    lines = [l for l in prompt.strip().splitlines()
             if not l.startswith("[消息时间 ")
             and not l.startswith("[会话路由 ")
             and not l.startswith(_STALE_CARD_MARK)
             and not l.startswith("[自动换新会话]")]
    label = (lines[0].strip() if lines else "")
    if label.startswith("用户发来一张图片"):        # handle_image 的包装文
        label = "你发的图片"
    elif label.startswith("[用户回复并引用"):        # 引用块包装文, 跳到真正正文
        parts = prompt.strip().splitlines()
        label = next((l.strip() for l in parts if l.strip()
                      and not l.startswith(("[", ">"))), label)
    return label[:limit] + "…" if len(label) > limit else label


def _quote_prefix(prompt: str) -> str:
    """回复顶部的「答哪条」引用行 — 消除串行处理下回复迟到看着像答上一条的错觉。空则不加。"""
    label = _msg_label(prompt)
    return f"↩︎ 答:「{label}」\n\n" if label else ""


def _answer_gist(answer: str, limit: int = 30) -> str:
    """从最终答案取一句摘要做卡片预览 (跳过 markdown 标题符/emoji 行首). 空则回落。"""
    for raw in (answer or "").splitlines():
        line = raw.strip().lstrip("#*>-•✅⚠️🤖📋↩︎ ").strip()
        if len(line) >= 4:
            return line[:limit] + "…" if len(line) > limit else line
    return ""


def interrupted_flag(chat_id: str) -> bool:
    """本轮是否被用户中断 (自愈重读的排除项: 中断本就该零产出, 不能再等一轮)。"""
    return chat_id in INTERRUPTED


@contextlib.contextmanager
def _busy_guard(chat_id: str):
    """为回合设置忙碌标志。所有调用路径共享守卫，且只清理由本次调用设置的标志。"""
    took = chat_id not in ACTIVE
    if took:
        ACTIVE.add(chat_id)
    try:
        yield took
    finally:
        if took:
            ACTIVE.discard(chat_id)


async def run_turn(chat_id: str, prompt: str, message_id: str | None = None,
                   plan: bool = False) -> None:
    if _MANAGED_ENTRY is not None:
        _MANAGED_ENTRY.require_turn(chat_id, plan)
    continuation = _QUESTION_CONTINUATION.get()
    if continuation is None:
        CODEX_QUESTIONS.cancel_work(chat_id, "new_turn")
    else:
        _question_dispatch_guard(continuation)
    with _busy_guard(chat_id):
        engine = "codex" if _runtime_snapshot()[2] == "codex" else "claude"
        if continuation is not None and engine != "codex":
            raise RuntimeError("Conversational card cannot change engine")
        t0 = time.monotonic()
        outcome = "异常退出"
        prev_display = TURN_DISPLAY.get(chat_id)
        TURN_DISPLAY[chat_id] = _display_chat_for(chat_id)
        try:
            if engine == "codex":
                await _run_turn_codex(chat_id, prompt, message_id, plan)
            else:
                await _run_turn_inner(chat_id, _rollover_first_turn(chat_id, prompt), message_id, plan)
            outcome = "结束"
        except asyncio.CancelledError:
            outcome = "被取消"
            raise
        finally:
            if prev_display is None:
                TURN_DISPLAY.pop(chat_id, None)
            else:
                TURN_DISPLAY[chat_id] = prev_display
            # End marker: without it, "still running" and "long done" look identical in
            # bridge.log, and that log is what operators read to decide a bot is idle.
            with contextlib.suppress(Exception):
                log(f"[{chat_id[:8]}] ⏹ 回合{outcome} · {engine} · "
                    f"{time.monotonic() - t0:.1f}s")


def _codex_archived_binding(work: str, sid: str, runtime: str, *, storage):
    # This is the exact Scope used by this ordinary child command/env/config and
    # its effective-config gate. Parent CODEX_HOME is not the child namespace.
    if type(storage) is not _codex_storage.Scope:
        return None
    if not sid or _load_session(_session_chat_key(work, "codex")) != sid:
        return None
    if _question_runtime_binding() != runtime:
        return None
    try:
        if _codex_storage.select() != storage:
            return None
        _codex_storage.session_mapping(_fs.SESSIONS_FILE,
            _session_chat_key(work, "codex"), sid, storage)
        storage.transcript(sid)
        image = _codex_archived.physical_pin(CODEX_BIN)
        if image is None:
            return None
        configured = _codex_archived.namespace(str(storage.home), str(storage.home))
        if configured is None:
            return None
        namespace = hashlib.sha256(json.dumps((configured, str(storage.home),
            storage.namespace, storage.config_sha256, storage.identity,
            str(storage.desktop_home)), sort_keys=True).encode()).hexdigest()
        return _codex_archived.Binding(work, sid,
            hashlib.sha256(json.dumps((runtime, image), sort_keys=True).encode()).hexdigest(), namespace)
    except (_codex_archived.StateUnknown, _codex_storage.StorageRefused):
        return None


def _codex_archived_reply(chat_id: str, message_id: str | None, *, unknown: bool = False):
    text = ("⚠ 当前 Codex 旧会话的续接状态无法确认，已阻塞续接。"
            if unknown else "⚠ 当前 Codex 旧会话已被原生服务拒绝续接：会话已归档。")
    text += "旧记录与归档保留；不会自动重试、解除归档或开新会话。后续切换须按已审核范围处理。"
    mid = SENDER.send_reply(chat_id, text, [], reply_to=message_id,
                           extra={"failed": True, "retryable": False}) if SENDER else None
    _remember(mid, text)


async def _run_turn_codex(chat_id: str, prompt: str, message_id: str | None = None,
                          plan: bool = False) -> None:
    managed_entry = _MANAGED_ENTRY
    managed_selected = managed_entry is not None
    if not CODEX_ENABLED:
        answer = "⚠ 这台节点暂不开放 Codex 引擎。"
        if not plan and not message_id:
            answer = _quote_prefix(prompt) + answer
        mid = SENDER.send_reply(chat_id, answer, [], reply_to=message_id,
                                extra={"failed": True, "retryable": False}) if SENDER else None
        _remember(mid, answer)
        return
    model, effort, _engine = _runtime_snapshot()
    if not model.lower().startswith("gpt-"):
        model = DEFAULT_CODEX_MODEL
    session_key = _session_chat_key(chat_id, "codex")
    thread = _load_session(session_key) or ""
    original_thread = thread
    continuation = _QUESTION_CONTINUATION.get()
    if continuation is not None:
        _question_dispatch_guard(continuation)
        if thread != continuation.origin.binding.thread:
            raise RuntimeError("Conversational card cannot start a replacement thread")
    question_origin = None
    question_protocol = None
    question_runtime = _question_runtime_binding()
    resume_binding = ((thread, _fs.sessions_revision(), question_runtime, _display_chat_for(chat_id))
                      if thread and continuation is None else None)
    recovering = False
    pending_note = None
    usage: dict = {}
    exit_code: int | None = None
    err_text = ""
    provider_error = ""
    answer_text = ""
    reasoning_text = ""
    stream_prefix = "" if plan or message_id else _quote_prefix(prompt)
    t0 = time.monotonic()
    reaction_id = SENDER.begin_busy(message_id) if message_id else None
    proc: asyncio.subprocess.Process | None = None
    control: _CodexActiveTurn | None = None
    stderr_task: asyncio.Task | None = None
    startup_steer_task: asyncio.Task | None = None
    approvals = None
    permissions_probe = False
    early_events = []
    startup_usage_events = []
    usage_tracker = None
    managed_native = None
    try:
        if managed_selected:
            managed_native = managed_entry.native_child(chat_id, prompt, message_id, plan)
        storage = _codex_storage.select()
        policy = codex_approvals.approval_policy(CODEX_APPROVAL_POLICY)
        reviewer = codex_approvals.reviewer_params(CODEX_APPROVALS_REVIEWER)
        permissions_probe = codex_approvals.permissions_probe(
            CODEX_PERMISSIONS_PROBE, policy, reviewer, CODEX_SANDBOX)
        if permissions_probe:
            thread = ""  # Ephemeral test must not resume or overwrite the normal session.
        if continuation is not None and permissions_probe:
            raise RuntimeError("Conversational card cannot enter a permissions probe")
        if thread:
            storage.transcript(thread)
        if not permissions_probe:
            _codex_storage.session_mapping(_fs.SESSIONS_FILE, session_key, thread, storage)
        archive_binding = (None if permissions_probe else
                           _codex_archived_binding(chat_id, thread, question_runtime, storage=storage))
        if archive_binding is not None:
            try:
                archived_blocked = _CODEX_ARCHIVED.blocked(archive_binding)
            except _codex_archived.StateUnknown:
                _codex_archived_reply(chat_id, message_id, unknown=True)
                return
            if archived_blocked:
                _codex_archived_reply(chat_id, message_id)
                return
        entry = None if permissions_probe else _read_sessions().get(session_key)
        required = not thread and isinstance(entry, dict) and entry.get("rollover_pending") is True
        pending_note = (None if permissions_probe else
                        _ro.peek_pending(_BRIDGE_HOME, session_key, strict=True, required=required))
        if thread:
            pending_note = None
        if pending_note:
            prompt = _rollover_first_turn(chat_id, prompt, "codex", consume=False)
        elif thread and continuation is None:
            _ro.take_pending(_BRIDGE_HOME, session_key, strict=True)  # /resume invalidates the hint
        await _drop_client(chat_id)
        question_protocol = await asyncio.to_thread(codex_questions.selected_protocol, CODEX_BIN)
        if continuation is not None:
            _question_dispatch_guard(continuation)
            if question_protocol != continuation.origin.binding.protocol:
                raise RuntimeError("Conversational producer protocol changed")
        log(f"[{chat_id[:8]}] Codex turn start (model={model}, effort={effort}, "
            f"{'resume ' + thread[:8] if thread else 'new thread'})")
        if managed_native is not None:
            proc = await managed_native.spawn(storage, CODEX_BIN, REPO, CODEX_STREAM_LIMIT)
        else:
            proc = await asyncio.create_subprocess_exec(
                *storage.command([CODEX_BIN, "app-server", "--listen", "stdio://"]),
                cwd=REPO, env=storage.env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=CODEX_STREAM_LIMIT)
        CODEX_PROCS[chat_id] = proc
        stderr_task = (managed_native.start_stderr() if managed_native is not None
                       else asyncio.create_task(proc.stderr.read() if proc.stderr else asyncio.sleep(0, b"")))

        async def native_send(frame, *, guard=None):
            if managed_native is not None:
                return await _codex_rpc_send(proc, frame, guard=guard, writer=managed_native)
            return await _codex_rpc_send(proc, frame,
                **({"guard": guard} if guard is not None else {}))

        async def native_response(request_id, **kwargs):
            if managed_native is not None:
                return await _codex_rpc_response(proc, request_id, reader=managed_native, **kwargs)
            return await _codex_rpc_response(proc, request_id, **kwargs)

        await native_send({"method": "initialize", "id": 1, "params": {
            "clientInfo": {"name": "fleet-bridge", "version": "1.0"},
            "capabilities": {"experimentalApi": True},
        }})
        await native_response(1)
        await native_send({"method": "notifications/initialized"})
        await native_send({"method": "config/read", "id": 21,
                                    "params": {"includeLayers": False, "cwd": REPO}})
        storage.effective((await native_response(21,
            on_event=startup_usage_events.append)).get("result", {}).get("config"))
        if continuation is not None:
            _question_dispatch_guard(continuation)
        def archived_resume_error(frame):
            if (not permissions_probe and original_thread and archive_binding is not None
                    and CODEX_PROCS.get(chat_id) is proc
                    and _codex_archived_binding(chat_id, original_thread, question_runtime, storage=storage) == archive_binding):
                refusal = _codex_archived.classify(frame, 2, archive_binding)
                if refusal is not None:
                    raise refusal
        if thread:
            await native_send({"method": "thread/resume", "id": 2,
                                        "params": {"threadId": thread, "excludeTurns": True,
                                                   "model": model, "sandbox": CODEX_SANDBOX,
                                                   "approvalPolicy": policy, **reviewer,
                                                   "config": storage.config()}})
            try:
                thread_response = await native_response(2, on_event=startup_usage_events.append,
                                                            on_error=archived_resume_error,
                                                            method="thread/resume", thread=thread)
            except RuntimeError as exc:
                if managed_selected or continuation is not None or not _codex_unusable_session(exc, proc, thread):
                    raise
                _codex_resume_guard(chat_id, session_key, resume_binding, proc)
                recovering = True
                log(f"[{chat_id[:8]}] Codex session {thread[:8]} 无法续接，保留旧登记并准备新会话")
                thread = ""
                startup_usage_events.clear()
        if not thread:
            thread_request_id = 4 if recovering else 2
            await native_send({"method": "thread/start", "id": thread_request_id, "params": {
                "cwd": REPO, "model": model, "sandbox": CODEX_SANDBOX,
                "approvalPolicy": policy, **reviewer,
                "config": {**storage.config(),
                           **({"features.request_permissions_tool": True} if permissions_probe else {})},
                **({"ephemeral": True} if permissions_probe else {}),
            }}, **({"guard": lambda: _codex_resume_guard(chat_id, session_key, resume_binding, proc)}
                   if recovering else {}))
            thread_response = await native_response(thread_request_id,
                                                         on_event=startup_usage_events.append)
        new_usage_thread = not thread
        codex_approvals.verify_reviewer(reviewer, thread_response)
        if managed_native is not None:
            thread = managed_native.identifier(thread_response, "thread")
        else:
            thread = str((((thread_response.get("result") or {}).get("thread") or {}).get("id"))
                         or thread)
        if not thread:
            raise RuntimeError("Codex App Server did not return a thread id")
        if original_thread and not permissions_probe and not recovering and thread != original_thread:
            raise RuntimeError("Codex resume returned a replacement thread; no session reset")
        if continuation is not None and thread != continuation.origin.binding.thread:
            raise RuntimeError("Conversational card resume returned a different thread")
        if recovering:
            _codex_resume_guard(chat_id, session_key, resume_binding, proc)
            if not _fs.record_replacement(session_key, thread, expected=original_thread,
                                           revision=resume_binding[1], preview=prompt,
                                           codex_namespace={"home": str(storage.home),
                                                            "namespace": storage.namespace}):
                raise RuntimeError("Codex replacement registration failed or changed; no turn sent")
            resume_binding = (thread, _fs.sessions_revision(), question_runtime, resume_binding[3])
            _codex_resume_guard(chat_id, session_key, resume_binding, proc)
            APP_FRONT.broadcast({"ev": "session_reset", "engine": "codex", "prev": original_thread,
                                 "ts": time.time()})
            detail = (f"旧会话 {original_thread} 无法续接。本次已建立新会话 {thread}，"
                      "不包含旧上下文；旧 SID 与历史登记均保留。")
            SENDER.send(chat_id, detail)

        if permissions_probe:
            codex_approvals.verify_probe_thread(thread_response)
            cursor = None
            seen_cursors = set()
            for request_id in range(4, 20):
                await native_send({"method": "experimentalFeature/list", "id": request_id,
                    "params": {"threadId": thread, "limit": 100,
                               **({"cursor": cursor} if cursor else {})}})
                feature_response = await asyncio.wait_for(native_response(request_id), 15)
                found, cursor = codex_approvals.probe_feature_page(feature_response)
                if found:
                    break
                if not cursor or cursor in seen_cursors:
                    raise RuntimeError("Codex 未返回 request_permissions_tool；未启动测试回合")
                seen_cursors.add(cursor)
            else:
                raise RuntimeError("Codex 权限工具分页超限；未启动测试回合")
            detail = (f"Codex 权限探针就绪：临时会话 {thread}；工具已启用；"
                      "只读、无网络、人工审批。尚未申请或授予权限。")
            log(detail)
            APP_FRONT.broadcast({"ev": "notice", "text": detail,
                                 "chat_id": _display_chat_for(chat_id)})

        await native_send({"method": "turn/start", "id": 3, "params": {
            "threadId": thread,
            "input": [{"type": "text", "text": _REPLY_DELIVERY_NOTE + "\n\n"
                       + (_CODEX_QUESTION_NOTE + "\n\n"
                          if question_protocol is not None and not permissions_probe else "")
                       + prompt}],
            "cwd": REPO, "model": model, "effort": effort,
            "summary": CODEX_REASONING_SUMMARY,
            "approvalPolicy": policy, **reviewer,
        }}, **({"guard": lambda: _question_dispatch_guard(continuation)}
                if continuation is not None else
                {"guard": lambda: _codex_resume_guard(chat_id, session_key, resume_binding, proc)}
                if recovering else {}))
        turn_response = await native_response(3, on_event=early_events.append)
        if managed_native is not None:
            turn_id = managed_native.identifier(turn_response, "turn")
        else:
            turn_id = str((((turn_response.get("result") or {}).get("turn") or {}).get("id")) or "")
        if not turn_id:
            raise RuntimeError("Codex App Server did not return a turn id")
        if recovering:
            _codex_resume_guard(chat_id, session_key, resume_binding, proc)
        if managed_native is not None:
            managed_native.bind_turn(thread, turn_id)
        if continuation is not None:
            CODEX_QUESTIONS.delivered(continuation, acknowledged=True)
            _QUESTION_TASKS.discard(asyncio.current_task())
        usage_tracker = _CodexTurnUsage(thread, turn_id, new_thread=new_usage_thread)
        for event in startup_usage_events:
            usage_tracker.feed(event, before_turn=True)
        for event in early_events:
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            if params.get("turnId") and params.get("turnId") != turn_id:
                usage_tracker.feed(event, before_turn=True)
        if pending_note:
            # Keep the note until BOTH ingestion and durable thread registration succeeded.
            _codex_storage.record_session(_fs, session_key, thread, storage, preview=prompt)
            if _load_session(session_key) != thread:
                raise RuntimeError("Codex handoff thread registration failed")
            _ro.take_pending(_BRIDGE_HOME, session_key, strict=True)
        control = _CodexActiveTurn(proc, thread, turn_id,
            **({"writer": managed_native} if managed_native is not None else {}))
        CODEX_TURNS[chat_id] = control
        if not permissions_probe:
            question_origin = _question_origin(chat_id, thread, turn_id, proc, question_protocol, question_runtime)
        approvals = codex_approvals.Approvals(
            thread, turn_id, _display_chat_for(chat_id),
            lambda frame: native_send(frame), APP_FRONT.broadcast, log,
            enabled=policy == "on-request")
        CODEX_APPROVALS[chat_id] = approvals
        # stdout below owns JSON-RPC responses, so startup steering must run beside it.
        # Awaiting here would deadlock: steer waits for an ack nobody has started reading.
        if COALESCE.get(chat_id):
            startup_steer_task = asyncio.create_task(_steer_pending(chat_id))

        turn_status = ""
        if proc.stdout is None:
            raise RuntimeError("Codex App Server stdout unavailable")
        async def frames():
            for event in early_events:
                yield event
            if managed_native is not None:
                while (event := await managed_native.read_frame(proc)) is not None:
                    yield event
                return
            async for raw in proc.stdout:
                try:
                    event = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(event, dict):
                    yield event

        async for ev in frames():
            if managed_native is not None and not managed_native.notification_matches(ev, thread, turn_id):
                continue
            # Observe typed question metadata before any text guard; do not consume
            # notifications needed by approvals, control, usage, or error accounting.
            if (question_origin is None and question_protocol is not None and not permissions_probe
                    and not provider_error and chat_id not in INTERRUPTED):
                qp = ev.get("params") if isinstance(ev.get("params"), dict) else {}
                qi = qp.get("item") if isinstance(qp.get("item"), dict) else {}
                if (ev.get("method") == "item/completed" and "id" not in ev and ev.get("replay") is not True
                        and qp.get("threadId") == thread and qp.get("turnId") == turn_id
                        and qi.get("type") == "agentMessage" and qi.get("questions") is not None
                        and (_load_session(session_key) or "") == original_thread):
                    # Only a real question needs early registration of a newly started
                    # thread. Never replace a concurrently changed active mapping.
                    _codex_storage.record_session(_fs, session_key, thread, storage, preview=prompt)
                    question_origin = _question_origin(chat_id, thread, turn_id, proc,
                                                       question_protocol, question_runtime)
            if question_origin is not None:
                _observe_question_event(question_origin, ev, process=proc,
                    current=(CODEX_PROCS.get(chat_id) is proc and CODEX_TURNS.get(chat_id) is control))
            if approvals.feed(ev):
                continue
            if control.resolve(ev):
                continue
            if managed_native is not None and not managed_native.turn_consumer_event(ev):
                continue
            method = str(ev.get("method") or ev.get("type") or ev.get("event") or "")
            params = ev.get("params") if isinstance(ev.get("params"), dict) else {}
            if managed_native is not None and method == "error":
                # Original native fields were qualified by this owner's scope
                # guard. Legacy aliases must not replace message or false retry.
                error_event = (params["error"]["message"] or "Codex reported an error",
                               params["willRetry"])
            else:
                error_event = _codex_error_event(ev, thread, turn_id)
            if error_event and _codex_is_informational(error_event[0]):
                # 提示不是失败: 不告警、不判失败, 但**留痕** —— 静默吞掉的话, 哪天它真变成
                # 失败的前兆, 回头查日志会发现"什么都没有"。
                log(f"[{chat_id[:8]}] ℹ Codex 提示 (不计为失败): {error_event[0][:160]}")
                error_event = None
            if error_event:
                detail, will_retry = error_event
                note = (f"⚠ Codex 工具链错误: {detail[:500]}"
                        + ("（正在自动重试）" if will_retry else ""))
                if SENDER:
                    SENDER.send(chat_id, note, kind="codex_error")
                if not will_retry:
                    provider_error = detail
                    if question_origin is not None:
                        CODEX_QUESTIONS.finish(question_origin, normal=False)
            if usage_tracker.feed(ev):
                usage = _codex_usage_from(ev, usage)
            reasoning, is_delta = _codex_reasoning_text(ev)
            if reasoning:
                reasoning_text = (reasoning_text + reasoning if is_delta
                                  else _join_blocks(reasoning_text, reasoning))
                APP_FRONT.broadcast({"ev": "reasoning", "text": reasoning_text})
            if method == "item/agentMessage/delta":
                answer_text += str(params.get("delta") or "")
                APP_FRONT.broadcast({"ev": "stream", "text": stream_prefix + answer_text})
            elif method == "item/completed" and not answer_text:
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                if item.get("type") == "agentMessage" and item.get("text"):
                    answer_text = str(item["text"])
                    APP_FRONT.broadcast({"ev": "stream", "text": stream_prefix + answer_text})
            if method == "item/started":
                tool = _codex_tool_brief(ev)
                if tool:
                    name, brief = tool
                    log(f"[{chat_id[:8]}] Codex 工具: {name}")
                    APP_FRONT.broadcast({"ev": "tool", "name": name, "sub": False,
                                         "brief": brief})
            if method == "turn/completed":
                if managed_native is not None and not managed_native.terminal_matches(params, thread, turn_id):
                    continue
                if managed_native is not None:
                    managed_native.observe_terminal(params, thread, turn_id)
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                if str(turn.get("id") or "") != turn_id:
                    continue
                turn_status = str(turn.get("status") or "")
                if turn_status != "completed":
                    err = turn.get("error") if isinstance(turn.get("error"), dict) else {}
                    err_text = str(err.get("message") or turn_status or "Codex turn failed")
                elif provider_error:
                    # 即使 completed 且进程正常退出，provider error
                    # 才是可信判据，不能让一个看起来正常的回答覆盖真正失败。
                    err_text = provider_error
                if question_origin is not None:
                    CODEX_QUESTIONS.finish(question_origin, normal=(params.get("threadId") == thread and turn_status == "completed"
                        and not provider_error and chat_id not in INTERRUPTED and not turn.get("error")))
                await approvals.close()
                break

        if managed_native is not None:
            managed_native.require_terminal(thread, turn_id)
        exit_code = 0 if turn_status == "completed" and not provider_error else 1
        answer_buf = answer_text.strip()

        interrupted = chat_id in INTERRUPTED
        INTERRUPTED.discard(chat_id)
        extra: dict = {}
        if interrupted:
            answer_buf = answer_buf or "(已中断)"
        elif exit_code not in (0, None):
            detail = (err_text or answer_buf or f"exit={exit_code}").strip()
            answer_buf = answer_buf or f"⚠ Codex CLI 失败: {detail[:500]}"
            extra = {"failed": True, "retryable": True,
                     "retry_hint": "Codex CLI 这轮没跑完; 可再发一句「重试」。"}
        elif not answer_buf:
            answer_buf = "(Codex 没有返回正文)"

        if recovering:
            _codex_resume_guard(chat_id, session_key, resume_binding, proc)
        answer_buf = _strip_leaked_toolcall(answer_buf)
        answer_buf, out_media = _extract_media_markers(answer_buf)
        answer_buf, want_pin, pin_key = _extract_pin_marker(answer_buf)
        if not plan and not message_id:
            answer_buf = _quote_prefix(prompt) + answer_buf
        mid = SENDER.send_reply(chat_id, answer_buf, [], reply_to=message_id,
                                extra=extra) if SENDER else None
        _remember(mid, answer_buf)
        if want_pin and mid:
            await asyncio.to_thread(_pin_keyed, mid, pin_key)
        _app_pin_last(want_pin, pin_key, mid, answer_buf)
        for kind, mpath in out_media:
            await asyncio.to_thread(_send_local_media, chat_id, kind, mpath)
        if thread and not permissions_probe and not recovering:
            _codex_storage.record_session(_fs, session_key, thread, storage, preview=prompt)
        if usage:
            APP_FRONT.broadcast({"ev": "ctx", "used": _codex_usage_total(usage),
                                 "max": 0, "pct": 0.0, "ts": time.time()})
        if not permissions_probe and (thread or usage or exit_code is not None):
            if managed_native is not None:
                _write_codex_status(chat_id, model, effort, thread, usage, exit_code, writer=managed_native)
            else:
                _write_codex_status(chat_id, model, effort, thread, usage, exit_code)
        if managed_native is not None:
            _append_codex_ledger(chat_id, model, effort, thread,
                                 usage_tracker.usage() if usage_tracker else {}, exit_code,
                                 time.monotonic() - t0, err_text, writer=managed_native)
        else:
            _append_codex_ledger(chat_id, model, effort, thread,
                                 usage_tracker.usage() if usage_tracker else {}, exit_code,
                                 time.monotonic() - t0, err_text)
    except _codex_archived.ArchivedResumeError as exc:
        if managed_selected:
            from feishu_bridge.fleet_managed_persistence import hold_entry_original
            if not hold_entry_original(managed_entry):
                managed_entry.runtime.hold_unknown()
            from feishu_bridge.fleet_managed_ingress import Refused
            raise Refused("managed_native_turn_unknown") from None
        err_text = f"{type(exc).__name__}: {exc}"
        log(f"[{chat_id[:8]}] Codex 原生续接拒绝: {err_text}")
        unknown = _codex_archived_binding(chat_id, original_thread, question_runtime, storage=storage) != exc.binding
        if not unknown:
            try:
                _CODEX_ARCHIVED.record(exc)
            except _codex_archived.StateUnknown:
                unknown = True  # preserve possible write; never retry or repair
        _codex_archived_reply(chat_id, message_id, unknown=unknown)
        _append_codex_ledger(chat_id, model, effort, thread,
                             usage_tracker.usage() if usage_tracker else {}, exit_code,
                             time.monotonic() - t0, err_text)
    except Exception as exc:  # noqa: BLE001
        if managed_selected:
            from feishu_bridge.fleet_managed_persistence import hold_entry_original
            if not hold_entry_original(managed_entry):
                managed_entry.runtime.hold_unknown()
            from feishu_bridge.fleet_managed_ingress import Refused
            raise Refused("managed_native_turn_unknown") from None
        err_text = f"{type(exc).__name__}: {exc}"
        log(f"[{chat_id[:8]}] Codex turn 异常: {err_text}")
        msg = f"⚠ Codex 内部错误: {err_text}"
        try:
            SENDER.feishu.send(chat_id, msg)
        except Exception:  # noqa: BLE001
            pass
        APP_FRONT.broadcast({"ev": "error", "text": msg})
        _append_codex_ledger(chat_id, model, effort, thread,
                             usage_tracker.usage() if usage_tracker else {}, exit_code,
                             time.monotonic() - t0, err_text)
    finally:
        if approvals is not None:
            await approvals.close()
        if CODEX_APPROVALS.get(chat_id) is approvals:
            CODEX_APPROVALS.pop(chat_id, None)
        if startup_steer_task is not None:
            if not startup_steer_task.done():
                startup_steer_task.cancel()
            with contextlib.suppress(BaseException):
                await startup_steer_task
        question_shutdown_ok = False
        try:
            if managed_selected and managed_native is None:
                from feishu_bridge.fleet_managed_ingress import Refused
                raise Refused("managed_native_owner_unbound")
            if managed_native is not None:
                try:
                    disposition = await managed_native.finish(proc, stderr_task)
                except BaseException:
                    from feishu_bridge.fleet_managed_persistence import hold_entry_original
                    if hold_entry_original(managed_entry):
                        from feishu_bridge.fleet_managed_ingress import Refused
                        raise Refused("managed_native_turn_unknown") from None
                    raise  # Pre-persistence cleanup remains exact predecessor scope.
                question_shutdown_ok = disposition.returncode == 0
                # Only after owned leader/group/stdio settlement. A failed
                # cleanup retains these owner references and its persistent lease.
                if CODEX_TURNS.get(chat_id) is control:
                    CODEX_TURNS.pop(chat_id, None)
                if control:
                    control.close()
                if CODEX_PROCS.get(chat_id) is proc:
                    CODEX_PROCS.pop(chat_id, None)
                    INTERRUPTED.discard(chat_id)
            else:
                if CODEX_TURNS.get(chat_id) is control:
                    CODEX_TURNS.pop(chat_id, None)
                if control:
                    control.close()
                if CODEX_PROCS.get(chat_id) is proc:
                    CODEX_PROCS.pop(chat_id, None)
                    INTERRUPTED.discard(chat_id)
            if managed_native is None and proc is not None:
                was_live = proc.returncode is None
                if proc.stdin:
                    proc.stdin.close()
                if proc.returncode is None:
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                        question_shutdown_ok = was_live and proc.returncode == 0
                    except asyncio.TimeoutError:
                        proc.terminate()
                        await proc.wait()
                if stderr_task:
                    if not stderr_task.done():
                        stderr_task.cancel()
                    with contextlib.suppress(BaseException):
                        err_raw = await stderr_task
                        if not err_text and isinstance(err_raw, bytes):
                            err_text = err_raw.decode("utf-8", errors="replace")
        finally:
            if question_origin is not None:
                CODEX_QUESTIONS.disconnect(question_origin, expected=(question_origin.normal and question_shutdown_ok))
        if reaction_id:
            SENDER.end_busy(message_id, reaction_id)


async def _run_turn_inner(chat_id: str, prompt: str, message_id: str | None = None,
                          plan: bool = False) -> None:
    answer_buf = ""                # 累积答案 (流式时实时刷, 否则末尾整发)
    partial_buf = ""               # 当前消息的增量文本 (StreamEvent 逐字); 整块到达后清零归入 answer_buf
    reasoning_buf = ""  # 实时广播给 App，不落历史或发送飞书
    reasoning_partial = ""         # 思考的逐字增量 (整块到达后归入 reasoning_buf)
    reasoning_last = 0.0           # 思考广播节流 (对齐 _TeeStream 的 0.4s)
    reasoning_idx: int | None = None   # 「思考中」指示: 亮着的 thinking 块 index (见 _thinking_signal)
    thinking: list[str] = []       # 工具活动 → 折叠"思维链"面板
    todos: list = []               # 最近一次 TodoWrite 的任务清单 (渲染勾选卡)
    subagents: set = set()         # 本轮真子代理的 parent_tool_use_id (后台 bash/Monitor 不计)
    was_fork = chat_id in FORK_PENDING   # 本轮是不是 /fork 触发 (提交成功后才确认+清标志)
    result_msg = None              # 末尾 ResultMessage (含 usage, 写 status 快照)
    saw_unsafe_tool = False        # 本轮用过写类工具 → 网络断不敢自动重试 (防重复副作用)
    plan_client = None             # plan 模式下要还原 bypass 的 client
    reaction_id = SENDER.begin_busy(message_id) if message_id else None
    stream = None
    if STREAMING_ON:                # 开则尝试建流式卡; 建/发失败 → stream=None 走整卡
        s = SENDER.open_stream(chat_id, message_id, _msg_label(prompt))  # 回复式 + 预览=本轮问题
        if await s.start():
            stream = s
        else:                       # 静默降级会像"没在处理" → 发占位, 免得回复凭空迟到
            SENDER.send_text(chat_id, "⏳ 收到, 处理中 (实时进度卡创建失败, 结果将一次性回复)")

    async def _ask_cut() -> None:
        """提问卡是段落边界：作答或超时后定稿提问前内容，累积清零、
        新开一段 —— 否则整轮累积流会在答完后把旧内容再流一遍 (客户端剪裁兜不全)。"""
        nonlocal answer_buf, partial_buf, thinking, stream, reasoning_buf, reasoning_partial
        ASK_CUT.discard(chat_id)
        pre = answer_buf.strip()
        if pre:
            done = False
            if stream and stream.ok:
                done = await stream.finish(pre, thinking)
            if done:
                _remember(stream.message_id, pre)
            elif stream and stream.delivered_chars >= max(len(pre) - 120, int(len(pre) * 0.9)):
                _remember(stream.message_id, pre)  # 已基本流出，不补发
            else:
                sender = SENDER.feishu if stream is not None else SENDER  # Tee 已广播过 → 只补飞书
                mid = sender.send_reply(chat_id, pre, thinking, reply_to=message_id)
                _remember(mid, pre)
        answer_buf, partial_buf, thinking = "", "", []
        reasoning_buf, reasoning_partial = "", ""   # 思考也按段清零 (客户端在 reply 时折叠)
        stream = None
        if STREAMING_ON:                # 后段新开流式卡; 失败 → 走末尾整卡兜底
            s2 = SENDER.open_stream(chat_id, message_id, _msg_label(prompt))
            if await s2.start():
                stream = s2

    try:
        client = await _get_client(chat_id)
        await _stop_idle_watch(chat_id)   # 交接: 后台回合正在播则等它播完, 防止吃掉本轮响应
        if plan:                    # /plan: 本轮切计划模式, can_use_tool 门 ExitPlanMode
            await client.set_permission_mode("plan")
            plan_client = client
            PLAN_ACTIVE.add(chat_id)
        # 重启或恢复后的流可能残留上一轮的
        # ResultMessage, 会立刻终止本轮 → 发出空回复, 且真正的响应留在流里污染下一轮
        # (表现为「回复上一条」)。故: 本轮若零模型活动, 再读一轮拿真正的响应。
        await client.query(prompt)
        CLAUDE_STEERABLE.add(chat_id)
        await _steer_pending(chat_id)
        for _stale_try in range(2):
            saw_activity = False
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):  # 只取顶层助手的 text_delta
                    ev = msg.event or {}
                    delta = ev.get("delta") or {}
                    if not msg.parent_tool_use_id:     # 「思考中」指示 (子代理的不点亮)
                        _sig, reasoning_idx = _thinking_signal(ev, reasoning_idx)
                        if _sig:
                            APP_FRONT.broadcast(_sig)
                    if (not msg.parent_tool_use_id and ev.get("type") == "content_block_delta"
                            and delta.get("type") == "thinking_delta" and delta.get("thinking")):
                        reasoning_partial += delta["thinking"]
                        saw_activity = True
                        now = time.monotonic()
                        if now - reasoning_last >= 0.4:   # 节流对齐 _TeeStream
                            reasoning_last = now
                            APP_FRONT.broadcast({"ev": "reasoning",
                                                 "text": reasoning_buf + reasoning_partial})
                    elif (not msg.parent_tool_use_id and ev.get("type") == "content_block_delta"
                            and delta.get("type") == "text_delta" and delta.get("text")):
                        if chat_id in ASK_CUT:
                            await _ask_cut()
                        partial_buf += delta["text"]
                        saw_activity = True
                        if stream and stream.ok:       # put_answer 自带节流, 高频调用无害
                            await stream.put_answer(answer_buf + partial_buf)
                elif isinstance(msg, AssistantMessage):
                    saw_activity = True
                    pid = getattr(msg, "parent_tool_use_id", None)  # 有值 = 子代理的消息 (SDK 原生归属)
                    is_sub = bool(pid)
                    if is_sub:
                        subagents.add(pid)                      # 真子代理计数依据 (后台 bash/Monitor 无此 → 不误计)
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            if not is_sub:  # 子代理中间文字不进面板
                                if chat_id in ASK_CUT:
                                    await _ask_cut()
                                answer_buf = _join_blocks(answer_buf, block.text)
                                partial_buf = ""    # 整块已归账, 对应增量清零 (防双计)
                                if stream and stream.ok:
                                    await stream.put_answer(answer_buf)
                        elif isinstance(block, ThinkingBlock) and not block.thinking.strip():
                            _note_empty_thinking()   # 空思考块: 说一次, 见该函数注释
                        elif isinstance(block, ThinkingBlock):
                            if not is_sub:  # 思考内容实时广播给 App
                                reasoning_buf += block.thinking
                                reasoning_partial = ""
                                APP_FRONT.broadcast({"ev": "reasoning", "text": reasoning_buf})
                        elif isinstance(block, ToolUseBlock):
                            if not is_sub and block.name not in _READONLY_TOOLS:
                                saw_unsafe_tool = True     # 见到写类工具 → 本轮重试不安全
                            log(f"[{chat_id[:8]}] 工具: {block.name}{' (子代理)' if is_sub else ''}")
                            # Web 端对齐 Claude 官方 App: 工具步骤实时广播 (飞书面板受卡片
                            # 限制只列任务名, 那是妥协, Web 不继承; fail-soft 不影响飞书)
                            APP_FRONT.broadcast({"ev": "tool", "name": block.name, "sub": is_sub,
                                                 "brief": tool_brief(block.name, block.input or {})})
                            # 捕获任务清单 → 末尾渲染勾选卡。
                            # TodoWrite 是旧版工具名, 当前 CLI 已换成 Task* 系列 (deferred,
                            # 模型自己 ToolSearch 加载), 两者数据模型不同:
                            #   TodoWrite  一次性传整个清单 input.todos[]
                            #   TaskCreate 逐条建 {subject, description, activeForm}
                            #   TaskUpdate 逐条改 {taskId, status}
                            # 只认 TodoWrite 的话这块永远收不到东西 —— 渲染函数写好了却从没
                            # 触发过。两种都收,
                            # 旧名保留是为了兼容别的环境。
                            if block.name == "TodoWrite":
                                todos = (block.input or {}).get("todos", []) or todos
                                _broadcast_todos(todos)
                            elif block.name == "TaskCreate":
                                inp = block.input or {}
                                tasks = _session_tasks(chat_id)  # 会话级任务
                                # id 留空待绑: 真实 taskId 由 harness 在**工具结果**里给
                                # ("Task #10 created…"), input 里没有 —— 早先按列表长度
                                # 猜测的编号可能与真实 taskId 不一致，
                                # 因此应等待工具结果绑定。
                                tasks.append({
                                    "id": "",
                                    "content": inp.get("subject") or inp.get("description") or "",
                                    "activeForm": inp.get("activeForm") or "",
                                    "status": inp.get("status") or "pending",
                                })
                                todos = tasks
                                _save_task_track(chat_id)
                                _broadcast_todos(todos)
                            elif block.name == "TaskUpdate":
                                inp = block.input or {}
                                tid, new_st = str(inp.get("taskId") or ""), inp.get("status")
                                tasks = _session_tasks(chat_id)
                                hit = next((t for t in tasks if t.get("id") == tid), None)
                                if hit is None and tid.isdigit():
                                    # 退路: 全局 taskId 减去本会话首条的号 = 本会话内序号
                                    # (id 绑定失败时兜底, 例如结果文本格式变了)
                                    base = next((int(t["id"]) for t in tasks
                                                 if str(t.get("id", "")).isdigit()), None)
                                    i = int(tid) - base if base is not None else int(tid) - 1
                                    hit = tasks[i] if 0 <= i < len(tasks) else None
                                if hit is not None:
                                    if new_st == "deleted":
                                        tasks.remove(hit)
                                    elif new_st:
                                        hit["status"] = new_st
                                    if inp.get("subject"):
                                        hit["content"] = str(inp["subject"])
                                    todos = tasks
                                    _save_task_track(chat_id)
                                    _broadcast_todos(todos)
                            # AskUserQuestion 的卡由 PreToolUse 钩子负责 (阻塞等答案), 此处不再重复发
                            # 工具调用只写日志，面板只列子代理任务名。
                elif isinstance(msg, UserMessage):
                    # 工具结果回流: 绑定 TaskCreate 的真实 id ("Task #10 created successfully")
                    for blk in (msg.content if isinstance(msg.content, list) else []):
                        if not isinstance(blk, ToolResultBlock):
                            continue
                        c = blk.content
                        txt = c if isinstance(c, str) else " ".join(
                            str(p.get("text", "")) for p in c or [] if isinstance(p, dict))
                        m = re.search(r"Task #(\d+) created", txt or "")
                        if m:
                            pend = next((t for t in _session_tasks(chat_id) if not t.get("id")), None)
                            if pend is not None:
                                pend["id"] = m.group(1)
                                _save_task_track(chat_id)
                elif isinstance(msg, TaskStartedMessage):          # 子代理启动 → 面板只记它的任务名
                    desc = (getattr(msg, "description", "") or "").strip() or "(未命名任务)"
                    thinking.append(f"🚀 {desc[:100]}")
                    if stream and stream.ok:
                        await stream.put_thinking(thinking)
                elif isinstance(msg, RateLimitEvent):
                    _persist_ratelimit(msg.rate_limit_info)        # 真·订阅额度落盘 (管理器读)
                elif isinstance(msg, TaskNotificationMessage):     # 任务收尾: 真子代理才进面板
                    # 只统计已见子代理的完成事件。
                    if getattr(msg, "tool_use_id", None) in subagents:
                        thinking.append(_task_done_brief(msg))
                        if stream and stream.ok:
                            await stream.put_thinking(thinking)
                elif isinstance(msg, SystemMessage) and not isinstance(
                        msg, (TaskStartedMessage, TaskProgressMessage)):  # Task* 是 SystemMessage 子类, 别误吞
                    pass  # 其他系统事件不进面板
                elif isinstance(msg, ResultMessage):
                    result_msg = msg            # 错误/结果文本统一在循环后处理 (要先判中断)
            CLAUDE_STEERABLE.discard(chat_id)
            if saw_activity or interrupted_flag(chat_id) or _stale_try or prompt == "/compact":
                break   # /compact 回合本就零模型活动 (只有系统事件+Result), 不是陈旧残留 —
                        
            log(f"[{chat_id[:8]}] ⚠ 空回合 (流中残留终止符) — 重新读取本轮真实响应")
            result_msg = None
        # 用户主动中断可能返回错误结果，客户端仍可复用。
        # handler 已发"🛑已中断", 这里不再发 error result, 有部分答案就补发, 跳过 bookkeeping.
        interrupted = chat_id in INTERRUPTED
        INTERRUPTED.discard(chat_id)
        if interrupted:
            partial = answer_buf.strip()
            sent = False
            if stream and stream.ok:
                sent = await stream.finish(partial or "(已中断)", thinking)
                if sent:
                    _remember(stream.message_id, partial)
            if not sent and partial and stream and \
                    stream.delivered_chars >= max(len(partial) - 120, int(len(partial) * 0.9)):
                
                _remember(stream.message_id, partial)
                sent = True
                log(f"[{chat_id[:8]}] 中断收尾: 已流出 {stream.delivered_chars}/{len(partial)} 字, 不补发")
            if not sent and partial:                           # 真没送出去多少 → 整卡补发
                sender = SENDER.feishu if stream is not None else SENDER   # 同上: App 已有流式广播
                mid = sender.send_reply(chat_id, partial, thinking, reply_to=message_id)
                _remember(mid, partial)
            if not (stream and stream.ok):    # finish 没被调到 → App 一个终结事件都没有
                _app_seal(partial or "(已中断)")
            return                          # finally 仍会跑 (撤表情)
        # is_error **或** result 文本本身就是一条 API Error —— SDK 不保证标那个位
        
        if turn_failed(result_msg):
            # 记录结果字段，便于诊断错误。
            log(f"[{chat_id[:8]}] ⚠ 本轮判定为出错 · {_result_fields_line(result_msg)}")
            _err = str(result_msg.result or result_msg.subtype or "")   # 仅供下面的网络错误关键词匹配
            _net = any(k in _err.lower() for k in _NET_ERR_KEYS)
            _tls = any(k in _err.lower() for k in _TLS_ERR_KEYS)   # 同样重试, 收尾话术不同
            # advisor 惹的错 (不兼容 / 计入 usage credits 而额度关着): 换一个不带 advisor 的 client
            # (resume 同一会话, 记忆不丢), 再走下面同一条自动续跑 —— 这一轮不因 advisor 失败。
            _adv = _advisor_turn_error(chat_id, _err)
            if _adv:
                _reject_advisor(CLIENT_ADVISOR.get(chat_id, "?"), _err[:200])
                await _drop_client(chat_id)
                client = await _get_client(chat_id)
            # 有工具调用的回合也可能需要续接；避免机械重发造成重复操作。
            #
            # **但放开保护不是重点, 换语义才是。** 原来这里发的是 `prompt` (机械重发
            # 原问题), 那个保护正是为它存在的: 从头再做一遍 = 已经写掉的文件再写一次。
            # 现在发「重试」二字 —— 模型看得到完整上下文, 自己决定从哪儿接, 这正是
            # 用户手点按钮时一直在工作的行为 (7e62a46 给 /retry 定的同一个语义)。
            # 语义换掉之后, saw_unsafe_tool 这个条件就失去了存在的理由。
            #
            # 两种情况**统一走 continuation** 而不是分两条: 一个语义一处判据, 不会漂移。
            # 无写操作那种原来机械重跑更"精确", 但 continuation 对它同样安全, 而且省掉
            # 重读一遍文件的开销。
            _retried = _net or _tls or _adv
            _retried_ok = False
            if _retried:
                # 日志里就把两类分开 —— 事后查"那天到底是抖动还是证书"靠的就是这一行
                log(f"[{chat_id[:8]}] {'TLS 证书校验失败' if _tls else ('advisor 不可用' if _adv else '网络中断')}, "
                    f"自动重试一次 (continuation): {_err[:60]}")
                _r_t0, _r_tools, _r_exc = time.monotonic(), 0, None
                _retry_note = ("（advisor 不可用，已关掉它，正在自动重试…）" if _adv and not (_net or _tls)
                               else "（连接中断，正在自动重试…）")
                if stream and stream.ok:
                    await stream.put_answer(_retry_note, force=True)
                else:
                    # stream 已经没了 → 上面那句用户一个字都收不到, 而这正是最需要
                    # 告知的时刻。SENDER 同时给飞书和 App notice; notice 故意不清
                    # 「生成中」(见下面 ev=error 那条注释), 因为这一轮还没结束。
                    SENDER.send(chat_id, _retry_note)
                r_final, r_part = "", ""       # 提到 try 外: 异常路径也要能统计拿回多少
                r_result = None                # **重试自己那条** ResultMessage (下面判成败要用)
                _r_bad = False                 # 重试拿回来的是不是一条报错
                try:
                    await client.query("重试")
                    CLAUDE_STEERABLE.add(chat_id)
                    await _steer_pending(chat_id)
                    async for msg in client.receive_response():
                        if isinstance(msg, StreamEvent):
                            ev = msg.event or {}; d = ev.get("delta") or {}
                            if (not msg.parent_tool_use_id and ev.get("type") == "content_block_delta"
                                    and d.get("type") == "text_delta" and d.get("text")):
                                r_part += d["text"]
                                if stream and stream.ok:
                                    await stream.put_answer(r_final + r_part)
                        elif isinstance(msg, AssistantMessage) and not getattr(msg, "parent_tool_use_id", None):
                            for b in msg.content:
                                if isinstance(b, TextBlock) and b.text.strip():
                                    r_final += b.text; r_part = ""
                                    if stream and stream.ok:
                                        await stream.put_answer(r_final)
                                # 主循环有这一支 (@2697), 重试循环原来没有 —— 于是重试
                                # 期间的工具调用在日志和 App 活动流里**都是空白**。
                                elif isinstance(b, ToolUseBlock):
                                    _r_tools += 1
                                    log(f"[{chat_id[:8]}] (重试中) 工具: {b.name}")
                                    APP_FRONT.broadcast({"ev": "tool", "name": b.name, "sub": False,
                                                         "brief": tool_brief(b.name, b.input or {})})
                        elif isinstance(msg, ResultMessage):
                            result_msg = msg
                            r_result = msg      # 单独留一份: result_msg 会被覆盖成这条
                    CLAUDE_STEERABLE.discard(chat_id)
                    # 依据重试结果判断状态；缺少结果不能视为成功。
                    _r_bad = (r_result is None or r_result.is_error
                              or looks_like_api_error(r_result.result))
                    if r_final.strip():
                        # 半截正文照样交给用户 —— 比一个字都没有强。但**不许标成功**:
                        # 标了就等于替这一轮打包票, 而它其实没跑完。
                        answer_buf = r_final
                        _retried_ok = not _r_bad
                except Exception as _re:  # noqa: BLE001
                    _r_exc = f"{type(_re).__name__}: {_re}"
                    # 这行文案原样保留 —— 有人在 grep 它 (同上面「本轮判定为出错」那条)
                    log(f"[{chat_id[:8]}] 自动重试异常: {_r_exc}")
                # 无论成败都留一行: 原来成功是静默的, 于是日志里「重试成功」和「还在跑」
                # 长得一模一样。
                log(f"[{chat_id[:8]}] " + _retry_summary(
                    time.monotonic() - _r_t0, len(r_final), _r_tools, _retried_ok,
                    _r_exc, _r_bad))
                APP_FRONT.broadcast({"ev": "notice",
                                     "text": _retry_user_note(_retried_ok, _r_exc, _r_bad)})
            # 这里同样不能只看 is_error, 否则「重试失败后」这条兜底提示也会漏掉 ——
            # 而那正是用户唯一能看到的、告诉他这轮没跑完的东西。
            # 即使已经生成部分正文，未完成的回合仍需要给用户明确提示。
            # 判据换成「这一轮到底跑完没有」: 自动重试拿回了内容才算跑完。
            if (result_msg is not None and not _retried_ok
                    and (result_msg.is_error or looks_like_api_error(result_msg.result))):
                answer_buf += _error_suffix(result_msg, answer_buf, saw_unsafe_tool, _retried)
                _fail = _failure_meta(result_msg, saw_unsafe_tool, _retried)
        elif result_msg is not None and not answer_buf.strip() and result_msg.result:
            answer_buf = result_msg.result
        _compact_done = False
        if prompt == "/compact" and not answer_buf.strip():
            # 压缩回合常无正文; 空回复不广播 reply → App 的「进行中」状态永远没人清
            # 因此需要发送完成确认。
            answer_buf = _COMPACT_DONE_TEXT
            _compact_done = True
        answer_buf = _strip_leaked_toolcall(answer_buf)        # 防模型把工具调用「说」成文本泄漏进回复
        answer_buf, out_media = _extract_media_markers(answer_buf)    # [[图片/视频: path]] → 剥离, 稍后补发
        answer_buf, want_pin, pin_key = _extract_pin_marker(answer_buf)   # [[置顶]] → 剥离, 发完 Pin
        if todos:                                              # 任务清单 (先 prepend → 在子代理计数之下)
            answer_buf = _render_todos(todos) + "\n\n" + answer_buf
        subagents.discard("")                                  # 去掉空 id
        if subagents:                                          # 子代理计数 (后 prepend → 在最顶, 详情在思维链)
            answer_buf = f"🤖 调度了 {len(subagents)} 个子代理\n\n" + answer_buf
        if not plan and not message_id:                        # 无触发 id (合并重跑) → 回复式不可用, 文字前缀兜底标注
            answer_buf = _quote_prefix(prompt) + answer_buf
        # 正常回合是空 dict → 三个字段一个都不出现在 reply 里 (客户端据此当正常回复画)
        _fail = _merge_reply_meta(locals().get("_fail") or {}, _compact_done)
        raw_len = len(answer_buf)      # 装饰前长度 (对比流出量要用它, 装饰会额外加字)
        delivered = False
        if stream and stream.ok:                               # 先发回复 (UX 优先)
            delivered = await stream.finish(answer_buf, thinking, _fail)
            if delivered:
                mid = stream.message_id
                _remember(mid, answer_buf)
        if not delivered and stream and stream.delivered_chars >= max(raw_len - 120, int(raw_len * 0.9)):
            # 卡死但正文基本已流出 (只差尾巴/装饰) → 别整卡重发, 否则同一回复出两张
            # 缺少的尾部单独补发。
            mid = stream.message_id
            _remember(mid, answer_buf)
            delivered = True
            log(f"[{chat_id[:8]}] 流式卡终写失败但已流出 {stream.delivered_chars}/{raw_len} 字, 不重发")
        if not delivered:                                      # 非流式 / 流式失败早 / 内容缺失多 → 整卡兜底重发
            if stream is not None:  # Tee 已广播给 App，只补飞书
                mid = SENDER.feishu.send_reply(chat_id, answer_buf, thinking, reply_to=message_id)
                if not (stream and stream.ok):   # finish 压根没被调到 → App 真没收到收尾
                    _app_seal(answer_buf, _fail)
            else:
                mid = SENDER.send_reply(chat_id, answer_buf, thinking,
                                        reply_to=message_id, extra=_fail)
            _remember(mid, answer_buf)
        if want_pin and mid:                                   # 飞书侧 (纯 App 时 mid 恒为 None)
            await asyncio.to_thread(_pin_keyed, mid, pin_key)
        _app_pin_last(want_pin, pin_key, mid, answer_buf)      # App 侧 —— **两种模式都要**
        for kind, mpath in out_media:                          # 文字卡之后补发图片/视频消息
            await asyncio.to_thread(_send_local_media, chat_id, kind, mpath)
        if result_msg is not None:                             # 再 bookkeeping: ctx 有 CLI 往返, 不拖慢回复
            ctx = None
            try:
                ctx = await client.get_context_usage()         # 权威上下文用量
            except Exception:  # noqa: BLE001
                pass
            _write_status(chat_id, result_msg, ctx)
            if ctx:  # 广播上下文用量快照
                # ts = **这个快照算出来的时刻** (epoch 秒), 不是客户端收到的时刻。
                
                # 「没压缩成功」还是「这个数是旧的」—— 真相是后者 (/compact 不产生
                # ResultMessage, 而 ctx 是在回合收尾时才算的, 那份快照比压缩早 较早)。
                # 客户端**只能给 live 事件打本地时刻**: 重连回放那份是网关缓存的原样重发,
                # 「收到即现在」会把 较早前的数标成「刚刚」—— **比不显示时间更坏**。
                # 网关侧配套 (beb9001): 缓存里原样存、回放时不刷新。
                APP_FRONT.broadcast({"ev": "ctx",
                                     "used": int(ctx.get("totalTokens", 0) or 0),
                                     "max": int(ctx.get("maxTokens", 0) or 0),
                                     "pct": round(float(ctx.get("percentage", 0.0) or 0.0), 1),
                                     "ts": time.time()})
            new_sid = getattr(result_msg, "session_id", "") or ""
            _record_session(chat_id, new_sid, preview=prompt)   # 记 active session (新会话用首条消息当默认名)
            # 登记这个会话是打到哪家的 (跨提供方 resume 护栏; 失败静默)
            _prov.tag_session(_SESSION_PROVIDERS_FILE, new_sid,
                              _prov.provider_for_model((CLIENT_SIG.get(chat_id) or (None,))[0]
                                                       or _runtime_snapshot()[0]))
            if was_fork:               # fork 已落盘提交 → 现在才清标志 + 确认 (中断/异常前都不清, 防丢 fork)
                FORK_PENDING.discard(chat_id)
                SENDER.send(chat_id, f"🌿 已分叉到新会话 `{new_sid[:8]}`（原会话保留，可 /resume 回去）。")
    except Exception as exc:  # noqa: BLE001
        log(f"[{chat_id[:8]}] turn 异常: {type(exc).__name__}: {exc}")
        msg = f"⚠ 内部错误: {type(exc).__name__}: {exc}"
        # 飞书侧照旧给文字; App/Web 侧必须收**终结事件** ev=error 而不是普通旁白 notice。
        # 客户端的「生成中」是靠 reply/error 这类收尾信号清的, notice 不清 —— 回合半路
        # 死掉后需要发送收尾信号。
        try:
            SENDER.feishu.send(chat_id, msg)
        except Exception:  # noqa: BLE001  (飞书发不出去也不能吞掉下面的终结事件)
            pass
        APP_FRONT.broadcast({"ev": "error", "text": msg})
        await _drop_client(chat_id)   # 连接可能已坏, 丢弃, 下条消息重建
    finally:
        CLAUDE_STEERABLE.discard(chat_id)
        if plan_client is not None:    # 还原 bypass; 用 CLIENTS 里的活 client (异常 drop 后下条新建即 bypass)
            PLAN_ACTIVE.discard(chat_id)
            live = CLIENTS.get(chat_id)
            if live is not None:
                try:
                    await live.set_permission_mode("bypassPermissions")
                except Exception:  # noqa: BLE001
                    pass
        if reasoning_idx is not None:
            # **终结信号**: 正常路径靠 content_block_stop 关掉, 但异常/中断/进程被打断时
            # 那个 stop 永远不会来 —— 没有这一行, App 上的「思考中」会一直转下去。
            # (同昨天给 codex 适配层立的那条: 每条异常路径都要有人宣告这一轮结束。)
            reasoning_idx = None
            try:
                APP_FRONT.broadcast({"ev": "reasoning", "text": "", "active": False})
            except Exception:  # noqa: BLE001  (收尾广播失败不许掀翻 finally)
                pass
        INTERRUPTED.discard(chat_id)   # 防泄漏 (异常路径)
        ASK_CUT.discard(chat_id)       # 作答后模型未再出文本 → 标记就地清掉, 别漏给下一轮
        if reaction_id:
            SENDER.end_busy(message_id, reaction_id)
        _start_idle_watch(chat_id)  # 消费后台任务唤醒的回合


# ── 斜杠命令 (CC 控制面接进飞书) ───────────────────────────────────

# 生成 — 客户端 "/" 补全从服务端拿, 加命令只改这里 (与 handle_message/handle_command
# 的真实分支同步维护; 此前 _HELP_TEXT 手写漏了 /effort 和 /ctx 别名)
_COMMANDS: list[dict] = [
    {"name": "/compact", "description": "压缩上下文 (折叠成摘要, 变快变省; 落盘内容不受影响)"},
    {"name": "/new", "description": "开新会话 (旧会话保留, 可 /resume 回去)", "aliases": ["/reset", "/clear"]},
    {"name": "/plan", "args": "<任务>", "description": "先给计划, 你回 y 才执行 (先提案后批准)"},
    {"name": "/sessions", "description": "会话列表"},
    {"name": "/resume", "args": "<id|名字>", "description": "切换到某个会话"},
    {"name": "/fork", "description": "下一条消息从当前会话分叉出副本"},
    {"name": "/name", "args": "<名>", "description": "给当前会话命名"},
    {"name": "/context", "description": "上下文用量", "aliases": ["/ctx"]},
    {"name": "/cost", "description": "成本 + 订阅额度"},
    {"name": "/model", "args": "<opus|sonnet|haiku|完整id|default>", "description": "切模型 (活体, 记忆保留)"},
    {"name": "/effort", "args": "<low|medium|high|xhigh|max|default>", "description": "思考强度 (下一条消息生效)"},
    {"name": "/help", "description": "命令帮助"},
]

_HELP_TEXT = "🤖 **可用命令**\n" + "\n".join(
    f"- `{c['name']}{' ' + c['args'] if c.get('args') else ''}` {c['description']}"
    + (f"（别名 {'、'.join(c['aliases'])}）" if c.get("aliases") else "")
    for c in _COMMANDS
) + "\n- 发 **停 / stop / 取消** 中断正在跑的任务\n（图片 / 语音 / 回复引用 均支持）"


def _read_ratelimit() -> dict:
    try:
        return json.loads(_RATELIMIT_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _fmt_ratelimit_line(rl: dict) -> str:
    status = rl.get("status", "?")
    emoji = {"allowed": "🟢", "allowed_warning": "🟡", "rejected": "🔴"}.get(status, "⚪")
    reset_s = ""
    if rl.get("resets_at"):
        try:
            dt = datetime.fromtimestamp(int(rl["resets_at"]), timezone(timedelta(hours=8)))
            reset_s = " · 重置 " + dt.strftime("%H:%M BJT")
        except Exception:  # noqa: BLE001
            pass
    util = rl.get("utilization")
    util_s = f" · 用量 {util}%" if util is not None else ""
    return f"{emoji} 订阅额度({rl.get('rate_limit_type', '?')}): {status}{util_s}{reset_s}"


def _fmt_context(cu: dict) -> str:
    tot = int(cu.get("totalTokens", 0) or 0)
    mx = int(cu.get("maxTokens", 0) or 0)
    pct = float(cu.get("percentage", 0) or 0)
    out = ["🧠 **上下文用量**",
           f"{tot:,} / {mx:,} tokens（{pct:.1f}%）· 模型 {cu.get('model', '?')}",
           f"记忆文件 {len(cu.get('memoryFiles', []))} 个 · MCP 工具 {len(cu.get('mcpTools', []))} 个"]
    if cu.get("isAutoCompactEnabled"):
        thr = cu.get("autoCompactThreshold")
        out.append("自动压缩: 开" + (f"（阈值 {int(thr):,}）" if thr else ""))
    return "\n".join(out)


def _fmt_cost(chat_id: str) -> str:
    out = ["💰 **用量**"]
    try:
        st = json.loads(_STATUS_FILE.read_text(encoding="utf-8")).get("sessions", {}).get(chat_id, {})
        if st:
            out.append(f"上一轮: ≈${st.get('last_cost_usd', 0):.4f} · "
                       f"上下文 {int(st.get('context_tokens', 0)):,} ({st.get('context_pct', 0)}%)")
    except Exception:  # noqa: BLE001
        pass
    rl = _read_ratelimit()
    if rl:
        out.append(_fmt_ratelimit_line(rl))
    out.append("（订阅定额，成本为等效 API 估值）")
    return "\n".join(out)


def _write_runtime_field(field: str, value: str, clear_words: tuple[str, ...],
                         err_note: str, *, lower: bool = False) -> None:
    """runtime.json 的单键读-改-写 —— **锁与原子写只此一份**。

    共享锁和原子替换，避免并发写入丢失数据。清空判定始终忽略大小写；
    保存值是否转小写由 lower 决定，各字段保留自己的清空词表。
    """
    CODEX_QUESTIONS.cancel_work(None, "runtime_changed")
    try:
        with open(_RUNTIME_FILE.with_suffix(".lock"), "w") as lf:
            if fcntl is not None:
                fcntl.flock(lf, fcntl.LOCK_EX)
            else:                     # Windows: 锁 1 字节等价互斥 (进程退出自动释放)
                msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
            cur = _read_runtime()
            if value.strip().lower() in clear_words:
                data = {k: v for k, v in cur.items() if k != field}
            else:
                kept = value.strip().lower() if lower else value.strip()
                data = {**cur, field: kept}
            tmp = _RUNTIME_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(_RUNTIME_FILE)
    except Exception as exc:  # noqa: BLE001
        log(f"{err_note}: {exc}")


def _write_runtime_effort(effort: str) -> None:
    """/effort 写 runtime.json (与 _write_runtime_model 同一把锁, 同一套语义)。"""
    _write_runtime_field("effort", effort, ("default", "默认"),
                         "写 runtime.json effort 失败", lower=True)   # 封闭枚举 ⇒ 存小写


def _write_runtime_model(model: str) -> None:
    """/model 写 runtime.json (管理器也能看到; _get_client 下一轮活体 set_model 应用).

    flock(<home>/runtime.lock) 串行化读-改-写, 防与 fleet_admin set-model 对同 home 并发覆盖.
    ⚠ 空串也算"清空" —— 词表里那个 "" 是**故意的**, 别当成手滑删掉。
    """
    _write_runtime_field("model", model, ("default", "默认", "none", ""),
                         "⚠ 写 runtime 失败")


def _set_runtime_model(model: str, chat_id: str = "") -> str:
    """Write a model override and mark a real engine boundary for App clients."""
    before_engine = _runtime_snapshot()[2]
    chat = chat_id or app_frontend.CUR_CHAT.get() or APP_FRONT.chat_id or "app"
    clear = model.strip().lower() in ("default", "默认", "none", "")
    target_engine = _engine_for_model(DEFAULT_MODEL if clear else model)
    if target_engine == "codex" and not CODEX_ENABLED:
        APP_FRONT.broadcast({"ev": "error", "kind": "unsupported_engine",
                             "engine": "codex",
                             "text": "这台节点暂不开放 Codex 引擎。"})
        return "denied"
    target_model = DEFAULT_MODEL if clear else model
    if _prov.provider_for_model(target_model) == "deepseek" and not _prov.deepseek_configured():
        APP_FRONT.broadcast({"ev": "error", "kind": "unsupported_model",
                             "provider": "deepseek",
                             "text": "这台节点还没配置 DeepSeek。"})
        return "denied"
    before_model = _runtime_snapshot()[0]
    prev = _load_session(_session_chat_key(chat, before_engine)) or ""
    _write_runtime_model(model)
    after_model, _eff, after_engine = _runtime_snapshot()
    if after_engine == before_engine:
        # 同 engine 但换了 provider (Anthropic↔DeepSeek): 下一轮会开新会话 (不跨提供方
        # resume, 见 _get_client), 这里先告诉客户端, 并把旧会话交出去做「回上一个会话」入口。
        bp, ap = _prov.provider_for_model(before_model), _prov.provider_for_model(after_model)
        if (after_engine == "claude" and bp != ap and prev
                and not _prov.cross_provider_resume_allowed()):
            APP_FRONT.broadcast({
                "ev": "session_reset", "engine": after_engine,
                "prev": prev if _session_exists(prev) else "",
                "prev_engine": before_engine, "prev_provider": bp, "provider": ap,
                "model": _prov.app_model(after_model),
                "hint": "provider_switched", "ts": time.time(),
            })
            return "switched"
        return "same"

    # Claude transcripts can be pruned independently of sessions.json. Codex threads are
    # managed by the CLI and have no Claude transcript path, so an active recorded thread is
    # the strongest local resumability signal available here (the /resume path uses the same).
    resumable = bool(prev) and (before_engine == "codex" or _session_exists(prev))
    APP_FRONT.broadcast({
        "ev": "session_reset",
        "engine": after_engine,
        "prev": prev if resumable else "",
        "prev_engine": before_engine if resumable else "",
        "hint": "engine_switched",
        "ts": time.time(),
    })
    return "switched"


def _bjt_short(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return iso or "?"


async def handle_command(chat_id: str, text: str) -> bool:
    """处理 /斜杠命令; 返回 True=已处理, False=未识别(交给正常 turn)."""
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    if cmd in {"/new", "/reset", "/clear", "/fork", "/resume", "/model", "/effort"}:
        CODEX_QUESTIONS.cancel_work(chat_id, "control_changed")
    arg = parts[1].strip() if len(parts) > 1 else ""
    engine = _runtime_snapshot()[2]
    session_key = _session_chat_key(chat_id, engine)
    if cmd == "/help":
        SENDER.send(chat_id, _HELP_TEXT)
        return True
    if cmd == "/compact":
        if engine == "codex":
            thread = _load_session(session_key)
            if not thread:
                SENDER.send(chat_id, "（尚无活动 GPT 会话, 无可压缩）")
                return True
            before = _read_status_ctx(chat_id)
            APP_FRONT.broadcast({"ev": "compact", "phase": "start",
                                 "used": before.get("used"), "max": before.get("max")})
            SENDER.send(chat_id, "🗜 开始压缩 GPT 上下文（落盘内容不受影响）…",
                        kind="compact_start")
            error = ""
            try:
                with _busy_guard(chat_id):
                    await asyncio.wait_for(_codex_compact_thread(thread),
                                           timeout=CODEX_COMPACT_TIMEOUT)
                SENDER.send(chat_id, _COMPACT_DONE_TEXT, kind="compact_done")
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                log(f"[{chat_id[:8]}] Codex compact 失败: {error}")
                SENDER.send(chat_id, f"⚠ GPT 上下文压缩失败: {exc}")
            finally:
                APP_FRONT.broadcast({"ev": "compact", "phase": "done",
                                     "used": before.get("used"), "max": before.get("max"),
                                     "before_used": before.get("used"), "error": error})
            return True
        # 必须裸传 /compact，否则 CLI 会将其当普通文本；
        # 走 run_turn 全套管线, 压缩后的 ResultMessage 会顺带刷新 ctx 快照
        if CLIENTS.get(chat_id) is None and not _load_session(chat_id):
            SENDER.send(chat_id, "（尚无活动会话, 无可压缩）")   # 重启后 client 未建但会话可 resume → 照常跑
            return True
        
        # 就失灵。start/done 各广播一次, done 带压缩后的用量, 进度条与百分比都不用猜。
        before = _read_status_ctx(chat_id)
        APP_FRONT.broadcast({"ev": "compact", "phase": "start",
                             "used": before.get("used"), "max": before.get("max")})
        SENDER.send(chat_id, "🗜 开始压缩上下文（折叠成摘要, 落盘的不受影响）…",
                    kind="compact_start")      # 契约 §2.19: 认字段, 别认文案
        try:
            await run_turn(chat_id, "/compact")
        finally:  # 异常或中断时也要发送结束信号
            after = _read_status_ctx(chat_id)
            APP_FRONT.broadcast({"ev": "compact", "phase": "done",
                                 "used": after.get("used"), "max": after.get("max"),
                                 "before_used": before.get("used")})
        return True
    if cmd in ("/context", "/ctx"):
        if engine == "codex":
            try:
                s = json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
                row = (s.get("sessions") or {}).get(chat_id) or {}
                used = int(row.get("context_tokens") or 0)
                ts = row.get("ts") or ""
                SENDER.send(chat_id, f"GPT/Codex 最近一轮用量: {used} tokens"
                            + (f" · {ts}" if ts else "")
                            + "\n（Codex CLI 自动化暂不提供上下文窗口百分比。）")
            except Exception:  # noqa: BLE001
                SENDER.send(chat_id, "（还没有 GPT/Codex 用量快照；发一条 GPT 消息后再看。）")
            return True
        client = CLIENTS.get(chat_id)       # 不为查询而新建会话 (避免空起一个 Claude 子进程)
        if client is None:
            SENDER.send(chat_id, "（尚无活动会话；发一条消息后再 /context）")
            return True
        try:
            cu = await client.get_context_usage()
            SENDER.send(chat_id, _fmt_context(cu))
        except Exception as exc:  # noqa: BLE001
            SENDER.send(chat_id, f"⚠ 取上下文失败: {exc}")
        return True
    if cmd == "/cost":
        SENDER.send(chat_id, _fmt_cost(chat_id))
        return True
    if cmd == "/model":
        if app_frontend.DISPLAY_CHAT.get().startswith("group:"):
            SENDER.send(chat_id, "（群聊不切模型；请到这个 bot 的单独对话里设置。）")
            return True
        if not arg:
            SENDER.send(chat_id, "用法: /model <opus|sonnet|haiku|完整id|default>")
            return True
        result = _set_runtime_model(arg, chat_id)
        if result == "denied":
            SENDER.send(chat_id, "（这台节点暂不开放 Codex 引擎。）")
            return True
        note = "已切到独立上下文" if result == "switched" else "活体切换，记忆保留"
        SENDER.send(chat_id, f"⚙️ 模型 → {arg}，下一条消息生效（{note}）。")
        return True
    if cmd == "/effort":
        if app_frontend.DISPLAY_CHAT.get().startswith("group:"):
            SENDER.send(chat_id, "（群聊不切思考强度；请到这个 bot 的单独对话里设置。）")
            return True
        if not arg or arg.lower() not in EFFORT_ACCEPTED:
            SENDER.send(chat_id, f"用法: /effort <{'|'.join(EFFORT_LEVELS + ('default',))}>")
            return True
        _write_runtime_effort(arg.lower())
        SENDER.send(chat_id, f"⚙️ 思考强度 → {arg}，下一条消息生效（记忆保留）。")
        return True
    if cmd == "/plan":               # 计划模式: 先提案后批准 (本轮门 ExitPlanMode)
        if not arg:
            SENDER.send(chat_id, "用法: /plan <任务描述> — 我先给计划, 你回 y 才执行")
            return True
        if chat_id in ACTIVE:        # 防御: 即便调用点已查, 这里再兜一道
            SENDER.send(chat_id, "⏳ 上一条还在处理，发「停」可中断。")
            return True
        ACTIVE.add(chat_id)          # 本命令直接跑一个 plan turn
        try:
            await run_turn(chat_id, arg, None, plan=True)
        finally:
            ACTIVE.discard(chat_id)
        return True
    if cmd in ("/sessions", "/session"):
        active, items = _list_sessions(session_key)
        if not items:
            SENDER.send(chat_id, "（暂无会话；发一条消息即开新会话）")
            return True
        lines = ["🗂 **会话列表**"]
        for sid, name, ts in items[:20]:
            mark = "▶" if sid == active else "·"
            lines.append(f"{mark} `{sid[:8]}` {name or '(未命名)'} · {_bjt_short(ts)}")
        if len(items) > 20:
            lines.append(f"…（仅显示最近 20 条，共 {len(items)} 条；用 /resume <id> 精确匹配）")
        lines.append("\n`/resume <id|名字>` 切换 · `/fork` 分叉 · `/name <名>` 命名 · `/new` 开新")
        SENDER.send(chat_id, "\n".join(lines))
        return True
    if cmd == "/resume":
        hits = _resolve_session(session_key, arg)
        if not hits:
            SENDER.send(chat_id, f"没找到会话「{arg}」（`/sessions` 看列表）")
            return True
        if len(hits) > 1:            # 歧义 → 让用户用更长前缀, 不静默切错
            SENDER.send(chat_id, f"「{arg}」匹配到 {len(hits)} 个会话，请用更长 id 前缀或完整 id：\n"
                        + "\n".join(f"· `{h[:8]}`" for h in hits[:8]))
            return True
        sid = hits[0]
        if engine != "codex" and not _session_exists(sid):
            SENDER.send(chat_id, f"会话 `{sid[:8]}` 的记录已不在（被清理），无法恢复。")
            return True
        _set_active(session_key, sid)
        _session_tasks(chat_id)          # 同 /new: 换会话 → 任务卡跟着换, 别挂着上一个的
        await _drop_client(chat_id)      # 下条消息按新 active resume
        SENDER.send(chat_id, f"↩️ 已切到会话 `{sid[:8]}`，下一条消息接着它聊。")
        return True
    if cmd == "/fork":
        if engine == "codex":
            SENDER.send(chat_id, "（GPT/Codex 暂不支持 /fork；可以 /new 开一个新的 GPT 线程。）")
            return True
        if not _load_session(session_key):
            SENDER.send(chat_id, "当前没有可分叉的会话（先聊一句）")
            return True
        await _drop_client(chat_id)      # 先 drop (会清 FORK_PENDING) 再 add, 否则被自己清掉
        FORK_PENDING.add(chat_id)
        SENDER.send(chat_id, "🌿 已标记：**下一条消息**会从当前会话分叉出新副本，到时确认新会话号（原会话保留）。")
        return True
    if cmd == "/name":
        if not arg:
            SENDER.send(chat_id, "用法: /name <会话名>")
            return True
        ok = _name_active(session_key, arg)
        SENDER.send(chat_id, f"🏷 当前会话已命名为「{arg}」" if ok
                    else "当前没有活动会话可命名（先聊一句）")
        return True
    return False                     # 未识别 → 交给正常 turn (Claude 自己处理)


_INTERRUPT_WAIT_S = 5.0        # 掐完等它真结束的上限 (超时就如实说没停下来)


async def _do_interrupt(chat_id: str) -> tuple[str, int]:
    """中断当前回合，返回状态和清空的暂存数。idle 表示无活动回合，stopped 表示确认结束，stuck 表示超时仍在运行。"""
    CODEX_QUESTIONS.cancel_work(chat_id, "stopped")
    proc = CODEX_PROCS.get(chat_id)
    if chat_id in ACTIVE and proc is not None and proc.returncode is None:
        approvals = CODEX_APPROVALS.get(chat_id)
        if approvals is not None:
            await approvals.close()
        _dropped_items = COALESCE.pop(chat_id, [])
        dropped = len(_dropped_items)
        if _visible_queue_count(_dropped_items):
            APP_FRONT.broadcast({"ev": "queue_flush"})
        INTERRUPTED.add(chat_id)
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=_INTERRUPT_WAIT_S)
            return "stopped", dropped
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
                await proc.wait()
            return "stopped", dropped
        except Exception:
            INTERRUPTED.discard(chat_id)
            raise
    client = CLIENTS.get(chat_id)
    if not ((chat_id in ACTIVE or chat_id in IDLE_ACTIVE) and client is not None):
        return "idle", 0
    _dropped_items = COALESCE.pop(chat_id, [])  # 停 → 同时清空忙时暂存队列
    dropped = len(_dropped_items)
    if _visible_queue_count(_dropped_items):
        APP_FRONT.broadcast({"ev": "queue_flush"})   # 被清的排队气泡也恢复正常样式
    INTERRUPTED.add(chat_id)        # 让 run_turn 抑制随后的 error_during_execution 回复
    try:
        await client.interrupt()
    except Exception:
        INTERRUPTED.discard(chat_id)
        raise
    # 有界地等它真的收尾: run_turn 的 finally 会把 chat 从 ACTIVE 里放掉。
    # 必须有界 —— 无界等 = 停止按钮自己也挂住, 那是拿一个卡死换另一个。
    deadline = time.monotonic() + _INTERRUPT_WAIT_S
    while time.monotonic() < deadline:
        if chat_id not in ACTIVE and chat_id not in IDLE_ACTIVE:
            return "stopped", dropped
        await asyncio.sleep(0.1)
    log(f"[{chat_id[:8]}] ⚠ 中断已发出, 但 {_INTERRUPT_WAIT_S:.0f}s 后这一轮仍占着 —— "
        f"回执如实报 stuck (客户端会显示「没停下来」)")
    return "stuck", dropped


def _queued_parts(item: QueuedInput) -> tuple[str, float, str | None, bool]:
    """Normalize queued input to text/time/display/visible; old tuples stay user-visible."""
    if len(item) == 2:
        return item[0], item[1], None, True
    if len(item) == 3:
        return item[0], item[1], item[2], True
    return item


def _visible_queue_count(items: list[QueuedInput]) -> int:
    return sum(1 for item in items if _queued_parts(item)[3])


def _steering_batch(items: list[QueuedInput]) -> str | None:
    """Return one ordered same-turn update, or None when control messages are present."""
    if not items:
        return None
    for item in items:
        text, _arrived, _display, _visible = _queued_parts(item)
        low = text.strip().lower()
        if text.startswith("/") or low in _NEW_SESSION_WORDS or low in _INTERRUPT:
            return None
    return "\n\n".join(_stamp(text, arrived, display)
                         for text, arrived, display, _visible in map(_queued_parts, items))


async def _steer_pending(chat_id: str) -> bool:
    """Move the current ordinary-message queue into the active model turn.

    Queue removal happens only after the provider accepted the update. Commands and any
    provider/protocol failure retain the old post-turn drain behavior.
    """
    lock = STEER_LOCKS.setdefault(chat_id, asyncio.Lock())
    async with lock:
        queued = COALESCE.get(chat_id)
        if not queued or chat_id in ROLLOVER_HOLD:
            return False
        snapshot = list(queued)
        # A turn's reply goes to one display. Steering a message from another display into
        # it answers that message in the wrong place. Leave it queued; drain runs it
        # under its own display.
        turn_display = TURN_DISPLAY.get(chat_id) or chat_id
        foreign = {d for _t, _a, d, _v in map(_queued_parts, snapshot)
                   if (d or chat_id) != turn_display}
        if foreign:
            log(f"[{chat_id[:8]}] 排队消息落点 {sorted(foreign)} ≠ 本轮 {turn_display}: "
                f"不并轮, 本轮结束后在原落点处理")
            return False
        text = _steering_batch(snapshot)
        if not text:
            return False
        try:
            control = CODEX_TURNS.get(chat_id)
            if control is not None:
                await control.steer(text)
                engine = "codex"
            elif chat_id in CLAUDE_STEERABLE:
                client = CLIENTS.get(chat_id)
                if client is None:
                    return False
                await client.query(text)
                engine = "claude"
            else:
                return False
        except Exception as exc:  # noqa: BLE001 - queue is the reliable fallback
            log(f"[{chat_id[:8]}] steering 未受理, 保留队列: {type(exc).__name__}: {exc}")
            return False

        current = COALESCE.get(chat_id) or []
        if current[:len(snapshot)] != snapshot:
            log(f"[{chat_id[:8]}] steering 后队列前缀变化, 未移除以免丢消息")
            return False
        remaining = current[len(snapshot):]
        if remaining:
            COALESCE[chat_id] = remaining
        else:
            COALESCE.pop(chat_id, None)
        # provider 已经接收这批 steering —— 这是唯一能证明「当前回合读到哪条补充」
        # 的边界。先发逐批 accept，再在整队列空时发旧的 flush；网关会把 count 对回
        # 具体 user entry_id，客户端据此只恢复已接收的气泡并切开 live 回复。
        # 文案只能叫「已接收」，不能叫「已执行」：steer 返回不证明模型做完了什么。
        _visible_count = _visible_queue_count(snapshot)
        if _visible_count:
            APP_FRONT.broadcast({"ev": "queue_accept", "count": _visible_count,
                                 "ts": time.time(), "chat_id": turn_display})
        if not remaining and _visible_count:
            APP_FRONT.broadcast({"ev": "queue_flush", "chat_id": turn_display})
        inflight_queue(chat_id, [_queued_parts(item)[0] for item in remaining])
        # ⚠ **两个数都要打**: 广播 `queue_accept` 看的是 `_visible_count`,
        # 而这行原来只打 `len(snapshot)` —— 全是不可见条目时日志照说"接收了 N 条",
        # 客户端却一帧都没收到, **两边都不报错, 谁也分辨不出是哪种**。
        
        # 因为日志给的那个数**恰好不是决定发不发的那个数**。
        # ⇒ 不是日志说谎, 是**日志和行为看的不是同一个变量** —— 于是日志越正常,
        #   越说明不了行为发生了。(同族: 退出码 0 不等于命令跑过。)
        _acc = "已发" if _visible_count else "未发"
        log(f"[{chat_id[:8]}] {engine} 同轮接收 {len(snapshot)} 条排队消息 "
            f"(可见 {_visible_count} 条 → queue_accept {_acc})")
        return True


@contextlib.contextmanager
def _display_scope(display: str | None):
    """Run the block under `display`; None (legacy/feishu items) keeps the current context."""
    if display is None:
        yield
        return
    token = app_frontend.DISPLAY_CHAT.set(display)
    try:
        yield
    finally:
        app_frontend.DISPLAY_CHAT.reset(token)


async def _drain_coalesce(chat_id: str) -> None:
    '按序处理忙时排队的消息。每条回合结束路径都应调用；处理期间继续排队以避免乱序。'
    with _busy_guard(chat_id):
        try:
            while COALESCE.get(chat_id):
                buffered = COALESCE.pop(chat_id)
                SENDER.feishu.send(chat_id, f"▶️ 处理暂存的 {len(buffered)} 条…")
                # 排队气泡恢复正常样式 —— 发给每个排了可见消息的落点, 不能靠 contextvar:
                # drain 跑在上一轮的 context 里, 群里的气泡会永远是半透明的。
                for d in dict.fromkeys(_d or chat_id for _t, _a, _d, v
                                       in map(_queued_parts, buffered) if v):
                    APP_FRONT.broadcast({"ev": "queue_flush", "chat_id": d})
                # Merged content runs as one turn only while the display stays the same:
                # each segment runs under its own display, or the reply lands where the
                # previous turn was shown (group messages answered in DM).
                content: list[str] = []
                content_display: str | None = None
                for queued in buffered:
                    item, item_at, item_display, _visible = _queued_parts(queued)
                    il = item.strip().lower()
                    if content and item_display != content_display:
                        with _display_scope(content_display):
                            await run_turn(chat_id, "\n\n".join(content))
                        content = []
                    if not (item.startswith("/") or il == "新会话"):
                        content.append(_stamp(item, item_at, item_display))
                        content_display = item_display
                        continue
                    if content:                            # 命令前先把累积内容合并跑掉, 保持顺序
                        with _display_scope(content_display):
                            await run_turn(chat_id, "\n\n".join(content))
                        content = []
                    with _display_scope(item_display):
                        if il in _NEW_SESSION_WORDS:
                            await _new_session(chat_id)
                        elif not await handle_command(chat_id, item):
                            await run_turn(chat_id, _stamp(item, item_at, item_display))
                if content:
                    with _display_scope(content_display):
                        await run_turn(chat_id, "\n\n".join(content))
        finally:
            # 一条炸了不能让剩下的永远躺在内存里 —— 那才是"消息真丢了"的形态
            COALESCE.pop(chat_id, None)


async def handle_message(chat_id: str, text: str, message_id: str | None = None) -> None:
    if _MANAGED_ENTRY is not None:
        return await _MANAGED_ENTRY.handle_message(chat_id, text, message_id)
    peer = app_frontend.PEER_REPORT.get()
    if peer is not None and _defer_peer_report(chat_id, _display_chat_for(chat_id), text,
            app_frontend.MESSAGE_ARRIVAL.get() or time.time(), peer):
        return
    CODEX_QUESTIONS.cancel_work(chat_id, "unrelated_input")
    arrived = time.time()          # 送达时刻 (非处理时刻) — 见 _stamp 注释
    display_chat = _display_chat_for(chat_id)  # 逐消息捕获; 排队后 context 可能已经换了
    text = text.strip()
    low = text.lower()
    # 1) 审批 y/n (PENDING 优先于一切)
    if chat_id in PENDING:
        fut = PENDING.get(chat_id)
        if fut and not fut.done():
            if low in _YES:
                fut.set_result(True)
            elif low in _NO or low in _INTERRUPT:   # 停/取消 当作拒绝 (审批时的逃生口)
                fut.set_result(False)
            else:
                SENDER.send(chat_id, "请回复 y 批准 / n 拒绝。")
        return
    # 1.5) 提问卡等答案中 → 本条消息就是答案 (停 = 取消提问并继续走中断)
    if chat_id in ASK_PENDING:
        _aid, fut = ASK_PENDING[chat_id]
        if text.startswith("📨 [来自"):
            pass  # bot 注入不是提问的答案
        elif fut and not fut.done():
            if low in _INTERRUPT:
                fut.set_result(None)          # 取消提问; 不 return, 落到下面的中断逻辑掐整轮
            else:
                fut.set_result(text)
                return
    # 2) 中断词 — 必须在 ACTIVE 拦截**之前** (否则被"上一条还在处理"挡掉)
    if low in _INTERRUPT:
        try:
            state, dropped = await _do_interrupt(chat_id)
        except Exception as exc:  # noqa: BLE001
            SENDER.send(chat_id, f"⚠ 中断失败: {exc}")
            return
        # 三种状态三句话 —— 原来「没在跑」和「掐不动」共用一句「当前没有在跑的任务」,
        # 正在运行但未能停止时，应报告不同状态。
        if state == "stopped":
            SENDER.send(chat_id, "🛑 已中断当前任务。" + (f"（并清空 {dropped} 条暂存）" if dropped else ""))
        elif state == "stuck":
            SENDER.send(chat_id, f"⚠ 中断已发出, 但这一轮还没停下来（等了 {_INTERRUPT_WAIT_S:.0f} 秒）。"
                                 + (f"已清空 {dropped} 条暂存。" if dropped else "")
                                 + "再发一次「停」可重试; 仍不动就得重启这个 bot。")
        else:
            SENDER.send(chat_id, "（当前没有在跑的任务）")
        return
    # 3) 同会话已有 turn 在跑 → 全部暂存 (含斜杠命令); 内容合并、命令逐条, 均在 turn 结束后按序处理
    if chat_id in ACTIVE or chat_id in ROLLOVER_HOLD:
        buf = COALESCE.setdefault(chat_id, [])
        _visible = not app_frontend.INTERNAL_GROUP_TASK.get()
        buf.append((text, arrived, display_chat, _visible))
        inflight_queue(chat_id, [_queued_parts(item)[0] for item in buf])
        # App 端不发「已暂存」文字: 广播结构化 queued, 客户端把该条
        # 用户气泡置浅色半透明; 飞书没有气泡样式, 仍用文字提示
        SENDER.feishu.send(chat_id, f"📥 已暂存（第 {len(buf)} 条），当前任务跑完后处理。")
        # ts = **服务端**收下它的时刻 (epoch 秒, 与 bg_tasks 一致)。客户端要显示
        # 「已排队 N 分钟」就必须用这个: 它冷启动/重连是从**历史回放**拿到这条的,
        # 本地根本没有"我什么时候收到" —— 拿回放时刻当起点等于每次重连把计时器清零,
        # 而卡住的那轮不会清零。**最需要它的场景下最不准。**
        if _visible:
            APP_FRONT.broadcast({"ev": "queued",
                                 "count": _visible_queue_count(buf),
                                 "ts": time.time()})
        if chat_id in ACTIVE:
            await _steer_pending(chat_id)
        return
    # 4) 开新会话 (active 置空; 旧会话仍留 list 可 /resume)
    if low == "/retry":
        # App 的「重试」按钮。**故意不是「机械重跑上一条 prompt」**:
        # 上一轮可能已经做了一半 (写了文件、发了消息), 机械重跑会重复那些副作用;
        # 让模型看到「重试」二字自己判断, 它能看到完整上下文, 自己决定从哪儿接。
        # 这是用户手打「重试」时**现在就在工作**的行为 —— 按钮只该省掉打字, 不该换掉语义。
        # 走 command 帧进来的不广播 user 事件, 所以气泡也省了 (见 _app_command)。
        text, low = "重试", "重试"
    if low in _NEW_SESSION_WORDS:
        await _new_session(chat_id)
        return
    # 5) 其它斜杠命令 —— 跑完也要放队列 (见 _drain_coalesce)
    if text.startswith("/") and await handle_command(chat_id, text):
        await _drain_coalesce(chat_id)
        return
    # 6) 正常 → 跑一个 Claude turn; 跑完按序处理忙时暂存 (连续内容合并成一条, 斜杠命令逐条真执行)
    ACTIVE.add(chat_id)
    inflight_mark(chat_id, text)      # 与 ACTIVE 同生共死, 清理在下面的 finally
    try:
        await run_turn(chat_id, _stamp(text, arrived, display_chat), message_id)
        await _drain_coalesce(chat_id)
    finally:
        ACTIVE.discard(chat_id)
        COALESCE.pop(chat_id, None)   # 防御: 异常退出时清理残留暂存
        # 出口清单就收在这一句上: 正常跑完、抛异常、被中断、/reset (它在进 ACTIVE 前就
        # return 了) 全覆盖 —— 逃得掉的只有进程被杀, 那正是要留痕的那种
        inflight_clear(chat_id)
        stale_ask = ASK_PENDING.pop(chat_id, None)
        if stale_ask and not stale_ask[1].done():
            ASK_DISPLAY.pop(stale_ask[0], None)
            stale_ask[1].set_result(None)   # 解除钩子阻塞, 防孤儿 future 挂 10 分钟


# 「开新会话」的全部说法 —— **必须只有一份**。
# 原来这个集合在主路径和排队 flush 路径上各写了一遍字面量: 加一个别名只改一处的话,
# 表现是「直接发认、排队时不认」, 而且不报错 —— 用户忙时发的那条会被当成普通消息

# checkout 写死的两个文件名, 而指纹覆盖全部运行时文件), 它那句总结值得抄在这儿:
# **写死清单 vs 动态集合** —— 凡是"同一份名单在两处各写一遍"的地方, 迟早分叉。
_NEW_SESSION_WORDS = {"/reset", "/new", "/clear", "新会话"}


async def _new_session(chat_id: str) -> None:
    """开新会话: 断开 client、清 active、告知两端。

    App 使用结构化 session_reset 更新界面，飞书端发送文字反馈。
    prev 保存前一个 session_id，供客户端提供恢复入口。
    """
    CODEX_QUESTIONS.cancel_work(chat_id, "session_reset")
    engine = _runtime_snapshot()[2]
    session_key = _session_chat_key(chat_id, engine)
    prev = _load_session(session_key) or ""
    if engine == "claude":
        await _drop_client(chat_id)
    _clear_session(session_key)
    _session_tasks(chat_id)     # 换会话 → 清单归零并广播, 卡当场收掉 (不等下次工具调用)
    SENDER.feishu.send(chat_id, "🔄 已开新会话（旧会话仍在 `/sessions` 里，可 `/resume` 回去）。")
    APP_FRONT.broadcast({"ev": "session_reset", "prev": prev, "engine": engine, "ts": time.time()})


# ── nightly auto-rollover after repeated compacts (pure half: fleet_rollover.py) ──────────────

# drift. At night, an idle session with >= N compacts writes a hand-off note from a FORKED
# one-shot session (off the chat channel: nothing reaches the App at 3 am, the original
# session is untouched), then gets a fresh session whose first turn is told to read the note.
# ⚠ Fail-soft everywhere: any failure keeps the old session; the whole fleet runs this code.
from feishu_bridge import fleet_rollover as _ro  # noqa: E402
ROLLOVER_HOLD: set[str] = set()          # chat keys mid-rollover: inbound messages queue
_ROLLOVER_TRIED: dict[str, str] = {}     # chat key -> local date attempted (no retry the same night)
_ROLLOVER_SKIPPED: dict[str, str] = {}   # chat key -> local date an "unsafe" skip was logged
ROLLOVER_NOTE_TIMEOUT_S = 600
ROLLOVER_TICK_S = 600
ROLLOVER_CODEX_MODEL = "gpt-6.1-sol"   # one-shot only; never changes runtime.json


def _rollover_chat_engine(key: str) -> tuple[str, str]:
    return (key[:-7], "codex") if key.endswith("::codex") else (key, "claude")


def _rollover_unsafe(key: str, transcript: Path, now: float) -> str:
    """Why this session must not roll over now ('' = safe)."""
    key, _engine = _rollover_chat_engine(key)
    if key in ACTIVE or key in CODEX_PROCS or key in CODEX_TURNS:
        return "turn in flight"
    if key in IDLE_ACTIVE:
        return "background-wake turn in flight"
    if COALESCE.get(key):
        return "queued input"
    if key in ASK_PENDING:
        return "pending question card"
    if key in FORK_PENDING:
        return "/fork pending"
    if key in ROLLOVER_HOLD:
        return "rollover in flight"
    if key in PENDING:
        return "pending approval"
    try:
        age = now - transcript.stat().st_mtime
    except OSError:
        return "transcript unreadable"
    # the bridge can't see a still-running background Bash/Agent task; a quiet transcript is the proxy
    if age < _ro.QUIET_S:
        return f"active {int(age // 60)} min ago"
    return ""


def _rollover_broadcast(key: str, ev: dict) -> None:
    """Show it where that session is displayed (a group session's divider belongs in the group)."""
    chat, _engine = _rollover_chat_engine(key)
    def _send() -> None:
        app_frontend.DISPLAY_CHAT.set(chat)
        APP_FRONT.broadcast(ev)
    contextvars.copy_context().run(_send)


async def _rollover_write_note(key: str, sid: str, path: Path, compacts: int) -> str:
    """Forked one-shot session writes the hand-off note. → '' on success, else the reason."""
    model, effort, _engine = _runtime_snapshot()
    if _rollover_chat_engine(key)[1] == "codex":
        return await _rollover_write_note_codex(sid, path, compacts)
    if not _prov.can_resume(_SESSION_PROVIDERS_FILE, sid, model):
        return "session belongs to another provider than the current model"
    try:
        prov_env = _prov.provider_env(model)
    except _prov.ProviderNotConfigured as exc:
        return str(exc)
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    opts = ClaudeAgentOptions(
        cwd=REPO,
        env={"FLEET_CHAT_ID": key, **prov_env},
        permission_mode="bypassPermissions",
        model=_prov.cli_model(model),
        effort=effort,
        resume=sid,
        fork_session=True,           # the original session is never written
        system_prompt={"type": "preset", "preset": "claude_code", "append": _BRIDGE_NOTE},
        max_buffer_size=16 * 1024 * 1024,
        cli_path=None,
    )
    client = ClaudeSDKClient(options=opts)

    async def _run() -> None:
        completed = False
        await client.connect()
        await client.query(_ro.handoff_prompt(path, compacts))
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                if msg.is_error:
                    raise RuntimeError("Claude handoff failed")
                completed = True
                break
        if not completed:
            raise RuntimeError("Claude handoff ended without success")

    try:
        await asyncio.wait_for(_run(), ROLLOVER_NOTE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
    try:
        st = path.stat()
    except OSError:
        return "note not written"
    if st.st_mtime < started or st.st_size < _ro.NOTE_MIN_BYTES:
        return "note missing or too short"
    return ""


def _rollover_codex_command(overrides: dict | None = None) -> list[str]:
    """Disable side-effect surfaces before startup, not just after the fork exists.

    Keep only supported feature switches (validated against Codex 0.159.2). Read-only
    sandboxing is a second boundary, not a substitute for disabling remote connectors.
    Hooks/notify must also stay off: they run outside the model's tool event stream.
    """
    features = ("apps", "plugins", "shell_tool", "unified_exec", "multi_agent",
                "multi_agent_v2", "hooks", "goals", "computer_use", "browser_use",
                "browser_use_external", "image_generation", "view_image", "skill_search",
                "tool_suggest", "code_mode_host", "memories", "sleep_tool", "remote_plugin",
                "workspace_dependencies")
    command = [CODEX_BIN, "app-server", "--listen", "stdio://"]
    for feature in features:
        command.extend(["-c", f"features.{feature}=false"])
    command.extend(["-c", 'web_search="disabled"', "-c", "notify=[]",
                    "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"'])
    # CLI overrides are TOML, not JSON. Literal table keys preserve dots/@ in names.
    def literal(value):
        if isinstance(value, dict):
            return "{" + ",".join(json.dumps(k) + "=" + literal(v)
                                  for k, v in value.items() if v is not None) + "}"
        if isinstance(value, list):
            return "[" + ",".join(literal(v) for v in value) + "]"
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    for section, value in (overrides or {}).items():
        command.extend(["-c", section + "=" + literal(value)])
    return command


async def _rollover_codex_tool_config(on_event) -> dict:
    """Config-only discovery, no thread/model/MCP-status calls. Never initialize tools.

    Thread-level overrides do not cover process-level MCP discovery. Read the effective
    layers first, then start the generation process with every connector already disabled.
    """
    storage = _codex_storage.select()
    proc = await asyncio.create_subprocess_exec(*storage.command(_rollover_codex_command()),
        cwd=REPO, env=storage.env(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=CODEX_STREAM_LIMIT)
    stderr = asyncio.create_task(proc.stderr.read())
    try:
        await _codex_rpc_send(proc, {"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "fleet-rollover-config", "version": "1.0"},
            "capabilities": {"experimentalApi": True}}})
        await _codex_rpc_response(proc, 1, on_event=on_event)
        await _codex_rpc_send(proc, {"method": "initialized"})
        await _codex_rpc_send(proc, {"id": 4, "method": "config/read",
            "params": {"includeLayers": False, "cwd": REPO}})
        response = await _codex_rpc_response(proc, 4, on_event=on_event)
        config = (response.get("result") or {}).get("config")
        if not isinstance(config, dict):
            raise RuntimeError("Codex handoff cannot inspect tool configuration")
        storage.effective(config)
        return config
    finally:
        proc.stdin.close()
        if proc.returncode is None:
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)
        stderr.cancel()
        with contextlib.suppress(BaseException):
            await stderr


async def _rollover_write_note_codex(sid: str, path: Path, compacts: int) -> str:
    """Private restricted fork; reject tool events and never work on the source thread."""
    if not CODEX_ENABLED:
        return "Codex disabled on this node"
    proc = None
    stderr_task = None
    buffered: deque[dict] = deque()

    def collect(ev: dict) -> None:
        # Fail closed even before thread/fork or turn/start replies. A denied tool
        # followed by a fluent answer is still not a successful handoff.
        error = _codex_error_event(ev)
        if error and not _codex_is_informational(error[0]):
            raise RuntimeError(error[0])
        if error:
            return  # known informational error-shaped frames are not tool activity
        method = ev.get("method")
        params = ev.get("params") or {}
        item = params.get("item") or {}
        if (("method" in ev and "id" in ev)
                or (method in ("item/started", "item/completed")
                    and item.get("type") not in ("agentMessage", "reasoning", "userMessage"))):
            raise RuntimeError("Codex handoff attempted a tool or server request")
        buffered.append(ev)

    async def events():
        while buffered:
            yield buffered.popleft()
        async for raw in proc.stdout:
            ev = json.loads(raw)
            if not isinstance(ev, dict):
                raise RuntimeError("Codex handoff received a malformed frame")
            collect(ev)
            if buffered:
                yield buffered.popleft()

    async def _run() -> None:
        nonlocal proc, stderr_task
        storage = _codex_storage.select()
        storage.transcript(sid)
        config = await _rollover_codex_tool_config(collect)
        buffered.clear()  # discovery events belong to a different, closed connection
        overrides = {}
        for section in ("mcp_servers", "plugins"):
            entries = config.get(section, {})
            if not isinstance(entries, dict):
                raise RuntimeError("Codex handoff tool configuration unrecognized")
            if any(not isinstance(value, dict) for value in entries.values()):
                raise RuntimeError("Codex handoff connector configuration unrecognized")
            # App Server's override path parser does not understand TOML-quoted keys.
            # Replace the section as one value; preserve transport fields and literal names.
            # config/read includes null optionals; JSON->TOML maps null to "", which
            # fails typed timeout fields. Omit those fields when round-tripping.
            overrides[section] = {name: {**{field: setting for field, setting in value.items()
                                            if setting is not None}, "enabled": False}
                                  for name, value in entries.items()}
        proc = await asyncio.create_subprocess_exec(*storage.command(_rollover_codex_command(overrides)),
            cwd=REPO, env=storage.env(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=CODEX_STREAM_LIMIT)
        stderr_task = asyncio.create_task(proc.stderr.read())
        await _codex_rpc_send(proc, {"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "fleet-rollover", "version": "1.0"},
            "capabilities": {"experimentalApi": True}}})
        await _codex_rpc_response(proc, 1, on_event=collect)
        await _codex_rpc_send(proc, {"method": "initialized"})
        await _codex_rpc_send(proc, {"id": 4, "method": "config/read",
            "params": {"includeLayers": False, "cwd": REPO}})
        effective = (await _codex_rpc_response(proc, 4, on_event=collect)).get("result", {}).get("config")
        if not isinstance(effective, dict):
            raise RuntimeError("Codex handoff cannot verify disabled connectors")
        storage.effective(effective)
        features = effective.get("features", {})
        if (not isinstance(features, dict) or any(features.get(name) is not False
                for name in ("apps", "plugins", "hooks", "goals", "shell_tool", "unified_exec"))):
            raise RuntimeError("Codex handoff side-effect features are not disabled")
        for section in ("mcp_servers", "plugins"):
            entries = effective.get(section, {})
            if (not isinstance(entries, dict) or any(not isinstance(value, dict)
                    or value.get("enabled") is not False for value in entries.values())):
                raise RuntimeError("Codex handoff connector is not disabled")
        await _codex_rpc_send(proc, {"id": 2, "method": "thread/fork", "params": {
            "threadId": sid, "excludeTurns": True, "ephemeral": True,
            "cwd": REPO,  # ephemeral forks cannot carry/defer a persisted goal (0.159.2)
            "model": ROLLOVER_CODEX_MODEL, "sandbox": "read-only",
            "approvalPolicy": "never", "config": {**overrides, **storage.config()},
            "developerInstructions": _ro.codex_handoff_prompt(compacts)}})
        response = await _codex_rpc_response(proc, 2, on_event=collect)
        fork = str(((response.get("result") or {}).get("thread") or {}).get("id") or "")
        if not fork or fork == sid:
            raise RuntimeError("Codex did not return an independent fork")
        await _codex_rpc_send(proc, {"id": 3, "method": "turn/start", "params": {
            "threadId": fork, "input": [{"type": "text", "text": _ro.codex_handoff_prompt(compacts)}],
            "model": ROLLOVER_CODEX_MODEL, "effort": "high", "approvalPolicy": "never",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False}}})
        response = await _codex_rpc_response(proc, 3, on_event=collect)
        turn_id = str(((response.get("result") or {}).get("turn") or {}).get("id") or "")
        if not turn_id:
            raise RuntimeError("Codex did not return a handoff turn")
        note = ""
        completed = False
        async for ev in events():
            params = ev.get("params") or {}
            error = _codex_error_event(ev, fork, turn_id)
            if error and not _codex_is_informational(error[0]):
                raise RuntimeError(error[0])
            if params.get("threadId") not in (None, fork):
                continue
            if params.get("turnId") not in (None, turn_id):
                continue
            method = ev.get("method")
            item = params.get("item") or {}
            if method == "item/started" and item.get("type") not in (
                    "agentMessage", "reasoning", "userMessage"):
                raise RuntimeError("Codex handoff attempted a tool")
            if method == "item/agentMessage/delta":
                note += str(params.get("delta") or "")
            elif method == "item/completed" and item.get("type") == "agentMessage":
                note = str(item.get("text") or note)
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                if turn.get("id") != turn_id:
                    continue
                if turn.get("status") != "completed":
                    raise RuntimeError("Codex handoff turn failed")
                completed = True
                break
        if not completed or len(note.strip().encode("utf-8")) < _ro.NOTE_MIN_BYTES:
            raise RuntimeError("Codex note missing, incomplete or too short")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(note.strip(), encoding="utf-8")
        tmp.replace(path)

    try:
        await asyncio.wait_for(_run(), ROLLOVER_NOTE_TIMEOUT_S)
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        if proc is not None:
            if proc.stdin:
                proc.stdin.close()
            if proc.returncode is None:
                proc.kill()  # bounded cleanup, including cancellation and a stuck child
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
        if stderr_task is not None:
            stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await stderr_task


async def _rollover_one(key: str, sid: str, transcript: Path, compacts: int) -> bool:
    if _fs.MANAGED_TRANSACTION is not None:
        # The legacy nightly path can manufacture model work and replace the
        # whole map outside the selected writer transaction. Until an explicit
        # reviewed rollover grant exists, stop BEFORE either effect; no retry.
        return False
    chat, engine = _rollover_chat_engine(key)
    path = _ro.note_path(_BRIDGE_HOME, key)
    try:
        before = _ro.fingerprint(transcript)
    except OSError:
        return False
    # Recheck after counting in a worker (input may have arrived during that await).
    if _rollover_unsafe(key, transcript, time.time()) or _load_session(key) != sid:
        return False
    ROLLOVER_HOLD.add(chat)
    try:
        await _drop_client(chat)      # never two processes on one session
        started = time.time()
        why = await _rollover_write_note(key, sid, path, compacts)
        if not why:
            try:
                moved = _ro.fingerprint(transcript) != before
                st = path.stat()
                if st.st_mtime < started or st.st_size < _ro.NOTE_MIN_BYTES:
                    why = "note missing, stale or too short"
            except OSError:
                moved = True
            if (moved or chat in ACTIVE or chat in IDLE_ACTIVE or chat in CODEX_PROCS
                    or chat in CODEX_TURNS or chat in ASK_PENDING
                    or chat in PENDING or _load_session(key) != sid
                    or _runtime_snapshot()[2] != engine):
                why = "the session changed while the note was being written"
        if why:
            log(f"[{key[:8]}] ⚠ auto-rollover failed after {compacts} compacts: {why} (session kept)")
            _rollover_broadcast(key, {"ev": "notice", "text": (
                f"⚠ 夜间自动换新会话没成功（{why}），还在原来的会话上，明晚再试。")})
            return False
        # Persist the handoff first. Strict writes here: session helpers deliberately swallow
        # errors, which would otherwise announce a reset that was never saved.
        _ro.mark_pending(_BRIDGE_HOME, key, path, time.time(), sid)
        data = json.loads(_fs.SESSIONS_FILE.read_text(encoding="utf-8"))
        entry = _fs.chat_entry(data, key)
        if entry.get("active") != sid:
            raise RuntimeError("active session changed before reset")
        data[key] = {"active": None, "sessions": entry["sessions"], "rollover_pending": True}
        tmp = _fs.SESSIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_fs.SESSIONS_FILE)
        _rollover_broadcast(key, {"ev": "session_reset", "prev": sid, "engine": engine,
                                  "hint": "auto_rollover", "ts": time.time()})
        log(_ro.log_line(key, compacts, path, sid))
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"[{key[:8]}] ⚠ auto-rollover failed: {type(exc).__name__}: {exc}")
        return False
    finally:
        ROLLOVER_HOLD.discard(chat)
        if COALESCE.get(chat):
            await _drain_coalesce(chat)       # what arrived during the note goes out now, in order


async def _rollover_tick(now: float | None = None) -> None:
    now = time.time() if now is None else now
    if not _ro.in_window(now):
        return
    enabled, need = _ro.settings(_read_runtime())
    engine = _runtime_snapshot()[2]
    if not enabled or engine not in ("claude", "codex") or (engine == "codex" and not CODEX_ENABLED):
        return
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    for key in list(_read_sessions()):
        if _rollover_chat_engine(key)[1] != engine or _ROLLOVER_TRIED.get(key) == today:
            continue
        sid = _load_session(key)
        transcript = (_codex_storage.select().transcript(sid)
                      if engine == "codex" else _fs.transcript_path(sid)) if sid else None
        if transcript is None:
            continue
        compacts = await asyncio.to_thread(_ro.count_compacts, transcript, engine)
        if compacts < need:
            continue
        why = _rollover_unsafe(key, transcript, now)
        if why:
            if _ROLLOVER_SKIPPED.get(key) != today:
                _ROLLOVER_SKIPPED[key] = today
                log(f"[{key[:8]}] auto-rollover waits: {compacts} compacts, not now ({why})")
            continue
        _ROLLOVER_TRIED[key] = today
        await _rollover_one(key, sid, transcript, compacts)


async def _rollover_loop() -> None:
    while True:
        await asyncio.sleep(ROLLOVER_TICK_S)
        try:
            await _rollover_tick()
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ auto-rollover tick failed: {type(exc).__name__}: {exc}")


def _rollover_first_turn(chat_id: str, prompt: str, engine: str = "claude",
                         consume: bool = True) -> str:
    """The new session's first turn is told where the note is. Taken once; dropped if the user
    went back to the old session (it is not a new session any more)."""
    try:
        key = _session_chat_key(chat_id, engine)
        got = (_ro.take_pending(_BRIDGE_HOME, key, strict=engine == "codex") if consume
               else _ro.peek_pending(_BRIDGE_HOME, key, strict=engine == "codex"))
        if not got or _load_session(key):
            return prompt
        hint = _ro.first_turn_note(Path(got["path"]), float(got.get("at") or time.time()), engine)
        if engine == "codex":
            # Embed the saved note so ingestion does not depend on tool availability.
            note = Path(got["path"]).read_text(encoding="utf-8")
            if len(note.strip().encode("utf-8")) < _ro.NOTE_MIN_BYTES:
                raise ValueError("pending handoff note missing or too short")
            hint += "\n[交接笔记正文]\n" + note + "\n[交接笔记结束]"
        return hint + "\n" + prompt
    except Exception as exc:  # noqa: BLE001
        log(f"[{chat_id[:8]}] ⚠ auto-rollover note hint skipped: {exc}")
        if engine == "codex":
            raise  # caller fails softly; never start a fresh thread without its handoff
        return prompt


async def handle_image(chat_id: str, message_id: str, image_key: str, caption: str = "") -> None:
    """图片: 下载存盘 → 让 Claude 用 Read 工具看图 (原生视觉, 不转文字)."""
    path = await asyncio.to_thread(
        SENDER.download_incoming, message_id, image_key, "image", ".png")
    if not path:
        SENDER.send(chat_id, "⚠ 图片下载失败。")
        return
    extra = f"\n用户附带文字: {caption}" if caption else "\n用户没有附加文字, 请看图理解需求。"
    prompt = f"用户发来一张图片, 已保存在本地: {path}\n请用 Read 工具查看这张图片并回应。{extra}"
    await handle_message(chat_id, prompt, message_id)


async def handle_audio(chat_id: str, message_id: str, file_key: str, duration_ms: int) -> None:
    """语音: 下载 opus → 豆包转文字 → 当文本消息处理."""
    if duration_ms and duration_ms > 300000:   # >5min 的语音一律拒收 (转写慢且多半是误触)
        SENDER.send(chat_id, "⚠ 语音太长 (>5分钟), 请发短一点或发文字。")
        return
    reaction_id = SENDER.begin_busy(message_id)
    try:
        path = await asyncio.to_thread(
            SENDER.download_incoming, message_id, file_key, "file", ".opus")
        if not path:
            SENDER.send(chat_id, "⚠ 语音下载失败。")
            return
        text = await asyncio.to_thread(_transcribe, path)
    finally:
        if reaction_id:
            SENDER.end_busy(message_id, reaction_id)
    if not text:
        SENDER.send(chat_id, "⚠ 语音识别失败 (空/出错), 请发文字。")
        return
    SENDER.send_text(chat_id, f"🎧 听到: {text}")   # 让用户确认识别对不对
    await handle_message(chat_id, text, message_id)


# ── 飞书长连接事件 ────────────────────────────────────────────────
def on_message(data: P2ImMessageReceiveV1) -> None:
    try:
        ev = data.event
        sender_open_id = getattr(getattr(ev.sender, "sender_id", None), "open_id", None)
        msg = ev.message
        chat_id = msg.chat_id
        # 白名单: 只认本人 open_id; 忽略 bot 自身/他人
        if ALLOWED_OPEN_ID and sender_open_id != ALLOWED_OPEN_ID:
            log(f"忽略非白名单发送人: {sender_open_id}")
            return
        message_id = getattr(msg, "message_id", None)
        mtype = msg.message_type
        content = json.loads(msg.content)
        if not MAIN_LOOP:
            return
        if mtype in ("text", "post", "interactive"):
            # post = 富文本; interactive = 卡片 (用户转发的通知/职位卡等) → 都扁平成纯文本走同一路径
            if mtype == "post":
                text = _post_to_text(content)
            elif mtype == "interactive":
                card_text = _msg_body_to_text("interactive", msg.content)
                log(f"[{chat_id[:8]}] 收到卡片 ({len(card_text)}字)")   # 卡片必留痕, 别静默
                # 飞书对小程序卡/特殊 app 卡只回占位符, 真实内容不下发给机器人 → 识别后明确告知
                if "请升级至最新版本客户端" in card_text or "查看内容" in card_text:
                    SENDER.send(chat_id, "这张卡片飞书不允许机器人读取内容（只返回了『请升级客户端』占位符，"
                                         "常见于小程序卡/特殊应用卡）。请把要点打字发我，或截图发我看。")
                    return
                if not card_text:                       # 抽不出字 → dump 原始结构定位 + 给反馈
                    try:
                        (_BRIDGE_HOME / "last_empty_card.json").write_text(
                            msg.content or "", encoding="utf-8")
                        log(f"[{chat_id[:8]}] 空卡片原始结构已存 last_empty_card.json ({len(msg.content or '')}字节)")
                    except Exception:  # noqa: BLE001
                        pass
                    SENDER.send(chat_id, "收到你转发的卡片，但没提取到文字内容（可能是纯图片/按钮卡）。"
                                         "可以直接把要点打字发我，或截图发我看。")
                    return
                text = f"[用户转发了一张卡片，内容如下]\n{card_text}"
            else:
                text = content.get("text", "").strip()
            if not text:
                return
            # 只认 parent_id，避免把普通线程消息误判为引用。
            # 不按 ref 去重，允许重复引用同一条。
            ref = pick_quote_ref(getattr(msg, "parent_id", None), _LAST_QUOTE_REF.get(chat_id))
            if ref:
                _LAST_QUOTE_REF[chat_id] = ref
                # 先 API 取 (用户文本能取到); 卡片(bot 回复/通知)API 只返占位符 → 兜底出站缓存
                quoted = (SENDER.fetch_message_text(ref) or _SENT_CARD_TEXT.get(ref)
                          or load_notify_entry(_NOTIFY_CARD_PATH, ref))
                if quoted:
                    blockquote = "\n".join("> " + ln for ln in quoted.splitlines())
                    text = f"[用户回复并引用了上一条消息]\n{blockquote}\n\n{text}"
                    log(f"[{chat_id[:8]}] 含引用 (ref={ref[:12]}, {len(quoted)}字)")
                else:
                    log(f"[{chat_id[:8]}] 有引用 ref 但取正文失败/空 (ref={ref[:12]})")
                    # 引用内容取不到 (多半是 cron 推送卡片, 不在出站缓存且 API 只返占位符):
                    # 静默吞掉会让 Claude 按上文猜指代 → 答非所问. 明确告知, 指代不明就先反问.
                    text = ("[用户回复并引用了一条消息, 但内容无法获取 (可能是通知卡片)。"
                            "若下文指代不明 (如「这个」「查一下」), 先反问用户指的是什么, 不要按上文猜。]\n\n"
                            + text)
            APP_FRONT.on_feishu_user(chat_id, text)   # 记 chat_id + 镜像给 Web 端
            log(f"[{chat_id[:8]}] 收到文本: {text[:80]}")
            asyncio.run_coroutine_threadsafe(
                handle_message(chat_id, text, message_id), MAIN_LOOP)
        elif mtype == "image":
            image_key = content.get("image_key", "")
            log(f"[{chat_id[:8]}] 收到图片: {image_key}")
            asyncio.run_coroutine_threadsafe(
                handle_image(chat_id, message_id, image_key), MAIN_LOOP)
        elif mtype == "audio":
            file_key = content.get("file_key", "")
            log(f"[{chat_id[:8]}] 收到语音: {file_key} ({content.get('duration')}ms)")
            asyncio.run_coroutine_threadsafe(
                handle_audio(chat_id, message_id, file_key, content.get("duration", 0)), MAIN_LOOP)
        else:
            SENDER.send(chat_id, f"暂不支持的消息类型: {mtype} (目前支持文本/富文本/卡片/图片/语音)")
    except Exception as exc:  # noqa: BLE001
        log(f"on_message 异常: {type(exc).__name__}: {exc}")


def _validate_config() -> None:
    if APP_ONLY:
        return              # 纯 App 模式无需飞书凭证 (Claude 认证走环境/CLI 登录)
    missing = [k for k, v in {
        "FEISHU_BRIDGE_APP_ID": APP_ID,
        "FEISHU_BRIDGE_APP_SECRET": APP_SECRET,
    }.items() if not v]
    if missing:
        raise SystemExit(
            f"缺少配置: {', '.join(missing)}\n"
            f"先跑 scripts/fleet_app_provision.py 扫码建号 (凭证写入 {_BRIDGE_HOME / '.env'})"
        )
    # Claude 认证: 优先 CLAUDE_CODE_OAUTH_TOKEN; 否则回落 claude CLI 订阅登录
    # 找不到 claude 时告警。
    if not OAUTH_TOKEN and not shutil.which("claude"):
        log("⚠ 未设 CLAUDE_CODE_OAUTH_TOKEN 且 PATH 上找不到 claude — Claude 无法认证")
    if not ALLOWED_OPEN_ID:
        log("⚠ 未设 FEISHU_BRIDGE_OPEN_ID — 任何人都能驱动! 强烈建议设白名单。")
    if "ANTHROPIC_API_KEY" in os.environ:
        os.environ.pop("ANTHROPIC_API_KEY")     # 强制走订阅, 不走 API 计费
        log("已移除 ANTHROPIC_API_KEY (强制订阅认证)")


async def _app_command(text: str) -> None:
    """App 菜单动作的静默命令：不回显用户气泡、不镜像飞书，直接进入命令管线
    (compact/new_session 等菜单项不该在聊天里留一条用户「说」的命令)。回复照常广播。"""
    chat = app_frontend.CUR_CHAT.get() or APP_FRONT.chat_id
    if not chat:
        APP_FRONT.broadcast({"ev": "error",
                             "text": "该 bot 还没有飞书会话记录, 先在飞书里发它一条消息"})
        return
    if not text.startswith("/"):        # 只收斜杠命令 — 普通消息必须走会回显的 user_message
        log(f"⚠ [app] 非斜杠命令被拒: {text[:40]!r}")
        return
    log(f"[{chat[:8]}] 🌐 静默命令: {text[:40]}")
    await handle_message(chat, text, None)


_DOMAINS_BLIND_LOGGED = 0.0      # 上次为「域表读不到」告警的时间 (别把日志刷爆)


def _group_chat_ok(chat_id: str) -> tuple[bool, str]:
    """`group:<domain>` 允许吗 —— 域须是工作群或公共群，且**我在里面**。

    返回 `(放行吗, 原因码)`。原因码: `""`(放行) / `not_group` / `no_bot_name` /
    **`domains_unreadable`** / `no_such_domain` / `no_lead` / `not_member`。

    ⚠ 条件缺一不可, 而且**默认拒绝**: 这个值来自客户端。放行任意值 =
    任何端能在任何 bot 上开任意会话键 ⇒ `CLIENTS` 无上限增长, 且跨域串台。
    旧工作群以 lead 为群标志；公共群按契约 §2.23 显式 `public:true + users`，故意没有 lead。

    域表不可读取与域不存在必须区分；前者限频记录基础设施错误。
    """
    if not chat_id.startswith("group:"):
        return False, "not_group"
    domain = chat_id[len("group:"):]
    if not domain:
        return False, "not_group"
    if not _BOT_NAME:
        return False, "no_bot_name"
    try:
        # ⚠ 域在 `{"domains": {...}}` 里, **不在顶层** —— 顶层还有 `_v`/`_note` 等元数据。
        _f = _fleet_ops_file("fleet_domains.json")     # 两路回落, 见 _fleet_ops_file
        if _f is None:
            raise FileNotFoundError("fleet_domains.json 两个候选位置都没有")
        doms = json.loads(_f.read_text(encoding="utf-8")).get("domains") or {}
    except Exception as exc:  # noqa: BLE001
        global _DOMAINS_BLIND_LOGGED
        now = time.time()
        if now - _DOMAINS_BLIND_LOGGED > 300:      # 5 分钟最多一条, 够醒目又不刷屏
            _DOMAINS_BLIND_LOGGED = now
            log(f"⚠ 读不到域表 fleet_domains.json ({exc}) —— **所有群会话都会被退回私聊**, "
                f"而群里看不到任何痕迹。这不是拒绝, 是判断不了。")
        return False, "domains_unreadable"
    if domain not in doms:
        return False, "no_such_domain"
    cfg = doms.get(domain) or {}
    public = cfg.get("public") is True and isinstance(cfg.get("users"), list) \
        and any(str(user).strip() for user in cfg.get("users") or [])
    if not cfg.get("lead") and not public:
        return False, "no_lead"
    if _BOT_NAME == cfg.get("lead") or _BOT_NAME in (cfg.get("bots") or []):
        return True, ""
    return False, "not_member"


async def _app_inject(text: str, client: str = "unknown") -> None:
    """App/Web 端消息注入: display 可在群里, work/session 始终按当前 bot 会话跑。"""
    if _MANAGED_ENTRY is not None:
        return await _MANAGED_ENTRY.inject(text, client)
    chat = app_frontend.CUR_CHAT.get() or APP_FRONT.chat_id
    display = _display_chat_for(chat)
    if not chat:
        APP_FRONT.broadcast({"ev": "error",
                             "text": "该 bot 还没有飞书会话记录, 先在飞书里发它一条消息"})
        return
    # Log the client identifier as supplied; older gateways may send unknown.
    where = f" display={display[:24]}" if display != chat else ""
    log(f"[{chat[:8]}] 💬 消息 (client={client}{where}): {text[:80]}")
    # 广播用户回执，使网关能够保存完整历史
    # 全量历史 (设备离线时段的消息重连后可回放)
    if not app_frontend.INTERNAL_GROUP_TASK.get():
        actor = app_frontend.APP_USER.get()
        shown = app_frontend.APP_USER_TEXT.get() or text
        APP_FRONT.broadcast(app_frontend.user_event(shown, origin="web", user=actor or ""))
    if display == chat:
        SENDER.feishu.send(chat, f"你: {text}")   # 镜像进飞书 (bot 代发, 标「你:」免得像 bot 自言自语)
    await handle_message(chat, text, None)


_WEB_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_WEB_VIDEO_EXTS = {".mp4", ".mov"}  # 飞书镜像仅支持 mp4，最大 30MB


def _app_answer_codex_approval(req: dict) -> None:
    # Dedicated structured channel: never parse chat text or bot messages as consent.
    for approvals in list(CODEX_APPROVALS.values()):
        if approvals.answer(req):
            return
    APP_FRONT.broadcast({"ev": "ask_cancel", "ask_id": req.get("ask_id", ""),
                         "chat_id": req.get("chat_id") or APP_FRONT.chat_id,
                         "reason": "stale"})


def _app_answer_ask(text: str, ask_id: str = "") -> bool:
    if ask_id.startswith("codex-question-"):
        return True  # Never interpret plain text or an unstamped fallback as a card answer.
    """App/Web 的提问卡作答 → 直接兑现等待中的 future (不进忙时暂存队列)。

    走普通消息通路的话, bot 正忙时答案会被「📥 已暂存」扣住, 等排到队 future 早没了,
    答案退化成普通消息, 模型多半再问一遍 (避免重复提问)。返回 True = 已消费。

    **必须校验 ask_id**: 否则某张卡的旧答案 (客户端重发/重放/另一张过期卡) 会被套到
    当前等待中的卡上，造成错误的自动作答。
    """
    # 按 ask_id 在所有会话里寻找等待中的卡。
    pending = None
    chat = None
    if ask_id:
        for _c, (_aid, _f) in ASK_PENDING.items():
            if _aid == ask_id:
                chat, pending = _c, (_aid, _f)
                break
    if pending is None:
        # 没带 ask_id 的老客户端 ⇒ 退回原行为 (只认私聊那张)
        chat = APP_FRONT.chat_id
        pending = ASK_PENDING.get(chat) if chat else None
    if not pending:
        return False
    cur_id, fut = pending
    if ask_id and cur_id and ask_id != cur_id:
        return False        # 答的不是当前这张卡 → 不消费, 交给上层当普通消息/丢弃
    if fut is None or fut.done() or MAIN_LOOP is None:
        return False
    MAIN_LOOP.call_soon_threadsafe(lambda: fut.done() or fut.set_result(text))
    log(f"[{(chat or '')[:8]}] 🖲 App 作答直通: {text[:40]}")
    # ⚠ **回执也要盖答的那个会话** —— 修完上面那半才发现的第二半:
    # 这条 broadcast 不带 chat_id, 扇出点会按 contextvar 兜底成私聊
    # ⇒ 群里答的卡, 答案气泡画进私聊。**同一个函数里两处, 修一处只修好一半。**
    actor = app_frontend.APP_USER.get()
    APP_FRONT.broadcast(app_frontend.user_event(
        app_frontend.APP_USER_TEXT.get() or text, origin="app",
        chat_id=ASK_DISPLAY.get(cur_id) or chat or APP_FRONT.chat_id,
        user=actor or ""))
    return True


async def _app_inject_image(paths: list[str], caption: str = "") -> None:
    """Web 端附件注入 (图片/文档): 附件+文字合成**一轮**, 并镜像进飞书。

    图片镜像为飞书图片消息; 文档飞书发不了 (sender 无文件消息), 镜像为文字说明。
    """
    feishu_chat = APP_FRONT.chat_id
    chat = app_frontend.CUR_CHAT.get() or feishu_chat   # ⚠ 限定名: 本模块没裸导入 CUR_CHAT
    display = _display_chat_for(chat)
    if not chat:
        APP_FRONT.broadcast({"ev": "error", "text": "该 bot 还没有飞书会话记录, 先在飞书里发它一条消息"})
        return
    # 群聊是 App 侧 display, work chat 仍是 bot 私聊; 镜像只在 display 也是私聊时启用。
    mirror = display == chat == feishu_chat and bool(feishu_chat)
    good = [p for p in paths if Path(p).is_file()]
    if not good:
        APP_FRONT.broadcast({"ev": "error", "text": f"附件文件不存在: {paths}"})
        return
    where = f" display={display[:24]}" if display != chat else ""
    log(f"[{chat[:8]}] 🌐 Web 附件 x{len(good)}{where}" + (f" (附言: {caption[:40]})" if caption else ""))
    actor = app_frontend.APP_USER.get()
    user_meta = {"user": actor} if actor else {}
    for p in good:                                       # 镜像进飞书会话 (只发飞书)
        sfx = Path(p).suffix.lower()
        if sfx in _WEB_IMG_EXTS:
            # origin_or: 远程节点上 p 是本地副本 (D:\\fleet\\...), 而 App 去网关取图 ——
            # 必须回显网关侧原路径, 否则用户看不见自己刚发的图。
            APP_FRONT.broadcast({"ev": "media", "kind": "image", "path": APP_FRONT.origin_or(p),
                                 "mine": True, **user_meta})
            if mirror:
                SENDER.feishu.send_media_file(feishu_chat, p, "image")
        elif sfx in _WEB_VIDEO_EXTS:
            APP_FRONT.broadcast({"ev": "media", "kind": "video", "path": APP_FRONT.origin_or(p),
                                 "mine": True, **user_meta})
            if not mirror:
                pass
            elif sfx == ".mp4" and Path(p).stat().st_size <= 30 * 1024 * 1024:
                SENDER.feishu.send_media_file(feishu_chat, p, "video")
            else:   # 飞书视频消息仅收 mp4≤30MB; 超限/.mov 只发文字说明, App 端照常可播
                SENDER.feishu.send(feishu_chat, f"你发来视频: {Path(p).name} (此格式飞书播不了, App 里可看)")
        else:
            APP_FRONT.broadcast({"ev": "file", "name": Path(p).name, "mine": True, **user_meta})
            if mirror:
                SENDER.feishu.send(feishu_chat, f"你发来文件: {Path(p).name}")
    if caption:
        APP_FRONT.broadcast(app_frontend.user_event(
            app_frontend.APP_USER_TEXT.get() or caption, origin="web", user=actor or ""))
    extra = f"\n用户附带文字: {caption}" if caption else "\n用户没有附加文字, 请查看后理解需求。"
    def _kind(p: str) -> str:
        sfx = Path(p).suffix.lower()
        if sfx in _WEB_IMG_EXTS:
            return "图片"
        return "视频" if sfx in _WEB_VIDEO_EXTS else "文件"
    if len(good) == 1:
        hint = ("请用 Bash (ffprobe 看元信息 / ffmpeg 抽帧后 Read 帧图) 了解内容后回应。"
                if _kind(good[0]) == "视频" else "请用 Read 工具查看后回应。")
        prompt = f"用户发来一个{_kind(good[0])}, 已保存在本地: {good[0]}\n{hint}{extra}"
    else:
        lst = "\n".join(f"{i + 1}. {p} ({_kind(p)})" for i, p in enumerate(good))
        prompt = (f"用户发来 {len(good)} 个附件, 已保存在本地:\n{lst}\n"
                  f"请逐个查看后回应 (图片/文档用 Read; 视频用 ffprobe/ffmpeg 抽帧)。{extra}")
    await handle_message(chat, prompt)


def _publish_defaults() -> None:
    """启动即把**舰队默认值**写进 status.json 的 defaults 段。

    为什么必须由桥来发布: 客户端的模型/强度胶囊要显示"这一轮会用哪个",
    而 runtime.json 里没覆盖时是 null —— 客户端只能显示「默认」, 等于什么都没说。
    它先前是**镜像了桥里的两个常量**, 那就有两份真相: 桥改了默认值而客户端没跟, 界面会
    **静默说谎**(用户看到 opus 5, 实际跑的是别的)。
    **只有本进程知道自己会用什么**, 所以由它发布, 网关照读即可。
    每轮 _write_status 也会带上, 这里补的是「还没跑过任何一轮」那段空窗。
    """
    try:
        cur = {}
        if _STATUS_FILE.exists():
            cur = json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
        cur["defaults"] = {"model": DEFAULT_MODEL, "effort": DEFAULT_EFFORT}
        tmp = _STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_STATUS_FILE)
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ 写默认值失败 ({exc}) —— 客户端胶囊可能显示不出生效模型")


_TASK_TRACK_POLL_S = 5.0        # 看一眼 task_track.json 的间隔 (一次 stat, 便宜)


async def _align_todos_once() -> None:
    """启动后广播当前任务清单，并持续监测文件变化，以同步外部写入和空清单。"""
    await asyncio.sleep(5)          # 等网关连上 (启动 3s 内通常已连); 没连则 no-op
    if _push_task_card():                      # 状态对齐 ≠ 回合里清单变了, 见 _broadcast_todos
        log(f"🧭 启动对齐任务清单: {len(task_card.merged(_TASK_TRACK))} 条")
    while True:                     # fail-soft: 这条循环任何异常都不许掀翻进程
        try:
            await asyncio.sleep(_TASK_TRACK_POLL_S)
            if not _reload_task_track_if_changed():
                continue
            if _push_task_card():
                log("🧭 task_track.json 被外部改过, 已重新对齐: "
                    f"{len(task_card.merged(_TASK_TRACK))} 条")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"⚠ 任务清单对齐循环异常 (下一轮继续): {exc}")


def _lead_of_domain(domain: str) -> str:
    """域 → lead。读不到/没有 lead ⇒ 空串 (**不猜**)。"""
    try:
        # 使用共享回落逻辑读取注册表。
        _f = _fleet_ops_file("fleet_domains.json")
        if _f is None:
            return ""
        cfg = (json.loads(_f.read_text(encoding="utf-8")).get("domains") or {}).get(domain) or {}
    except Exception:  # noqa: BLE001
        return ""
    return str(cfg.get("lead") or "")


def _tell_lead_turn_was_cut(cid: str, entry: dict) -> None:
    'Notify the domain lead of an interrupted turn without automatically repeating side effects.'
    if not cid.startswith("group:"):
        return
    domain = cid[len("group:"):]
    lead = _lead_of_domain(domain)
    if not lead or lead == _BOT_NAME:
        return              # 没有 lead 的域不是群; lead 是自己就不用递给自己
    hook = os.environ.get("FLEET_SESSION_INJECT_HOOK", "").strip()
    script = Path(hook).expanduser() if hook else None
    if script is None or not script.is_file():
        log(f"⚠ 无法通知 lead「{domain} 的回合被掐断」：注入钩子未配置或不存在")
        return
    head = (entry.get("text") or "").strip().replace("\n", " ")[:80]
    msg = (f"[群回合被掐断 · {domain}] 我({_BOT_NAME})上一轮在 `{cid}` 里的活被一次"
           f"**桥重启**掐断了, 没跑完。\n原话开头: 「{head}」\n"
           f"⚠ 我**没有**自动重跑 —— 那一轮可能已经写了文件或提交了代码, 重跑会重复副作用。\n"
           f"要不要重派你定 (查一下 tasks.jsonl 上它是不是还挂着 open)。")
    try:
        import subprocess
        r = subprocess.run(
            ["python3", str(script), "--bot", lead, "--from", _BOT_NAME or "unknown",
             "--text", msg],
            capture_output=True, text=True, timeout=20)
        ok = r.returncode == 0
        log(f"{'📮' if ok else '⚠'} 已告知 lead {lead}: {domain} 的回合被掐断"
            if ok else f"⚠ 告知 lead {lead} 失败: {(r.stderr or r.stdout or '')[:120]}")
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ 告知 lead {lead} 异常 ({exc}) —— 不影响本次启动")


async def _report_interrupted_turns() -> None:
    """启动时把「被打断的回合」变成一条给用户的提示。

    在此之前, 进程被杀 = 那一轮和它后面排队的消息**一起无声消失**, bot 重启后一脸空闲,
    用户可能误以为交代过的事仍在进行。

    只提示不重跑 —— 理由见 inflight_notice。取走即删, 所以同一件事只说一次。
    """
    await asyncio.sleep(5)                 # 与 _align_todos_once 同步: 等网关连上
    try:
        pending = inflight_take_pending()
        if not pending:
            return
        for cid, entry in pending.items():
            log(f"⚠ 上次有未完成的回合 (chat={cid[:12]}): {(entry.get('text') or '')[:60]}")
            # 发回**记录里的那个会话**, 不是当前会话: 被打断的活属于哪个对话, 提示就该
            # 回到哪个对话 (飞书多群时尤其明显)。App 模式下 chat_id 从文件读, 重启后不变。
            try:
                SENDER.send(cid, inflight_notice(entry))
            except Exception as exc:  # noqa: BLE001
                # 单个会话发失败 (群没了/凭证过期) 不该让别的会话也收不到
                log(f"⚠ 未完成回合提示发送失败 (chat={cid[:12]}): {exc}")
            # 群回合**再递一条给 lead** —— 上面那条是给人看的, 这条才有人会处理
            _tell_lead_turn_was_cut(cid, entry)
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ 上报未完成回合失败 ({exc}) —— 不影响本次启动")


async def _app_interrupt() -> None:
    """App 停止按钮/提问卡✕ 的静默中断: 不注入聊天、不回显「停」,
    处理完广播 {"ev":"interrupted", ok, cleared} 作回执 (仅 App 端, 飞书不发)。
    注意: 回执发出后仍可能有少量在途 stream 事件, 客户端按回执定稿即可。"""
    chat = app_frontend.CUR_CHAT.get() or APP_FRONT.chat_id
    if not chat:
        return
    if chat in ASK_PENDING:                 # 提问卡等答案中 → 先取消提问 (会广播 ask_cancel)
        _aid, fut = ASK_PENDING[chat]
        if fut and not fut.done():
            fut.set_result(None)
    if chat in PENDING:                     # 审批等 y/n 中 → 视同拒绝 (与「停」文本一致)
        fut = PENDING.get(chat)
        if fut and not fut.done():
            fut.set_result(False)
    try:
        state, dropped = await _do_interrupt(chat)
    except Exception as exc:  # noqa: BLE001
        log(f"[app] 静默中断失败: {exc}")
        APP_FRONT.broadcast({"ev": "interrupted", "ok": False, "state": "error",
                             "cleared": 0, "error": str(exc)})
        return
    log(f"[app] 静默中断: {state}, 清暂存 {dropped} 条")
    # ok 保留给老客户端 (含义收紧成「真的停了」); state 是新增的精确字段:
    # idle=本来就没在跑 / stopped=确认停了 / stuck=掐了没停下来
    APP_FRONT.broadcast({"ev": "interrupted", "ok": state == "stopped",
                         "state": state, "cleared": dropped})


def _app_set_runtime(model: str | None, effort: str | None) -> None:
    """App 切模型/强度的静默通道: 只写 runtime.json, 不进聊天、
    不发提醒; 下一条消息 _get_client 读到差异后在内部切换。"""
    if effort is not None:
        if effort.lower() in EFFORT_ACCEPTED:
            _write_runtime_effort(effort.lower())
            log(f"⚙️ [app] 静默设 effort={effort} (下一条消息生效)")
        else:
            log(f"⚠ [app] 非法 effort 被忽略: {effort!r}")
    if model is not None:
        if re.fullmatch(r"[A-Za-z0-9._\-]{1,64}", model):
            result = _set_runtime_model(model)
            if result != "denied":
                log(f"⚙️ [app] 静默设 model={model} (下一条消息生效)")
            else:
                log(f"⚠ [app] 当前节点拒绝 Codex model={model}")
        else:
            log(f"⚠ [app] 非法 model 被忽略: {model!r}")


async def amain() -> None:
    global MAIN_LOOP, SENDER, PEER_REPORTS
    MAIN_LOOP = asyncio.get_running_loop()
    try:
        PEER_REPORTS = peer_reports.Journal(_BRIDGE_HOME / "peer_reports.json")
        recovery = sum(r["state"] in {"recovery_required", "delivery_unknown", "blocked"}
                       for r in PEER_REPORTS.rows)
        if recovery:
            log(f"Peer reports retained review_required={recovery}; no automatic replay/card restoration")
    except (OSError, ValueError, KeyError, TypeError):
        log("Peer report journal unreadable; retained untouched, deferral disabled")
    if APP_ONLY:
        SENDER = TeeTransport(NullTransport(), APP_FRONT)
        APP_FRONT.chat_id = APP_FRONT.chat_id or "app"      # 无飞书 chat, 用固定标识
    else:
        SENDER = TeeTransport(FeishuTransport(APP_ID, APP_SECRET, DOMAIN), APP_FRONT)
    await APP_FRONT.start(_app_inject, _app_inject_image, _app_answer_ask,
                          _app_set_runtime, _app_interrupt,
                          _app_command, approval_answer=_app_answer_codex_approval,
                          question_answer=_app_answer_codex_question,
                          question_disconnect=lambda: CODEX_QUESTIONS.cancel_work(None, "gateway_disconnected"),
                          peer_report=_peer_qualified,
                          managed_ingress=(_MANAGED_ENTRY.handle_frame if _MANAGED_ENTRY is not None else None))
    asyncio.create_task(_question_expiry_loop())
    _migrate_sessions_once()
    _cleanup_media()                   # media/ 超 7 天的收发图清掉
    try:  # 经文件 IPC 提供命令补全清单
        (_BRIDGE_HOME / "commands.json").write_text(
            json.dumps(_COMMANDS, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ 写 commands.json 失败: {exc}")
    _publish_defaults()          # 启动即发布默认值 (客户端胶囊要显示生效值)
    # 同一份值也交给 App 前端: 网关每次连上都会收到, 远程节点靠它 (文件那条路读不到)
    APP_FRONT.defaults = {"model": DEFAULT_MODEL, "effort": DEFAULT_EFFORT}
    # 当前覆盖值的取值口: defaults 是常量, 这个才是"用户切到了哪个"。
    # 远程节点的 runtime.json 网关读不到, 只能由桥报 —— 见 AppFrontend.runtime_now。
    APP_FRONT.set_chat_validator(_group_chat_ok)   # 群聊: 校验客户端送来的 chat_id
    APP_FRONT.set_runtime_reader(_read_runtime)
    asyncio.create_task(_align_todos_once())
    asyncio.create_task(_report_interrupted_turns())
    asyncio.create_task(_rollover_loop())
    if OAUTH_TOKEN:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = OAUTH_TOKEN

    if APP_ONLY:
        log(f"✅ bridge 启动 (纯App模式) · repo={REPO}")
        asyncio.create_task(_heartbeat(threading.current_thread()))   # 无 ws 线程, 用主线程占位
        await asyncio.Event().wait()
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )
    ws_client = lark.ws.Client(
        APP_ID, APP_SECRET,
        event_handler=handler,
        domain=_WS_DOMAIN.get(DOMAIN, lark.FEISHU_DOMAIN),
        log_level=lark.LogLevel.WARNING,
    )
    # lark ws .start() 是阻塞同步 → 丢到线程, 事件回调里用 run_coroutine_threadsafe 回主 loop
    ws_thread = threading.Thread(target=ws_client.start, daemon=True, name="lark-ws")
    ws_thread.start()
    log(f"✅ bridge 启动 · repo={REPO} · 白名单={ALLOWED_OPEN_ID or '(无!)'}")
    asyncio.create_task(_heartbeat(ws_thread))   # 心跳: loop+ws 线程活着才写, 给 watchdog 检测卡死
    await asyncio.Event().wait()       # 常驻


_HEARTBEAT_FILE = _BRIDGE_HOME / "heartbeat"     # watchdog 读: epoch 秒, 陈旧=卡死/聋了


async def _heartbeat(ws_thread: threading.Thread) -> None:
    """每 30s 写心跳 (仅当 asyncio loop 跑着 **且** lark ws 线程活着). 任一挂 → 心跳变陈旧
    → watchdog launchctl kickstart 重启. (进程死/重启由 launchd KeepAlive+RunAtLoad 兜底)"""
    while True:
        try:
            if ws_thread.is_alive():
                _HEARTBEAT_FILE.write_text(str(int(time.time())), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(30)


def main() -> None:
    _validate_config()
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log("退出。")


if __name__ == "__main__":
    main()
