"""Switches for UniFi Protect Alarm Hub output channels."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import logic
from .coordinator import AlarmHubCoordinator
from .entity import AlarmHubBaseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AlarmHubCoordinator = entry.runtime_data
    entities: list[OutputSwitch] = []
    for hub_id, hub in coordinator.data.items():
        for output_id in hub.alarm_hub_outputs:
            entities.append(OutputSwitch(coordinator, hub_id, output_id))
    async_add_entities(entities)


class OutputSwitch(AlarmHubBaseEntity, SwitchEntity):
    def __init__(
        self, coordinator: AlarmHubCoordinator, hub_id: str, output_id: int
    ) -> None:
        super().__init__(coordinator, hub_id)
        self._output_id = output_id
        self._attr_unique_id = logic.output_unique_id(self.hub.mac, output_id)
        output = self._output
        self._attr_name = (
            logic.output_name(output, output_id) if output else f"Output {output_id}"
        )

    @property
    def _output(self):
        hub = self.hub
        return hub.alarm_hub_outputs.get(self._output_id) if hub else None

    @property
    def available(self) -> bool:
        return super().available and self._output is not None

    @property
    def is_on(self) -> bool | None:
        output = self._output
        return logic.output_is_on(output) if output else None

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        output = self._output
        if output is None:
            return {}
        return {
            "status": output.status,
            "delay": output.delay,
            "duration": output.duration,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.client.async_trigger_output(
            self._hub_id, self._output_id, True
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.client.async_trigger_output(
            self._hub_id, self._output_id, False
        )
        await self.coordinator.async_request_refresh()
