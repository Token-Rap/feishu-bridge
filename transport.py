"""前端传输层抽象 — 把「聊天平台」从 bridge 核心里解耦。

背景: bridge 约 81% 的代码 (会话生命周期 / 模型热切换 / 忙时暂存合并 /
中断 / 用量统计 / 子代理归属 / 媒体标记 / 时间戳注入 …) 与平台无关, 但核心逻辑直接
调用了飞书专有方法 (`create_stream_card` / `stream_put` / `add_reaction` /
`upload_image`+`send_image` 两步式 …), 想接第二个前端 (自研多端 app) 就得改核心。

本模块定义**平台中立**的接口, 让核心只依赖动作语义, 不依赖平台概念:
  - 「发一条最终回复」而不是「POST 一张 interactive 卡片」
  - 「发本地图片」而不是「先 upload 拿 image_key 再 send」
  - 「开始/结束忙碌指示」而不是「加/撤 Typing 表情」
  - 「开一个流式会话」而不是「建 CardKit 卡 + 按 element_id 分片 PUT」

飞书实现见 `FeishuTransport` (它包住既有的 FeishuSender, 行为不变)。
新增前端只需实现这两个 Protocol, 核心一行不用改。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StreamSession(Protocol):
    """一个回合的流式输出会话 (打字机效果)。

    平台若不支持流式, 实现成「全部 no-op, ok=False」即可 —— 核心会自动回退到
    结束时一次性发整条回复。
    """

    ok: bool
    """False = 流式不可用/已失效, 核心据此回退整发。"""

    message_id: str | None
    """流式载体的消息 id (用于出站正文缓存/引用解析); 平台无此概念可为 None。"""

    async def start(self) -> bool:
        """建立流式会话。返回是否成功 (失败核心会回退整发)。"""
        ...

    async def put_answer(self, full: str, force: bool = False) -> None:
        """刷新答案区为 `full` (全量, 非增量)。实现内部自行节流。"""
        ...

    async def put_thinking(self, lines: list[str]) -> None:
        """刷新「子代理活动」折叠区。"""
        ...

    async def finish(self, full: str, lines: list[str]) -> None:
        """定稿: 落最终内容并关闭流式。"""
        ...


@runtime_checkable
class Transport(Protocol):
    """聊天前端需要提供的能力。方法均以**动作语义**命名, 不含平台名词。

    约定:
      - 所有方法 fail-soft: 失败记日志/返回 False, 不抛给核心 (前端故障不该杀掉回合)。
      - `chat` 是前端自己的会话标识 (飞书是 chat_id), 核心只透传不解释。
    """

    # ── 发送 ──────────────────────────────────────────────────────
    def send_text(self, chat: str, text: str) -> None:
        """发一条轻量文本 (状态提示/错误提示这类, 不需要富渲染)。"""
        ...

    def send(self, chat: str, text: str) -> None:
        """发一条富文本消息 (markdown), 过长自动分片。"""
        ...

    def send_reply(self, chat: str, answer_md: str, thinking_lines: list[str],
                   reply_to: str | None = None, extra: dict | None = None) -> str | None:
        # extra: 只有会广播的实现 (TeeTransport) 用得上 —— 它把结构化收尾信息
        # (failed/retryable/retry_hint) 带进 reply 事件。**签名仍放进 Protocol**:
        # SENDER 的静态类型就是这个接口, 只给某一个实现加参数会让类型与现实不符,
        # 而那种不符不报错、只在换实现时才炸。纯平台实现忽略它即可。
        """发本回合的最终回复 (答案 + 可折叠的子代理活动)。返回消息 id。

        reply_to 给了则挂到该消息下 (回复式), 平台不支持就普通发送。
        """
        ...

    def send_media_file(self, chat: str, path: str, kind: str) -> bool:
        """发送**本地文件**。kind: "image" | "video"。

        平台需要「先上传拿 key 再发」的两步式请在实现内部完成 —— 核心不关心。
        """
        ...

    # ── 接收侧辅助 ────────────────────────────────────────────────
    def download_incoming(self, message_id: str, key: str, kind: str,
                          suffix: str) -> str | None:
        """把用户发来的媒体下载到本地, 返回路径。kind: "image" | "file"。"""
        ...

    def fetch_message_text(self, message_id: str) -> str | None:
        """取某条历史消息的可读正文 (解析「引用回复」用); 取不到返 None。"""
        ...

    # ── 状态指示 ──────────────────────────────────────────────────
    def begin_busy(self, message_id: str) -> str | None:
        """标记「正在处理」(飞书是加 Typing 表情)。返回一个句柄供结束时撤销。"""
        ...

    def end_busy(self, message_id: str, handle: str) -> None:
        """撤销忙碌指示。"""
        ...

    def pin(self, message_id: str) -> bool:
        """置顶一条消息。平台不支持则返回 False。"""
        ...

    def unpin(self, message_id: str) -> bool:
        """撤销置顶 ([[置顶:key]] 同 key 顶替时撤旧的)。平台不支持则返回 False。"""
        ...

    # ── 流式 ──────────────────────────────────────────────────────
    def open_stream(self, chat: str, reply_to: str | None = None,
                    summary: str = "") -> StreamSession:
        """创建一个流式会话对象 (尚未建立, 需 await 其 start())。

        summary 是会话列表/推送里的预览文本 (通常放本轮问题)。
        """
        ...
