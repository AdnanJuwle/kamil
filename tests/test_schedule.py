"""Tests for core/schedule.py — time-of-day blocks and quiet hours."""
from datetime import datetime

from core import schedule


def at(h, m=0):
    return datetime(2026, 6, 21, h, m)  # 2026-06-21 is a Sunday


def test_current_block_known_times():
    assert schedule.current_block(at(2)) == "sleep hours"
    assert schedule.current_block(at(10)) == "deep work block 1"   # 600 min
    assert schedule.current_block(at(13)) == "deep work block 2"   # 780 min
    assert schedule.current_block(at(23, 30)) == "wind down, sleep by 11pm"


def test_is_quiet_hours():
    assert schedule.is_quiet_hours(at(2)) is True      # sleep
    assert schedule.is_quiet_hours(at(8, 30)) is True  # morning wind-down (510-540)
    assert schedule.is_quiet_hours(at(23, 30)) is True # evening wind-down
    assert schedule.is_quiet_hours(at(10)) is False    # deep work
    assert schedule.is_quiet_hours(at(19)) is False    # gaming/free


def test_minutes_into_block():
    # deep work block 1 starts at 540 (09:00); at 09:02 we're 2 min in
    assert schedule.minutes_into_block(at(9, 2)) == 2


def test_weekly_focus_sunday():
    assert "Sunday" in schedule.weekly_focus(at(10))


def test_schedule_context_shape():
    ctx = schedule.schedule_context(at(10))
    assert "Current block: deep work block 1" in ctx
    assert "Today's focus:" in ctx and "Current time:" in ctx
