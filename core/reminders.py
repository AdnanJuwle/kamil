"""
core/reminders.py — simple user-set reminders that the presence engine fires.

A reminder is something Adnan asked Kamil to bring up at a time ("remind me to
call the lab at 3pm"). Stored as JSON; the heartbeat delivers due ones.
"""

import json
import os
import uuid
from datetime import datetime
from typing import Optional

from core.config import DATA_DIR

REMINDERS_PATH = os.path.join(DATA_DIR, "reminders.json")


def _load() -> list[dict]:
    try:
        with open(REMINDERS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(items: list[dict]) -> None:
    os.makedirs(os.path.dirname(REMINDERS_PATH), exist_ok=True)
    with open(REMINDERS_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)


def add_reminder(text: str, due: datetime) -> dict:
    item = {
        "id": uuid.uuid4().hex[:12],
        "text": text.strip(),
        "due": due.isoformat(),
        "created": datetime.now().isoformat(),
        "fired": False,
    }
    items = _load()
    items.append(item)
    _save(items)
    return item


def list_reminders(include_fired: bool = False) -> list[dict]:
    items = _load()
    return items if include_fired else [r for r in items if not r.get("fired")]


def due_reminders(now: Optional[datetime] = None) -> list[dict]:
    """Unfired reminders whose due time has arrived."""
    now = now or datetime.now()
    out = []
    for r in _load():
        if r.get("fired"):
            continue
        try:
            if datetime.fromisoformat(r["due"]) <= now:
                out.append(r)
        except Exception:
            continue
    return out


def mark_fired(reminder_id: str) -> None:
    items = _load()
    for r in items:
        if r.get("id") == reminder_id:
            r["fired"] = True
    _save(items)
