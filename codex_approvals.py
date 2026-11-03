"""Once-only App Server approvals over Fleet's existing ask/answer transport.

No policy mutation or retry: only an actual server request can create an approval.
The gateway authenticates human clients; the bridge socket retains its existing
trusted-local/tailnet boundary. Ordinary chat and bot injections are never answers.
"""
from __future__ import annotations

import asyncio
import copy
import json
import unicodedata
import uuid
from pathlib import PurePosixPath, PureWindowsPath

PREFIX = "codex-approval-"
HEADER = "Codex 审批"
DECLINE = "拒绝本次"
ACCEPT = "批准本次"
GRANT = "授予本轮权限"
PERMISSIONS_METHOD = "item/permissions/requestApproval"
METHODS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval", PERMISSIONS_METHOD}


def approval_policy(value: str) -> str:
    value = value.strip() or "never"
    if value not in {"never", "on-request"}:
        raise ValueError("FLEET_CODEX_APPROVAL_POLICY must be never or on-request")
    return value


def reviewer_params(value: str) -> dict:
    """Empty preserves the effective Codex setting; never synthesize acceptance."""
    value = value.strip()
    if not value:
        return {}
    if value not in {"user", "auto_review"}:
        raise ValueError("FLEET_CODEX_APPROVALS_REVIEWER must be user or auto_review")
    return {"approvalsReviewer": value}


def verify_reviewer(requested: dict, response: dict) -> None:
    expected = requested.get("approvalsReviewer")
    if not expected:
        return
    observed = (response.get("result") or {}).get("approvalsReviewer")
    if observed == "guardian_subagent":  # 0.159.2 accepts the legacy alias
        observed = "auto_review"
    if observed != expected:
        raise RuntimeError("Codex 未确认请求的审批审核方式；请检查版本和有效策略，未启动新回合")


def permissions_probe(value: str, policy: str, reviewer: dict, sandbox: str) -> bool:
    """Explicit operator-only test mode; never relax the ordinary runtime defaults."""
    value = value.strip().lower()
    if value in {"", "0", "false"}:
        return False
    if value not in {"1", "true"}:
        raise ValueError("FLEET_CODEX_PERMISSIONS_PROBE must be true or false")
    if (policy != "on-request" or reviewer != {"approvalsReviewer": "user"}
            or sandbox != "read-only"):
        raise ValueError("权限探针仅允许 on-request / user / read-only，未启动测试")
    return True


def verify_probe_thread(response: dict) -> None:
    result = response.get("result") or {}
    sandbox = result.get("sandbox")
    if (result.get("approvalPolicy") != "on-request"
            or result.get("approvalsReviewer") != "user"
            or not isinstance(sandbox, dict)
            or sandbox.get("type") != "readOnly"
            or sandbox.get("networkAccess") is not False):
        raise RuntimeError("Codex 未确认探针的只读、无网络及人工审批设置；未启动测试回合")


def probe_feature_page(response: dict) -> tuple[bool, str | None]:
    result = response.get("result") or {}
    data = result.get("data")
    cursor = result.get("nextCursor")
    if not isinstance(data, list) or (cursor is not None and not isinstance(cursor, str)):
        raise RuntimeError("Codex 权限工具元数据格式不明；未启动测试回合")
    matches = [row for row in data if isinstance(row, dict)
               and row.get("name") == "request_permissions_tool"]
    if len(matches) > 1 or (matches and matches[0].get("enabled") is not True):
        raise RuntimeError("Codex 未启用 request_permissions_tool；未启动测试回合")
    return bool(matches), cursor


def requested_permissions(raw):
    """Bounded supported subset, copied exactly; unknown shapes never gain access."""
    def absolute(value):
        return (isinstance(value, str) and bool(value) and
                (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()))

    if not isinstance(raw, dict) or not raw or set(raw) - {"network", "fileSystem"}:
        return None
    network = raw.get("network")
    if network is not None:
        if not isinstance(network, dict) or set(network) - {"enabled"}:
            return None
        if network.get("enabled") is not None and type(network["enabled"]) is not bool:
            return None
    fs = raw.get("fileSystem")
    if fs is not None:
        if not isinstance(fs, dict) or set(fs) - {"read", "write", "entries", "globScanMaxDepth"}:
            return None
        for mode in ("read", "write"):
            paths = fs.get(mode)
            if paths is not None and (not isinstance(paths, list) or not all(absolute(p) for p in paths)):
                return None
        entries = fs.get("entries")
        if entries is not None:
            if not isinstance(entries, list):
                return None
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != {"access", "path"}:
                    return None
                if entry["access"] not in ("read", "write", "deny"):
                    return None
                path = entry["path"]
                if (not isinstance(path, dict) or set(path) != {"type", "path"} or
                        path["type"] != "path" or not absolute(path["path"])):
                    return None  # glob/special profiles need their own review UI
        depth = fs.get("globScanMaxDepth")
        if depth is not None and (type(depth) is not int or depth < 1):
            return None
    if not ((network or {}).get("enabled") is True or
            any((fs or {}).get(k) for k in ("read", "write", "entries"))):
        return None
    return copy.deepcopy(raw)


class Approvals:
    def __init__(self, thread, turn, chat, send, emit, log, *, enabled=False, timeout=600):
        self.thread, self.turn, self.chat = thread, turn, chat
        self.send, self.emit, self.log = send, emit, log
        self.enabled, self.timeout = enabled, timeout
        self.pending = {}
        self.seen = set()
        self.items = {}
        self.reviews = set()
        self.tasks = set()
        self.closed = False

    def _emit(self, event):
        self.emit({**event, "chat_id": self.chat})

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _respond(self, request_id, result=None, error=None):
        try:
            await self.send({"id": request_id, **({"error": error} if error else {"result": result})})
        except Exception as exc:
            # Never retry a response: a broken pipe may have accepted the first write.
            self.log(f"Codex approval response failed: {type(exc).__name__}")
            self._emit({"ev": "notice", "text": "Codex 审批回传失败，未确认执行；请停止当前回合后检查连接。"})

    def _question(self, method, params):
        details = {k: v for k, v in params.items() if k not in {"threadId", "turnId", "itemId"}}
        item_id = params.get("itemId")
        if not isinstance(item_id, str) or not item_id:
            return None
        item = self.items.get(item_id, {})
        if method == PERMISSIONS_METHOD:
            if requested_permissions(params.get("permissions")) is None:
                return None
            title = "Codex 请求临时权限（仅当前回合有效）"
        elif method == "item/fileChange/requestApproval":
            changes = item.get("changes")
            if not isinstance(changes, list) or not changes:
                return None
            details["changes"] = changes
            title = "Codex 请求修改文件"
        else:
            command = params.get("command") or item.get("command")
            network = params.get("networkApprovalContext")
            if not isinstance(command, str) or not command.strip():
                if not isinstance(network, dict) or not network.get("host"):
                    return None
            details["command"] = command
            details["cwd"] = params.get("cwd") or item.get("cwd")
            title = "Codex 请求访问网络" if network else "Codex 请求执行命令"
        body = json.dumps(details, ensure_ascii=False, indent=2)
        # JSON escapes ordinary controls; reject invisible formatting (including
        # bidi overrides) and overlong previews rather than hiding any argv/diff.
        if len(body) > 24000 or any(unicodedata.category(c) == "Cf" for c in body):
            return None
        grant = method == PERMISSIONS_METHOD
        explanation = ("下列权限仅在当前回合有效；不会延续到整个会话。" if grant else "仅批准这一次请求；未回答会拒绝。")
        return {"header": HEADER, "question": title + "\n" + explanation + "\n" + body,
                "multiSelect": False,
                "options": [{"label": DECLINE, "description": "不执行本次请求"},
                            {"label": GRANT if grant else ACCEPT,
                             "description": "仅授予上面展示的权限，当前回合结束即失效" if grant else "仅允许上面完整展示的本次请求"}]}

    def feed(self, event):
        """Nonblocking: stdout must continue draining while a human considers a card."""
        if not isinstance(event, dict) or self.closed:
            return False
        method = event.get("method")
        params = event.get("params")
        params = params if isinstance(params, dict) else {}
        matching = params.get("threadId") == self.thread and params.get("turnId") == self.turn
        if method in {"item/autoApprovalReview/started", "item/autoApprovalReview/completed"} and matching:
            review = params.get("review")
            review = review if isinstance(review, dict) else {}
            status, rid = review.get("status"), params.get("reviewId")
            labels = {"inProgress": "正在自动审核", "approved": "自动审核通过，等待执行结果",
                      "denied": "自动审核拒绝，未放行", "timedOut": "自动审核超时，未确认放行",
                      "aborted": "自动审核已中止，未确认放行"}
            if isinstance(rid, str) and rid and isinstance(status, str) and status in labels:
                key = (rid, status)
                if key not in self.reviews:
                    self.reviews.add(key)
                    rationale = review.get("rationale")
                    rationale = rationale if isinstance(rationale, str) else ""
                    detail = rationale[:2000] + ("…（原因过长，已截断）" if len(rationale) > 2000 else "")
                    self._emit({"ev": "notice", "text": "Codex：" + labels[status] + ("\n" + detail if detail else "")})
                    self.log(f"Codex auto review: chat={self.chat} review={rid} status={status}")
        if method == "item/started" and matching:
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") in {"commandExecution", "fileChange"}:
                if isinstance(item.get("id"), str) and len(self.items) < 128:
                    self.items[item["id"]] = item
        if method in {"item/completed", "serverRequest/resolved"}:
            if params.get("threadId") == self.thread:
                item = params.get("item")
                item = item if isinstance(item, dict) else {}
                for ask, pending in list(self.pending.items()):
                    resolved = (method == "serverRequest/resolved" and
                                params.get("requestId") == pending["id"])
                    completed = (method == "item/completed" and matching and
                                 item.get("id") == pending["item"])
                    if resolved or completed:
                        self._remove(ask, "resolved")
                if method == "item/completed" and matching:
                    if isinstance(item.get("id"), str):
                        self.items.pop(item["id"], None)
        if "id" not in event or not isinstance(method, str):
            return False
        rid = event["id"]
        if type(rid) not in {str, int}:
            return True
        key = (type(rid), rid)
        if key in self.seen:
            return True
        self.seen.add(key)
        if method not in METHODS:
            self._spawn(self._respond(rid, error={"code": -32601,
                "message": "Fleet does not support this server request; no permission granted"}))
            return True
        permission_request = method == PERMISSIONS_METHOD
        choices = None if permission_request else params.get("availableDecisions")
        if choices is not None and (not isinstance(choices, list) or
                                   not any(c in choices for c in ("decline", "cancel"))):
            self._spawn(self._respond(rid, error={"code": -32602,
                "message": "Approval request has no supported refusal decision"}))
            self._emit({"ev": "notice", "text": "Codex 审批请求缺少可用的拒绝选项，未授予权限。"})
            return True
        negative = "decline"
        if isinstance(choices, list) and "decline" not in choices and "cancel" in choices:
            negative = "cancel"
        can_accept = choices is None or (isinstance(choices, list) and "accept" in choices)
        question = self._question(method, params) if matching and can_accept else None
        refusal = {"permissions": {}, "scope": "turn"} if permission_request else {"decision": negative}
        if not self.enabled or not question or len(self.pending) >= 8:
            self._spawn(self._respond(rid, refusal))
            self._emit({"ev": "notice", "text": "Codex 审批请求未展示，已拒绝：审批未启用、请求不完整或范围不匹配。"})
            return True
        ask = PREFIX + uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        accepted = ({"permissions": requested_permissions(params["permissions"]), "scope": "turn"}
                    if permission_request else {"decision": "accept"})
        self.pending[ask] = {"id": rid, "item": params.get("itemId"), "future": future,
                             "accept_label": GRANT if permission_request else ACCEPT}
        self._spawn(self._wait(ask, future, rid, accepted, refusal))
        self._emit({"ev": "ask", "ask_id": ask, "questions": [question]})
        self.log(f"Codex approval waiting: chat={self.chat} ask={ask}")
        return True

    def answer(self, req):
        ask = req.get("ask_id")
        if not isinstance(ask, str):
            return False
        pending = self.pending.get(ask)
        if not pending or pending["future"].done() or self.closed:
            return False
        # Older authenticated notification answers omit chat_id: the unguessable
        # ask id identifies the original chat. Explicit mismatches never fall back.
        if req.get("chat_id") and req["chat_id"] != self.chat:
            return False
        answers = req.get("answers")
        decision = "decline"
        if isinstance(answers, list) and len(answers) == 1 and isinstance(answers[0], dict):
            answer = answers[0]
            values = [answer[k] for k in ("answer", "label", "text") if k in answer]
            if answer.get("header") == HEADER and values and all(v == pending["accept_label"] for v in values):
                decision = "accept"
        # Invalid/free-form responses fail closed; gateway clears submitted cards.
        pending["future"].set_result(decision)
        return True

    def _remove(self, ask, reason):
        pending = self.pending.pop(ask, None)
        if pending:
            if not pending["future"].done():
                pending["future"].cancel()
            self._emit({"ev": "ask_cancel", "ask_id": ask, "reason": reason})

    async def _wait(self, ask, future, rid, accepted, refusal):
        reason = "cancelled"
        try:
            try:
                decision = await asyncio.wait_for(future, self.timeout)
                reason = "answered"
            except asyncio.TimeoutError:
                decision, reason = "decline", "timeout"
            if ask in self.pending and not self.closed:
                await self._respond(rid, accepted if decision == "accept" else refusal)
                self.log(f"Codex approval decision: chat={self.chat} ask={ask} decision={decision} reason={reason}")
        except asyncio.CancelledError:
            pass
        finally:
            self._remove(ask, reason)

    async def close(self):
        self.closed = True
        for ask in list(self.pending):
            self._remove(ask, "cancelled")
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.items.clear()
        self.reviews.clear()
