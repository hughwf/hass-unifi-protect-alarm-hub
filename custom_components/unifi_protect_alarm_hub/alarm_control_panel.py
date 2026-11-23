"""Alarm control panel for the UniFi Protect Alarm Hub: arm and disarm.

The Alarm Hub forces the Global Alarm Manager, and in that mode the Integration
API will not set the arm profile. The one thing it does allow is firing an Alarm
Manager alarm whose Trigger is *Webhook*, so arming is done by configuring one
such alarm per action in Protect (Arm Away, Arm Night, Disarm) and giving this
integration each alarm's Trigger ID. State comes back the way every other entity
here gets it: from the hub, read over REST when the WebSocket says it changed.

Two limits follow from that and are deliberate rather than missing:

* **No ``arming`` / ``pending`` states.** Protect does not report an exit delay,
  so the panel goes straight from disarmed to the armed state once the hub
  reports it, instead of claiming a transition it cannot see.
* **No ``triggered`` state.** The hub reports outputs going active, but a beeper
  chirping through an entry delay looks the same as a siren, and a panel that
  cries "triggered" on every arrival is worse than one that does not say. Use
  the output switches for that.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AlarmHubConfigEntry, logic
from .api import AlarmHubAuthError, AlarmHubConnectionError
from .const import CONF_ARM_AWAY_TRIGGER, CONF_ARM_NIGHT_TRIGGER, CONF_DISARM_TRIGGER
from .coordinator import AlarmHubCoordinator
from .entity import (
    AlarmHubBaseEntity,
    async_hub_device_ids,
    async_reconcile_on_update,
)
from .logic import PROFILE_AWAY, PROFILE_DISARMED, PROFILE_NIGHT, HubDeviceIds
from .models import AlarmHub
from .profiles import ProfileBook

# How long an arm command waits for the hub to report the profile it took before
# the wait is abandoned. An arm that never took effect (a webhook pointed at the
# wrong alarm, a hub refusing because a zone is open) must not leave the panel
# waiting to label whatever profile someone arms next.
LEARN_WINDOW = 120.0

_STATES = {
    PROFILE_DISARMED: AlarmControlPanelState.DISARMED,
    PROFILE_AWAY: AlarmControlPanelState.ARMED_AWAY,
    PROFILE_NIGHT: AlarmControlPanelState.ARMED_NIGHT,
}


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
        return

    coordinator: AlarmHubCoordinator = entry.runtime_data
    devices = async_hub_device_ids(hass, entry)
    book = ProfileBook(hass, entry.entry_id)
    await book.async_load()

    @callback
    def _build(hub_id: str, hub: AlarmHub) -> list[AlarmHubAlarmPanel]:
        return [AlarmHubAlarmPanel(coordinator, devices, hub_id, triggers, book)]

    async_reconcile_on_update(entry, coordinator, devices, async_add_entities, _build)


class AlarmHubAlarmPanel(AlarmHubBaseEntity, AlarmControlPanelEntity):
    _attr_name = None
    _attr_code_arm_required = False

    def __init__(
        self,
        coordinator: AlarmHubCoordinator,
        devices: HubDeviceIds,
        hub_id: str,
        triggers: dict[str, str],
        book: ProfileBook,
    ) -> None:
        super().__init__(coordinator, devices, hub_id)
        self._triggers = triggers
        self._book = book
        self._attr_unique_id = logic.entity_unique_id(self._device_id, "alarm_panel")
        features = AlarmControlPanelEntityFeature(0)
        if triggers[CONF_ARM_AWAY_TRIGGER]:
            features |= AlarmControlPanelEntityFeature.ARM_AWAY
        if triggers[CONF_ARM_NIGHT_TRIGGER]:
            features |= AlarmControlPanelEntityFeature.ARM_NIGHT
        self._attr_supported_features = features
        # (profile being armed, the fingerprint in force when it was commanded,
        # deadline). See ``_learn``.
        self._pending: tuple[str, str | None, float] | None = None

    @property
    def _default_profile(self) -> str:
        """What an armed hub with an unrecognised profile is called.

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
        profile = logic.arm_profile(
            hub, self._book.for_device(self._device_id), self._default_profile
        )
        return _STATES.get(profile) if profile else None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._learn()
        super()._handle_coordinator_update()

    @callback
    def _learn(self) -> None:
        """Label the fingerprint of the profile we just armed, once it shows.

        The fingerprint has to differ from the one in force when the command was
        sent. Otherwise the first read after arming Night *from Away* can still
        be Away's zones -- the hub has been told but has not switched -- and
        Away's fingerprint would be filed as Night, mislabelling the very mode
        that was working. If Away and Night light identical zones there is
        nothing to tell them apart by and the label is never learned; the
        default stands, which is the honest answer.
        """
        if self._pending is None:
            return
        profile, before, deadline = self._pending
        if self.coordinator.hass.loop.time() > deadline:
            self._pending = None
            return
        hub = self.hub
        if hub is None or logic.armed_is_on(hub.alarm_hub_armed) is not True:
            return
        fingerprint = logic.profile_fingerprint(hub)
        if fingerprint is None or fingerprint == before:
            return
        self._book.learn(self._device_id, fingerprint, profile)
        self._pending = None

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
        reports, and the refresh below is what fetches it.
        """
        trigger_id = self._triggers[option]
        if not trigger_id:
            raise HomeAssistantError(
                f"No webhook Trigger ID is configured to {action}; add one in this"
                " integration's options (Alarm Manager alarm, Trigger = Webhook)"
            )
        hub = self.hub
        before = logic.profile_fingerprint(hub) if hub else None
        try:
            await self.coordinator.client.async_fire_webhook(trigger_id)
        except AlarmHubAuthError as err:
            raise HomeAssistantError(
                f"UniFi Protect rejected the API key while trying to {action}"
                f" ({err}); reconfigure the integration with a valid key"
            ) from err
        except AlarmHubConnectionError as err:
            raise HomeAssistantError(
                f"Could not {action}: UniFi Protect did not accept the webhook"
                f" ({err}). Check that the Trigger ID matches an Alarm Manager"
                " alarm whose Trigger is Webhook"
            ) from err
        self._pending = (
            None
            if profile is None
            else (profile, before, self.coordinator.hass.loop.time() + LEARN_WINDOW)
        )
        await self.coordinator.async_request_refresh()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        hub = self.hub
        return {"armed": hub.alarm_hub_armed if hub else None}
