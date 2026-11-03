"""UNWIRED SOURCE proposal for explicit original UI defaults; never an answer.

No production import or selected-producer capability is allocated by this file.
The declaration is a reviewed producer input, never derived from option wording.
"""
from __future__ import annotations

import json
import math

CAPABILITY = "fleet-question-ui-default-declaration-v1-SOURCE"
DRAFT_SCHEMA = "UNPUBLISHED_EXPLICIT_DEFAULT_DRAFT_V1"
BINDING_KEYS = ("binding_id", "thread_id", "turn_id", "item_id", "protocol")
PROTOCOL = "codex-0.159.2-agentMessage.questions-v1"


def _text(value, limit):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def original_basis(card, *, now):
    """Mirror the unchanged original shape without importing any live runtime."""
    if (not isinstance(card, dict) or card.get("ev") != "ask"
            or not _text(card.get("ask_id"), 256)
            or not card["ask_id"].startswith("codex-question-")
            or card.get("answered") or not _text(card.get("bot"), 256)
            or not _text(card.get("chat_id"), 256)):
        return None
    created, expires = card.get("created_at"), card.get("expires_at")
    if (any(type(v) not in (int, float) or not math.isfinite(v)
            for v in (now, created, expires))
            or not created <= now < expires or not 0 < expires - created <= 600):
        return None
    binding, qs = card.get("question_binding"), card.get("questions")
    if (not isinstance(binding, dict) or set(binding) != set(BINDING_KEYS)
            or any(not _text(binding[k], 256) for k in BINDING_KEYS)
            or binding["protocol"] != PROTOCOL
            or not isinstance(qs, list) or not 1 <= len(qs) <= 3):
        return None
    questions = []
    for i, q in enumerate(qs, 1):
        if (not isinstance(q, dict) or set(q) != {"header", "question", "multiSelect", "options"}
                or q["header"] != f"Q{i}" or q["multiSelect"] is not False
                or not _text(q["question"], 6000) or not isinstance(q["options"], list)
                or not 2 <= len(q["options"]) <= 8):
            return None
        opts = q["options"]
        if (any(not isinstance(o, dict) or set(o) != {"label", "description"}
                or not _text(o["label"], 1024) or o["description"] != "" for o in opts)
                or len({o["label"].strip() for o in opts}) != len(opts)):
            return None
        questions.append([q["header"], q["question"], False,
                          [[o["label"], o["description"]] for o in opts]])
    try:
        if len(json.dumps(card, ensure_ascii=False).encode()) > 32768:
            return None
    except (TypeError, ValueError):
        return None
    return [card["chat_id"], card["bot"], card["ask_id"], created, expires,
            [binding[k] for k in BINDING_KEYS], questions]


def _same_original_basis(value, original):
    """Typed JSON identity; only finite numeric timestamps may mix int/float."""
    kind = type(original)
    if kind is list:
        return (type(value) is list and len(value) == len(original)
                and all(_same_original_basis(v, o) for v, o in zip(value, original)))
    if kind is bool:
        return type(value) is bool and value is original
    if kind is str:
        return type(value) is str and value == original
    if kind in (int, float):
        try:
            return (type(value) in (int, float) and math.isfinite(value)
                    and math.isfinite(original) and value == original)
        except OverflowError:
            return False
    return False


def producer_default_draft(card, declaration, *, capability, current, now):
    """Return a local-selection draft only for a complete explicit declaration.

    `current` is a SOURCE caller obligation, not self-authentication by this helper.
    Nothing here observes/claims a registry card, mutates input, or sends a frame.
    """
    if capability != CAPABILITY or current is not True:
        return None
    basis = original_basis(card, now=now)
    if (basis is None or not isinstance(declaration, dict)
            or set(declaration) != {"version", "basis", "defaults"}
            or type(declaration["version"]) is not int or declaration["version"] != 1
            or not _same_original_basis(declaration["basis"], basis)):
        return None
    defaults = declaration["defaults"]
    if not isinstance(defaults, list) or len(defaults) != len(card["questions"]):
        return None
    selections = []
    for q, d in zip(card["questions"], defaults):
        if (not isinstance(d, dict) or set(d) != {"header", "optionIndex", "label", "role"}
                or d["header"] != q["header"] or d["role"] not in ("approval", "choice")
                or type(d["optionIndex"]) is not int
                or not 0 <= d["optionIndex"] < len(q["options"])
                or d["label"] != q["options"][d["optionIndex"]]["label"]):
            return None
        selections.append({"header": d["header"], "optionIndex": d["optionIndex"], "label": d["label"]})
    # JSON copy severs references to the producer's mutable declaration/card.
    return json.loads(json.dumps({"schema": DRAFT_SCHEMA, "basis": basis, "selections": selections}))
