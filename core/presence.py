"""
core/presence.py — Kamil's heartbeat: deciding when to reach out unprompted.

This is what makes Kamil feel present rather than merely responsive. A background
tick (the heartbeat in the web backend) gathers the current state and asks
`evaluate()` one question: is there something worth saying right now, unprompted?

The decision core is PURE — it takes explicit values (time, the active thread,
due reminders, cooldown/dedupe state) and returns at most one ProactiveIntent.
That keeps it fully testable offline and keeps the "don't nag" guarantees honest:

  - Due reminders fire regardless of quiet hours / cooldown (you asked for them).
  - Everything else is suppressed during quiet hours (sleep, wind-downs) and within
    a cooldown window of the last proactive message.
  - Every trigger is de-duplicated by a key, so the same dormant thread or reminder
    never fires twice.

Delivery (phrasing it in Kamil's voice, pushing it to the UI) is the caller's job.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from core.config import DATA_DIR
from core import schedule

PRESENCE_STATE_PATH = os.path.join(DATA_DIR, "presence_state.json")

# Tunables (conservative by default — Adnan dislikes nagging).
COOLDOWN_MINUTES = 45          # min gap between proactive messages (reminders exempt)
DORMANT_THREAD_HOURS = 8       # an active thread idle this long invites a check-in
AWAY_HOURS = 4                 # gap that counts as "been away" for welcome-back
ENABLE_SCHEDULE_NUDGES = False # schedule-transition pings off by default


@dataclass
class ProactiveIntent:
    trigger: str        # unique de-dupe key, e.g. "reminder:ab12" / "dormant:thread_x:2026-06-21"
    kind: str           # "reminder" | "dormant_thread" | "schedule"
    reason: str         # one line of WHY, for the model to phrase naturally
    text: str           # ready-to-send fallback if not LLM-phrased
    payload: dict = field(default_factory=dict)


# ── the pure decision core ────────────────────────────────────────

def evaluate(
    now: datetime,
    *,
    active_thread: Optional[dict] = None,   # {id, type, focus, last_updated: datetime, message_count}
    due_reminders: Optional[list[dict]] = None,
    last_proactive: Optional[datetime] = None,
    delivered_triggers: Optional[set] = None,
    cooldown_minutes: int = COOLDOWN_MINUTES,
    dormant_hours: int = DORMANT_THREAD_HOURS,
    enable_schedule: bool = ENABLE_SCHEDULE_NUDGES,
) -> Optional[ProactiveIntent]:
    delivered = delivered_triggers or set()
    reminders = due_reminders or []

    # 1) Due reminders — highest priority, bypass quiet hours and cooldown.
    for r in reminders:
        key = f"reminder:{r.get('id')}"
        if key not in delivered:
            return ProactiveIntent(
                trigger=key, kind="reminder",
                reason=f"a reminder you set is due: {r.get('text','')}",
                text=f"Reminder: {r.get('text','')}",
                payload={"reminder_id": r.get("id")},
            )

    # 2) Everything below respects cooldown + quiet hours.
    if last_proactive and (now - last_proactive) < timedelta(minutes=cooldown_minutes):
        return None
    if schedule.is_quiet_hours(now):
        return None

    # 3) Dormant active thread — check in on what was being worked on.
    if active_thread:
        last_updated = active_thread.get("last_updated")
        mcount = active_thread.get("message_count", 0)
        focus = (active_thread.get("focus") or active_thread.get("type") or "").strip()
        if isinstance(last_updated, datetime) and mcount >= 2 and focus:
            idle = now - last_updated
            if idle >= timedelta(hours=dormant_hours):
                key = f"dormant:{active_thread.get('id','')}:{now.strftime('%Y-%m-%d')}"
                if key not in delivered:
                    return ProactiveIntent(
                        trigger=key, kind="dormant_thread",
                        reason=f"earlier you were working on '{focus}' and went quiet "
                               f"{int(idle.total_seconds() // 3600)}h ago",
                        text=f"Earlier you were on {focus} — still on that, or did it resolve?",
                        payload={"thread_id": active_thread.get("id", ""), "focus": focus},
                    )

    # 4) Schedule transition (off by default).
    if enable_schedule:
        block = schedule.current_block(now)
        if schedule.minutes_into_block(now) < 3 and "deep work" in block:
            key = f"schedule:{block}:{now.strftime('%Y-%m-%d-%H')}"
            if key not in delivered:
                return ProactiveIntent(
                    trigger=key, kind="schedule",
                    reason=f"a new block just started: {block}",
                    text=f"{block} starting — want to line up what you're going into?",
                    payload={"block": block},
                )

    return None


# ── temporal awareness (reactive: folded into normal replies) ─────

def time_since_phrase(earlier: datetime, now: datetime) -> str:
    secs = max(0, (now - earlier).total_seconds())
    if secs < 90:
        return "moments"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)} minutes"
    hrs = mins / 60
    if hrs < 24:
        return f"{int(hrs)} hours"
    days = hrs / 24
    return f"{int(days)} day{'s' if int(days) != 1 else ''}"


def temporal_note(last_interaction: Optional[datetime], now: datetime) -> str:
    """A line for the system prompt so even reactive replies feel continuous."""
    if not last_interaction:
        return ""
    gap = now - last_interaction
    if gap >= timedelta(hours=AWAY_HOURS):
        return (f"It has been {time_since_phrase(last_interaction, now)} since you last "
                "spoke with Adnan. Acknowledge the gap naturally if it fits — don't force it.")
    return ""


# ── state persistence (impure; kept out of evaluate) ──────────────

def _load_state() -> dict:
    try:
        with open(PRESENCE_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_interaction": None, "last_proactive": None, "delivered_triggers": []}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(PRESENCE_STATE_PATH), exist_ok=True)
    with open(PRESENCE_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _parse(dt: Optional[str]) -> Optional[datetime]:
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt)
    except Exception:
        return None


def get_last_interaction() -> Optional[datetime]:
    return _parse(_load_state().get("last_interaction"))


def record_interaction(now: Optional[datetime] = None) -> None:
    now = now or datetime.now()
    state = _load_state()
    state["last_interaction"] = now.isoformat()
    _save_state(state)


def get_last_proactive() -> Optional[datetime]:
    return _parse(_load_state().get("last_proactive"))


def get_delivered_triggers() -> set:
    return set(_load_state().get("delivered_triggers", []))


def record_proactive(intent: ProactiveIntent, now: Optional[datetime] = None) -> None:
    """Mark a proactive message as delivered: stamp cooldown + remember the trigger."""
    now = now or datetime.now()
    state = _load_state()
    state["last_proactive"] = now.isoformat()
    delivered = state.get("delivered_triggers", [])
    if intent.trigger not in delivered:
        delivered.append(intent.trigger)
    # keep the dedupe list bounded
    state["delivered_triggers"] = delivered[-200:]
    _save_state(state)
