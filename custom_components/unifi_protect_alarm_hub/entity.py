"""Shared entity base for the UniFi Protect Alarm Hub."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from uiprotect.data.public_devices import LinkStation

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import AlarmHubCoordinator


class AlarmHubBaseEntity(CoordinatorEntity[AlarmHubCoordinator]):
    """Base entity bound to one alarm hub device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AlarmHubCoordinator, hub_id: str) -> None:
        super().__init__(coordinator)
        self._hub_id = hub_id
        hub = self.hub
        mac = hub.mac if hub else hub_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=hub.name if hub else "Alarm Hub",
        )

    @property
    def hub(self) -> LinkStation | None:
        """Return the live hub object from the latest coordinator snapshot."""
        return self.coordinator.data.get(self._hub_id)

    @property
    def available(self) -> bool:
        return super().available and self.hub is not None
