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


def test_zone_device_class_mapping():
    assert logic.zone_device_class(_zone(input_type="MOTION")) == "motion"
    assert logic.zone_device_class(_zone(input_type="ENTRY")) == "door"
    assert logic.zone_device_class(_zone(input_type="SMOKE")) == "smoke"
    assert logic.zone_device_class(_zone(input_type="GLASS_BREAK")) == "sound"
    assert logic.zone_device_class(_zone(input_type="EMERGENCY_BUTTON")) == "safety"


def test_zone_device_class_defaults_to_safety():
    # input_type omitted -> None on the model
    z = AlarmHubInput(enable="on", type="nc", status="normal")
    assert logic.zone_device_class(z) == "safety"


def test_zone_enabled_default_follows_enable_flag():
    assert logic.zone_enabled_default(_zone(enable="on")) is True
    assert logic.zone_enabled_default(_zone(enable="off")) is False


def test_zone_name_falls_back_to_zone_id():
    assert logic.zone_name(_zone(name="Front Door"), 3) == "Front Door"
    assert logic.zone_name(_zone(name=None), 3) == "Zone 3"


def test_unique_ids():
    assert logic.zone_unique_id("AABBCC", 2) == "AABBCC_zone_2"
    assert logic.zone_fault_unique_id("AABBCC", 2) == "AABBCC_zone_2_fault"
    assert logic.output_unique_id("AABBCC", 5) == "AABBCC_output_5"
