#!/usr/bin/env python3
"""Probe SDK thinking blocks and streaming deltas."""
import asyncio
import collections
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, StreamEvent,
    TextBlock, ThinkingBlock,
)

PROMPT = ("请先仔细推理再回答: 一个农夫有 17 只羊, 除了 9 只以外全都跑了, "
          "还剩几只? 说明你的推理过程。")


async def main() -> None:
    opts = ClaudeAgentOptions(
        cwd="/tmp",
        permission_mode="bypassPermissions",
        model="claude-opus-5",
        effort="high",
        include_partial_messages=True,     # 与桥接一致
        max_buffer_size=16 * 1024 * 1024,
        cli_path=None,
    )
    blocks: collections.Counter = collections.Counter()
    deltas: collections.Counter = collections.Counter()
    thinking_chars = 0
    async with ClaudeSDKClient(options=opts) as client:
        await client.query(PROMPT)
        async for msg in client.receive_response():
            if isinstance(msg, StreamEvent):
                ev = msg.event or {}
                d = ev.get("delta") or {}
                t = ev.get("type") or "?"
                if t == "content_block_delta":
                    deltas[f"delta:{d.get('type') or '?'}"] += 1
                elif t == "content_block_start":
                    deltas[f"start:{((ev.get('content_block') or {}).get('type') or '?')}@{ev.get('index')}"] += 1
                elif t == "content_block_stop":
                    deltas[f"stop:@{ev.get('index')}"] += 1
                else:
                    deltas[t] += 1
            elif isinstance(msg, AssistantMessage):
                for b in msg.content:
                    blocks[type(b).__name__] += 1
                    if isinstance(b, ThinkingBlock):
                        thinking_chars += len(b.thinking or "")
                    elif isinstance(b, TextBlock):
                        pass
    print("block 类型:", dict(blocks))
    print("delta 类型:", dict(deltas))
    print("ThinkingBlock 文本总长:", thinking_chars)


asyncio.run(main())
