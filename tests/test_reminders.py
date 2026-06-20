"""Tests for core/reminders.py — the user-set reminder store."""
from datetime import datetime, timedelta

import pytest

from core import reminders


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(reminders, "REMINDERS_PATH", str(tmp_path / "reminders.json"))
    yield


def test_add_and_list():
    reminders.add_reminder("call the lab", datetime(2026, 6, 21, 15, 0))
    items = reminders.list_reminders()
    assert len(items) == 1 and items[0]["text"] == "call the lab"
    assert items[0]["fired"] is False


def test_due_only_returns_past_unfired():
    now = datetime(2026, 6, 21, 12, 0)
    reminders.add_reminder("past", now - timedelta(minutes=5))
    reminders.add_reminder("future", now + timedelta(hours=1))
    due = reminders.due_reminders(now)
    assert [r["text"] for r in due] == ["past"]


def test_mark_fired_excludes():
    now = datetime(2026, 6, 21, 12, 0)
    r = reminders.add_reminder("past", now - timedelta(minutes=5))
    reminders.mark_fired(r["id"])
    assert reminders.due_reminders(now) == []
    assert reminders.list_reminders() == []
    assert len(reminders.list_reminders(include_fired=True)) == 1
