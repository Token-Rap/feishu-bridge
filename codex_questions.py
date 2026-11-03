"""Ephemeral conversational cards; never an App Server request/permission handler.

All transitions run synchronously on the bridge event loop. Nothing is recovered
from disk, and no transition emits JSON-RPC, selects a default, or grants a tool.
The selected 0.159.2 agentMessage.questions schema is the only producer supported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import secrets
import time

PREFIX = "codex-question-"
PROTOCOL = "codex-0.159.2-agentMessage.questions-v1"
EXECUTABLE_SHA256 = "16593cc2f422d5f398a8e40f550ebbaf1245392528957be342c295920a300704"
TTL = 600
MAX_PENDING = 4
MAX_CARD_BYTES = 32768
_verified_binary: tuple | None = None


class CompletedBeforeWrite(RuntimeError):
    """Exact guard outcome before any steering bytes; not a failed RPC retry."""


def selected_protocol(executable: str) -> str | None:
    """Metadata only. A different binary gets no conversational producer support."""
    global _verified_binary
    try:
        path = Path(executable).resolve(strict=True)
        st = path.stat()
        key = (str(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if key != _verified_binary:
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != EXECUTABLE_SHA256 or path.stat() != st:
                return None
            _verified_binary = key
        return PROTOCOL
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class Binding:
    work: str
    display: str
    thread: str
    turn: str
    runtime: str
    protocol: str
    connection: str


@dataclass
class Origin:
    binding: Binding
    process: object
    normal: bool = False
    disconnected: bool = False
    failed: bool = False
    seen: set[str] = field(default_factory=set)


@dataclass
class Card:
    ask_id: str
    origin: Origin
    item: str
    questions: tuple[tuple[str, str, tuple[str, ...]], ...]
    binding_id: str
    deadline: float
    created_at: float
    expires_at: float
    state: str = "pending"
    answer: str = ""
    user: str = ""
    outcome_recorded: bool = False

    def wire_binding(self) -> dict:
        b = self.origin.binding
        return {"binding_id": self.binding_id, "thread_id": b.thread,
                "turn_id": b.turn, "item_id": self.item, "protocol": b.protocol}


class Registry:
    def __init__(self, emit, audit, *, clock=time.monotonic, wall=time.time):
        self.emit, self.audit = emit, audit
        self.clock, self.wall = clock, wall
        self.cards: dict[str, Card] = {}

    def _event(self, card: Card, state: str, reason: str = "") -> None:
        # No question/answer/exception bodies, credentials, or reconstructed authority.
        self.audit(f"Codex question ask={card.ask_id} state={state} reason={reason}")

    def cancel(self, card: Card, reason: str) -> None:
        if self.cards.pop(card.ask_id, None) is not card:
            return
        card.state = "cancelled"
        self.emit({"ev": "ask_cancel", "ask_id": card.ask_id,
                   "chat_id": card.origin.binding.display, "reason": reason})
        self._event(card, "cancelled", reason)

    def cancel_work(self, work: str | None, reason: str) -> None:
        for card in list(self.cards.values()):
            if work is None or card.origin.binding.work == work:
                self.cancel(card, reason)

    def sweep(self, valid=None) -> None:
        for card in list(self.cards.values()):
            if self.clock() >= card.deadline:
                self.cancel(card, "expired")
            elif valid is not None and not valid(card):
                self.cancel(card, "binding_changed")

    def observe(self, origin: Origin, event: dict, *, process: object, current: bool) -> Card | None:
        """Called only by the owning current stdout reader, before text accumulation."""
        if (not current or process is not origin.process or origin.failed
                or origin.normal or origin.disconnected or "id" in event
                or event.get("method") != "item/completed" or event.get("replay") is True):
            return None
        p = event.get("params")
        if not isinstance(p, dict) or p.get("threadId") != origin.binding.thread or p.get("turnId") != origin.binding.turn:
            return None
        item = p.get("item")
        if not isinstance(item, dict) or item.get("type") != "agentMessage" or item.get("questions") is None:
            return None
        iid = item.get("id")
        if not isinstance(iid, str) or not iid.strip() or len(iid) > 256:
            return None
        if origin.binding.protocol != PROTOCOL or iid in origin.seen:
            return None
        if len(origin.seen) >= 4096:
            self.cancel_work(origin.binding.work, "producer_limit")
            origin.failed = True
            return None
        origin.seen.add(iid)  # Unsupported and duplicate completed items never repeat a card.
        raw = item.get("questions")
        parsed = []
        if isinstance(raw, list) and 1 <= len(raw) <= 3:
            for i, q in enumerate(raw, 1):
                if not isinstance(q, dict) or set(q) - {"title", "options"}:
                    break  # Includes secret/isOther/unsupported shapes: never display their bodies.
                title, options = q.get("title"), q.get("options")
                if (not isinstance(title, str) or not title.strip() or len(title) > 6000
                        or not isinstance(options, list) or not 2 <= len(options) <= 8
                        or any(not isinstance(o, str) or not o.strip() or len(o) > 1024 for o in options)
                        or len(set(options)) != len(options)
                        or len({o.strip() for o in options}) != len(options)):
                    break
                parsed.append((f"Q{i}", title, tuple(options)))
        if not isinstance(raw, list) or len(parsed) != len(raw) or not parsed:
            self.audit("Codex question refused reason=unsupported_selectable_shape")
            self.emit({"ev": "notice", "chat_id": origin.binding.display,
                       "text": "Codex 提问未形成可选卡：仅支持 1–3 道完整选择题；未作默认选择。"})
            return None
        self.sweep()
        conflicts = [c for c in self.cards.values() if c.origin.binding.work == origin.binding.work]
        if conflicts:
            for old in conflicts:
                self.cancel(old, "conflicting_question")
            origin.failed = True
            self.audit("Codex question refused reason=conflicting_question")
            return None
        if len(self.cards) >= MAX_PENDING:
            self.audit("Codex question refused reason=pending_limit")
            return None
        now = self.wall()
        card = Card(PREFIX + secrets.token_hex(16), origin, iid, tuple(parsed),
                    secrets.token_hex(16), self.clock() + TTL, now, now + TTL)
        frame = {"ev": "ask", "ask_id": card.ask_id, "chat_id": origin.binding.display,
                 "questions": [{"header": h, "question": t, "multiSelect": False,
                                "options": [{"label": o, "description": ""} for o in opts]}
                               for h, t, opts in card.questions],
                 "created_at": now, "expires_at": now + TTL,
                 "question_binding": card.wire_binding()}
        if len(json.dumps(frame, ensure_ascii=False).encode()) > MAX_CARD_BYTES:
            self.audit("Codex question refused reason=card_bytes_limit")
            return None
        self.cards[card.ask_id] = card
        try:
            self.emit(frame)
        except Exception:
            self.cancel(card, "card_send_failed")
            return None
        self._event(card, "emitted_unconfirmed")  # broadcast is not native delivery proof.
        return card

    def finish(self, origin: Origin, *, normal: bool) -> None:
        origin.normal = bool(normal and not origin.failed and not origin.disconnected)
        origin.failed = not origin.normal
        if not origin.normal:
            for card in list(self.cards.values()):
                if card.origin is origin:
                    self.cancel(card, "origin_failed")

    def disconnect(self, origin: Origin, *, expected: bool) -> None:
        origin.disconnected = True
        origin.process = None  # Retain logical binding only, never an exited process handle.
        if not expected or not origin.normal:
            origin.failed = True
            for card in list(self.cards.values()):
                if card.origin is origin:
                    self.cancel(card, "origin_disconnected")

    def claim(self, req: dict, valid) -> Card | None:
        self.sweep(valid)
        aid = req.get("ask_id")
        card = self.cards.get(aid) if isinstance(aid, str) else None
        if card is None or card.state != "pending" or req.get("type") != "answer":
            return None
        stamp = req.get("question_answer")
        if (not isinstance(stamp, dict) or set(stamp) != {"source", "user", "binding"}
                or stamp.get("source") != "gateway_authenticated_human"
                or not isinstance(stamp.get("user"), str) or not stamp["user"].strip()
                or stamp.get("binding") != card.wire_binding()
                or req.get("chat_id") != card.origin.binding.display
                or req.get("text") not in (None, "")):
            return None
        answers = req.get("answers")
        if not isinstance(answers, list) or len(answers) != len(card.questions):
            return None
        mapped = {}
        for answer in answers:
            if not isinstance(answer, dict) or set(answer) - {"header", "answer", "label"}:
                return None
            head = answer.get("header")
            label = answer.get("answer", answer.get("label"))
            if "answer" in answer and "label" in answer and answer["answer"] != answer["label"]:
                return None
            if not isinstance(head, str) or not isinstance(label, str) or head in mapped:
                return None
            mapped[head] = label
        if set(mapped) != {q[0] for q in card.questions} or any(mapped[h] not in opts for h, _, opts in card.questions):
            return None
        card.answer = "\n\n".join(f"{h} — {title}\n选择：{mapped[h]}" for h, title, _ in card.questions)
        card.user = stamp["user"]
        card.state = "received"
        self.emit({"ev": "ask_cancel", "ask_id": card.ask_id,
                   "chat_id": card.origin.binding.display, "reason": "answered"})
        self._event(card, "human_answer_received")
        return card

    def transition(self, card: Card, state: str, valid) -> bool:
        self.sweep(valid)
        allowed = {"queued": {"received"}, "dispatching": {"received", "queued"}}
        if self.cards.get(card.ask_id) is not card or card.state not in allowed.get(state, set()):
            return False
        card.state = state
        self._event(card, state)
        return True

    def delivered(self, card: Card, *, acknowledged: bool) -> None:
        if self.cards.pop(card.ask_id, None) is card:
            card.state = "sent_same_thread" if acknowledged else "delivery_failed_or_unknown"
            card.outcome_recorded = True
            self._event(card, card.state)
        elif card.state == "cancelled" and not card.outcome_recorded:
            card.outcome_recorded = True
            self._event(card, "acknowledged_after_cancel" if acknowledged else "delivery_failed_or_unknown")

    def requeue_completed_before_write(self, card: Card, valid) -> bool:
        self.sweep(valid)
        if (self.cards.get(card.ask_id) is not card or card.state != "dispatching"
                or not card.origin.normal):
            return False
        card.state = "queued"
        self._event(card, "queued", "normal_completion_before_write")
        return True
