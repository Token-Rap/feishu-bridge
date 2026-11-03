"""fleet-app Web 前端接入层 — bridge 的第二事件源 + 回复双写。

「同一会话续聊」的实现 (用户明确选定):
Web 消息不自开 Claude 会话 (两进程 resume 同一 session 会分叉打架), 而是通过
unix socket (<home>/app.sock) 注入本 bridge 进程, 走与飞书消息**完全相同**的
handle_message 管线 (同 chat_id → 同会话; 忙时暂存/中断/审批语义免费继承)。

回复侧用 TeeTransport 双写: 每条回复/流式更新同时发到飞书和 socket, 两端都看到
完整对话 (飞书里问的 Web 上也出现, 反之亦然)。

fail-soft 纪律: 网关没起 / 没有 Web 客户端时, 一切 broadcast 都是 no-op ——
本模块故障不能影响飞书主路。

协议 (newline-delimited JSON):
  网关 → bridge: {"type": "user_message", "text": "...", "client": "ios|web|..."}
    (client = 连接级来源标识, 网关归一后带上; 老网关不带 → bridge 记 unknown, **不猜**)
               | {"type": "answer", "ask_id": "...", "answers": [{"header","label"}]}
               | {"type": "user_image", "paths": ["..."], "caption": "..."}
               | {"type": "set_runtime", "model"?: "...", "effort"?: "..."}  # 静默切换, 不进聊天
               | {"type": "interrupt"}  # 静默中断, 回执广播 {"ev":"interrupted", ok, cleared}
               | {"type": "command", "text": "/..."}  # 静默斜杠命令, 不回显用户气泡
  bridge → 注入方: {"type": "inject_ack", "chat_id": "<显示落点>"}
    (对 user_message 与 command 回, 且在派任务之前写 —— 注入方据此分辨"进群了"还是
     "被静默退回私聊了"; 老桥不回这行, 注入方会说「落点未知」而不是报成功。
     command 那支是 ctx_compact_guard 直连 socket 发 /compact,
     发完只 drain 3 秒, 没有回执它压错了会话也看不出来)
  bridge → 网关: {"ev": "user"|"stream"|"thinking"|"reply"|"notice"|"media"|"error", ...}
               | {"ev": "queue_accept", "count": int, "ts": float}
                 # provider 已接收 count 条排队补充进入当前回合；只证明“接收”，不证明执行。
                 # 网关按 queued 顺序补 items[{entry_id,text}]，客户端据此切开 live 回复。
               | {"ev": "compact", "phase": "start"|"done",   # 上下文压缩进度 (成对)
                  "used": int, "max": int,                    # **token 数**, 不是百分比
                  "before_used": int}                         # 仅 done 带: 压缩前的 used
               | {"ev": "session_reset", "prev": "<旧 session_id, 无则 \"\">", "ts": float,
                  "engine"?: "claude|codex", "prev_engine"?: "claude|codex|\"\"",
                  "hint"?: "engine_switched"|"provider_switched",
                  "prev_provider"?: str, "provider"?: str, "model"?: str}
                 # hint=provider_switched (同 engine 换提供方, 如 Anthropic↔DeepSeek) 另带
                 # prev_provider/provider 和新模型的 App 侧 id `model` (如 "deepseek-flash")。
                 # 开新会话 (/new /reset /clear 「新会话」)。iOS 上
                 # **不要**再往聊天里塞「🔄 已开新会话…」那条文字, 用 UI 状态表达
                 # (分隔线 / 上下文百分比归零 / 拿 prev 做「回上一个会话」的入口)。
                 # 飞书没有这些 UI, 那边仍是文字 —— 同 `queued` 的分法。切引擎也复用
                 # 这条事件, hint=engine_switched 表示后面是另一条上下文, 不另发 notice。
                 # **收到它要把上下文胶囊清掉**: 旧会话的百分比留着就是陈旧快照当现状。
                 # 网关侧配套: 进历史 (role=session_reset, 重连后分隔线还在) 且清 last_ctx。
                 # 客户端认 phase 起收进度条, 别再认中文文案 (改一个字两端一起哑)。
                 # done 在 finally 里发 —— 异常/中断也保证有收条。
                 # 只覆盖**手动** /compact; CLI 上下文满时的自动压缩不走这条路径。
                 # 网关 _fanout 会统一加 {"bot": ...} 再转给客户端。

  ⚠ **`ctx` 带 `ts`** (epoch 秒) = **这份快照算出来的时刻**, 不是收到的时刻。
    网关缓存 `last_ctx` 并在重连时**原样回放 (带 replay:true, 不刷新 ts)** —— 客户端据此
    显示快照时间。**别拿收到时刻当快照时刻**: 回放的数据可能早已计算,
    标成「刚刚」比不标更坏 (用户 /compact 完看到 81%, 分不出是没压缩成功还是数是旧的)。
    老桥接不带 ts → 客户端只显示百分比, 不猜。

  ⚠ **`ask_cancel` 可能重复到达, 客户端必须按 `ask_id` 幂等** 。它有**两个发送方且都保留**: bridge 在答完/超时/取消时广播,
    网关在收到作答时自己也 fanout 一条 (它兜 bridge 已重启/超时的情况, 见 server/main.py)。
    两者覆盖的失效场景不同, 收敛成一条就会丢一半覆盖 —— 所以代价由客户端的幂等来付。
    目前 iOS/Web 都按 ask_id 清卡, 天然幂等; **新写的客户端别假设它只来一次**。

  Codex 单次审批复用 ask/answer，ask_id 保留前缀 codex-approval-。
  仅结构化 answer 可回传决定；普通消息、过期卡、自由文本不当成批准。
  不添加字段或会话级允许选项；10 分钟未答即拒绝，终止回合即撤卡。

  ⚠ **服务端只在客户端先说 `{"type": "hello"}` 之后才主动写东西** (补发 + defaults)。
    不说 hello 的客户端, 桥**一个字节都不会写给它** —— 这样 `session_inject` 那种
    "sendall 完立刻 close"的只写客户端才是安全的: 对已关闭的 socket 写会把连接拆掉,
    **连它刚发的那一行都会一起丢**, 而且两侧都不报错。
    别再改回"连上就写" —— 那是把义务推给每一个客户端。

  ⚠ `{"ev": "runtime", "defaults": {"model": ..., "effort": ...}}` —— **网关每次连上时**
    本进程报一次自己的生效默认值。客户端的模型/强度胶囊显示的是"这一轮会用哪个",
    没有它就只能显示「默认」(等于什么都没说) 或者**镜像桥里的常量**(第二份真相, 桥改了
    它不会跟, 且过期时不报错)。
    **走事件而不是让网关读文件**: 远程节点 (Windows) 的文件网关读不到, 读文件那版对那
    远程节点可能返回空 —— 同一个函数的限制在新语义下从"信息少一点"退化成了"功能达不到"。

  ⚠ **中断的事件序** :
        stream … → interrupted → **stream** → reply
    `interrupted` 只是**受理回执**, 不是收尾 —— 它由 WS 处理路径立刻广播, 而 run_turn
    还在 `receive_response()` 里把已在途的 StreamEvent 排干, 所以**它后面还会有 stream**。
    因此客户端**不能在 interrupted 时收尾**: 收了会被紧跟的 stream 重建成新气泡, 最终
    reply 再落进去, 半截正文显示两遍。终结仍然是 reply — 它本来就替换 live 气泡。
    那条 reply 的正文 = `partial or "(已中断)"`, 即**生成过内容时带的是那半截全文**,
    只有一个字都没生成时才是「(已中断)」。飞书侧照旧需要它: 没有 live 气泡, 必须 finish
    才定稿。
    不应在 interrupted 事件上定稿；等待最终回复。

  ⚠ **reply 之后仍可能出现 stream** —— 但那是**下一轮**, 不是上一轮的残留:
    回合进行中到达的消息会被暂存, 而它的 `user` 事件在**到达那一刻**就广播了 (早于
    上一轮的 reply), 等上一轮收尾后才轮到它跑。所以客户端看到的是
        user(1) → stream… → user(2, 排队) → reply(1) → queue_flush → stream(2)…
    两个显式信号可用: user(2) 早已到达; `queue_flush` 紧贴新回合之前。
    **别把 reply 之后的 stream 一律当残留丢掉** —— 丢了就把新回合吞了。

  ⚠ 「一轮结束」的终结事件只有 reply 和 error 两个 —— **别用 interrupted 当收尾**:
    它是「用户按了停」的受理回执, 语义不同。两端都不拿它收尾 (web 只更新状态+续兜底,
    iOS 靠时间窗丢弃随后的 stream), 所以别指望它宣告结束。
    例如回合中途 CLI 子进程被 SIGTERM (bridge 活着、链路没断 → 网关的
    掉线兜底不触发), 异常路径当时只发 notice → 两端都永久卡「生成中」。所以**每条
    异常路径都必须以 reply 或 error 收口**, 光发旁白等于没收尾。
"""
from __future__ import annotations

import contextvars

import asyncio
import json
import os
import time
import urllib.request
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from urllib.error import HTTPError
from typing import Any, Awaitable, Callable
from feishu_bridge.fleet_env import env as _env   # 新名 FLEET_BRIDGE_* 优先, 回落旧名

# 中转给网关时按后缀声明的 content-type。网关 /upload 靠它认扩展名 —— 认不出来就 415。
# 与网关 main.py 的 _CT_EXT 是同一张表的反向 (那边 content-type → 后缀)。
_CT_BY_SUFFIX = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
                 ".mp4": "video/mp4", ".mov": "video/quicktime"}

# 值得重试的 HTTP 状态: 中间层「够不到上游」的瞬时态, 不含上游自己出的错。
# ⚠ **这套判据只适用于 relay_media, 不要同步到 fleet_bridge._req_json** ——
# 早上立的「同一判据两处长一样」在这里有例外, 而例外的理由比规则本身重要:
# **两处重复提交的代价不一样。** relay_media 重复 = 网关多一个孤儿文件 (uploads 目录本来

# 建表/写行那类 POST, **重复 = 多一行脏数据, 用户要手工清**。
# 所以判据的一致性要让位给**重复执行的代价** —— 谁更幂等, 谁才配放宽。
_UPLOAD_RETRY_MAX = 3   # 媒体上传的重试次数 (**只此一份**; 与飞书 API 那份各自演进)
_RETRY_STATUS = {502, 503, 504}
def codex_default_for(osname: str) -> bool:
    '返回平台默认的 Codex 开关。Windows 默认关闭，因为工具调用或回合可能静默卡住；心跳和注入回执不能证明回合完成。'
    return osname != "nt"


def codex_enabled_from(env: str, osname: str) -> bool:
    """显式 `FLEET_CODEX_ENABLED` 永远压过平台默认 —— 留一条不改代码就能做对照实验的路。"""
    v = (env or "").strip().lower()
    if not v:
        return codex_default_for(osname)
    return v not in {"0", "false", "no", "off"}


CODEX_CAPABLE = codex_enabled_from(os.getenv("FLEET_CODEX_ENABLED", ""), os.name)


def _client_of(req: dict) -> str:
    """帧里的来源标识; 老网关不带 → unknown。**不猜** —— 猜出来的标识和真的长得一样。"""
    got = str(req.get("client") or "").strip().lower()
    return got if 0 < len(got) <= 16 else "unknown"


def _human_size(path: str) -> str:
    """给日志用的文件大小; 取不到就是「-」—— 别拿 0.0KB 冒充「空文件」。"""
    try:
        n = Path(path).stat().st_size
    except Exception:  # noqa: BLE001
        return "-"
    if n < 1024:
        return f"{n}B"
    return f"{n / (1024 * 1024):.1f}MB" if n >= 1024 * 1024 else f"{n / 1024:.1f}KB"


CUR_CHAT: contextvars.ContextVar[str] = contextvars.ContextVar("cur_chat", default="")
"""当前回合真正跑在哪个 bot 会话里 (也就是 CLIENTS / session_id / 压缩阈值的键)。

群聊不是一个独立 bot, 只是多个 bot 回复聚合展示的地方: 群里 @ 到本 bot 时, 这一轮仍然
进本 bot 自己的私聊会话, 只把事件显示到 `DISPLAY_CHAT` 指向的 `group:<domain>`。

用 contextvar 而不是参数: 事件是从回合深处直接 `broadcast` 的, 那儿看不到会话 ——
要么每个调用点加一个参数(必漏), 要么在回合入口置一次由扇出点兜底。**选后者。**
asyncio 创建任务时会复制上下文, 所以回合内派生的子任务自动继承。
"""


DISPLAY_CHAT: contextvars.ContextVar[str] = contextvars.ContextVar("display_chat", default="")
"""当前回合的 UI / 网关落点。私聊时等于 CUR_CHAT; 群聊时是 `group:<domain>`。"""


INTERNAL_GROUP_TASK: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "internal_group_task", default=False)
"""结构化群任务的内部指令。

它仍要送进模型，但群时间线已经有 `group_event` 摘要卡，不再重复广播 user 气泡。
必须是机器字段，不能从 `[group:…]` 正文嗅探：协议说明里也会原样引用那段文字。
"""

# App/Web 多人会话里当前这条人类消息的身份与原文。网关给 bridge 的 `from` 既要进模型
# 上下文，也要原样回到客户端；只把名字拼进 text 会让客户端无法可靠区分“谁说的”和正文。
APP_USER: contextvars.ContextVar[str] = contextvars.ContextVar("app_user", default="")
APP_USER_TEXT: contextvars.ContextVar[str] = contextvars.ContextVar("app_user_text", default="")
PEER_REPORT: contextvars.ContextVar[dict | None] = contextvars.ContextVar("peer_report", default=None)
MESSAGE_ARRIVAL: contextvars.ContextVar[float] = contextvars.ContextVar("message_arrival", default=0.0)


def _engine_from_model(model: str) -> str:
    return "codex" if str(model or "").strip().lower().startswith("gpt-") else "claude"


_OMIT = object()        # 区分「没传 chat_id」与「传了空串」—— 见 user_event 文档


def user_event(text: str, *, origin: str, chat_id=_OMIT,
               user: str = "") -> dict:
    """Build an ``ev:"user"`` frame.  **Every producer should go through here.**

    Why this exists: this frame had 6 producers across 2 files (an earlier note said
    8 across 3 -- that counted the ``feishu_claude_bridge.py`` symlink as a
    separate file)
    and **no two of them emitted the same field set**.  That is not cosmetic --
    it cost a full misdiagnosis.  While tracing "who writes the human line into
    group history", three separate reads of the code each looked self-consistent
    and the last one was *backwards*, because only one of the three files had
    been grepped.  Nothing errored at any point; the error surfaced only when
    the real store was inspected and the two writers turned out to leave
    different key sets behind.

    ``user`` is **omitted rather than emitted empty**, matching what the
    hand-rolled literals did.  (Key *order* may differ; every consumer looks keys
    up by name.)

    ``chat_id`` is **not** like that, and the difference is load-bearing.
    The fan-out stamp ``AppFrontend._with_chat`` tests ``if "chat_id" in ev`` --
    *key presence*, not truthiness.  So an explicit empty string and an absent
    key take different branches: the former is sent as-is, the latter gets
    stamped with ``DISPLAY_CHAT``/``CUR_CHAT``/``self.chat_id``.  Two producers
    here always emitted the key (its value being an ``or`` chain that *may* be
    empty), so collapsing "empty" into "absent" would silently reroute those
    frames to a different chat.  Hence the ``_OMIT`` sentinel: pass ``chat_id``
    and it is emitted verbatim, empty or not; leave it out and the fan-out
    decides.  Tests: ``tests/test_user_event.py``.

    ``origin`` is effectively a *boolean* today: all three consumers (web
    ``app.js``, gateway ``main.py``, iOS ``FleetStore.swift``) only ever test
    ``origin == "feishu"``.  The ``"web"`` vs ``"app"`` split is never consumed
    by anyone.  Collapsing it is a **semantic** change and is deliberately NOT
    bundled into this extraction -- mixing "move code" with "decide meaning" in
    one commit is how a correct line gets "fixed" into a wrong one.
    """
    ev = {"ev": "user", "origin": origin, "text": text}
    if chat_id is not _OMIT:
        ev["chat_id"] = chat_id
    if user:
        ev["user"] = user
    return ev


class AppFrontend:
    """unix socket 服务端: 收 Web 消息注入管线, 向所有连接的网关广播回合事件。"""

    def __init__(self, home: Path, log: Callable[[str], None]) -> None:
        self._sock_path = home / "app.sock"
        self._chat_file = home / "app_chat"          # 持久化「用户的飞书 chat_id」
        self._log = log
        self._writers: set[asyncio.StreamWriter] = set()
        self._gateway_writers: set[asyncio.StreamWriter] = set()
        # 本地副本 → 网关侧原路径 (远程节点回显用, 见 origin_or)。只活在进程内就够:
        # 它答的是"这一轮刚收到的图怎么回显", 跨重启的回显走网关 history (那里存的
        # 已经是换算好的路径)。
        self._origins: dict[str, str] = {}
        self._runtime_reader = None      # 宿主注入; 见 runtime_now
        # 保存 SDK 返回的完整模型 ID，避免从短名猜测版本。
        self.observed_model: str = ""
        self._bg: set[asyncio.Future] = set()      # 在跑的注入任务 (防 GC)
        self._pending: list[tuple[float, dict]] = []   # 掉线期间攒下的事件 (见 broadcast)
        self._answer_ask: Callable[[str, str], bool] | None = None
        self._approval_answer: Callable[[dict], None] | None = None
        self._question_answer: Callable[[dict], None] | None = None
        self._question_disconnect: Callable[[], None] | None = None
        self._peer_report = None
        self._managed_ingress = None  # Operator-selected home; no legacy fallback.
        self._set_runtime: "Callable[[str | None, str | None], None] | None" = None
        self._interrupt: "Callable[[], Awaitable[None]] | None" = None
        self._command: "Callable[[str], Awaitable[None]] | None" = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inject: Callable[[str], Awaitable[None]] | None = None
        self._inject_image: "Callable[[list[str], str], Awaitable[None]] | None" = None
        # 本进程的**生效默认** model/effort。网关每次连上都会收到一次 (见 _on_conn):
        # 客户端的模型胶囊要显示"这一轮会用哪个", 而只有本进程知道自己会用什么。
        # 走事件而不是让网关读文件, 是因为**远程节点 (Windows) 的文件网关根本读不到** ——
        # 读文件那版对那远程节点可能返回空, 胶囊永远显示未知。
        self.defaults: dict = {}
        try:
            self.chat_id: str | None = self._chat_file.read_text(encoding="utf-8").strip() or None
        except Exception:  # noqa: BLE001
            self.chat_id = None
        if not self.chat_id:
            # 冷启动兜底: app_chat 要等下一条飞书消息才落盘, 但 sessions.json 的键
            # 就是历史 chat_id (p2p 单用户 → 取第一个)。没有它 Web 端要干等用户先去飞书发话。
            try:
                sessions = json.loads((home / "sessions.json").read_text(encoding="utf-8"))
                self.chat_id = next(iter(sessions), None)
            except Exception:  # noqa: BLE001
                pass

    # ── 生命周期 ─────────────────────────────────────────────────
    async def start(self, inject: "Callable[[str, str], Awaitable[None]]",
                    inject_image: "Callable[[list[str], str], Awaitable[None]] | None" = None,
                    answer_ask: "Callable[[str, str], bool] | None" = None,
                    set_runtime: "Callable[[str | None, str | None], None] | None" = None,
                    interrupt: "Callable[[], Awaitable[None]] | None" = None,
                    command: "Callable[[str], Awaitable[None]] | None" = None,
                    approval_answer: "Callable[[dict], None] | None" = None,
                    question_answer: "Callable[[dict], None] | None" = None,
                    question_disconnect: "Callable[[], None] | None" = None,
                    peer_report=None, managed_ingress=None) -> None:
        """开始监听。inject(text, client) / inject_image(paths, caption) 是消息注入回调;
        answer_ask(text) 直接兑现等待中的提问卡 (返回 True = 已消费, 不再走消息通路);
        approval_answer(req) 专门处理保留前缀的结构化审批，不回落到普通消息;
        set_runtime(model, effort) 静默写 runtime.json (App 切模型/强度, 不进聊天);
        interrupt() 静默掐当前回合 (App 停止按钮, 不进聊天);
        command(text) 静默斜杠命令 (App 菜单动作, 不回显用户气泡)。"""
        self._inject = inject
        self._inject_image = inject_image
        self._answer_ask = answer_ask
        self._approval_answer = approval_answer
        self._question_answer = question_answer
        self._question_disconnect = question_disconnect
        self._peer_report = peer_report
        self._managed_ingress = managed_ingress
        self._set_runtime = set_runtime
        self._interrupt = interrupt
        self._command = command
        self._loop = asyncio.get_running_loop()
        tcp = _env("APP_TCP").strip()   # "host:port" → 远程节点模式
        if tcp:
            # Windows 的 asyncio 不支持 unix socket → TCP。只绑 tailnet IP,
            # 信任边界 = tailnet (别绑 0.0.0.0: 此口无鉴权且能驱动 bypassPermissions 的 Claude)
            host, _, port = tcp.rpartition(":")
            server = await asyncio.start_server(self._on_conn, host, int(port))
            self._log(f"🌐 app 前端已监听 tcp://{tcp}")
        else:
            self._sock_path.unlink(missing_ok=True)     # 上次异常退出的残留 socket
            server = await asyncio.start_unix_server(self._on_conn, path=str(self._sock_path))
            self._sock_path.chmod(0o600)
            self._log(f"🌐 app 前端已监听 {self._sock_path}")
        # server 随进程存活, 不需要显式 serve_forever (start_unix_server 已在跑)
        self._server = server

    def _greet(self, writer: asyncio.StreamWriter) -> None:
        """应答 hello 并补发待处理事件和运行默认值。写入失败应留日志。"""
        from feishu_bridge.fleet_managed_output import before_greet
        before_greet(self, writer)
        out = []
        if self._pending:
            cutoff = time.time() - self._PENDING_TTL
            out = [e for ts, e in self._pending if ts >= cutoff]
            self._pending.clear()
        if self.defaults:
            # runtime = 当前的 model/effort **覆盖值** (跟 defaults 是两回事: defaults 是
            # 舰队常量, 永不变)。远程节点的 runtime.json 在自己机器上, 网关读不到 ——
            # 因此由 bridge 报告当前覆盖值。
            out.append(self._runtime_event())
        for e in out:
            try:
                writer.write((json.dumps(e, ensure_ascii=False) + "\n").encode())
            except Exception as exc:  # noqa: BLE001
                self._log(f"⚠ 向网关写 {e.get('ev')} 失败 ({exc}) —— 该连接大概率已断")
                return
        if out:
            self._log(f"🔁 已应答 hello: {len(out)} 条 (补发 + 默认值)")

    def _inject_ack(self, writer: asyncio.StreamWriter, chat: str, why: str = "") -> None:
        """在启动处理前返回实际解析的展示落点。写入失败只记录日志，不影响处理。"""
        if writer is None:
            # 生产只有 `_on_conn` 一个调用点, 那里 writer 恒是真的; 传 None 的只有测试
            # (它们验的是别的事)。⚠ **不写就静静返回, 别抛** —— 注入方那侧看到的是
            # 「落点未知」, 那是安全的一侧; 而抛出去会掀翻这一轮, 违反 fail-soft。
            return
        try:
            ack: dict[str, Any] = {"type": "inject_ack", "chat_id": chat}
            if why:
                # ⚠ **退回的原因也要带**: 只说"落在私聊"的话, 派活方分不出
                # 「我不是那个域的成员」(它的锅) 和 **「这台桥读不到域表」(基础设施坏了)**。
                ack["reason"] = why
            writer.write((json.dumps(ack, ensure_ascii=False) + "\n").encode())
        except Exception as exc:  # noqa: BLE001
            self._log(f"⚠ 回 inject_ack 失败 ({exc}) —— 注入方会看到「落点未知」, 不会误报成功")

    def _push_chat_context(self, work_chat: str, display_chat: str | None = None) -> tuple[Any, Any]:
        """同时放入真实会话和展示落点; asyncio 派任务时会复制这两个 contextvar。"""
        tok_work = CUR_CHAT.set(work_chat)
        tok_display = DISPLAY_CHAT.set(display_chat or work_chat)
        return tok_work, tok_display

    @staticmethod
    def _pop_chat_context(tokens: tuple[Any, Any]) -> None:
        tok_work, tok_display = tokens
        DISPLAY_CHAT.reset(tok_display)
        CUR_CHAT.reset(tok_work)

    def _spawn(self, coro) -> None:
        """后台运行注入协程，避免读循环等待整轮而阻塞回答帧；串行化由消息处理机制负责。"""
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _handle_frame(self, req: dict, writer: asyncio.StreamWriter) -> None:
        """处理网关来的一帧。抛异常由调用方兜: 丢这一帧, 连接不动。"""
        if req.get("type") == "hello":
            if writer is not None:
                self._gateway_writers.add(writer)
            # **服务端只在客户端自报身份后才主动写**:
            # 原来是"连上就写"(补发 + defaults)。而 session_inject 这类**只写不读**
            # 的客户端是 sendall 完立刻 close —— 对着已关的 socket 写会把连接拆掉,
            # **它刚发的那一行还没被 readline() 读走就没了**, 而且两侧都不报错
            # 两端都可能误以为发送成功。
            # 现在: 不认识的客户端我一个字节都不写, 想要就先说 hello。
            self._greet(writer)
            return
        managed_ingress = getattr(self, "_managed_ingress", None)
        if managed_ingress is not None:
            # Selection is a startup dependency, never a caller-supplied frame
            # field. The managed handler owns durable admission and lifecycle
            # receipts for EVERY mutating frame. A rejection/exception/None
            # result cannot fall through to an unfenced legacy entry point.
            await managed_ingress(req, writer)
            return
        if req.get("type") == "user_message" and (req.get("text") or "").strip():
            raw_text = req["text"].strip()
            actor = str(req.get("from") or "").strip().lower()
            text = f"[{actor}] {raw_text}" if actor else raw_text
            if self._inject:
                # 会话在**派任务之前**定好: asyncio 建任务时复制上下文, 所以整个回合
                # (含它派生的子任务) 都带着真实会话 + 展示落点, 事件在扇出点自动盖章。
                # ⚠ 先 `resolve_chat` 再置 —— 客户端送来的值不校验就放行 = 任意会话键。
                work, display, _why = self.resolve_work_display_ex(req.get("chat_id"))
                self._inject_ack(writer, display, _why)
                peer = None
                # Gateway user frames are human-owned. Local/tailnet IPC is the
                # existing transport trust, not cryptographic bot authentication.
                if (not _why and not actor and writer not in self._gateway_writers
                        and req.get("message_origin") != {"version": 1, "kind": "gateway_human"}
                        and self._peer_report is not None):
                    peer = self._peer_report(req.get("peer_report"), display)
                toks = self._push_chat_context(work, display)
                tok_internal = INTERNAL_GROUP_TASK.set(bool(req.get("internal_group_task")))
                tok_user = APP_USER.set(actor)
                tok_text = APP_USER_TEXT.set(raw_text)
                tok_peer = PEER_REPORT.set(peer)
                tok_arrival = MESSAGE_ARRIVAL.set(time.time())
                try:
                    self._spawn(self._inject(text, _client_of(req)))
                finally:
                    MESSAGE_ARRIVAL.reset(tok_arrival)
                    PEER_REPORT.reset(tok_peer)
                    APP_USER_TEXT.reset(tok_text)
                    APP_USER.reset(tok_user)
                    INTERNAL_GROUP_TASK.reset(tok_internal)
                    self._pop_chat_context(toks)
        elif req.get("type") == "answer":
            # Dedicated conversational namespace never reaches text/default fallback.
            if str(req.get("ask_id") or "").startswith("codex-question-"):
                if self._question_answer:
                    self._question_answer(req)
                return
            # Reserved approval cards MUST NOT fall through to conversational answers,
            # including stale cards after a restart. No policy changes happen here.
            if str(req.get("ask_id") or "").startswith("codex-approval-"):
                if self._approval_answer:
                    self._approval_answer(req)
                return
            # 提问卡的结构化作答 → 拼成可读文本走消息注入 (bridge 的 ASK_PENDING
            # 分支会把它当答案消费; 顺带镜像进飞书, 两端都看得到答了什么)
            parts = []
            for ans in (req.get("answers") or []):
                head = str(ans.get("header") or "").strip()
                val = str(ans.get("answer") or ans.get("label") or ans.get("text") or "").strip()
                if val:
                    parts.append(f"{head}: {val}" if head else val)
            text = "; ".join(parts) or str(req.get("text") or "").strip()
            actor = str(req.get("from") or "").strip().lower()
            tok_user = APP_USER.set(actor)
            tok_text = APP_USER_TEXT.set(text)
            # 作答优先直接兑现等待中的提问 —— 走普通消息通路的话, bot 正忙时
            # 会被「📥 已暂存」扣住, 等排到队 future 早没了, 答案退化成普通消息,
            
            try:
                aid = str(req.get("ask_id") or "")
                if self._answer_ask and self._answer_ask(text, aid):
                    pass
                elif self._inject and text:
                    # 兜底注入也必须使用正确的显示会话。
                    work, display, _why = self.resolve_work_display_ex(req.get("chat_id"))
                    toks = self._push_chat_context(work, display)
                    try:
                        self.broadcast({"ev": "ask_cancel", "ask_id": str(req.get("ask_id") or ""),
                                        "reason": "stale"})   # 没有等待中的提问 → 清掉各端的卡
                        self._spawn(self._inject(text, _client_of(req)))
                    finally:
                        self._pop_chat_context(toks)
            finally:
                APP_USER_TEXT.reset(tok_text)
                APP_USER.reset(tok_user)
        elif req.get("type") == "group_event":
            self.handle_group_event(req)
        elif req.get("type") == "set_runtime":
            # 模型/强度静默切换: 不注入聊天、不发提醒,
            # 直接落 runtime.json, 下一条消息在 bridge 内部生效
            if self._set_runtime:
                m, e = req.get("model"), req.get("effort")
                work, display, _why = self.resolve_work_display_ex(req.get("chat_id"))
                if str(req.get("chat_id") or "").strip().startswith("group:"):
                    self._log(f"⚠ 忽略群聊 set_runtime ({display}) —— model/effort 是 bot 属性")
                    return
                toks = self._push_chat_context(work, display)
                try:
                    self._set_runtime(str(m).strip() if m is not None else None,
                                      str(e).strip() if e is not None else None)
                    # **落盘不等于对面知道。** 切完立刻把新值报回去, 否则网关手上还是旧的,
                    # 而客户端每 15s 轮询到的就是旧值 —— 用户看到的是"切了没反应"。
                    self.broadcast(self._runtime_event())
                finally:
                    self._pop_chat_context(toks)
        elif req.get("type") == "interrupt":
            # 停止按钮/提问卡✕ 的静默中断: 不注入聊天不回显;
            # 处理完由回调广播 interrupted 回执。放后台跑, 别堵读循环。
            if self._interrupt:
                # ⚠ 会话要跟着帧走: 写死私聊的话, **在群里按停止会停掉私聊那轮**
                
                work, display, _why = self.resolve_work_display_ex(req.get("chat_id"))
                toks = self._push_chat_context(work, display)
                try:
                    self._spawn(self._interrupt())
                finally:
                    self._pop_chat_context(toks)
        elif req.get("type") == "command" and (req.get("text") or "").strip():
            # 静默命令不回显、不镜像，直接进入命令管线。
            if self._command:
                # ⚠ 同上, 而且这条更狠: 在群里点「新会话」会去 **/reset 私聊那个会话**,
                # 用户想开的是群的新一轮, 丢掉的是私聊几个月的上下文。
                # 回执跟注入同一支: ctx_compact_guard 直连 socket 发
                # /compact, 发完只 drain 3 秒 —— 没有 ack 它就只能回头翻日志猜落点,
                # 而**静默退回私聊时它根本分辨不出来**。
                work, display, _why = self.resolve_work_display_ex(req.get("chat_id"))
                self._inject_ack(writer, display, _why)
                toks = self._push_chat_context(work, display)
                try:
                    self._spawn(self._command(req["text"].strip()))
                finally:
                    self._pop_chat_context(toks)
        elif req.get("type") == "user_image":
            paths = [str(x).strip() for x in
                     (req.get("paths") or [req.get("path")]) if x and str(x).strip()]
            for url in (req.get("urls") or []):      # 远程节点: URL → 下载落地
                local = await asyncio.to_thread(self._fetch_to_local, str(url))
                if local:
                    paths.append(local)
            if self._inject_image and paths:
                # ⚠ 会话要跟着帧走 —— 漏了它**在群里发的图会落进私聊**。
                # 这是 user_message / answer / interrupt / command 之后的**第四处**同形状:
                # 每次都是"逐条补", 每次都漏下一条。所以这次同时加了一条源码断言
                # (tests/test_group_chat_id.py), 守的是「所有干活的分支都解会话」,
                # 而不是「user_image 这条对了」。
                work, display, _why = self.resolve_work_display_ex(req.get("chat_id"))
                toks = self._push_chat_context(work, display)
                raw_caption = (req.get("caption") or "").strip()
                actor = str(req.get("from") or "").strip().lower()
                caption = f"[{actor}] {raw_caption}" if actor and raw_caption else raw_caption
                tok_user = APP_USER.set(actor)
                tok_text = APP_USER_TEXT.set(raw_caption)
                try:
                    self._spawn(self._inject_image(paths, caption))
                finally:
                    APP_USER_TEXT.reset(tok_text)
                    APP_USER.reset(tok_user)
                    self._pop_chat_context(toks)

    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        gateway_hello = False
        if self._managed_ingress is not None:
            from feishu_bridge.fleet_managed_output import remember_connection
            remember_connection(self, reader, writer)
        self._writers.add(writer)
        self._log(f"🌐 网关已连接 (当前 {len(self._writers)} 个)")
        try:
            while line := await reader.readline():
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    gateway_hello = gateway_hello or req.get("type") == "hello"
                    await self._handle_frame(req, writer)
                except Exception as exc:  # noqa: BLE001
                    # 一帧处理出错**不许拆掉整条连接**: 外层那个
                    # `except: pass` 本来是给「网关掉线」用的, 但它同样吃掉分派里的
                    # 任何异常 —— 结果是这一帧被静默丢弃、连接断开、网关 5 秒后重连,
                    # 两侧都不报错。今天改 inject 签名时正是这样炸的 (测试里的旧回调
                    # 少一个参数 → TypeError → 整条通道无声消失)。
                    self._log(f"⚠ 处理网关帧 {req.get('type')!r} 出错, 本帧丢弃 (连接保留): {exc}")
        except Exception:  # noqa: BLE001  (网关掉线等, 静默清理)
            pass
        finally:
            from feishu_bridge.fleet_managed_output import before_disconnect, close_connection
            original_connection = before_disconnect(self, writer)
            self._writers.discard(writer)
            self._gateway_writers.discard(writer)
            if gateway_hello and not self._gateway_writers and self._question_disconnect:
                self._question_disconnect()
            if original_connection is not None:
                await close_connection(original_connection)
            else:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass

    # ── 文件跨机 (远程节点专用, 本机 unix 模式不走这些) ───────────
    def _fetch_to_local(self, url: str) -> str | None:
        """从网关拉附件到本地 media/web_inbox/ (bot 与网关不同机时, 路径传不过来)。

        **顺带记住它在网关侧的原路径**: 回显给 App 要用那个, 见 `origin_or`。
        """
        try:
            name = url.split("path=")[-1].split("&")[0].split("%2F")[-1].split("/")[-1] or "file.bin"
            dest = self._sock_path.parent / "media" / "web_inbox" / f"{int(time.time()*1000)}_{name}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(url, timeout=30) as r:
                dest.write_bytes(r.read())
            try:
                # 网关侧的绝对路径就在 URL 的 path= 里 —— 这个文件本来就是从那儿下来的,
                # 所以回显**不用把 615KB 再传回去**, 记住这个路径就够。
                origin = parse_qs(urlparse(url).query).get("path", [""])[0]
                if origin:
                    self._origins[str(dest)] = origin
            except Exception:  # noqa: BLE001  (记不住只是回显退回老行为, 不该拦下载)
                pass
            return str(dest)
        except Exception as exc:  # noqa: BLE001
            self._log(f"⚠ 附件下载失败: {exc}")
            return None

    def _runtime_event(self) -> dict:
        """runtime 事件的唯一拼装口 —— 三处发它 (hello / 切换后 / 每轮结束), 拼三遍会漂。"""
        rt = self.runtime_now()
        model = str(rt.get("model") or self.defaults.get("model") or "")
        engine = _engine_from_model(model)
        return {"ev": "runtime", "defaults": self.defaults, "runtime": rt,
                "observed": self.observed_model, "engine": engine, "caps": self.caps(engine)}

    def caps(self, engine: str = "claude") -> list:
        """本进程支持的能力。网关据此判断哪些入口能给客户端 (老桥不报 = 空集)。"""
        # `group_chat` = 听得懂带 chat_id 的帧和 group_event。老桥不报它 ⇒ 网关 fail-closed
        # 不给群入口; 否则老桥会静默忽略群里的消息, 用户等一个不来的回复。
        # Claude 与 Codex 都支持 compact。Codex 由桥调用原生 app-server compact,
        # 因此不能再按 engine 摘掉这项 —— 否则客户端会灰掉一个实际可用的入口。
        caps = ["group_chat", "compact"]
        # Codex is cross-platform; operators can fail closed per node through
        # FLEET_CODEX_ENABLED when a local CLI installation is unhealthy.
        if CODEX_CAPABLE:
            caps.append("codex")
        # 有 DeepSeek key 才报 —— 网关据此决定 /api/models 里放不放 deepseek-*,
        # 以及 set_model 要不要当场拒。每次现查 (只看 env/文件非空, 不读内容)。
        try:
            from feishu_bridge import fleet_providers
            if fleet_providers.deepseek_configured():
                caps.append("deepseek")
        except Exception:  # noqa: BLE001  (查不到 = 不报, 网关 fail-closed)
            pass
        return caps

    def broadcast_runtime(self, observed: str = "") -> None:
        """把当前 runtime + 这一轮实际跑的模型报给网关 (宿主每轮结束调一次)。"""
        if observed:
            self.observed_model = observed
        self.broadcast(self._runtime_event())

    def runtime_now(self) -> dict:
        """当前 model/effort 覆盖 —— 由宿主注入取值口 (`set_runtime_reader`)。

        不在这里自己读 runtime.json: 那个文件的位置和格式归 fleet_bridge 管,
        在这儿再写一份路径拼接就是第二份真相 (今天已经在别处吃过这个亏)。
        没注入 → 返回 {} → 网关那侧退回老行为, 不会更坏。
        """
        try:
            return self._runtime_reader() if self._runtime_reader else {}
        except Exception:  # noqa: BLE001  (读不到只影响胶囊显示, 不许拦下这一帧)
            return {}

    def set_runtime_reader(self, fn) -> None:
        self._runtime_reader = fn

    def origin_or(self, local: str) -> str:
        """回显用的路径: 有网关侧原路径就用它, 否则原样返回。

        **为什么必须换**: 用户在 App 里发图给远程节点时, bridge 把文件下载到本地
        `media/web_inbox/`, 回显事件却广播**那份本地副本的路径** ——
        `D:\\fleet\\winops-home\\...`。而 App 是去 **Mac 网关**取图: 那台机器上没有这个
        文件; 更早一步, `D:\\...` 在 POSIX 下连绝对路径都不算, /media 的 `is_absolute()`
        直接判 403。
        **文件从头到尾都在网关的 uploads/ 里**, bridge 只是拉了一份副本给模型读 ——
        所以回显该指回原件, 不是副本。

        出站媒体 (`_TeeTransport.send_media_file`) 早就处理了同一个问题, 用 `relay_media`
        把字节传上去; 入站这条**连传都不用传**, 原件就在对面。
        `relays_to_gateway` 的 docstring 当时就写着「这个为真时, 广播本地路径对 App 一定
        是死路」—— 那句话对两条路都成立, 而只有一条照做了。
        """
        return self._origins.get(local) or local

    @property
    def relays_to_gateway(self) -> bool:
        """这个实例的出站媒体是否**必须**经网关中转 (= 它是远程节点)。

        配了 FEISHU_BRIDGE_GATEWAY 的只有远程节点 (本机 bot 走 unix socket, 网关直接
        读得到它的文件)。所以这个为真时, 广播本地路径对 App 一定是死路。
        """
        return bool(_env("GATEWAY").strip() and _env("GATEWAY_TOKEN").strip())

    def relay_media(self, path: str) -> str | None:
        """上传本地媒体并返回网关路径。返回 None 时，调用方须通过 relays_to_gateway 区分本地模式和中转失败。"""
        base = _env("GATEWAY").rstrip("/")
        token = _env("GATEWAY_TOKEN")
        if not base or not token:
            return None
        try:
            p = Path(path)
            blob = p.read_bytes()      # 循环外只读一次: 重试不必重复读一个几十 MB 的文件,
                                       # 而且文件读不出来 (已被消费/权限) 重试也没用
        except Exception as exc:  # noqa: BLE001
            self._log(f"⚠ 媒体读取失败, 未中转: {exc}")
            return None
        # 连接层瞬断 → 重试; HTTPError (服务端已应答的 4xx/5xx) 不重试。
        # ⚠ 次数只由 `_UPLOAD_RETRY_MAX` 说了算 (理由同 fleet_bridge 那处)。
        # 判据与 fleet_bridge._req_json 那条**故意长一样** —— 同一个问题在两处
        # 应该用同一套规矩, 否则以后改一处忘一处。
        
        # 连接 → 单次异常就放弃, 而 **outbox 那份文件此前已被消费删除**, 于是用户的图
        # **永久丢失**, 只看到一行「未送达」。10054 是典型可重试的瞬时错误。
        # ⚠ 已知代价: 若连接断在「上传完成、响应未回」之间, 服务端可能已落盘, 重试会
        # 多留一个孤儿文件。拿一个孤儿文件换一张不丢的图, 这笔账是划算的 —— 但它不是零成本。
        last_exc = None
        for attempt in range(_UPLOAD_RETRY_MAX):
            try:
                boundary = f"fleet{int(time.time()*1000)}"
                bot = _env("BOT_NAME", "unknown")
                # Preserve known media types; unknown suffixes use octet-stream.
                ctype = _CT_BY_SUFFIX.get(p.suffix.lower(), "application/octet-stream")
                body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"bot\"\r\n\r\n{bot}\r\n"
                        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                        f"filename=\"{p.name}\"\r\nContent-Type: {ctype}\r\n\r\n"
                        ).encode() + blob + f"\r\n--{boundary}--\r\n".encode()
                req = urllib.request.Request(
                    f"{base}/upload", data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                             "X-Fleet-Token": token})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read()).get("path")
            except HTTPError as exc:
                
                # Retry transient gateway errors; reject other HTTP errors.
                if exc.code in _RETRY_STATUS:
                    last_exc = exc
                    if attempt < _UPLOAD_RETRY_MAX - 1:
                        time.sleep(0.6 * (attempt + 1))
                    continue
                self._log(f"⚠ 媒体上传网关被拒 (不重试): {exc}")
                return None
            except Exception as exc:  # noqa: BLE001  连接层错误 → 可重试
                last_exc = exc
                if attempt < _UPLOAD_RETRY_MAX - 1:
                    time.sleep(0.6 * (attempt + 1))
        self._log(f"⚠ 媒体上传网关失败 (重试 {_UPLOAD_RETRY_MAX} 次): {last_exc}")
        return None

    # ── 出站广播 (线程安全, fail-soft) ────────────────────────────
    # 网关重启期间掉线的**已成型**事件 (回复/通知/媒体/用户消息) 暂存, 等网关回来补发。
    
    # 回复恰好在网关重启的空档发出, App 端永久丢失)
    # ask 和 ask_cancel 必须可补发，避免重连后丢失提问卡。
    # compact 的 start 和 done 也必须成对补发。
    _REPLAYABLE = {"reply", "notice", "media", "file", "user", "push", "error",
                   "ask", "ask_cancel", "compact"}
    _PENDING_MAX = 50
    _PENDING_TTL = 900          # 15 分钟前的就别补了, 上下文早过去了

    _GROUP_KINDS = ("dispatch", "report")
    _GROUP_KEYS = ("task", "kind", "actor", "owner", "status", "title", "doc")

    def handle_group_event(self, req: dict[str, Any]) -> None:
        """群任务事件 → 群时间线 (契约 fleet_api §2 「派活/汇报进群」)。

        ⚠ **意图只由字段表达, 正文一个字都不解析。** 否掉的方案是"网关扫注入正文里的
        `[group:<domain> #<id>]` 标记就镜像进群"。正文可能**原样引用该标记**, 内容嗅探会把解释协议的话当成真派活。
        **标记是给人读的, 不能同时当路由依据**: 引用和真事件长得一模一样。
        ⇒ 所以这里**只取白名单字段**, `text` 之类一律不带进事件。

        入口从严 (域/任务号/kind 任一不合就整条丢弃), 展示从宽 (客户端对未知值兜底显示)
        —— 拦在入口的是**没有出处的东西**, 兜底的是**已经进来的东西**。
        """
        group = str(req.get("group") or "").strip()
        task = str(req.get("task") or "").strip()
        kind = str(req.get("kind") or "").strip()
        if not group or not task or kind not in self._GROUP_KINDS:
            return
        chat = f"group:{group}"
        if self.resolve_chat(chat) != chat:      # 域不在名单里 ⇒ 默认拒绝那一侧
            return
        ev: dict[str, Any] = {"ev": "group_event", "group": group, "chat_id": chat}
        for k in self._GROUP_KEYS:
            v = req.get(k)
            if v not in (None, ""):
                ev[k] = v
        self.broadcast(ev)

    def set_chat_validator(self, fn) -> None:
        """注入"这个 chat_id 允许吗" —— 域名单是 fleet_bridge 的知识, 不在这儿读第二份真相
        (同 `set_runtime_reader` 的理由)。**没注入 ⇒ 一律退回私聊**, 见 `resolve_chat`。"""
        self._chat_validator = fn

    def resolve_chat_ex(self, requested: Any) -> tuple[str, str]:
        """解析会话键, 并**带回拒绝原因**。返回 `(落点, 原因码)`; 放行时原因码为 `""`。

        ⚠ 原因码要透出去 (`inject_ack.reason`):
        校验器只回 bool 时, **「域表读不到」和「域不存在」长得一模一样**。
        **「拒绝」和「我判断不了」必须分开**, 而分开之后要有人**看得到**。
        """
        want = str(requested or "").strip()
        if not want:
            return self.chat_id or "app", ""
        if want == (self.chat_id or "app"):
            return want, ""
        if self._chat_validator is None:
            # 没装校验器 = 这台桥不支持群 ⇒ 退回私聊 (默认拒绝那一侧)
            return self.chat_id or "app", "no_validator"
        try:
            ok, reason = self._chat_validator(want)
        except Exception as exc:  # noqa: BLE001
            self._log(f"⚠ 会话校验器抛了 ({exc}) —— 按拒绝处理, 退回私聊")
            return self.chat_id or "app", "validator_error"
        if ok:
            return want, ""
        return self.chat_id or "app", reason or "rejected"

    def resolve_chat(self, requested: Any) -> str:
        """解析会话键 (只要落点)。校验不过一律退回私聊 —— **默认拒绝那一侧**。"""
        return self.resolve_chat_ex(requested)[0]

    def resolve_work_display_ex(self, requested: Any) -> tuple[str, str, str]:
        """解析一帧的真实会话与展示落点。

        私聊: `(bot 私聊, bot 私聊, "")`。
        群聊: `(bot 私聊, group:<domain>, "")`。群不是独立上下文, 只是 UI 聚合面。
        校验失败: `(bot 私聊, bot 私聊, reason)`。
        """
        display, why = self.resolve_chat_ex(requested)
        private = self.chat_id or "app"
        if display.startswith("group:"):
            return private, display, why
        return display, display, why

    def _with_chat(self, ev: dict[str, Any]) -> dict[str, Any]:
        """给事件盖上会话章。**在唯一的扇出点做**, 不在每个调用点做 ——
        逐个调用点加, 证明不了"每条都带", 而漏掉的那条会**静默画进别的会话**
        (契约 fleet_api §2.5: 下行恒带 `chat_id`)。
        已经带了就不动 (补发路径会带上原来那条的会话)。不就地改调用方的字典。
        """
        if "chat_id" in ev:
            return ev
        return {**ev, "chat_id": DISPLAY_CHAT.get() or CUR_CHAT.get() or self.chat_id or "app"}

    def broadcast(self, ev: dict[str, Any], *, _managed_entry=None) -> None:
        """把事件推给所有网关连接。可从任意线程调; 网关不在则按需暂存待补发。"""
        if _managed_entry is not None:
            from feishu_bridge.fleet_managed_output import begin
            return begin(self, ev, _managed_entry)
        ev = self._with_chat(ev)
        if not self._writers or self._loop is None:
            if ev.get("ev") in self._REPLAYABLE:
                self._pending.append((time.time(), ev))
                del self._pending[:-self._PENDING_MAX]
            return
        try:
            line = (json.dumps(ev, ensure_ascii=False) + "\n").encode()
        except (TypeError, ValueError):
            return
        self._loop.call_soon_threadsafe(self._write_all, line)

    def _write_all(self, line: bytes, _managed_tail=None) -> None:
        if _managed_tail is not None:
            from feishu_bridge.fleet_managed_output import callback
            return callback(self, line, _managed_tail)
        for w in list(self._writers):
            try:
                w.write(line)
            except Exception:  # noqa: BLE001  (单个网关写失败 → 踢掉, 不影响其它)
                self._writers.discard(w)

    # ── 飞书侧钩子 ───────────────────────────────────────────────
    def on_feishu_user(self, chat_id: str, text: str) -> None:
        """飞书来了用户消息: 记住 chat_id (Web 注入要用) + 镜像给 Web 端。"""
        if chat_id != self.chat_id:
            self.chat_id = chat_id
            try:
                self._chat_file.write_text(chat_id, encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        self.broadcast(user_event(text, origin="feishu"))


class _TeeStream:
    """StreamSession 双写: 飞书流式卡照旧, 同时把进度广播给 Web (自带轻节流)。"""

    _MIN_INTERVAL = 0.4          # Web 端刷新节流 (本地 socket 便宜, 但浏览器渲染不便宜)

    def __init__(self, inner, front: AppFrontend) -> None:
        self._i = inner
        self._front = front
        self._last = 0.0

    @property
    def ok(self) -> bool:                    # 镜像内层状态 (核心靠它决定回退)
        return self._i.ok

    @property
    def message_id(self) -> str | None:
        return self._i.message_id

    @property
    def delivered_chars(self) -> int:
        return getattr(self._i, "delivered_chars", 0)

    async def start(self) -> bool:
        return await self._i.start()

    async def put_answer(self, full: str, force: bool = False) -> None:
        await self._i.put_answer(full, force)
        now = time.monotonic()
        if force or now - self._last >= self._MIN_INTERVAL:
            self._front.broadcast({"ev": "stream", "text": full})
            self._last = now

    async def put_thinking(self, lines: list[str]) -> None:
        await self._i.put_thinking(lines)
        self._front.broadcast({"ev": "thinking", "lines": lines})

    async def finish(self, full: str, lines: list[str],
                     extra: dict | None = None) -> bool:
        """Forward the platform result and broadcast the reply once to the app."""
        ok = await self._i.finish(full, lines)
        # extra: 这一轮的结构化收尾信息 (失败时的 failed/retryable/retry_hint)。
        # **走参数而不是挂在实例上**: 挂状态没被消费就会泄漏到下一条 reply, 而那种错
        # 不报错、只是上一轮的按钮出现在这一轮。
        self._front.broadcast({"ev": "reply", "text": full, **(extra or {})})
        return bool(ok)


class TeeTransport:
    """Transport 双写: 飞书为主路 (返回值以它为准), Web 广播为旁路 (fail-soft)。

    只覆盖「用户可见输出」类方法; 其余 (download_incoming / fetch_message_text /
    begin_busy / pin …) 经 __getattr__ 直通飞书实现 —— 那些是飞书侧概念, Web 不需要。
    """

    def __init__(self, feishu, front: AppFrontend) -> None:
        self.feishu = feishu          # 暴露内层: 需要「只发飞书不广播」时用 (如 Web 消息镜像)
        self._front = front

    def __getattr__(self, name: str):
        return getattr(self.feishu, name)

    def send(self, chat: str, text: str, kind: str = "") -> None:
        """`kind` 只作用于**广播**那一路, 不传给内层 transport (飞书不认识它)。

        A structured kind lets clients identify notices independently of text prefixes.
        """
        self.feishu.send(chat, text)
        ev = {"ev": "notice", "text": text}
        if kind:
            ev["kind"] = kind
        self._front.broadcast(ev)

    def send_text(self, chat: str, text: str) -> None:
        self.feishu.send_text(chat, text)
        self._front.broadcast({"ev": "notice", "text": text})

    def send_reply(self, chat: str, answer_md: str, thinking_lines: list[str],
                   reply_to: str | None = None, extra: dict | None = None) -> str | None:
        mid = self.feishu.send_reply(chat, answer_md, thinking_lines, reply_to)
        self._front.broadcast({"ev": "reply", "text": answer_md,
                               "thinking": thinking_lines, **(extra or {})})
        return mid

    def send_media_file(self, chat: str, path: str, kind: str) -> bool:
        ok = self.feishu.send_media_file(chat, path, kind)
        keep = path
        try:
            src = Path(path)
            if "outbox" in src.parts and src.is_file():
                
                # 广播前留持久副本到 media/sent/ (随 _cleanup_media 按天数自动清)。
                import shutil
                dest = self._front._sock_path.parent / "media" / "sent" / \
                       f"{int(time.time() * 1000)}_{src.name}"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                keep = str(dest)
        except Exception:  # noqa: BLE001
            pass
        # 远程节点: 本地路径浏览器拿不到 → 先中继给网关换成网关侧路径
        relayed = self._front.relay_media(keep)
        if kind == "file":
            # §2.24: 文件走 `ev:"file"` —— 网关收到后当场收库, 把 path 换成 file_id 再下发。
            # ⚠ 不许发成 `ev:"media"`: 那条路网关按 /media 取图, 永远不会收库, 客户端也不出下载卡。
            self._front.broadcast({"ev": "file", "name": Path(path).name,
                                   "path": relayed or keep, "ok": ok})
        else:
            self._front.broadcast({"ev": "media", "kind": kind, "path": relayed or keep, "ok": ok})
        # 出站媒体留一行:
        # bridge.log 对出站媒体**一个字都没有** —— 发没发、发的哪个、多大, 只能从媒体
        # 副本的 mtime 和网关历史倒推, 而那两样都不是为此设的。
        # 在线连接数不是成败判据 (0 个也会由历史补发), 但它和「发给了 2 个客户端」是
        # 两回事 —— 分不开就没法回答用户那句「我这边什么都没看到」。
        try:
            self._front._log(
                f"📤 出站媒体 {kind} {Path(keep).name} {_human_size(keep)} "
                f"→ {'中转网关' if relayed else '本地路径'}, "
                f"在线 {len(self._front._writers)} 个客户端, ok={ok}")
        except Exception:  # noqa: BLE001  (留痕是旁路, 炸了也不许拦下这条媒体)
            pass
        # 中转失败必须报告：None 可能表示本机模式或中转失败。
        # 合并了 —— 本机模式不需要中转 (正常) 和配了网关却失败 (异常) 走同一条路。
        # 后者广播的本地路径在远程节点上, App 必然取不到, 但 bot 视角看文件已被消费并
        # 删除, 和成功一模一样。媒体事件照旧发 (协议面不变, 客户端不用改), 额外补一条
        # notice —— 宁可用户看到「没送达」, 也别让他对着一张永远加载不出来的图猜。
        if relayed is None and self._front.relays_to_gateway:
            self._front.broadcast({
                "ev": "notice",
                "text": f"⚠ 图片未能送达 App (中转网关失败): {Path(keep).name}"})
        return ok

    def open_stream(self, chat: str, reply_to: str | None = None, summary: str = ""):
        return _TeeStream(self.feishu.open_stream(chat, reply_to, summary), self._front)


def tool_brief(name: str, tin: dict) -> str:
    """工具输入的一行摘要 (Web 活动流用, 对齐 Claude 官方 App 的步骤条)。

    纯函数无平台概念。飞书面板因卡片长度限制只列子代理任务名，
    Web 端不继承该妥协 —— 这里给每个工具调用一条可读摘要。
    """
    for key in ("file_path", "path", "command", "description", "pattern",
                "prompt", "query", "url", "skill", "text"):
        val = tin.get(key)
        if isinstance(val, str) and val.strip():
            val = " ".join(val.split())
            if key in ("file_path", "path"):
                val = val.rstrip("/").rsplit("/", 1)[-1]
            return val[:80]
    return ""
