"""Lightweight dataclasses parsed from the UniFi Protect public-API JSON.

Pure: only stdlib. Attribute names deliberately mirror the shape the entity
layer consumes (``alarm_hub_inputs`` etc.) so platforms stay thin. All status /
type values are kept as their raw wire strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _int_keyed(raw: Any, parse) -> dict[int, Any]:
    """Map a ``{"1": {...}}`` wire dict to ``{1: parse(1, {...})}``; skip junk."""
    if not isinstance(raw, dict):
        return {}
    out: dict[int, Any] = {}
    for key, value in raw.items():
        if isinstance(key, str) and key.isdigit() and isinstance(value, dict):
            out[int(key)] = parse(int(key), value)
    return out


@dataclass(frozen=True)
class InputZone:
    zone_id: int
    enable: str | None
    type: str | None
    status: str | None
    input_type: str | None
    name: str | None
    last_triggered_at: int | None
    camera_id: str | None

    @classmethod
    def from_json(cls, zone_id: int, data: dict[str, Any]) -> InputZone:
        return cls(
            zone_id=zone_id,
            enable=data.get("enable"),
            type=data.get("type"),
            status=data.get("status"),
            input_type=data.get("inputType"),
            name=data.get("name"),
            last_triggered_at=data.get("lastTriggeredAt"),
            camera_id=data.get("cameraId"),
        )


@dataclass(frozen=True)
class OutputChannel:
    output_id: int
    active: str | None
    enable: str | None
    status: str | None
    name: str | None
    delay: int | None
    duration: int | None

    @classmethod
    def from_json(cls, output_id: int, data: dict[str, Any]) -> OutputChannel:
        return cls(
            output_id=output_id,
            active=data.get("active"),
            enable=data.get("enable"),
            status=data.get("status"),
            name=data.get("name"),
            delay=data.get("delay"),
            duration=data.get("duration"),
        )


@dataclass(frozen=True)
class Battery:
    connection: str | None
    charging: str | None
    voltage: float | None
    battery_status: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Battery:
        return cls(
            connection=data.get("connection"),
            charging=data.get("charging"),
            voltage=data.get("voltage"),
            battery_status=data.get("batteryStatus"),
        )


@dataclass(frozen=True)
class Cover:
    status: str | None
    distance: int | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Cover:
        return cls(status=data.get("status"), distance=data.get("distance"))


@dataclass(frozen=True)
class AlarmHub:
    id: str
    name: str | None
    mac: str
    state: str | None
    is_alarm_hub: bool
    alarm_hub_armed: str | None
    alarm_hub_battery: Battery | None
    alarm_hub_cover: Cover | None
    alarm_hub_inputs: dict[int, InputZone] = field(default_factory=dict)
    alarm_hub_outputs: dict[int, OutputChannel] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AlarmHub:
        hub = data.get("alarmHub") or {}
        battery = hub.get("battery")
        cover = hub.get("cover")
        return cls(
            id=data.get("id", ""),
            name=data.get("name"),
            mac=data.get("mac", ""),
            state=data.get("state"),
            is_alarm_hub=bool(data.get("isAlarmHub", False)),
            alarm_hub_armed=hub.get("armed"),
            alarm_hub_battery=Battery.from_json(battery)
            if isinstance(battery, dict)
            else None,
            alarm_hub_cover=Cover.from_json(cover) if isinstance(cover, dict) else None,
            alarm_hub_inputs=_int_keyed(hub.get("input"), InputZone.from_json),
            alarm_hub_outputs=_int_keyed(hub.get("output"), OutputChannel.from_json),
        )
