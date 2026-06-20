"""Tier-1 tests for JSON parsing into lightweight models (pytest only)."""

from __future__ import annotations

from custom_components.unifi_protect_alarm_hub.models import AlarmHub

RAW = {
    "id": "ah1",
    "modelKey": "linkstation",
    "name": "Alarm Hub",
    "mac": "AABBCCDDEEFF",
    "state": "CONNECTED",
    "isAlarmHub": True,
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
                "lastTriggeredAt": 1700,
                "cameraId": "cam1",
            },
            "2": {"enable": "off", "type": "no", "status": "alarm"},
        },
        "output": {
            "1": {
                "active": "off",
                "enable": "on",
                "status": "dry",
                "name": "Siren",
                "delay": 0,
                "duration": 30,
            },
        },
    },
}


def test_parses_top_level_fields():
    hub = AlarmHub.from_json(RAW)
    assert hub.id == "ah1"
    assert hub.name == "Alarm Hub"
    assert hub.mac == "AABBCCDDEEFF"
    assert hub.state == "CONNECTED"
    assert hub.is_alarm_hub is True
    assert hub.alarm_hub_armed == "on"


def test_parses_inputs_keyed_by_int():
    hub = AlarmHub.from_json(RAW)
    zones = hub.alarm_hub_inputs
    assert set(zones) == {1, 2}
    z1 = zones[1]
    assert z1.status == "normal"
    assert z1.input_type == "ENTRY"
    assert z1.enable == "on"
    assert z1.type == "nc"
    assert z1.name == "Front Door"
    assert z1.last_triggered_at == 1700
    assert z1.camera_id == "cam1"
    # zone 2 has no inputType/name -> None
    assert zones[2].input_type is None
    assert zones[2].name is None
    assert zones[2].status == "alarm"


def test_parses_outputs_battery_cover():
    hub = AlarmHub.from_json(RAW)
    out = hub.alarm_hub_outputs[1]
    assert out.active == "off"
    assert out.status == "dry"
    assert out.name == "Siren"
    assert out.duration == 30
    assert hub.alarm_hub_battery.battery_status == "ok"
    assert hub.alarm_hub_battery.voltage == 13.2
    assert hub.alarm_hub_battery.connection == "connected"
    assert hub.alarm_hub_cover.status == "open"


def test_minimal_hub_without_subsections():
    hub = AlarmHub.from_json(
        {
            "id": "ah2",
            "name": None,
            "mac": "X",
            "state": "DISCONNECTED",
            "isAlarmHub": True,
            "alarmHub": {},
        }
    )
    assert hub.alarm_hub_armed is None
    assert hub.alarm_hub_battery is None
    assert hub.alarm_hub_cover is None
    assert hub.alarm_hub_inputs == {}
    assert hub.alarm_hub_outputs == {}


def test_missing_alarmhub_key():
    hub = AlarmHub.from_json(
        {"id": "x", "mac": "Y", "state": "CONNECTED", "isAlarmHub": True}
    )
    assert hub.alarm_hub_inputs == {}
    assert hub.alarm_hub_battery is None
