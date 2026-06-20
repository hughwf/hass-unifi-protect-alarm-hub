"""Polling data update coordinator for the UniFi Protect Alarm Hub."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AlarmHubApiClient, AlarmHubAuthError, AlarmHubConnectionError
from .const import DOMAIN, SCAN_INTERVAL
from .models import AlarmHub

_LOGGER = logging.getLogger(__name__)


class AlarmHubCoordinator(DataUpdateCoordinator[dict[str, AlarmHub]]):
    """Polls the Protect public API for alarm-hub state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AlarmHubApiClient,
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.entry = entry
        self.client = client

    async def _async_update_data(self) -> dict[str, AlarmHub]:
        try:
            hubs = await self.client.async_get_alarm_hubs()
        except AlarmHubAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AlarmHubConnectionError as err:
            raise UpdateFailed(f"Error talking to UniFi Protect: {err}") from err
        return {hub.id: hub for hub in hubs if hub.is_alarm_hub}
