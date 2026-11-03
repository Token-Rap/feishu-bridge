"""Typed trusted-IPC reports and a bounded, non-authoritative delivery journal.

This proves registry/route qualification, not cryptographic bot identity. Only
session_inject --from ... --kind report may produce this envelope. No body parser.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
from pathlib import Path

KEYS = {"version", "kind", "source_bot", "source_chat", "target_bot", "target_chat", "id"}
GROUP = re.compile(r"group:[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def qualify(value, *, receiver, display, registry, domains):
    """Read current canonical inputs on every intake/release; unknown denies."""
    if (not isinstance(value, dict) or set(value) != KEYS
            or type(value["version"]) is not int or value["version"] != 1
            or value["kind"] != "report" or value["target_bot"] != receiver
            or value["target_chat"] != display
            or not all(isinstance(value[k], str) for k in KEYS - {"version"})
            or not re.fullmatch(r"[0-9a-f]{32}", value["id"])
            or not GROUP.fullmatch(value["source_chat"])
            or not GROUP.fullmatch(value["target_chat"])):
        return None
    try:
        bots = json.loads(Path(registry).read_text())
        config = json.loads(Path(domains).read_text())
        groups = config["domains"]
        source, target = value["source_bot"], value["target_bot"]
        a, b = value["source_chat"][6:], value["target_chat"][6:]
        ga, gb = groups[a], groups[b]
        if (source == target or not isinstance(bots[source], dict)
                or not isinstance(bots[target], dict)
                or source not in ga["bots"] or target not in gb["bots"]
                or ga.get("lead") not in ga["bots"] or gb.get("lead") not in gb["bots"]):
            return None
        if a == b:
            if source != ga["lead"] and target != ga["lead"]:
                return None
        elif (source != ga["lead"] or target != gb["lead"]
              or not any(isinstance(p, list) and len(p) >= 2 and {p[0], p[1]} == {a, b}
                         for p in config.get("related", []))):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return dict(value)


class Journal:
    """Original reports only; never persist/restore card authority or answers.

    One bot event loop owns the file. Held/claimed records surviving a restart
    require explicit recovery review; no automatic replay of possibly delivered
    work. Bounds refuse new deferral rather than dropping older reports.
    """
    MAX_RECORDS = 128
    MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, path):
        self.path = Path(path)
        self.rows = []
        if self.path.exists():
            if self.path.stat().st_size > self.MAX_BYTES:
                raise ValueError("oversized peer journal; retained, no replay")
            data = json.loads(self.path.read_text())
            if (not isinstance(data, dict) or set(data) != {"version", "reports"}
                    or type(data["version"]) is not int or data["version"] != 1 or not isinstance(data["reports"], list)
                    or len(data["reports"]) > self.MAX_RECORDS):
                raise ValueError("invalid peer report journal; retained, no replay")
            self.rows = data["reports"]
            seen = set()
            for row in self.rows:
                if (not isinstance(row, dict) or set(row) != {"origin", "work", "display", "text", "arrived",
                        "visible", "cards", "expires", "state", "reason"}
                        or row["state"] not in {"held", "claimed", "completed", "delivery_unknown", "recovery_required", "blocked"}
                        or not isinstance(row["origin"], dict) or set(row["origin"]) != KEYS
                        or type(row["origin"]["version"]) is not int or row["origin"]["version"] != 1
                        or row["origin"]["kind"] != "report"
                        or not all(isinstance(row["origin"][k], str) for k in KEYS - {"version"})
                        or not re.fullmatch(r"[0-9a-f]{32}", row["origin"]["id"])
                        or row["origin"]["id"] in seen
                        or not isinstance(row["work"], str) or not 0 < len(row["work"]) <= 256
                        or not isinstance(row["display"], str) or not GROUP.fullmatch(row["display"])
                        or row["origin"]["target_chat"] != row["display"]
                        or not isinstance(row["text"], str) or len(row["text"].encode()) > 128 * 1024
                        or type(row["visible"]) is not bool
                        or not isinstance(row["cards"], list) or len(row["cards"]) > 4
                        or not all(isinstance(c, str) and 0 < len(c) <= 256 for c in row["cards"])
                        or not isinstance(row["reason"], str)
                        or any(type(row[k]) not in (int, float) or not math.isfinite(row[k]) for k in ("arrived", "expires"))):
                    raise ValueError("invalid peer report record; retained, no replay")
                seen.add(row["origin"]["id"])
            recovered = copy.deepcopy(self.rows)
            for row in recovered:
                if row["state"] == "held":
                    row.update(state="recovery_required", reason="process_restart_no_card_authority")
                elif row["state"] == "claimed":
                    row.update(state="delivery_unknown", reason="process_restart_possible_write")
            if recovered != self.rows:
                self._store(recovered)

    def _store(self, rows):
        raw = json.dumps({"version": 1, "reports": rows}, ensure_ascii=False).encode()
        if len(rows) > self.MAX_RECORDS or len(raw) > self.MAX_BYTES:
            raise ValueError("peer report journal capacity; older originals retained")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".peer-reports-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
            # No post-replace failure can make a caller fall through and resend.
            self.rows = rows
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def lookup(self, ident):
        return next((r for r in self.rows if r["origin"]["id"] == ident), None)

    def hold(self, origin, *, work, display, text, arrived, visible, cards, expires):
        old = self.lookup(origin["id"])
        if old is not None:
            if any(old[k] != v for k, v in {"origin": origin, "work": work,
                                           "display": display, "text": text}.items()):
                raise ValueError("peer notification identity conflict")
            return old, False
        if not isinstance(text, str) or len(text.encode()) > 128 * 1024 or not math.isfinite(arrived):
            raise ValueError("invalid bounded peer report")
        row = dict(origin=origin, work=work, display=display, text=text, arrived=arrived,
                   visible=bool(visible), cards=list(cards), expires=expires, state="held", reason="")
        self._store([*self.rows, row])
        return row, True

    def transition(self, ident, state, reason=""):
        rows = copy.deepcopy(self.rows)
        row = next(r for r in rows if r["origin"]["id"] == ident)
        row.update(state=state, reason=reason)
        self._store(rows)

    def held(self, work=None):
        return [r for r in self.rows if r["state"] == "held" and (work is None or r["work"] == work)]
