"""Data update coordinator for the UniFi Protect Alarm Hub."""

from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from uiprotect import ProtectApiClient
from uiprotect.data.public_devices import LinkStation
from uiprotect.exceptions import NotAuthorized, NvrError

from . import logic
from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class AlarmHubCoordinator(DataUpdateCoordinator[dict[str, LinkStation]]):
    """Polls the Protect public API and pushes WebSocket device updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ProtectApiClient,
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.entry = entry
        self.client = client
        self._unsub_ws: Callable[[], None] | None = None

    async def _async_update_data(self) -> dict[str, LinkStation]:
        try:
            await self.client.update_public()
        except NotAuthorized as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except NvrError as err:
            raise UpdateFailed(f"Error talking to UniFi Protect: {err}") from err
        return logic.snapshot(self.client.public_bootstrap)

    @callback
    def async_subscribe_ws(self) -> None:
        """Subscribe to the public devices WebSocket for real-time updates."""
        self._unsub_ws = self.client.subscribe_devices_websocket(self._ws_callback)

    @callback
    def _ws_callback(self, _message) -> None:
        # The library has already applied the change to public_bootstrap.
        self.async_set_updated_data(logic.snapshot(self.client.public_bootstrap))

    async def async_shutdown(self) -> None:
        if self._unsub_ws is not None:
            self._unsub_ws()
            self._unsub_ws = None
        await self.client.async_disconnect_ws()
        await self.client.close_public_api_session()
        await super().async_shutdown()
