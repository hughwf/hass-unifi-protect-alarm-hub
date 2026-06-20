# custom_components/unifi_protect_alarm_hub/logic.py
"""Pure derivation logic for the alarm hub.

NO Home Assistant imports — only ``uiprotect`` + stdlib — so this module is
unit-testable with plain pytest. device_class values are returned as plain
strings matching Home Assistant's BinarySensorDeviceClass values; the entity
layer wraps them.
"""

from __future__ import annotations

from typing import Any

from uiprotect.data.public_devices import (
    AlarmHubBattery,
    AlarmHubCover,
    AlarmHubInput,
    AlarmHubOutput,
    LinkStation,
)
from uiprotect.data.types import (
    AlarmHubConnectionState,
    AlarmHubCoverStatus,
    AlarmHubInputStatus,
    AlarmHubInputType,
    DeviceState,
    OnOffState,
)

ZONE_FAULT_STATUSES = {
    AlarmHubInputStatus.FAULT,
    AlarmHubInputStatus.SHORT,
    AlarmHubInputStatus.CUT,
}


def zone_is_on(zone: AlarmHubInput) -> bool:
    """True when the zone is triggered (status == alarm)."""
    return zone.status == AlarmHubInputStatus.ALARM


def zone_fault_is_on(zone: AlarmHubInput) -> bool:
    """True when the zone wiring is faulted (fault/short/cut)."""
    return zone.status in ZONE_FAULT_STATUSES


ZONE_DEVICE_CLASS: dict[AlarmHubInputType, str] = {
    AlarmHubInputType.MOTION: "motion",
    AlarmHubInputType.ENTRY: "door",
    AlarmHubInputType.SMOKE: "smoke",
    AlarmHubInputType.GLASS_BREAK: "sound",
    AlarmHubInputType.EMERGENCY_BUTTON: "safety",
}
DEFAULT_ZONE_DEVICE_CLASS = "safety"


def zone_device_class(zone: AlarmHubInput) -> str:
    """Map a zone's input_type to a Home Assistant binary_sensor device_class."""
    if zone.input_type is None:
        return DEFAULT_ZONE_DEVICE_CLASS
    return ZONE_DEVICE_CLASS.get(zone.input_type, DEFAULT_ZONE_DEVICE_CLASS)


def zone_enabled_default(zone: AlarmHubInput) -> bool:
    """Whether this zone's entities should be enabled by default.

    Conservatively returns False for OnOffState.UNKNOWN.
    """
    return zone.enable == OnOffState.ON


def zone_name(zone: AlarmHubInput, zone_id: int) -> str:
    """Return the zone's configured name, falling back to ``Zone <zone_id>``."""
    return zone.name or f"Zone {zone_id}"


def zone_unique_id(mac: str, zone_id: int) -> str:
    """Build the unique id for a zone's primary binary sensor entity."""
    return f"{mac}_zone_{zone_id}"


def zone_fault_unique_id(mac: str, zone_id: int) -> str:
    """Build the unique id for a zone's wiring-fault binary sensor entity."""
    return f"{mac}_zone_{zone_id}_fault"


def output_unique_id(mac: str, output_id: int) -> str:
    """Build the unique id for an output relay entity."""
    return f"{mac}_output_{output_id}"


def output_is_on(output: AlarmHubOutput) -> bool:
    """True when the output relay is energised (``active == on``)."""
    return output.active == OnOffState.ON


def output_name(output: AlarmHubOutput, output_id: int) -> str:
    """Return the output's configured name, falling back to ``Output <output_id>``."""
    return output.name or f"Output {output_id}"


def hub_is_connected(hub: LinkStation) -> bool:
    """True when the hub's connection state is CONNECTED."""
    return hub.state == DeviceState.CONNECTED


def armed_is_on(armed: OnOffState | None) -> bool:
    return armed == OnOffState.ON


def cover_is_on(cover: AlarmHubCover | None) -> bool:
    """True when the tamper cover is open."""
    return cover is not None and cover.status == AlarmHubCoverStatus.OPEN


def battery_connected_is_on(battery: AlarmHubBattery | None) -> bool:
    """True when the backup battery is connected."""
    return (
        battery is not None and battery.connection == AlarmHubConnectionState.CONNECTED
    )


def snapshot(public_bootstrap: Any) -> dict[str, LinkStation]:
    """Return a ``{hub_id: LinkStation}`` copy of the bootstrap's alarm-hub map.

    ``public_bootstrap.alarm_hubs`` is already filtered to alarm hubs by
    uiprotect, so this is a straight copy.
    """
    return dict(public_bootstrap.alarm_hubs)
