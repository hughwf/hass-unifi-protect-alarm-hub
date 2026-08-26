"""Diagnostic sensors for the UniFi Protect Alarm Hub backup battery."""

from __future__ import annotations

from math import isfinite

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfElectricPotential
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AlarmHubConfigEntry, logic
from .coordinator import AlarmHubCoordinator
from .entity import (
    AlarmHubBaseEntity,
    async_hub_device_ids,
    async_reconcile_on_update,
)
from .logic import HubDeviceIds
from .models import AlarmHub

# The three states a backup battery can actually be in, per the design spec.
# "Unreadable" is deliberately not among them -- see ``battery_status_option``.
BATTERY_STATUS_OPTIONS = ["ok", "low", "critical"]


def battery_status_option(raw: object) -> str | None:
    """Map the console's battery status onto the enum's closed set, or to no reading.

    ``SensorEntity.state`` raises when a value is not in ``options``, and it
    raises inside the coordinator's listener -- so a single status this
    integration does not model does not just skip one update, it kills every
    update after it and the entity's state stops advancing for good. The design
    spec writes the value set as ok/low/critical, in a case the lowercase
    options do not contain, and the console has an ``unknown`` of its own
    besides, so casing alone was enough to do it. Fold case, and report anything
    else as no reading rather than raising.

    No reading is the *only* way this sensor says it cannot tell, which is why
    ``unknown`` is not one of the options. Home Assistant already renders a
    ``None`` state as ``unknown``, so listing it would have made "the hub says
    it cannot determine the battery" and "we have no value at all" the same
    string with no way for a template to tell them apart -- a distinction that
    cannot survive to the user is not one worth drawing. One string, one
    meaning: ``unknown`` here is always "no usable reading", whatever the cause.
    """
    if raw is None:
        return None
    option = str(raw).lower()
    return option if option in BATTERY_STATUS_OPTIONS else None


def battery_voltage(raw: object) -> float | None:
    """The backup battery voltage as a finite number, or None if it is not one.

    A voltage sensor is numeric, so ``SensorEntity.state`` raises on a value it
    cannot read as one -- including the NaN a console can send for "no reading".
    That raise lands the first time the entity is asked for a state, which is
    during setup, so the sensor never gets one at all: not a gap in the graph,
    a permanently blank entity. Report "no reading" instead. Numeric strings
    are accepted because the wire format is JSON and "12.6" is the same volts.

    ``bool`` is excluded before the numeric check because it is a subclass of
    ``int``: a JSON ``true`` would otherwise publish a confident 1.0 V, and this
    sensor carries a MEASUREMENT state_class, so that number goes into long-term
    statistics where nothing distinguishes it from a real reading of a battery
    about to fail.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        voltage = float(raw)
    except ValueError:
        return None
    return voltage if isfinite(voltage) else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlarmHubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the battery sensors, including for a battery that appears later.

    ``alarm_hub_battery`` is optional on the model and only arrives once the
    console reports it, so gating on it at setup meant a backup battery fitted
    (or first reported) afterwards produced no sensors until a reload. See
    ``async_reconcile_on_update``.
    """
    coordinator: AlarmHubCoordinator = entry.runtime_data
    devices = async_hub_device_ids(hass, entry)

    @callback
    def _build(hub_id: str, hub: AlarmHub) -> list[SensorEntity]:
        if hub.alarm_hub_battery is None:
            return []
        return [
            BatteryStatusSensor(coordinator, devices, hub_id),
            BatteryVoltageSensor(coordinator, devices, hub_id),
        ]

    async_reconcile_on_update(entry, coordinator, devices, async_add_entities, _build)


class BatteryStatusSensor(AlarmHubBaseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = BATTERY_STATUS_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Backup battery status"

    def __init__(
        self, coordinator: AlarmHubCoordinator, devices: HubDeviceIds, hub_id: str
    ) -> None:
        super().__init__(coordinator, devices, hub_id)
        self._attr_unique_id = logic.entity_unique_id(self._device_id, "battery_status")

    @property
    def native_value(self) -> str | None:
        hub = self.hub
        if hub is None or hub.alarm_hub_battery is None:
            return None
        return battery_status_option(hub.alarm_hub_battery.battery_status)


class BatteryVoltageSensor(AlarmHubBaseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.VOLTAGE
    # Without a state_class the recorder keeps five-day history and no
    # long-term statistics, so the one graph that matters for a backup battery
    # -- volts sagging over months -- cannot be drawn.
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Backup battery voltage"

    def __init__(
        self, coordinator: AlarmHubCoordinator, devices: HubDeviceIds, hub_id: str
    ) -> None:
        super().__init__(coordinator, devices, hub_id)
        self._attr_unique_id = logic.entity_unique_id(
            self._device_id, "battery_voltage"
        )

    @property
    def native_value(self) -> float | None:
        hub = self.hub
        if hub is None or hub.alarm_hub_battery is None:
            return None
        return battery_voltage(hub.alarm_hub_battery.voltage)
