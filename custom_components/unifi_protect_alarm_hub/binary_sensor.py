"""Binary sensors for the UniFi Protect Alarm Hub."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
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
from .models import AlarmHub, InputZone


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlarmHubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create binary sensors for what exists now, and for whatever appears later.

    ``cover`` and ``battery`` are optional sections the console only reports on
    a hub that has them, so their sensors are built per hub rather than per
    entry. See ``async_reconcile_on_update`` for why this runs on every update.
    """
    coordinator: AlarmHubCoordinator = entry.runtime_data
    devices = async_hub_device_ids(hass, entry)

    @callback
    def _build(hub_id: str, hub: AlarmHub) -> list[BinarySensorEntity]:
        entities: list[BinarySensorEntity] = [
            ArmedBinarySensor(coordinator, devices, hub_id),
            ConnectivityBinarySensor(coordinator, devices, hub_id),
        ]
        for zone_id in hub.alarm_hub_inputs:
            entities.append(ZoneBinarySensor(coordinator, devices, hub_id, zone_id))
            entities.append(
                ZoneFaultBinarySensor(coordinator, devices, hub_id, zone_id)
            )
        if hub.alarm_hub_cover is not None:
            entities.append(TamperBinarySensor(coordinator, devices, hub_id))
        if hub.alarm_hub_battery is not None:
            entities.append(BatteryConnectionBinarySensor(coordinator, devices, hub_id))
        return entities

    async_reconcile_on_update(entry, coordinator, devices, async_add_entities, _build)


class _ZoneBase(AlarmHubBaseEntity, BinarySensorEntity):
    def __init__(
        self,
        coordinator: AlarmHubCoordinator,
        devices: HubDeviceIds,
        hub_id: str,
        zone_id: int,
    ) -> None:
        super().__init__(coordinator, devices, hub_id)
        self._zone_id = zone_id
        zone = self._zone
        self._attr_entity_registry_enabled_default = (
            logic.zone_enabled_default(zone) if zone else True
        )

    @property
    def _zone(self) -> InputZone | None:
        hub = self.hub
        return hub.alarm_hub_inputs.get(self._zone_id) if hub else None

    @property
    def _zone_name(self) -> str:
        """The zone's name as the console reports it *now*.

        Read live rather than captured in __init__: labelling a zone in the
        UniFi app is how someone tells "Garage Entry" from "Back Door", and a
        name frozen at construction never changed again for the life of the
        entity. ``unique_id`` stays frozen -- that is the registry's identity
        for this zone, and a rename must not orphan it.
        """
        zone = self._zone
        return logic.zone_name(zone, self._zone_id) if zone else f"Zone {self._zone_id}"

    @property
    def available(self) -> bool:
        return super().available and self._zone is not None


class ZoneBinarySensor(_ZoneBase):
    def __init__(
        self,
        coordinator: AlarmHubCoordinator,
        devices: HubDeviceIds,
        hub_id: str,
        zone_id: int,
    ) -> None:
        super().__init__(coordinator, devices, hub_id, zone_id)
        self._attr_unique_id = logic.zone_unique_id(self._device_id, zone_id)

    @property
    def name(self) -> str:
        return self._zone_name

    @property
    def device_class(self) -> BinarySensorDeviceClass | None:
        """What the zone is now, not what it was when Home Assistant started.

        ``inputType`` is a wiring detail someone changes in the UniFi app --
        moving a channel from a door contact to a PIR -- and the device_class
        is what decides whether the frontend says open/closed or detected. Read
        once, a re-typed zone kept rendering as the old kind indefinitely.
        """
        zone = self._zone
        if zone is None:
            return None
        return BinarySensorDeviceClass(logic.zone_device_class(zone))

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
            "status": zone.status,
            "contact_type": zone.type,
            "input_type": zone.input_type,
            "last_triggered_at": zone.last_triggered_at,
            "camera_id": zone.camera_id,
        }


class ZoneFaultBinarySensor(_ZoneBase):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: AlarmHubCoordinator,
        devices: HubDeviceIds,
        hub_id: str,
        zone_id: int,
    ) -> None:
        super().__init__(coordinator, devices, hub_id, zone_id)
        self._attr_unique_id = logic.zone_fault_unique_id(self._device_id, zone_id)

    @property
    def name(self) -> str:
        return f"{self._zone_name} Fault"

    @property
    def is_on(self) -> bool | None:
        zone = self._zone
        return logic.zone_fault_is_on(zone) if zone else None


class TamperBinarySensor(AlarmHubBaseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.TAMPER
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Tamper"

    def __init__(
        self, coordinator: AlarmHubCoordinator, devices: HubDeviceIds, hub_id: str
    ) -> None:
        super().__init__(coordinator, devices, hub_id)
        self._attr_unique_id = logic.entity_unique_id(self._device_id, "tamper")

    @property
    def is_on(self) -> bool | None:
        hub = self.hub
        return logic.cover_is_on(hub.alarm_hub_cover) if hub else None


class ArmedBinarySensor(AlarmHubBaseEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Armed"

    def __init__(
        self, coordinator: AlarmHubCoordinator, devices: HubDeviceIds, hub_id: str
    ) -> None:
        super().__init__(coordinator, devices, hub_id)
        self._attr_unique_id = logic.entity_unique_id(self._device_id, "armed")

    @property
    def is_on(self) -> bool | None:
        hub = self.hub
        return logic.armed_is_on(hub.alarm_hub_armed) if hub else None


class ConnectivityBinarySensor(AlarmHubBaseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Connectivity"
    # This entity reports the outage that takes the others unavailable, so it
    # cannot be taken unavailable by it -- an entity that reads "unavailable"
    # exactly when it should read "off" is the one that answers nothing. See
    # ``AlarmHubBaseEntity.available``.
    _survives_hub_offline = True

    def __init__(
        self, coordinator: AlarmHubCoordinator, devices: HubDeviceIds, hub_id: str
    ) -> None:
        super().__init__(coordinator, devices, hub_id)
        self._attr_unique_id = logic.entity_unique_id(self._device_id, "connectivity")

    @property
    def is_on(self) -> bool | None:
        hub = self.hub
        return logic.hub_is_connected(hub) if hub else None


class BatteryConnectionBinarySensor(AlarmHubBaseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PLUG
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Backup battery connection"

    def __init__(
        self, coordinator: AlarmHubCoordinator, devices: HubDeviceIds, hub_id: str
    ) -> None:
        super().__init__(coordinator, devices, hub_id)
        self._attr_unique_id = logic.entity_unique_id(
            self._device_id, "battery_connection"
        )

    @property
    def is_on(self) -> bool | None:
        hub = self.hub
        return logic.battery_connected_is_on(hub.alarm_hub_battery) if hub else None
