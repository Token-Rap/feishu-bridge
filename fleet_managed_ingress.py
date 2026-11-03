"""Strict input preparation for an explicitly selected managed bridge home.

This module does not create a coordinator, admit work, acknowledge delivery, or
execute a request.  The operator-owned coordinator is the only queue/fence.
Its caller must commit admission before sending an acknowledgement.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Mapping


class Refused(ValueError):
    """Fixed diagnostic only; private input values never enter the exception."""


def _refuse(code: str):
    raise Refused(code)


def _identity(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        _refuse("identity_invalid")
    return value


def _string(value, *, maximum=256, empty=False):
    if type(value) is not str or "\x00" in value or (not value and not empty):
        _refuse("scope_invalid")
    try:
        if len(value.encode("utf-8", errors="strict")) > maximum:
            _refuse("scope_invalid")
    except UnicodeError:
        _refuse("scope_invalid")
    return value


@dataclass(frozen=True)
class Selection:
    directory: Path
    selector_id: str
    target_home: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str], home: Path):
        keys = ("FLEET_TARGET_CONTROL_REQUIRED", "FLEET_TARGET_CONTROL_DIR",
                "FLEET_TARGET_CONTROL_ID")
        values = tuple(environ.get(key) for key in keys)
        if all(value is None for value in values):
            return None  # Explicit legacy policy; never a managed-error fallback.
        if values[0] != "1" or type(values[1]) is not str:
            _refuse("managed_selector_invalid")
        selector_id = _identity(values[2])
        directory = Path(values[1])
        target_home = Path(home)
        if (not directory.is_absolute() or directory != directory.resolve()
                or not target_home.is_absolute() or target_home != target_home.resolve()):
            _refuse("managed_selector_invalid")
        return cls(directory, selector_id, target_home)


@dataclass(frozen=True)
class Request:
    work_id: str
    work_chat: str
    display_chat: str
    engine: str
    payload: bytes


def gateway_message(req: dict, *, work_chat: str, display_chat: str,
                    engine: str, bot: str, gateway_connection: bool) -> Request:
    """Whitelist an actual gateway-human frame without normalizing its text.

    work_id is supplied by the trusted gateway's dispatch producer, never by a
    Web client's arbitrary fields. Local/tailnet IPC remains the transport trust
    boundary; a hello or this marker is not cryptographic identity proof.
    Helpers, images, commands and answers need their own reviewed admission.
    """
    if type(req) is not dict or req.get("type") != "user_message":
        _refuse("managed_frame_unsupported")
    origin = req.get("message_origin")
    if (gateway_connection is not True or type(origin) is not dict
            or set(origin) != {"version", "kind"}
            or type(origin.get("version")) is not int or origin["version"] != 1
            or origin.get("kind") != "gateway_human"
            or ("internal_group_task" in req and req["internal_group_task"] is not False)
            or req.get("peer_report") is not None):
        _refuse("managed_origin_unknown")
    work_id = _identity(req.get("work_id"))
    if engine not in {"claude", "codex"}:
        _refuse("managed_engine_unknown")
    text = _string(req.get("text"), maximum=1024 * 1024)
    if not text.strip():
        _refuse("managed_text_empty")
    # Actor is an outer authenticated gateway attribution, distinct from bot or
    # chat. It is not reconstructed from a private account/history lookup.
    payload = json.dumps({
        "version": 1, "kind": "gateway_human", "work_id": work_id,
        "work_chat": _string(work_chat), "display_chat": _string(display_chat),
        "engine": engine, "bot": _identity(bot),
        "client": _identity(req.get("client")),
        "actor": _string(req.get("work_actor"), maximum=256),
        "display_actor": _string(req.get("from", ""), maximum=128, empty=True),
        "text": text,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > 1024 * 1024:
        _refuse("managed_payload_capacity")
    return Request(work_id, work_chat, display_chat, engine, payload)
