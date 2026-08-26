"""Switches for UniFi Protect Alarm Hub output channels."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import REQUEST_REFRESH_DEFAULT_COOLDOWN

from . import AlarmHubConfigEntry, logic
from .api import AlarmHubAuthError, AlarmHubConnectionError
from .coordinator import AlarmHubCoordinator
from .entity import (
    AlarmHubBaseEntity,
    async_hub_device_ids,
    async_reconcile_on_update,
)
from .logic import HubDeviceIds
from .models import AlarmHub, OutputChannel

# How long a command's expected state may stand in for the hub's own reading.
#
# Tied to the request-refresh cooldown because the refresh ``_async_trigger``
# fires is what settles the question: the debouncer runs it at once when nothing
# is pending and at worst one cooldown later, so this is exactly long enough for
# that answer to arrive, and no longer. It is deliberately *not* tied to the
# poll interval -- with the socket down the next reconciling poll is five
# minutes away, and five minutes is not a length of time a switch may claim a
# siren is running on the strength of a command nobody confirmed.
OPTIMISTIC_WINDOW = float(REQUEST_REFRESH_DEFAULT_COOLDOWN)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlarmHubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create switches for the outputs that exist now, and for later ones.

    An output channel wired up after setup existed only in ``coordinator.data``
    until someone reloaded the entry; see ``async_reconcile_on_update``.
    """
    coordinator: AlarmHubCoordinator = entry.runtime_data
    devices = async_hub_device_ids(hass, entry)

    @callback
    def _build(hub_id: str, hub: AlarmHub) -> list[SwitchEntity]:
        return [
            OutputSwitch(coordinator, devices, hub_id, output_id)
            for output_id in hub.alarm_hub_outputs
        ]

    async_reconcile_on_update(entry, coordinator, devices, async_add_entities, _build)


class OutputSwitch(AlarmHubBaseEntity, SwitchEntity):
    def __init__(
        self,
        coordinator: AlarmHubCoordinator,
        devices: HubDeviceIds,
        hub_id: str,
        output_id: int,
    ) -> None:
        super().__init__(coordinator, devices, hub_id)
        self._output_id = output_id
        self._attr_unique_id = logic.output_unique_id(self._device_id, output_id)
        # What we told the hub to do, held until the hub says what it did or
        # the deadline for saying so passes. See ``_async_trigger``.
        self._optimistic_is_on: bool | None = None
        self._optimism_unsub: CALLBACK_TYPE | None = None

    @property
    def _output(self) -> OutputChannel | None:
        hub = self.hub
        return hub.alarm_hub_outputs.get(self._output_id) if hub else None

    @property
    def name(self) -> str:
        """The output's name as the console reports it now.

        Live for the same reason a zone's is: renaming the channel that drives
        the siren is how it gets labelled, and a name captured in __init__
        never changed again. ``unique_id`` stays frozen.
        """
        output = self._output
        return (
            logic.output_name(output, self._output_id)
            if output
            else f"Output {self._output_id}"
        )

    @property
    def available(self) -> bool:
        """Unavailable once the hub stops describing this output.

        A relay the snapshot no longer carries is one nobody can read or
        command, and the optimistic value must not paper over that: an entity
        that answers a service call is one Home Assistant will route a
        ``turn_on`` to, and this one has nowhere to send it.
        """
        return super().available and self._output is not None

    @property
    def is_on(self) -> bool | None:
        if self._optimistic_is_on is not None:
            return self._optimistic_is_on
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

    @callback
    def _handle_coordinator_update(self) -> None:
        """Retire the expected state once the hub actually reports it.

        Retiring it on the *next* update, whatever that update said, was the
        same thing as not having it at all. ``_async_trigger`` follows every
        command with ``async_request_refresh``, whose debouncer is immediate, so
        the REST GET runs inside the service call -- and it races the relay. A
        hub that has accepted the command but not yet moved answers with the old
        value, and the switch sprang back to it before the user saw anything,
        on the very first command, which is the failure the optimism exists to
        prevent.

        The deadline in ``_async_trigger`` is the other half: a console that
        accepted a command and never acted on it never sends an agreeing update,
        and a switch stuck claiming a siren is running is the worst thing for a
        panic automation to leave behind. So the expectation is bounded rather
        than conditional on an update that may never come.
        """
        if self._optimistic_is_on is not None and logic.output_confirms(
            self._output, self._optimistic_is_on
        ):
            self._drop_optimism()
        super()._handle_coordinator_update()

    @callback
    def _drop_optimism(self) -> None:
        """Forget the expected state, and the deadline that would have."""
        self._optimistic_is_on = None
        if self._optimism_unsub is not None:
            self._optimism_unsub()
            self._optimism_unsub = None

    @callback
    def _optimism_lapsed(self, _now: datetime) -> None:
        """The hub never confirmed the command: publish what it does report."""
        self._optimism_unsub = None
        self._optimistic_is_on = None
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_trigger(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_trigger(False)

    async def _async_trigger(self, enable: bool) -> None:
        """Command the output, show the expected state, then ask for the real one.

        The optimistic write is what makes a command visible at once. Only
        ``async_request_refresh`` follows the call, and its debouncer holds a
        ten-second cooldown, so a turn_off issued moments after a turn_on used
        to render from a snapshot that predated it: the toggle sprang back to
        "on" for the rest of the cooldown, which reads as the command having
        been rejected. It stands until the hub confirms it (see
        ``_handle_coordinator_update``) or ``OPTIMISTIC_WINDOW`` passes,
        whichever comes first -- an expectation, bounded, never a claim.

        The refresh is what usually ends it: it is the request that fetches what
        the relay actually did, and without it a hub that took the command would
        keep rendering from the pre-command snapshot until the next scheduled
        poll, up to five minutes later.

        API errors become HomeAssistantError so the service call says something
        the user can act on, and nothing is claimed for a command that never
        reached the hub. ``AlarmHubConnectionError`` and ``AlarmHubAuthError``
        are ours and subclass neither, and Home Assistant renders anything that
        is not a HomeAssistantError as "Unknown error occurred" with a
        traceback -- which aborts the automation noisily rather than failing it
        with a reason.
        """
        try:
            await self.coordinator.client.async_trigger_output(
                self.hub_id, self._output_id, enable
            )
        except AlarmHubAuthError as err:
            raise HomeAssistantError(
                f"UniFi Protect rejected the API key while switching {self.entity_id}"
                f" ({err}); reconfigure the integration with a valid key"
            ) from err
        except AlarmHubConnectionError as err:
            raise HomeAssistantError(
                f"Could not reach UniFi Protect to switch {self.entity_id}: {err}"
            ) from err
        self._drop_optimism()
        self._optimistic_is_on = enable
        self._optimism_unsub = async_call_later(
            self.hass, OPTIMISTIC_WINDOW, self._optimism_lapsed
        )
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_will_remove_from_hass(self) -> None:
        """Drop the deadline with the entity, as the base does with its own timer."""
        self._drop_optimism()
        await super().async_will_remove_from_hass()
