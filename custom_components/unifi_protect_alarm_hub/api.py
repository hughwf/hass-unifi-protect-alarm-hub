"""Minimal async client for the UniFi Protect public integration API.

Talks to ``/proxy/protect/integration/v1`` with an ``X-API-KEY`` header. No
uiprotect dependency, so it never conflicts with the HA-bundled uiprotect used
by the official integration. The caller supplies an ``aiohttp.ClientSession``
already configured for SSL verification (HA's ``async_get_clientsession``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import aiohttp

from .models import AlarmHub

_LOGGER = logging.getLogger(__name__)

# modelKey of an Alarm Hub on the Protect devices WebSocket.
_ALARM_HUB_MODEL_KEY = "linkstation"


def is_alarm_hub_frame(raw: str) -> bool:
    """Return True iff a devices-WS text frame is for an alarm hub.

    Frames are partial JSON deltas: ``{"type": ..., "item": {"modelKey": ...}}``.
    Only the ``modelKey`` matters (add/remove/update of a hub all warrant a
    refresh). Returns False for any other model or malformed JSON.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    item = data.get("item")
    if not isinstance(item, dict):
        return False
    return item.get("modelKey") == _ALARM_HUB_MODEL_KEY


class AlarmHubAuthError(Exception):
    """Invalid or revoked API key (HTTP 401/403)."""


class AlarmHubConnectionError(Exception):
    """Network failure or non-auth error talking to the console."""


class AlarmHubApiClient:
    """Tiny REST client for the Protect public alarm-hub endpoints."""

    def __init__(
        self,
        host: str,
        port: int,
        api_key: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._base = f"https://{host}:{port}/proxy/protect/integration"
        self._headers = {"X-API-KEY": api_key}
        self._session = session

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._base}{path}"
        try:
            async with self._session.request(
                method, url, headers=self._headers, **kwargs
            ) as resp:
                if resp.status in (401, 403):
                    raise AlarmHubAuthError(f"Auth failed ({resp.status})")
                if resp.status >= 400:
                    raise AlarmHubConnectionError(f"HTTP {resp.status} from {path}")
                if resp.status == 204:
                    return None
                return await resp.json()
        except aiohttp.ClientError as err:
            raise AlarmHubConnectionError(str(err)) from err

    async def async_get_alarm_hubs(self) -> list[AlarmHub]:
        """Return all adopted alarm hubs with full current state."""
        data = await self._request("GET", "/v1/alarm-hubs")
        if not isinstance(data, list):
            _LOGGER.warning(
                "Unexpected response from /v1/alarm-hubs: %s", type(data).__name__
            )
            return []
        return [AlarmHub.from_json(item) for item in data if isinstance(item, dict)]

    async def async_trigger_output(
        self, hub_id: str, output_id: int, enable: bool
    ) -> None:
        """Trigger (enable=True) or clear (enable=False) an output channel."""
        await self._request(
            "POST",
            f"/v1/alarm-hubs/{hub_id}/outputs/{output_id}/trigger",
            json={"enable": enable},
        )

    async def async_subscribe_devices(
        self, on_alarm_hub_change: Callable[[], None]
    ) -> None:
        """Listen on the devices WebSocket, calling back on alarm-hub frames.

        Opens ``/v1/subscribe/devices`` and consumes the (chatty, all-device)
        text-JSON delta stream, invoking ``on_alarm_hub_change`` for each
        ``linkstation`` frame. Returns on a closed/errored socket so the caller
        can reconnect; the caller owns the reconnect/backoff loop.
        """
        url = f"{self._base}/v1/subscribe/devices"
        async with self._session.ws_connect(url, headers=self._headers) as ws:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if is_alarm_hub_frame(msg.data):
                        on_alarm_hub_change()
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
