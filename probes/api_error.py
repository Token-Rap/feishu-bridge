#!/usr/bin/env python3
"""Probe SDK error fields against a local upstream that returns HTTP 429."""
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8791


class _Always429(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.dumps({"type": "error", "error": {
            "type": "rate_limit_error", "message": "probe: forced 429"}}).encode()
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST

    def log_message(self, *a):        # 别把每条请求打到 stderr
        return


def _serve():
    HTTPServer(("127.0.0.1", PORT), _Always429).serve_forever()


async def main() -> None:
    threading.Thread(target=_serve, daemon=True).start()
    os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{PORT}"
    os.environ["CLAUDE_CODE_MAX_RETRIES"] = "1"        # 别为了探针把 429 重试很多轮

    from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient,  # noqa: E402
                                  ResultMessage)
    opts = ClaudeAgentOptions(cwd="/tmp", permission_mode="bypassPermissions",
                              model="claude-opus-5", include_partial_messages=True,
                              cli_path=None)
    seen_result = None
    err = None
    try:
        async with ClaudeSDKClient(options=opts) as client:
            await client.query("说一个字")
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    seen_result = msg
    except Exception as exc:  # noqa: BLE001  —— 异常本身也是结论的一部分
        err = exc

    print("=== 抛出的异常 ===")
    print(f"  类型: {type(err).__name__ if err else '(没抛)'}")
    if err is not None:
        for attr in ("subtype", "terminal_reason", "api_error_status", "errors", "exit_code"):
            if hasattr(err, attr):
                print(f"  err.{attr} = {getattr(err, attr)!r}")
        print(f"  str(err)[:200] = {str(err)[:200]!r}")

    print("=== 收到的 ResultMessage ===")
    if seen_result is None:
        print("  (没有 —— 说明这条路上错误只以异常形式出现)")
    else:
        for attr in ("subtype", "is_error", "api_error_status", "terminal_reason",
                     "errors", "result"):
            print(f"  {attr} = {getattr(seen_result, attr, '(无此字段)')!r}")


asyncio.run(main())
