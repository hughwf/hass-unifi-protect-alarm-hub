# custom_components/unifi_protect_alarm_hub/logic.py
"""Pure derivation logic for the alarm hub.

NO Home Assistant imports — only ``uiprotect`` + stdlib — so this module is
unit-testable with plain pytest. device_class values are returned as plain
strings matching Home Assistant's BinarySensorDeviceClass values; the entity
layer wraps them.
"""

from __future__ import annotations

from uiprotect.data.public_devices import AlarmHubInput, AlarmHubOutput
from uiprotect.data.types import AlarmHubInputStatus, OnOffState

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
