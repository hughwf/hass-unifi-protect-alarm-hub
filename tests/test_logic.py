# tests/test_logic.py
"""Tier-1 pure-logic tests: only pytest + uiprotect required."""

from __future__ import annotations

from uiprotect.data.public_devices import AlarmHubInput, AlarmHubOutput, LinkStation
from custom_components.unifi_protect_alarm_hub import logic


def _zone(**kw) -> AlarmHubInput:
    base = {"enable": "on", "type": "nc", "status": "normal", "input_type": "ENTRY"}
    base.update(kw)
    return AlarmHubInput(**base)


def test_zone_is_on_only_when_alarm():
    assert logic.zone_is_on(_zone(status="alarm")) is True
    assert logic.zone_is_on(_zone(status="normal")) is False
    assert logic.zone_is_on(_zone(status="fault")) is False


def test_zone_fault_is_on_for_wire_faults():
    assert logic.zone_fault_is_on(_zone(status="fault")) is True
    assert logic.zone_fault_is_on(_zone(status="short")) is True
    assert logic.zone_fault_is_on(_zone(status="cut")) is True
    assert logic.zone_fault_is_on(_zone(status="normal")) is False
    assert logic.zone_fault_is_on(_zone(status="alarm")) is False
