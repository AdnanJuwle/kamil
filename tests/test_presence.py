"""Tests for core/presence.py — the pure proactive-decision engine."""
from datetime import datetime, timedelta

from core import presence


def at(h, m=0):
    return datetime(2026, 6, 21, h, m)


def dormant_thread(now, idle_hours=9, mcount=4, focus="EKF debugging"):
    return {
        "id": "thread_x",
        "type": "code",
        "focus": focus,
        "last_updated": now - timedelta(hours=idle_hours),
        "message_count": mcount,
    }


# ── nothing-to-say ────────────────────────────────────────────────

def test_silent_when_nothing_pending():
    assert presence.evaluate(at(10)) is None


# ── reminders ─────────────────────────────────────────────────────

def test_due_reminder_fires():
    intent = presence.evaluate(at(10), due_reminders=[{"id": "r1", "text": "call the lab"}])
    assert intent is not None and intent.kind == "reminder"
    assert "call the lab" in intent.text


def test_reminder_fires_even_in_quiet_hours_and_cooldown():
    intent = presence.evaluate(
        at(2),  # sleep = quiet
        due_reminders=[{"id": "r1", "text": "take meds"}],
        last_proactive=at(2) - timedelta(minutes=5),  # within cooldown
    )
    assert intent is not None and intent.kind == "reminder"


def test_reminder_deduped():
    assert presence.evaluate(
        at(10),
        due_reminders=[{"id": "r1", "text": "x"}],
        delivered_triggers={"reminder:r1"},
    ) is None


# ── dormant thread ────────────────────────────────────────────────

def test_dormant_thread_fires():
    now = at(10)
    intent = presence.evaluate(now, active_thread=dormant_thread(now))
    assert intent is not None and intent.kind == "dormant_thread"
    assert "EKF debugging" in intent.text


def test_recent_thread_does_not_fire():
    now = at(10)
    assert presence.evaluate(now, active_thread=dormant_thread(now, idle_hours=2)) is None


def test_dormant_thread_deduped():
    now = at(10)
    key = f"dormant:thread_x:{now.strftime('%Y-%m-%d')}"
    assert presence.evaluate(now, active_thread=dormant_thread(now),
                             delivered_triggers={key}) is None


def test_dormant_suppressed_in_quiet_hours():
    now = at(2)  # sleep
    assert presence.evaluate(now, active_thread=dormant_thread(now)) is None


def test_dormant_suppressed_within_cooldown():
    now = at(10)
    assert presence.evaluate(
        now, active_thread=dormant_thread(now),
        last_proactive=now - timedelta(minutes=10),
    ) is None


# ── schedule nudges (off by default) ──────────────────────────────

def test_schedule_nudge_off_by_default():
    now = at(9, 1)  # 1 min into deep work block 1
    assert presence.evaluate(now) is None


def test_schedule_nudge_when_enabled():
    now = at(9, 1)
    intent = presence.evaluate(now, enable_schedule=True)
    assert intent is not None and intent.kind == "schedule"


# ── temporal awareness ────────────────────────────────────────────

def test_time_since_phrase():
    now = at(12)
    assert presence.time_since_phrase(now - timedelta(seconds=30), now) == "moments"
    assert presence.time_since_phrase(now - timedelta(minutes=5), now) == "5 minutes"
    assert presence.time_since_phrase(now - timedelta(hours=3), now) == "3 hours"
    assert presence.time_since_phrase(now - timedelta(days=2), now) == "2 days"


def test_temporal_note_only_when_away():
    now = at(12)
    assert presence.temporal_note(now - timedelta(hours=5), now) != ""
    assert presence.temporal_note(now - timedelta(minutes=30), now) == ""
    assert presence.temporal_note(None, now) == ""


# ── state persistence (cooldown + dedupe round-trip) ──────────────

def test_state_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(presence, "PRESENCE_STATE_PATH", str(tmp_path / "presence_state.json"))
    assert presence.get_last_interaction() is None
    assert presence.get_delivered_triggers() == set()

    presence.record_interaction(at(10))
    assert presence.get_last_interaction() == at(10)

    # Record the dormant trigger for thread_x, then confirm it de-dupes on reload.
    now = at(10, 5)
    key = f"dormant:thread_x:{now.strftime('%Y-%m-%d')}"
    presence.record_proactive(
        presence.ProactiveIntent(trigger=key, kind="dormant_thread", reason="r", text="t"), now)
    assert presence.get_last_proactive() == now
    assert key in presence.get_delivered_triggers()

    # Same dormant thread, fresh evaluate using persisted dedupe state -> silent.
    later = at(10, 6)
    assert presence.evaluate(
        later, active_thread=dormant_thread(later),
        last_proactive=None,
        delivered_triggers=presence.get_delivered_triggers(),
    ) is None
