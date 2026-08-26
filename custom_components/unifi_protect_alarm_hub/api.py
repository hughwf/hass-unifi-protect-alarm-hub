"""Minimal async client for the UniFi Protect public integration API.

Talks to ``/proxy/protect/integration/v1`` with an ``X-API-KEY`` header. No
uiprotect dependency, so it never conflicts with the HA-bundled uiprotect used
by the official integration. The caller supplies an ``aiohttp.ClientSession``
already configured for SSL verification (HA's ``async_get_clientsession``).

Every entry point narrows the world to two exceptions -- ``AlarmHubAuthError``
and ``AlarmHubConnectionError`` -- because the callers act on that distinction:
one starts a reauth flow, the other retries. A raw aiohttp or asyncio exception
escaping here surfaces to the user as "unknown error" with a traceback, so the
mapping has to cover the awkward cases too, and several of them are not
``ClientError`` subclasses.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import aiohttp

from .models import AlarmHub, describe_hub_payload

_LOGGER = logging.getLogger(__name__)

# modelKey of an Alarm Hub on the Protect devices WebSocket.
_ALARM_HUB_MODEL_KEY = "linkstation"

# The console is on the LAN and these endpoints return small JSON documents, so
# a healthy call lands in well under a second. aiohttp's default (total=300) is
# a WAN-scale number and HA's shared session does not override it: a host that
# accepts TCP and then goes quiet would freeze the config-flow dialog for five
# minutes and hang a switch command behind it. Ten seconds is generous for a LAN
# round trip and short enough that a poll fails well inside its own interval.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10, sock_connect=5)

# The upgrade handshake is a normal request and gets the same budget; the socket
# it opens is then meant to live for days, so the deadline is disarmed as soon
# as it is up. Without this the handshake would inherit the session's five
# minutes and the reconnect loop would sit on a dead console for all of it.
WS_CONNECT_TIMEOUT = 10.0

# Seconds between WebSocket pings. aiohttp sends none by default, so a socket
# that died without a close frame (console reboot, NAT idle timeout) leaves the
# reader blocked forever: no exception, no reconnect, and updates silently fall
# back to five-minute polling. Pinging turns that into an error we can act on.
WS_HEARTBEAT = 30.0

# Close codes that mean the console shut the socket down on purpose: 1000, 1001,
# and 0 for a close frame carrying no payload at all.
_GRACEFUL_CLOSE_CODES = frozenset(
    {0, aiohttp.WSCloseCode.OK, aiohttp.WSCloseCode.GOING_AWAY}
)


def alarm_hub_frame(raw: str) -> dict[str, Any] | None:
    """Return a parsed devices-WS text frame iff it is for an alarm hub.

    Frames look like ``{"type": ..., "item": {"modelKey": ..., ...}}``, where
    ``item`` carries only the fields that changed. Returns None for any other
    model or for malformed JSON, so the caller can ignore the rest of the
    (chatty, all-device) stream.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    item = data.get("item")
    if not isinstance(item, dict):
        return None
    if item.get("modelKey") != _ALARM_HUB_MODEL_KEY:
        return None
    return data


def ws_close_reason(
    close_code: int | None, exception: BaseException | None
) -> str | None:
    """Return why a read loop ended, or None if the console closed it on purpose.

    ``ClientWebSocketResponse.receive`` turns a mid-read ``ClientError`` into a
    plain CLOSED message rather than an ERROR frame, so a console that rebooted
    and a console that said goodbye both fall out of ``async for`` with no
    exception at all. Afterwards the close code is what tells them apart: an
    abrupt drop leaves 1006, a real close leaves the code the peer sent.
    """
    if exception is not None:
        return str(exception)
    if close_code is None or close_code in _GRACEFUL_CLOSE_CODES:
        return None
    return f"closed abnormally (code {close_code})"


def _isolate(what: str, call: Callable[[], None]) -> None:
    """Run a caller-supplied callback so a raising one cannot end the stream.

    Both callbacks land in HA's listener dispatch, which has no per-listener
    guard, so an entity blowing up would otherwise unwind the reader: the frame
    handler would read as a socket drop, and the connect handler would escape
    ``async_subscribe_devices`` as a raw exception, past the two-error contract
    its docstring states. Its bug, its traceback; the socket carries on.

    ``except Exception`` on purpose -- ``CancelledError`` is a BaseException and
    must keep propagating, because that is how shutdown ends the read loop.
    """
    try:
        call()
    except Exception:
        _LOGGER.exception("Alarm-hub %s handler raised; ignoring it", what)


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

    async def _request(
        self, method: str, path: str, *, parse_body: bool = True, **kwargs: Any
    ) -> Any:
        """Perform one call, mapping every way it can fail onto our two errors.

        ``parse_body=False`` returns None on any 2xx without reading the body:
        for a command endpoint the status *is* the answer, and the console has
        already acted by the time we get it, so an empty or non-JSON body must
        not be reported as the command failing.
        """
        url = f"{self._base}{path}"
        try:
            async with self._session.request(
                method, url, headers=self._headers, timeout=REQUEST_TIMEOUT, **kwargs
            ) as resp:
                if resp.status in (401, 403):
                    raise AlarmHubAuthError(f"Auth failed ({resp.status})")
                if resp.status >= 400:
                    raise AlarmHubConnectionError(f"HTTP {resp.status} from {path}")
                if resp.status == 204 or not parse_body:
                    return None
                return await resp.json()
        except aiohttp.ClientError as err:
            # Covers ContentTypeError too: a 2xx that did not claim JSON.
            raise AlarmHubConnectionError(str(err)) from err
        except TimeoutError as err:
            # aiohttp raises the builtin TimeoutError, which is not a
            # ClientError, so it needs its own clause or it escapes raw.
            raise AlarmHubConnectionError(f"Timed out talking to {path}") from err
        except ValueError as err:
            # JSONDecodeError: a body that claimed JSON and was not.
            raise AlarmHubConnectionError(f"Malformed JSON from {path}: {err}") from err
        except RuntimeError as err:
            # ``ClientSession._request`` raises a bare RuntimeError("Session is
            # closed") when HA's shared session was shut down while this
            # coordinator was still live. It is neither a ClientError nor a
            # TimeoutError, so without this it lands as HA's "Unexpected error
            # fetching ... data" with a traceback. CancelledError is a
            # BaseException and still passes straight through, which is what
            # shutdown relies on.
            raise AlarmHubConnectionError(f"Session unusable: {err}") from err

    async def async_get_alarm_hubs(self) -> list[AlarmHub]:
        """Return all adopted alarm hubs with full current state.

        An empty list is a real answer (no hubs adopted); anything that is not a
        list at all is a broken console, and reporting it as "no hubs" would
        take every entity unavailable behind a successful refresh instead of
        letting HA hold the last known state.
        """
        data = await self._request("GET", "/v1/alarm-hubs")
        # The other half of the shape question: what a delta is merged *into*.
        # Two lines, because the full payload is routinely truncated by log
        # exporters and the summary is what survives.
        _LOGGER.debug("Alarm-hub snapshot: %s", data)
        if _LOGGER.isEnabledFor(logging.DEBUG) and isinstance(data, list):
            for item in data:
                _LOGGER.debug("Alarm-hub shape: %s", describe_hub_payload(item))
        if not isinstance(data, list):
            raise AlarmHubConnectionError(
                f"Expected a list from /v1/alarm-hubs, got {type(data).__name__}"
            )
        return [AlarmHub.from_json(item) for item in data if isinstance(item, dict)]

    async def async_trigger_output(
        self, hub_id: str, output_id: int, enable: bool
    ) -> None:
        """Trigger (enable=True) or clear (enable=False) an output channel."""
        await self._request(
            "POST",
            f"/v1/alarm-hubs/{hub_id}/outputs/{output_id}/trigger",
            parse_body=False,
            json={"enable": enable},
        )

    async def async_subscribe_devices(
        self,
        on_alarm_hub_frame: Callable[[dict[str, Any]], None],
        on_connected: Callable[[], None] | None = None,
    ) -> None:
        """Listen on the devices WebSocket, handing alarm-hub frames to a callback.

        Opens ``/v1/subscribe/devices`` and consumes the (chatty, all-device)
        text-JSON delta stream, calling ``on_alarm_hub_frame`` with the parsed
        frame for every ``linkstation`` message, and ``on_connected`` once the
        socket is up. Neither callback can end the subscription: both are run
        through ``_isolate``, so a caller's bug is logged, not raised.

        Returns only when the console closed the socket gracefully. Raises
        ``AlarmHubAuthError`` when the key was rejected on the upgrade and
        ``AlarmHubConnectionError`` for every other ending, so the caller's
        reconnect loop can tell "this key is dead" from "the link broke, try
        again" — retrying a revoked key forever is the failure this prevents.
        """
        url = f"{self._base}/v1/subscribe/devices"
        try:
            async with (
                asyncio.timeout(WS_CONNECT_TIMEOUT) as connect_deadline,
                self._session.ws_connect(
                    url, headers=self._headers, heartbeat=WS_HEARTBEAT
                ) as ws,
            ):
                connect_deadline.reschedule(None)
                if on_connected is not None:
                    _isolate("connect", on_connected)
                await self._read_frames(ws, on_alarm_hub_frame)
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                raise AlarmHubAuthError(f"Auth failed ({err.status})") from err
            raise AlarmHubConnectionError(str(err)) from err
        except aiohttp.ClientError as err:
            raise AlarmHubConnectionError(str(err)) from err
        except TimeoutError as err:
            raise AlarmHubConnectionError("Timed out opening the WebSocket") from err
        except RuntimeError as err:
            # ``ws_connect`` goes through ``ClientSession._request``, so a shared
            # session closed under a live coordinator surfaces here as the same
            # bare RuntimeError the REST path sees. See ``_request``.
            raise AlarmHubConnectionError(f"Session unusable: {err}") from err

    async def _read_frames(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        on_alarm_hub_frame: Callable[[dict[str, Any]], None],
    ) -> None:
        """Drain the socket until it ends, raising unless the end was graceful."""
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                frame = alarm_hub_frame(msg.data)
                if frame is not None:
                    # Logged before anything interprets it: the whole delta path
                    # rests on the console sending the same field shape as the
                    # REST snapshot, and this is the only place that assumption
                    # can be checked against real hardware. The README asks for
                    # debug logs on an issue; this is what makes them worth
                    # asking for.
                    _LOGGER.debug("Alarm-hub frame: %s", msg.data)
                    _isolate("frame", lambda: on_alarm_hub_frame(frame))
            elif msg.type == aiohttp.WSMsgType.ERROR:
                # Where a heartbeat timeout lands, as a ServerTimeoutError:
                # the console stopped answering pings. Carry the reason out
                # rather than letting it read as an ordinary close — that
                # distinction is the whole point of pinging.
                err = msg.data or ws.exception()
                raise AlarmHubConnectionError(str(err) if err else "socket error")
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            ):
                # Unreachable with aiohttp 3.13: ``__anext__`` raises
                # StopAsyncIteration for these. Kept so a future version that
                # yields them cannot spin this loop; the check below still
                # decides whether the ending was graceful.
                break
        reason = ws_close_reason(ws.close_code, ws.exception())
        if reason is not None:
            raise AlarmHubConnectionError(reason)
