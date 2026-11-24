"""Alarm control panel for the UniFi Protect Alarm Hub: arm and disarm.

The Alarm Hub forces the Global Alarm Manager, and in that mode the Integration
API will not set the arm profile. The one thing it does allow is firing an Alarm
Manager alarm whose Trigger is *Webhook*, so arming is done by configuring one
such alarm per action in Protect (Arm Away, Arm Night, Disarm) and giving this
integration each alarm's Trigger ID. State comes back the way every other entity
here gets it: from the hub, read over REST when the WebSocket says it changed.

The hub says *whether* it is armed and nothing about *which* profile. Measured
on a real UP-AlarmHub-Kit pair under the Global Alarm Manager (Protect 7.3.74,
2026-11-23), arming and disarming from the Protect app moved ``armed`` and
nothing else: every zone's ``triggerOnCurrentArmingProfile`` stayed ``on``, and
the console's ``armMode`` (``GET /v1/nvrs``) stayed ``disabled`` throughout. So
the panel shows the profile Home Assistant itself last armed, and a default when
the hub was armed from a keypad, a fob or the Protect app.

Limits that follow from what the hub reports, deliberate rather than missing:

* **Armed from elsewhere reads as the default profile.** There is nothing to
  tell Away from Night by.
* **No ``arming`` / ``pending`` states.** Protect does not report an exit delay,
  so the panel goes straight from disarmed to the armed state once the hub
  reports it, instead of claiming a transition it cannot see.
* **No ``triggered`` state.** The hub reports outputs going active, but a beeper
  chirping through an entry delay looks the same as a siren, and a panel that
  cries "triggered" on every arrival is worse than one that does not say. Use
  the output switches for that.
"""

from __future__ import annotations

import asyncio
from typing import Any, NamedTuple

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import AlarmHubConfigEntry, logic
from .api import AlarmHubAuthError, AlarmHubConnectionError
from .const import (
    CONF_ARM_AWAY_TRIGGER,
    CONF_ARM_NIGHT_TRIGGER,
    CONF_DISARM_TRIGGER,
    DOMAIN,
)
from .coordinator import AlarmHubCoordinator
from .entity import (
    AlarmHubBaseEntity,
    async_hub_device_ids,
    async_reconcile_on_update,
)
from .logic import PROFILE_AWAY, PROFILE_DISARMED, PROFILE_NIGHT, HubDeviceIds
from .models import AlarmHub

# How long after an arm command the hub may take to report armed and still have
# that credited to the command. Arms from the Protect app showed up within a few
# seconds; the rest is margin. Kept short because inside it an arm from anywhere
# else cannot be told from ours: a webhook that was accepted and did nothing,
# followed by someone arming at a keypad, shows our profile until the next
# disarm.
ARM_WINDOW = 20.0

# Seconds to wait before each follow-up read after a command, until the hub
# reports what was asked for. Arming and disarming send no frame on the devices
# WebSocket -- six transitions on a real console, none pushed -- so without
# these the panel sits on the old state until the next SCAN_INTERVAL poll.
#
# The first four are quick: a real hub answers within a second, and an arm has
# to be seen inside ARM_WINDOW to be credited. The rest are slow, for a hub that
# has not answered in 15s and may still act: "disarmed" over an armed house must
# not stand for five minutes. An arm seen that late reads as the default
# profile. They add up to five minutes; after the last, the hub is left to the
# regular poll.
SETTLE_DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0, 60.0, 60.0, 60.0)

_STATES = {
    PROFILE_DISARMED: AlarmControlPanelState.DISARMED,
    PROFILE_AWAY: AlarmControlPanelState.ARMED_AWAY,
    PROFILE_NIGHT: AlarmControlPanelState.ARMED_NIGHT,
}
_PROFILES = {state.value: profile for profile, state in _STATES.items()}


class _Command(NamedTuple):
    """An arm command this entry sent: its profile, and when crediting it stops."""

    profile: str
    deadline: float


class _LastCommand:
    """The latest arm command, shared by every panel of one entry.

    Shared because the webhooks belong to the entry, not to a hub: the Alarm
    Manager arms every hub at once, so the panel that was pressed and the panel
    of the hub beside it both have to credit the same command.
    """

    def __init__(self) -> None:
        self.command: _Command | None = None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlarmHubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a panel per hub -- but only once at least one webhook is configured.

    Without a trigger ID the panel could not do the one thing it is for, and a
    read-only panel would only duplicate the *Armed* binary sensor. Changing the
    options reloads the entry, so configuring a trigger makes it appear.
    """
    triggers = {
        key: (entry.options.get(key) or "").strip()
        for key in (CONF_ARM_AWAY_TRIGGER, CONF_ARM_NIGHT_TRIGGER, CONF_DISARM_TRIGGER)
    }
    if not any(triggers.values()):
        # Clearing the options takes the panel with it. Everywhere else this
        # integration leaves entities in place, because one that vanishes takes
        # its automations along; here the user has removed the only thing the
        # panel could do, and what stayed behind would be an Arm button that
        # can never work.
        registry = er.async_get(hass)
        for row in er.async_entries_for_config_entry(registry, entry.entry_id):
            if row.domain == Platform.ALARM_CONTROL_PANEL:
                registry.async_remove(row.entity_id)
        return

    coordinator: AlarmHubCoordinator = entry.runtime_data
    devices = async_hub_device_ids(hass, entry)
    last = _LastCommand()

    @callback
    def _build(hub_id: str, hub: AlarmHub) -> list[AlarmHubAlarmPanel]:
        return [AlarmHubAlarmPanel(coordinator, devices, hub_id, triggers, last)]

    async_reconcile_on_update(entry, coordinator, devices, async_add_entities, _build)


class AlarmHubAlarmPanel(AlarmHubBaseEntity, AlarmControlPanelEntity, RestoreEntity):
    _attr_name = None
    _attr_code_arm_required = False

    def __init__(
        self,
        coordinator: AlarmHubCoordinator,
        devices: HubDeviceIds,
        hub_id: str,
        triggers: dict[str, str],
        last: _LastCommand,
    ) -> None:
        super().__init__(coordinator, devices, hub_id)
        self._triggers = triggers
        self._last = last
        self._attr_unique_id = logic.entity_unique_id(self._device_id, "alarm_panel")
        features = AlarmControlPanelEntityFeature(0)
        if triggers[CONF_ARM_AWAY_TRIGGER]:
            features |= AlarmControlPanelEntityFeature.ARM_AWAY
        if triggers[CONF_ARM_NIGHT_TRIGGER]:
            features |= AlarmControlPanelEntityFeature.ARM_NIGHT
        self._attr_supported_features = features
        # The profile this armed spell is credited to, and the command that was
        # credited for it -- kept so one command cannot label two spells: ours,
        # then a keypad re-arm that lands inside the same window.
        self._armed_as: str | None = None
        self._credited: _Command | None = None
        self._settle: object | None = None

    async def async_added_to_hass(self) -> None:
        """Pick the credited profile back up across a restart or a reload.

        Without it, restarting Home Assistant while armed Night reads Away until
        the next arm. It is only as good as the last thing seen: a hub disarmed
        and re-armed from a keypad while Home Assistant was down keeps the old
        label until it disarms.
        """
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        restored = _PROFILES.get(last_state.state) if last_state else None
        if restored in (PROFILE_AWAY, PROFILE_NIGHT):
            self._armed_as = restored
            self._credit()

    @property
    def _default_profile(self) -> str:
        """What a hub armed from somewhere else is called.

        Away unless only Night can be armed from here, in which case calling it
        Away would be naming a mode this panel cannot even command.
        """
        if (
            not self._triggers[CONF_ARM_AWAY_TRIGGER]
            and self._triggers[CONF_ARM_NIGHT_TRIGGER]
        ):
            return PROFILE_NIGHT
        return PROFILE_AWAY

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        hub = self.hub
        if hub is None:
            return None
        profile = logic.arm_profile(hub, self._armed_as, self._default_profile)
        return _STATES.get(profile) if profile else None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._credit()
        super()._handle_coordinator_update()

    @callback
    def _credit(self) -> None:
        """Decide which profile the hub's armed state belongs to.

        Disarmed ends the spell, so whatever arms it next starts unlabelled.
        Armed is credited to the latest arm command sent from here if that
        command is recent and has not already labelled a spell on this hub;
        otherwise the label stands as it was -- ours from earlier in this spell,
        or none, which reads as the default.
        """
        hub = self.hub
        armed = logic.armed_is_on(hub.alarm_hub_armed) if hub else None
        if armed is False:
            self._armed_as = None
            return
        command = self._last.command
        if (
            armed
            and command is not None
            and command is not self._credited
            and self.coordinator.hass.loop.time() <= command.deadline
        ):
            self._armed_as = command.profile
            self._credited = command

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._async_fire(CONF_DISARM_TRIGGER, "disarm", None)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._async_fire(CONF_ARM_AWAY_TRIGGER, "arm away", PROFILE_AWAY)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self._async_fire(CONF_ARM_NIGHT_TRIGGER, "arm night", PROFILE_NIGHT)

    async def _async_fire(self, option: str, action: str, profile: str | None) -> None:
        """Fire the configured webhook, then ask the hub what it did.

        Errors become ``HomeAssistantError`` for the reason the output switch
        gives: anything else reaches the user as "Unknown error occurred" and
        aborts an automation with a traceback instead of a reason. Nothing is
        claimed about the outcome -- the panel keeps showing what the hub
        reports, and the follow-up is what fetches it.
        """
        trigger_id = self._triggers[option]
        if not trigger_id:
            raise HomeAssistantError(
                f"No webhook Trigger ID is configured to {action}; add one in this"
                " integration's options (Alarm Manager alarm, Trigger = Webhook)"
            )
        try:
            await self.coordinator.client.async_fire_webhook(trigger_id)
        except AlarmHubAuthError as err:
            raise HomeAssistantError(
                f"UniFi Protect rejected the API key while trying to {action}"
                f" ({err}); reconfigure the integration with a valid key"
            ) from err
        except AlarmHubConnectionError as err:
            if err.status is None or err.status >= 500:
                # No answer, or a server error, is not a refusal: a console that
                # acted and then replied too late or fell over looks exactly
                # like this. Follow up as if it had acted, so the panel catches
                # up instead of sitting on the old state while the error says
                # nothing happened.
                await self._async_follow_up(profile)
                raise HomeAssistantError(
                    f"Could not confirm the request to {action}: UniFi Protect"
                    f" gave no usable answer ({err}). It may still have acted;"
                    " the panel shows what the hub reports"
                ) from err
            raise HomeAssistantError(
                f"Could not {action}: UniFi Protect did not accept the webhook"
                f" ({err}). Check that the Trigger ID matches an Alarm Manager"
                " alarm whose Trigger is Webhook"
            ) from err
        await self._async_follow_up(profile)

    async def _async_follow_up(self, profile: str | None) -> None:
        """Record the command, then go and see what the hub did with it."""
        # A disarm withdraws any arm still waiting to be credited.
        self._last.command = (
            None
            if profile is None
            else _Command(profile, self.coordinator.hass.loop.time() + ARM_WINDOW)
        )
        # The newest follow-up is the only one that goes on reading. An older
        # one is superseded rather than cancelled: a cancellation landing inside
        # the coordinator's refresh is recorded as a failed update for every
        # entity of the entry. Claimed before the await below, or a press that
        # arrives during it would be superseded by this older one.
        self._settle = mine = object()
        await self.coordinator.async_request_refresh()
        # A background task of the entry, so unloading cancels it.
        self.coordinator.entry.async_create_background_task(
            self.hass,
            self._async_settle(mine, want_armed=profile is not None),
            name=f"{DOMAIN}_panel_settle",
        )

    async def _async_settle(self, mine: object, *, want_armed: bool) -> None:
        """Re-read the hub until it reports what was asked for (see SETTLE_DELAYS).

        It reads before it looks: the refresh in ``_async_follow_up`` is
        debounced, so what is cached may predate the command, and a cache that
        happens to match already would end this without a single read.
        """
        for delay in SETTLE_DELAYS:
            await asyncio.sleep(delay)
            if self._settle is not mine:
                return  # a later press has its own follow-up
            await self.coordinator.async_refresh()
            hub = self.hub
            if hub and logic.armed_is_on(hub.alarm_hub_armed) is want_armed:
                return

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        hub = self.hub
        return {"armed": hub.alarm_hub_armed if hub else None}
