"""Pure derivation logic. No Home Assistant, no uiprotect — stdlib + local
models only — so it is unit-testable with plain pytest. device_class values are
plain strings matching HA's BinarySensorDeviceClass values; the entity layer
wraps them.
"""

from __future__ import annotations

from .models import AlarmHub, Battery, Cover, InputZone, OutputChannel

ZONE_FAULT_STATUSES = {"fault", "short", "cut"}

ZONE_DEVICE_CLASS: dict[str, str] = {
    "MOTION": "motion",
    "ENTRY": "door",
    "SMOKE": "smoke",
    "GLASS_BREAK": "sound",
    "EMERGENCY_BUTTON": "safety",
}
DEFAULT_ZONE_DEVICE_CLASS = "safety"


def zone_is_on(zone: InputZone) -> bool:
    """True when the zone is triggered (status == 'alarm')."""
    return zone.status == "alarm"


def zone_fault_is_on(zone: InputZone) -> bool:
    """True when the zone wiring is faulted (fault/short/cut)."""
    return zone.status in ZONE_FAULT_STATUSES


def zone_device_class(zone: InputZone) -> str:
    """Map a zone's input_type to an HA binary_sensor device_class string."""
    if zone.input_type is None:
        return DEFAULT_ZONE_DEVICE_CLASS
    return ZONE_DEVICE_CLASS.get(zone.input_type, DEFAULT_ZONE_DEVICE_CLASS)


def zone_enabled_default(zone: InputZone) -> bool:
    """Whether this zone's entities are enabled by default (enable == 'on')."""
    return zone.enable == "on"


def zone_name(zone: InputZone, zone_id: int) -> str:
    """Zone name, or 'Zone {id}' when unnamed."""
    return zone.name or f"Zone {zone_id}"


def zone_unique_id(mac: str, zone_id: int) -> str:
    return f"{mac}_zone_{zone_id}"


def zone_fault_unique_id(mac: str, zone_id: int) -> str:
    return f"{mac}_zone_{zone_id}_fault"


def output_unique_id(mac: str, output_id: int) -> str:
    return f"{mac}_output_{output_id}"


def output_is_on(output: OutputChannel) -> bool:
    """True when the output relay is energised (active == 'on')."""
    return output.active == "on"


def output_name(output: OutputChannel, output_id: int) -> str:
    """Output name, or 'Output {id}' when unnamed."""
    return output.name or f"Output {output_id}"


def hub_is_connected(hub: AlarmHub) -> bool:
    """True when the hub's connection state is CONNECTED."""
    return hub.state == "CONNECTED"


def armed_is_on(armed: str | None) -> bool:
    return armed == "on"


def cover_is_on(cover: Cover | None) -> bool:
    """True when the tamper cover is open."""
    return cover is not None and cover.status == "open"


def battery_connected_is_on(battery: Battery | None) -> bool:
    """True when the backup battery is connected."""
    return battery is not None and battery.connection == "connected"
