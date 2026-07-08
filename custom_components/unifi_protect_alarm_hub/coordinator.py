"""Data update coordinator for the UniFi Protect Alarm Hub.

Real-time updates come from the Protect devices WebSocket (see
``AlarmHubApiClient.async_subscribe_devices``): an alarm-hub frame triggers a
debounced full refresh. REST polling at ``SCAN_INTERVAL`` remains as a fallback
safety net (and the initial load).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AlarmHubApiClient, AlarmHubAuthError, AlarmHubConnectionError
from .const import DOMAIN, SCAN_INTERVAL
from .models import AlarmHub

_LOGGER = logging.getLogger(__name__)

# WebSocket reconnect backoff bounds (seconds).
BACKOFF_INITIAL = 1.0
BACKOFF_CAP = 60.0


def next_backoff(prev: float) -> float:
    """Return the next reconnect delay: double, capped, floored at the initial."""
    if prev <= 0:
        return BACKOFF_INITIAL
    return min(prev * 2, BACKOFF_CAP)


class AlarmHubCoordinator(DataUpdateCoordinator[dict[str, AlarmHub]]):
    """Coordinates Protect alarm-hub state via WebSocket push + REST fallback."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AlarmHubApiClient,
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.entry = entry
        self.client = client
        self._ws_task: asyncio.Task[None] | None = None

    async def _async_update_data(self) -> dict[str, AlarmHub]:
        try:
            hubs = await self.client.async_get_alarm_hubs()
        except AlarmHubAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AlarmHubConnectionError as err:
            raise UpdateFailed(f"Error talking to UniFi Protect: {err}") from err
        return {hub.id: hub for hub in hubs if hub.is_alarm_hub}

    @callback
    def start_ws(self) -> None:
        """Start the WebSocket reconnect loop as a background task."""
        if self._ws_task is None:
            self._ws_task = self.entry.async_create_background_task(
                self.hass, self._ws_listen(), name=f"{DOMAIN}_ws_listener"
            )

    async def _ws_listen(self) -> None:
        """Keep a devices-WS subscription alive, reconnecting with backoff.

        Best-effort: failures only delay the next attempt; REST polling keeps
        state fresh meanwhile. Backoff grows on errors and resets after a
        connection that returned cleanly.
        """
        backoff = BACKOFF_INITIAL
        while True:
            clean = False
            try:
                await self.client.async_subscribe_devices(self._on_ws_change)
                clean = True
            except asyncio.CancelledError:
                raise
            except AlarmHubAuthError as err:
                _LOGGER.warning(
                    "UniFi Protect WS auth failed; retrying in %.0fs: %s",
                    backoff,
                    err,
                )
            except Exception as err:
                _LOGGER.debug(
                    "UniFi Protect WS disconnected; retrying in %.0fs: %s",
                    backoff,
                    err,
                )
            if clean:
                backoff = BACKOFF_INITIAL
            await asyncio.sleep(backoff)
            if not clean:
                backoff = next_backoff(backoff)

    @callback
    def _on_ws_change(self) -> None:
        """An alarm-hub frame arrived: request a debounced full refresh."""
        self.hass.async_create_task(self.async_request_refresh())

    async def async_shutdown(self) -> None:
        """Cancel the WS task and shut the coordinator down cleanly."""
        if self._ws_task is not None:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
        await super().async_shutdown()
