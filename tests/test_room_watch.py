"""Tests for core/room_watch.py — the 24x7 webcam room monitor.

Fully offline: no camera, no model, no clock. The motion maths is exercised with
synthetic frames (which is why MotionDetector is its own object) and the event
store against a temp database.
"""
import importlib
from datetime import datetime, timedelta

import pytest

from core import room_watch as rw

cv2 = pytest.importorskip("cv2", reason="opencv is needed for the room watcher")
np = pytest.importorskip("numpy")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the event store and clip directory at a temp dir."""
    monkeypatch.setattr(rw, "DB_PATH", str(tmp_path / "room_events.db"))
    monkeypatch.setattr(rw, "ROOM_DIR", tmp_path / "clips")
    rw.ROOM_DIR.mkdir(parents=True, exist_ok=True)
    yield tmp_path


def blank(value=40, h=480, w=640):
    return np.full((h, w, 3), value, np.uint8)


def with_blob(value=40, box=((250, 150), (420, 430)), colour=(220, 220, 220)):
    f = blank(value)
    cv2.rectangle(f, box[0], box[1], colour, -1)
    return f


# ── motion detection ──────────────────────────────────────────────

def test_first_frame_only_establishes_a_baseline():
    """A watcher that reported motion on its first frame would record a clip every
    time the camera was reopened."""
    d = rw.MotionDetector(cv2, np)
    assert d.update(blank()) == (False, 0)


def test_still_room_is_not_motion():
    d = rw.MotionDetector(cv2, np)
    d.update(blank())
    for _ in range(6):
        motion, area = d.update(blank())
    assert motion is False
    assert area == 0


def test_sensor_noise_does_not_trip_it():
    """Scattered single-pixel noise must not count: each region has to clear
    min_area on its own, which is what keeps a grainy night-time feed quiet."""
    d = rw.MotionDetector(cv2, np)
    d.update(blank())
    rng = np.random.default_rng(1234)
    tripped = 0
    for _ in range(10):
        noisy = np.clip(blank().astype(np.int16) + rng.integers(-12, 12, (480, 640, 3)),
                        0, 255).astype(np.uint8)
        if d.update(noisy)[0]:
            tripped += 1
    assert tripped == 0


def test_person_sized_region_is_motion():
    d = rw.MotionDetector(cv2, np)
    d.update(blank())
    d.update(blank())
    motion, area = d.update(with_blob())
    assert motion is True
    assert area > rw.MIN_AREA


def test_motion_persists_while_the_subject_holds_still():
    """Someone who walks in and then sits motionless must not vanish immediately --
    otherwise the clip is cut while they are still in the room."""
    d = rw.MotionDetector(cv2, np)
    d.update(blank())
    frame = with_blob()
    assert d.update(frame)[0] is True
    held = [d.update(frame)[0] for _ in range(4)]
    assert all(held), "subject dissolved into the background too quickly"


def test_background_eventually_reclaims_an_empty_room():
    d = rw.MotionDetector(cv2, np)
    d.update(blank())
    d.update(with_blob())
    for _ in range(40):
        motion, _area = d.update(blank())
    assert motion is False


def test_region_below_min_area_is_ignored():
    d = rw.MotionDetector(cv2, np)
    d.update(blank())
    d.update(blank())
    motion, area = d.update(with_blob(box=((10, 10), (24, 24))))
    assert motion is False
    assert area == 0


def test_threshold_is_configurable():
    """A very high pixel threshold should make even a bright blob invisible."""
    d = rw.MotionDetector(cv2, np, pixel_threshold=250)
    d.update(blank())
    d.update(blank())
    assert d.update(with_blob())[0] is False


def test_detector_handles_grayscale_frames():
    d = rw.MotionDetector(cv2, np)
    gray = np.full((480, 640), 40, np.uint8)
    assert d.update(gray) == (False, 0)
    assert d.update(gray)[0] is False


# ── event store ───────────────────────────────────────────────────

def test_event_lifecycle(store):
    started = datetime.now()
    eid = rw._event_open(started, str(rw.ROOM_DIR / "a.mp4"))
    rw._event_close(eid, started + timedelta(seconds=4), 4.0, 9000, 24)
    rw._event_describe(eid, "Adnan sat down at the desk.", "Adnan")

    events = rw.recent_events(10)
    assert len(events) == 1
    e = events[0]
    assert e["duration_s"] == 4.0
    assert e["peak_area"] == 9000
    assert e["frames"] == 24
    assert e["description"] == "Adnan sat down at the desk."
    assert e["faces"] == "Adnan"
    assert e["clip"] == "a.mp4"


def test_events_are_newest_first(store):
    for i in range(5):
        rw._event_open(datetime.now(), str(rw.ROOM_DIR / f"{i}.mp4"))
    ids = [e["id"] for e in rw.recent_events(10)]
    assert ids == sorted(ids, reverse=True)


def test_since_filters_the_window(store):
    old = datetime.now() - timedelta(hours=30)
    rw._event_open(old, str(rw.ROOM_DIR / "old.mp4"))
    rw._event_open(datetime.now(), str(rw.ROOM_DIR / "new.mp4"))
    cutoff = (datetime.now() - timedelta(hours=12)).isoformat(timespec="seconds")
    recent = rw.recent_events(10, since=cutoff)
    assert [e["clip"] for e in recent] == ["new.mp4"]


def test_short_blip_is_discarded_with_its_clip(store):
    clip = rw.ROOM_DIR / "blip.mp4"
    clip.write_bytes(b"x")
    eid = rw._event_open(datetime.now(), str(clip))
    rw._discard_event(eid)
    assert rw.recent_events(10) == []
    assert not clip.exists()


def test_retention_purges_old_clips_and_rows(store, monkeypatch):
    monkeypatch.setattr(rw, "RETENTION_DAYS", 7)
    old_clip = rw.ROOM_DIR / "old.mp4"
    old_clip.write_bytes(b"x" * 100)
    new_clip = rw.ROOM_DIR / "new.mp4"
    new_clip.write_bytes(b"x" * 100)
    rw._event_open(datetime.now() - timedelta(days=30), str(old_clip))
    rw._event_open(datetime.now(), str(new_clip))

    removed = rw._purge_old()
    assert removed == 1
    assert not old_clip.exists()
    assert new_clip.exists()
    assert [e["clip"] for e in rw.recent_events(10)] == ["new.mp4"]


def test_retention_disabled_keeps_everything(store, monkeypatch):
    monkeypatch.setattr(rw, "RETENTION_DAYS", 0)
    clip = rw.ROOM_DIR / "ancient.mp4"
    clip.write_bytes(b"x")
    rw._event_open(datetime.now() - timedelta(days=9999), str(clip))
    assert rw._purge_old() == 0
    assert clip.exists()


def test_purge_survives_a_missing_clip_file(store, monkeypatch):
    monkeypatch.setattr(rw, "RETENTION_DAYS", 1)
    rw._event_open(datetime.now() - timedelta(days=10),
                   str(rw.ROOM_DIR / "never-written.mp4"))
    assert rw._purge_old() == 0          # nothing to delete, no exception
    assert rw.recent_events(10) == []    # row still goes


# ── prompt digest ─────────────────────────────────────────────────

def test_summary_is_empty_with_no_events(store):
    assert rw.summary(hours=12) == ""


def test_summary_reads_as_prose(store):
    eid = rw._event_open(datetime.now(), str(rw.ROOM_DIR / "a.mp4"))
    rw._event_describe(eid, "Someone walked past the doorway.", "")
    text = rw.summary(hours=12)
    assert "Room activity" in text
    assert "Someone walked past the doorway." in text


def test_summary_marks_undescribed_events(store):
    rw._event_open(datetime.now(), str(rw.ROOM_DIR / "a.mp4"))
    assert "not yet described" in rw.summary(hours=12)


# ── status / control ──────────────────────────────────────────────

def test_status_is_safe_before_start(store):
    s = rw.status()
    assert s["running"] is False
    assert s["recording"] is False
    assert s["has_frame"] is False
    assert "retention_days" in s


def test_latest_frame_is_none_before_any_capture():
    assert rw.latest_frame_jpeg() is None


def test_disabled_watcher_refuses_to_start(monkeypatch):
    monkeypatch.setattr(rw, "ENABLED", False)
    assert rw.start() is False
    assert "disabled" in rw.status()["error"]


def test_stop_is_safe_when_never_started():
    rw.stop()            # must not raise
    assert rw.is_running() is False


def test_enabled_defaults_to_linux(monkeypatch):
    """24x7 watching is the point on Fedora; elsewhere Kamil is a guest."""
    from core import paths
    monkeypatch.delenv("KAMIL_ROOM_WATCH", raising=False)
    m = importlib.reload(rw)
    try:
        assert m.ENABLED is paths.IS_LINUX
    finally:
        importlib.reload(rw)


def test_room_watch_env_forces_on(monkeypatch):
    monkeypatch.setenv("KAMIL_ROOM_WATCH", "1")
    m = importlib.reload(rw)
    try:
        assert m.ENABLED is True
    finally:
        monkeypatch.delenv("KAMIL_ROOM_WATCH", raising=False)
        importlib.reload(rw)
