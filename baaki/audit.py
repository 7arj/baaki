"""Append-only, hash-chained audit log. Every decision, gate verdict, API call and payment is recorded.

Each line is JSON with `prev` (hash of the previous line) and `hash` = sha256(prev + canonical payload),
so any edit or deletion is detectable with `baaki audit verify`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

GENESIS = "0" * 64


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class AuditLog:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.entries: list[dict[str, Any]] = []
        self._prev = GENESIS
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        payload = {
            "seq": len(self.entries),
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **fields,
        }
        body = _canonical(payload)
        h = hashlib.sha256((self._prev + body).encode()).hexdigest()
        entry = {**payload, "prev": self._prev, "hash": h}
        self._prev = h
        self.entries.append(entry)
        if self.path:
            with self.path.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        return entry

    def filter(self, event: str | None = None, **match: Any) -> list[dict[str, Any]]:
        out = []
        for e in self.entries:
            if event and e["event"] != event:
                continue
            if all(e.get(k) == v for k, v in match.items()):
                out.append(e)
        return out


def iter_entries(path: Path) -> Iterator[dict[str, Any]]:
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def verify(path: Path) -> tuple[bool, str]:
    prev = GENESIS
    n = 0
    for entry in iter_entries(path):
        n += 1
        claimed = entry.get("hash")
        payload = {k: v for k, v in entry.items() if k not in ("prev", "hash")}
        if entry.get("prev") != prev:
            return False, f"chain break at seq {payload.get('seq')}: prev mismatch"
        expected = hashlib.sha256((prev + _canonical(payload)).encode()).hexdigest()
        if expected != claimed:
            return False, f"tampered entry at seq {payload.get('seq')}: hash mismatch"
        prev = claimed
    return True, f"{n} entries verified; chain intact (head {prev[:12]}…)"
