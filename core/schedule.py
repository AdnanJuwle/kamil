"""
core/schedule.py — Adnan's daily/weekly schedule, as data.

Single source of truth for time-of-day awareness. Both the orchestrator (for the
system-prompt schedule block) and the presence engine (for deciding when it's OK
to reach out) read from here. Times are local wall-clock minutes since midnight.
"""

from datetime import datetime
from typing import Optional

# (start_min, end_min, label, quiet) — quiet = genuine rest, do not proactively interrupt.
BLOCKS = [
    (0,    360,  "sleep hours", True),
    (360,  390,  "wake up and get ready", False),
    (390,  480,  "gym", False),
    (480,  510,  "home, shower, breakfast", False),
    (510,  540,  "morning wind-down — rest, not productive", True),
    (540,  690,  "deep work block 1", False),
    (690,  720,  "break — walk, snack, away from screen", False),
    (720,  840,  "deep work block 2", False),
    (840,  900,  "lunch and rest", False),
    (900,  960,  "applications block — Upwork, LinkedIn, Wellfound", False),
    (960,  1005, "exam prep", False),
    (1005, 1035, "leetcode — one easy problem, 30 min max", False),
    (1035, 1080, "free time", False),
    (1080, 1200, "gaming or entertainment — guilt free", False),
    (1200, 1260, "dinner", False),
    (1260, 1305, "optional light evening block", False),
    (1305, 1440, "wind down, sleep by 11pm", True),
]

WEEKLY = {
    0: "Monday — deploy something + MLOps course",
    1: "Tuesday — MLOps course + PRANA work",
    2: "Wednesday — build day + Kamil work",
    3: "Thursday — MLOps course + deploy or build",
    4: "Friday — PRANA + Twitter build series doc",
    5: "Saturday — full project day, extended gaming evening",
    6: "Sunday — weekly review + rest, no work after 12pm",
}


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now()


def _minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


def _block_tuple(now: Optional[datetime] = None):
    total = _minutes(_now(now))
    for start, end, label, quiet in BLOCKS:
        if start <= total < end:
            return (start, end, label, quiet)
    return (0, 1440, "unscheduled", False)


def current_block(now: Optional[datetime] = None) -> str:
    return _block_tuple(now)[2]


def is_quiet_hours(now: Optional[datetime] = None) -> bool:
    """True during genuine-rest blocks (sleep, wind-downs) — no proactive pings."""
    return _block_tuple(now)[3]


def minutes_into_block(now: Optional[datetime] = None) -> int:
    now = _now(now)
    start, _end, _label, _q = _block_tuple(now)
    return _minutes(now) - start


def weekly_focus(now: Optional[datetime] = None) -> str:
    return WEEKLY.get(_now(now).weekday(), "")


def schedule_context(now: Optional[datetime] = None) -> str:
    """The schedule block injected into the orchestrator's system prompt."""
    now = _now(now)
    return (
        f"Current time: {now.strftime('%A')}, {now.strftime('%B %d, %Y')}, "
        f"{now.strftime('%I:%M %p')} IST\n"
        f"Current block: {current_block(now)}\n"
        f"Today's focus: {weekly_focus(now)}"
    )
