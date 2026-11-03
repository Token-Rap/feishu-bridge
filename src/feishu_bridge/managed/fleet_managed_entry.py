"""Actual ordinary bridge/frontend entry adapter, inactive until trusted startup.

Only an already durable gateway-human reservation can enter this route. Other
producers/control frames still require their own complete reviewed lifecycle.
No runtime registration, native proof, queue replay or startup bypass here.
"""
from __future__ import annotations

import time
import contextvars
from dataclasses import dataclass

from feishu_bridge.bridge import app_frontend as frontend_context
from feishu_bridge.managed import fleet_managed_ingress as ingress
from feishu_bridge.managed import fleet_managed_runtime as runtime_module

_NATIVE_PROMPT = contextvars.ContextVar("managed_original_native_prompt", default=None)


def require(ok, code="managed_entry_scope_unknown"):
    if not ok:
        raise ingress.Refused(code)


@dataclass(frozen=True)
class Route:
    native_chat: str
    work_chat: str
    engine: str
    bot: str


class Entry:
    def __init__(self, runtime, frontend, *, bot, engine, session_key,
                 current_engine, turn, stamp, busy, terminal):
        require(type(runtime) is runtime_module.Runtime)
        native = ingress._string(frontend.chat_id)
        engine = ingress._identity(engine)
        require(engine in {"codex", "claude"})
        # The trusted loader binds the actual bridge session-key function. It
        # derives the engine row from the existing native chat; raw frame WORK
        # and suffix guesses are not inputs to that derivation.
        work = ingress._string(session_key(native, engine))
        self.route = Route(native, work, engine, ingress._identity(bot))
        self.runtime, self.frontend = runtime, frontend
        self._session_key, self._current_engine = session_key, current_engine
        self._turn, self._stamp, self._busy, self._terminal = turn, stamp, busy, terminal
        self._native_started = set()
        self._native_owners = {}  # Strong Action/work ownership BEFORE spawn.
        self._native_settlements = {}  # Original physical/wire facts, no parent-ready grant.
        self._persistence_owners = {}  # Strong consumed file-helper observations; no settlement grant.

    def binding(self):
        r = self.route
        native = self.frontend.chat_id
        work = self._session_key(r.native_chat, r.engine)
        engine = self._current_engine()
        require(type(native) is str and native == r.native_chat
                and type(work) is str and work == r.work_chat
                and type(engine) is str and engine == r.engine,
                "managed_entry_binding_drift")
        return r

    async def handle_frame(self, req, writer):
        from feishu_bridge.managed.fleet_managed_output import refuse_entry_delivery
        refuse_entry_delivery(self)
        from feishu_bridge.managed.fleet_managed_diagnostic import refuse_entry_delivery
        refuse_entry_delivery(self)
        route = self.binding()
        require(type(req) is dict and req.get("type") == "user_message",
                "managed_frame_unsupported")
        requested = req.get("chat_id")
        require(type(requested) is str and bool(requested), "managed_display_unknown")
        if requested == route.bot:
            # The authenticated gateway uses bot name for private DISPLAY.
            # Native Feishu chat and its engine map row remain separately bound.
            native, display, reason = route.native_chat, route.bot, ""
        else:
            require(requested.startswith("group:"), "managed_display_unknown")
            native, display, reason = self.frontend.resolve_work_display_ex(requested)
        require(type(reason) is str and reason == "" and type(native) is str
                and native == route.native_chat and type(display) is str
                and display == requested, "managed_display_unknown")
        request, scope = self.runtime.prepare(req, work_chat=route.work_chat,
            display_chat=display, engine=route.engine, bot=route.bot,
            gateway_connection=writer in self.frontend._gateway_writers)
        self.runtime.verify_queued(request, scope)
        # Same existing inject_ack means resolved routing only, not dispatch or
        # model/HUMAN completion. It follows the exact durable reservation check.
        self.frontend._inject_ack(writer, display)
        payload = self._payload(request)
        tokens = self.frontend._push_chat_context(route.native_chat, display)
        context = [(frontend_context.INTERNAL_GROUP_TASK, False),
                   (frontend_context.APP_USER, payload["display_actor"]),
                   (frontend_context.APP_USER_TEXT, payload["text"]),
                   (frontend_context.PEER_REPORT, None),
                   (frontend_context.MESSAGE_ARRIVAL, time.time())]
        set_tokens = []
        try:
            for variable, value in context:
                set_tokens.append((variable, variable.set(value)))
            self.frontend._spawn(self.runtime.execute_new(request, scope,
                self._execute, self._terminal))
        finally:
            for variable, token in reversed(set_tokens):
                variable.reset(token)
            self.frontend._pop_chat_context(tokens)

    @staticmethod
    def _payload(request):
        import json
        return json.loads(request.payload)

    async def _execute(self, payload):
        # This dispatch context still has the native WORK chat and exact DISPLAY,
        # actor, original client and full text; no old normalization or mirror.
        from feishu_bridge.managed.fleet_managed_diagnostic import bind_entry
        bind_entry(self)
        from feishu_bridge.managed.fleet_managed_output import remember_frontend
        remember_frontend(self)
        await self.inject(payload["text"], payload["client"])

    def action(self, *, native=None, text=None, client=None):
        route = self.binding()
        payload = self.runtime.message()
        require(payload["work_chat"] == route.work_chat
                and payload["engine"] == route.engine and payload["bot"] == route.bot
                and frontend_context.CUR_CHAT.get() == route.native_chat
                and frontend_context.DISPLAY_CHAT.get() == payload["display_chat"]
                and frontend_context.APP_USER.get() == payload["display_actor"]
                and frontend_context.INTERNAL_GROUP_TASK.get() is False
                and frontend_context.PEER_REPORT.get() is None)
        if native is not None:
            require(type(native) is str and native == route.native_chat)
        if text is not None:
            require(type(text) is str and text.encode("utf-8", errors="strict") ==
                    payload["text"].encode("utf-8"))
        if client is not None:
            require(type(client) is str and client == payload["client"])
        return payload

    async def inject(self, text, client):
        payload = self.action(text=text, client=client)
        event = frontend_context.user_event(text, origin="web",
            chat_id=payload["display_chat"], user=payload["display_actor"])
        event.update(work_id=payload["work_id"], work_chat=payload["work_chat"],
                     work_actor=payload["actor"], client=payload["client"])
        if payload["display_actor"]:
            event["from"] = payload["display_actor"]
        self.frontend.broadcast(event, _managed_entry=self)
        await self.handle_message(self.route.native_chat, text, None)

    async def handle_message(self, chat, text, message_id):
        payload = self.action(native=chat, text=text)
        # Old PENDING/ASK/COALESCE/steer/reset/commands do not own this message.
        # Concurrent legacy/native state is a hold, never a merge or a reset.
        require(message_id is None and self._busy(chat) is False,
                "managed_legacy_work_unsettled")
        prompt = self._stamp(text, frontend_context.MESSAGE_ARRIVAL.get(), payload["display_chat"])
        action = self.runtime.action()
        token = _NATIVE_PROMPT.set((self, action, prompt))
        try:
            await self._turn(chat, prompt, None)
        finally:
            _NATIVE_PROMPT.reset(token)

    def native_child(self, chat, prompt, message_id, plan):
        payload = self.action(native=chat)
        saved = _NATIVE_PROMPT.get()
        action = self.runtime.action()
        require(type(saved) is tuple and len(saved) == 3 and saved[0] is self
                and saved[1] is action and type(prompt) is str and type(saved[2]) is str
                and prompt.encode("utf-8", "strict") == saved[2].encode("utf-8", "strict")
                and message_id is None and plan is False and self.route.engine == "codex",
                "managed_native_prompt_unknown")
        require(payload["work_id"] not in self._native_started,
                "managed_native_attempt_consumed")
        require(payload["work_id"] not in self._native_owners,
                "managed_native_owner_conflict")
        self._native_started.add(payload["work_id"])
        from feishu_bridge.managed import fleet_managed_child
        owner = fleet_managed_child.NativeChild(self)
        # No subprocess can be created until _live verifies this exact record.
        # Identity failure/UNKNOWN retains the owner and its returned Process.
        self._native_owners[payload["work_id"]] = owner
        from feishu_bridge.managed.fleet_managed_diagnostic import bind_native
        bind_native(owner, self)
        return owner

    def require_turn(self, chat, plan):
        self.action(native=chat)
        require(plan is False, "managed_turn_kind_unknown")
