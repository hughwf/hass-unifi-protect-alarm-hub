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


def _output(**kw) -> AlarmHubOutput:
    base = {"active": "off", "enable": "on", "status": "dry"}
    base.update(kw)
    return AlarmHubOutput(**base)


def test_output_is_on_from_active():
    assert logic.output_is_on(_output(active="on")) is True
    assert logic.output_is_on(_output(active="off")) is False


def test_output_name_fallback():
    assert logic.output_name(_output(name="Siren"), 1) == "Siren"
    assert logic.output_name(_output(name=None), 1) == "Output 1"


def _hub() -> LinkStation:
    raw = {
        "id": "ah1",
        "modelKey": "linkstation",
        "state": "CONNECTED",
        "name": "Alarm Hub",
        "mac": "AABBCCDDEEFF",
        "isAlarmHub": True,
        "ledSettings": {"isEnabled": True},
        "alarmHub": {
            "armed": "on",
            "battery": {
                "connection": "connected",
                "charging": "on",
                "voltage": 13.2,
                "batteryStatus": "ok",
            },
            "cover": {"status": "open", "distance": 5},
            "input": {
                "1": {
                    "enable": "on",
                    "type": "nc",
                    "status": "normal",
                    "inputType": "ENTRY",
                    "name": "Front Door",
                }
            },
            "output": {
                "1": {"active": "off", "enable": "on", "status": "dry", "name": "Siren"}
            },
        },
    }
    return LinkStation.from_unifi_dict(**raw)


def test_armed_is_on():
    assert logic.armed_is_on(_hub().alarm_hub_armed) is True


def test_cover_is_on_when_open():
    assert logic.cover_is_on(_hub().alarm_hub_cover) is True


def test_battery_connected_is_on():
    assert logic.battery_connected_is_on(_hub().alarm_hub_battery) is True


def test_snapshot_extracts_only_alarm_hubs():
    # a fake bootstrap exposing .alarm_hubs
    class FakeBootstrap:
        alarm_hubs = {"ah1": _hub()}

    snap = logic.snapshot(FakeBootstrap())
    assert set(snap) == {"ah1"}
    assert snap["ah1"].is_alarm_hub is True
