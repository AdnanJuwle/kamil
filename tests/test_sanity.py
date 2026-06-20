"""Tests for memory/sanity.py — compression validation + drift monitoring."""
import pytest

from memory import sanity


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(sanity, "SANITY_LOG_PATH", str(tmp_path / "sanity_log.json"))
    monkeypatch.setattr(sanity, "DRIFT_STATE_PATH", str(tmp_path / "drift_state.json"))
    yield


# ── validate_compression ──────────────────────────────────────────

def _orig(*texts):
    return [{"role": "user", "content": t} for t in texts]


def test_empty_compression_invalid():
    r = sanity.validate_compression("", _orig("anything"))
    assert r.valid is False
    assert r.score == 0.0


def test_too_short_compression_penalized():
    r = sanity.validate_compression("too short", _orig("the rover navigation"))
    assert "min_length" in r.checks_failed


def test_refusal_marker_invalidates():
    text = "I cannot help with that. " * 5  # long enough, but a refusal
    r = sanity.validate_compression(text, _orig("the rover navigation stack ekf planner"))
    assert "no_refusal" in r.checks_failed
    assert r.valid is False


def test_good_compression_is_valid():
    original = _orig(
        "We debugged the EKF update_gps method in the navigation stack",
        "The planner uses A-star with a terrain weighted cost map",
    )
    compressed = (
        "Adnan and Kamil debugged the EKF update_gps method and reviewed the "
        "planner which uses A-star over a terrain-weighted cost map in navigation."
    )
    r = sanity.validate_compression(compressed, original)
    assert r.valid is True
    assert r.score >= sanity.QUALITY_SCORE_FLOOR
    assert "word_overlap" in r.checks_passed


def test_hallucination_markers_penalized():
    original = _orig("the navigation planner and the terrain cost map details here")
    compressed = (
        "As we discussed earlier, the navigation planner and terrain cost map "
        "were the focus of this longer conversation about the rover."
    )
    r = sanity.validate_compression(compressed, original)
    assert "no_hallucination_markers" in r.checks_failed


# ── drift monitoring ──────────────────────────────────────────────

def test_learning_enabled_by_default():
    assert sanity.is_learning_enabled() is True


def test_repeated_corrections_disable_learning():
    # Need at least 5 entries for a recalc; all corrections -> correction_rate 1.0
    for _ in range(6):
        sanity.record_correction()
    assert sanity.is_learning_enabled() is False
    report = sanity.get_drift_report()
    assert report["flagged"] is True
    assert report["drift_score"] >= sanity.DRIFT_CORRECTION_THRESHOLD


def test_reset_restores_learning():
    for _ in range(6):
        sanity.record_correction()
    assert sanity.is_learning_enabled() is False
    sanity.reset_drift_state()
    assert sanity.is_learning_enabled() is True
    assert sanity.get_drift_report()["drift_score"] == 0.0


def test_clean_interactions_keep_learning_on():
    for i in range(10):
        sanity.record_interaction(
            user_message="question",
            response="a clear helpful answer of reasonable length here",
            was_corrected=False,
        )
    assert sanity.is_learning_enabled() is True


def test_drift_report_shape():
    sanity.record_interaction("q", "an answer", was_corrected=False)
    report = sanity.get_drift_report()
    for key in ("learning_enabled", "drift_score", "flagged", "total_interactions",
                "correction_rate", "last_check", "window_size"):
        assert key in report
