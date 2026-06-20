"""Binary sensors for the UniFi Protect Alarm Hub."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import logic
from .coordinator import AlarmHubCoordinator
from .entity import AlarmHubBaseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry,  # AlarmHubConfigEntry; entry.runtime_data is the coordinator
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AlarmHubCoordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for hub_id, hub in coordinator.data.items():
        for zone_id in hub.alarm_hub_inputs:
            entities.append(ZoneBinarySensor(coordinator, hub_id, zone_id))
            entities.append(ZoneFaultBinarySensor(coordinator, hub_id, zone_id))
        if hub.alarm_hub_cover is not None:
            entities.append(TamperBinarySensor(coordinator, hub_id))
        entities.append(ArmedBinarySensor(coordinator, hub_id))
        entities.append(ConnectivityBinarySensor(coordinator, hub_id))
        if hub.alarm_hub_battery is not None:
            entities.append(BatteryConnectionBinarySensor(coordinator, hub_id))
    async_add_entities(entities)


class _ZoneBase(AlarmHubBaseEntity, BinarySensorEntity):
    def __init__(
        self, coordinator: AlarmHubCoordinator, hub_id: str, zone_id: int
    ) -> None:
        super().__init__(coordinator, hub_id)
        self._zone_id = zone_id
        zone = self._zone
        self._attr_entity_registry_enabled_default = (
            logic.zone_enabled_default(zone) if zone else True
        )

    @property
    def _zone(self):
        hub = self.hub
        return hub.alarm_hub_inputs.get(self._zone_id) if hub else None

    @property
    def available(self) -> bool:
        return super().available and self._zone is not None


class ZoneBinarySensor(_ZoneBase):
    def __init__(self, coordinator, hub_id, zone_id):
        super().__init__(coordinator, hub_id, zone_id)
        self._attr_unique_id = logic.zone_unique_id(self.hub.mac, zone_id)
        zone = self._zone
        self._attr_name = logic.zone_name(zone, zone_id) if zone else f"Zone {zone_id}"
        if zone:
            self._attr_device_class = BinarySensorDeviceClass(
                logic.zone_device_class(zone)
            )

    @property
    def is_on(self) -> bool | None:
        zone = self._zone
        return logic.zone_is_on(zone) if zone else None

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        zone = self._zone
        if zone is None:
            return {}
        return {
            "status": zone.status.value,
            "contact_type": zone.type.value,
            "input_type": zone.input_type.value if zone.input_type else None,
            "last_triggered_at": zone.last_triggered_at,
            "camera_id": zone.camera_id,
        }


class ZoneFaultBinarySensor(_ZoneBase):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, hub_id, zone_id):
        super().__init__(coordinator, hub_id, zone_id)
        self._attr_unique_id = logic.zone_fault_unique_id(self.hub.mac, zone_id)
        zone = self._zone
        base = logic.zone_name(zone, zone_id) if zone else f"Zone {zone_id}"
        self._attr_name = f"{base} Fault"

    @property
    def is_on(self) -> bool | None:
        zone = self._zone
        return logic.zone_fault_is_on(zone) if zone else None


class TamperBinarySensor(AlarmHubBaseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.TAMPER
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Tamper"

    def __init__(self, coordinator, hub_id):
        super().__init__(coordinator, hub_id)
        self._attr_unique_id = f"{self.hub.mac}_tamper"

    @property
    def is_on(self) -> bool | None:
        hub = self.hub
        return logic.cover_is_on(hub.alarm_hub_cover) if hub else None


class ArmedBinarySensor(AlarmHubBaseEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Armed"

    def __init__(self, coordinator, hub_id):
        super().__init__(coordinator, hub_id)
        self._attr_unique_id = f"{self.hub.mac}_armed"

    @property
    def is_on(self) -> bool | None:
        hub = self.hub
        return logic.armed_is_on(hub.alarm_hub_armed) if hub else None


class ConnectivityBinarySensor(AlarmHubBaseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Connectivity"

    def __init__(self, coordinator, hub_id):
        super().__init__(coordinator, hub_id)
        self._attr_unique_id = f"{self.hub.mac}_connectivity"

    @property
    def is_on(self) -> bool | None:
        hub = self.hub
        return logic.hub_is_connected(hub) if hub else None


class BatteryConnectionBinarySensor(AlarmHubBaseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Backup battery connection"

    def __init__(self, coordinator, hub_id):
        super().__init__(coordinator, hub_id)
        self._attr_unique_id = f"{self.hub.mac}_battery_connection"

    @property
    def is_on(self) -> bool | None:
        hub = self.hub
        return logic.battery_connected_is_on(hub.alarm_hub_battery) if hub else None
