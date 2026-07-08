"""Tier-1 tests for coordinator pure helpers."""

from __future__ import annotations

from custom_components.unifi_protect_alarm_hub.coordinator import (
    BACKOFF_CAP,
    next_backoff,
)


def test_next_backoff_grows_by_doubling():
    assert next_backoff(1.0) == 2.0
    assert next_backoff(2.0) == 4.0
    assert next_backoff(4.0) == 8.0
    assert next_backoff(8.0) == 16.0


def test_next_backoff_caps_at_max():
    assert next_backoff(32.0) == BACKOFF_CAP
    assert next_backoff(60.0) == BACKOFF_CAP
    assert next_backoff(1000.0) == BACKOFF_CAP
    assert BACKOFF_CAP == 60.0


def test_next_backoff_zero_or_negative_starts_at_one():
    # A reset (prev <= 0) should restart the ramp at the floor, not stay at 0.
    assert next_backoff(0.0) == 1.0
    assert next_backoff(-5.0) == 1.0
