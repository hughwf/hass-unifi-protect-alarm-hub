"""Tier-1 pure-logic tests (pytest only)."""

from __future__ import annotations

from custom_components.unifi_protect_alarm_hub import logic
from custom_components.unifi_protect_alarm_hub.models import (
    AlarmHub,
    Battery,
    Cover,
    InputZone,
    OutputChannel,
)


def _zone(**kw) -> InputZone:
    base = dict(
        zone_id=1,
        enable="on",
        type="nc",
        status="normal",
        input_type="ENTRY",
        name=None,
        last_triggered_at=None,
        camera_id=None,
    )
    base.update(kw)
    return InputZone(**base)


def _output(**kw) -> OutputChannel:
    base = dict(
        output_id=1,
        active="off",
        enable="on",
        status="dry",
        name=None,
        delay=None,
        duration=None,
    )
    base.update(kw)
    return OutputChannel(**base)


def _hub(state="CONNECTED") -> AlarmHub:
    return AlarmHub(
        id="ah1",
        name="Hub",
        mac="AABBCC",
        state=state,
        is_alarm_hub=True,
        alarm_hub_armed="on",
        alarm_hub_battery=Battery("connected", "on", 13.2, "ok"),
        alarm_hub_cover=Cover("open", 5),
        alarm_hub_inputs={1: _zone()},
        alarm_hub_outputs={1: _output()},
    )


def test_zone_is_on_only_when_alarm():
    assert logic.zone_is_on(_zone(status="alarm")) is True
    assert logic.zone_is_on(_zone(status="normal")) is False
    assert logic.zone_is_on(_zone(status="fault")) is False


def test_zone_fault_is_on():
    for s in ("fault", "short", "cut"):
        assert logic.zone_fault_is_on(_zone(status=s)) is True
    for s in ("normal", "alarm"):
        assert logic.zone_fault_is_on(_zone(status=s)) is False


def test_zone_device_class_mapping():
    assert logic.zone_device_class(_zone(input_type="MOTION")) == "motion"
    assert logic.zone_device_class(_zone(input_type="ENTRY")) == "door"
    assert logic.zone_device_class(_zone(input_type="SMOKE")) == "smoke"
    assert logic.zone_device_class(_zone(input_type="GLASS_BREAK")) == "sound"
    assert logic.zone_device_class(_zone(input_type="EMERGENCY_BUTTON")) == "safety"
    assert logic.zone_device_class(_zone(input_type=None)) == "safety"
    assert logic.zone_device_class(_zone(input_type="unknown")) == "safety"


def test_zone_enabled_default():
    assert logic.zone_enabled_default(_zone(enable="on")) is True
    assert logic.zone_enabled_default(_zone(enable="off")) is False


def test_zone_name_fallback():
    assert logic.zone_name(_zone(name="Front Door"), 3) == "Front Door"
    assert logic.zone_name(_zone(name=None), 3) == "Zone 3"


def test_unique_ids():
    assert logic.zone_unique_id("M", 2) == "M_zone_2"
    assert logic.zone_fault_unique_id("M", 2) == "M_zone_2_fault"
    assert logic.output_unique_id("M", 5) == "M_output_5"


def test_output_is_on_and_name():
    assert logic.output_is_on(_output(active="on")) is True
    assert logic.output_is_on(_output(active="off")) is False
    assert logic.output_name(_output(name="Siren"), 1) == "Siren"
    assert logic.output_name(_output(name=None), 1) == "Output 1"


def test_hub_predicates():
    assert logic.hub_is_connected(_hub("CONNECTED")) is True
    assert logic.hub_is_connected(_hub("DISCONNECTED")) is False
    assert logic.armed_is_on("on") is True
    assert logic.armed_is_on("off") is False
    assert logic.armed_is_on(None) is False
    assert logic.cover_is_on(Cover("open", 0)) is True
    assert logic.cover_is_on(Cover("close", 0)) is False
    assert logic.cover_is_on(None) is False
    assert logic.battery_connected_is_on(Battery("connected", None, None, "ok")) is True
    assert (
        logic.battery_connected_is_on(Battery("disconnected", None, None, None))
        is False
    )
    assert logic.battery_connected_is_on(None) is False
