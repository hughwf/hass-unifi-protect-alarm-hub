"""Diagnostic sensors for the UniFi Protect Alarm Hub backup battery."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfElectricPotential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AlarmHubCoordinator
from .entity import AlarmHubBaseEntity

BATTERY_STATUS_OPTIONS = ["ok", "low", "critical", "unknown"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AlarmHubCoordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for hub_id, hub in coordinator.data.items():
        if hub.alarm_hub_battery is not None:
            entities.append(BatteryStatusSensor(coordinator, hub_id))
            entities.append(BatteryVoltageSensor(coordinator, hub_id))
    async_add_entities(entities)


class BatteryStatusSensor(AlarmHubBaseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = BATTERY_STATUS_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Backup battery status"

    def __init__(self, coordinator, hub_id):
        super().__init__(coordinator, hub_id)
        self._attr_unique_id = f"{self.hub.mac}_battery_status"

    @property
    def native_value(self) -> str | None:
        hub = self.hub
        if hub is None or hub.alarm_hub_battery is None:
            return None
        status = hub.alarm_hub_battery.battery_status
        return status.value if status else None


class BatteryVoltageSensor(AlarmHubBaseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Backup battery voltage"

    def __init__(self, coordinator, hub_id):
        super().__init__(coordinator, hub_id)
        self._attr_unique_id = f"{self.hub.mac}_battery_voltage"

    @property
    def native_value(self) -> float | None:
        hub = self.hub
        if hub is None or hub.alarm_hub_battery is None:
            return None
        return hub.alarm_hub_battery.voltage
