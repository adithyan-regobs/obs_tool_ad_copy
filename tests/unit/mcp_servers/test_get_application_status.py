"""get_application_status — the state machine that decides what the user is told.

The interesting logic is _describe: what a health value means depends on
whether ArgoCD has acted on this deploy yet, and on whether the wait window
has run out. Pure function, so it is tested directly.
"""
import pytest

from app.mcp_servers.devlift_mcp.tools.get_application_status import (
    _describe,
    _latest,
    _parse_ts,
)


def _state(health, *, sync="Synced", stale=False, expired=False):
    state, message, settled = _describe(
        identifier="svc", health=health, sync=sync, stale=stale, expired=expired
    )
    return state, settled, message


def test_healthy_is_up_and_stops_the_loop():
    state, settled, message = _state("Healthy")
    assert (state, settled) == ("up", True)
    assert "up and healthy" in message


def test_stale_reading_never_reports_up():
    # Argo has not acted since the deploy, so its Healthy describes the
    # PREVIOUS revision — reporting it would be a false success.
    state, settled, _ = _state("Healthy", stale=True)
    assert (state, settled) == ("waiting_for_argocd", False)


def test_stale_past_the_window_becomes_a_warning():
    state, settled, message = _state("Healthy", stale=True, expired=True)
    assert (state, settled) == ("not_picked_up", True)
    assert "ArgoCD" in message


def test_degraded_keeps_watching_inside_the_window():
    state, settled, message = _state("Degraded")
    assert (state, settled) == ("unhealthy", False)
    assert "still watching" in message


def test_degraded_past_the_window_is_a_failure():
    state, settled, _ = _state("Degraded", expired=True)
    assert (state, settled) == ("unhealthy", True)


def test_progressing_keeps_polling_then_hands_over():
    assert _state("Progressing")[:2] == ("starting", False)
    assert _state("Progressing", expired=True)[:2] == ("starting", True)


def test_unknown_health_is_reported_verbatim():
    state, settled, message = _state(None, sync="OutOfSync", expired=True)
    assert (state, settled) == ("starting", True)
    state, settled, message = _state("Weird", sync="OutOfSync")
    assert state == "unknown"
    assert "Weird" in message


@pytest.mark.parametrize("value", [None, "", "not-a-date"])
def test_bad_timestamps_are_ignored(value):
    assert _parse_ts(value) is None


def test_latest_picks_the_newest_stamp():
    newest = _latest("2026-09-15T10:00:00Z", None, "2026-09-16T09:00:00Z", "bad")
    assert newest.isoformat().startswith("2026-09-16T09:00:00")


def test_latest_of_nothing_is_none():
    assert _latest(None, "", "nope") is None
