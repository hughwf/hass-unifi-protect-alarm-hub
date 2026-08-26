"""Data update coordinator for the UniFi Protect Alarm Hub.

Real-time updates come from the Protect devices WebSocket (see
``AlarmHubApiClient.async_subscribe_devices``): an alarm-hub frame is applied
straight to the cached state, so every edge the hub reports reaches entities at
the moment it happens. REST polling at ``SCAN_INTERVAL`` is the reconciling
fallback (and the initial load), and a reconnect resyncs in full.

Push and poll race, so the two paths are kept from overwriting each other. A
snapshot describes the console as it was when its request went out, which makes
any frame that lands while it is in flight newer than the reply: those frames
are buffered and replayed over the snapshot before it is published. And a
pushed update never touches the poll schedule, because the poll is what heals a
frame we never received -- a chatty hub must not be able to postpone it.

A frame the cache cannot absorb falls back to a snapshot, and how eagerly
depends on whether the thing that caused it will ever stop. A hub being adopted
or removed happens once and is settled by one request. An id we do not hold, a
payload in a shape we cannot parse, a frame type this integration was not
written against: those are properties of the console rather than events -- each
repeats for every frame it ever sends -- so they share a throttle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Iterable
from typing import Any

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

# How long a connection must last before the drop that ends it counts as the
# end of a working link rather than another failed attempt. Only that restarts
# the backoff ramp; a console that accepts the socket and drops it straight away
# keeps backing off instead of retrying once a second forever.
WS_HEALTHY_UPTIME = 60.0

# How long a connection must last to also re-arm the outage warning. Deliberately
# longer than WS_HEALTHY_UPTIME: at one shared threshold a console that holds the
# socket for just over a minute and then drops would reset the ramp *and* the
# log latch every cycle, which is the once-a-second warning the latch exists to
# suppress, reappearing at its own boundary. Re-arming asks for a connection long
# enough to read as a recovery rather than as a slower flap.
WS_WARN_REARM_UPTIME = 300.0

# How many deltas to hold while a REST request is in flight. The window is one
# round trip, so this is already far past anything a real hub sends; the cap is
# there so a request that hangs against a chatty console cannot grow the buffer
# without bound. Every well-formed update takes a slot, whether or not it moved
# anything in the cache (see ``_on_ws_frame``), and overflow is not free -- the
# dropped frame is an edge nobody re-reports, so an eviction forces one snapshot
# once the request it raced has landed.
MAX_PENDING_DELTAS = 64

# Minimum gap between the snapshots the fallback paths spend (seconds). They all
# guard standing conditions -- an id the REST filter never returns, hub state in
# a shape we do not parse -- rather than events: asking once every few minutes
# diagnoses them as well as asking once per frame, and costs nothing in between.
FALLBACK_RESYNC_INTERVAL = 300.0


def next_backoff(prev: float) -> float:
    """Return the next reconnect delay: double, capped, floored at the initial."""
    if prev <= 0:
        return BACKOFF_INITIAL
    return min(prev * 2, BACKOFF_CAP)


def _uptime_at_least(connected_at: float | None, now: float, seconds: float) -> bool:
    """Whether a connection opened at ``connected_at`` has lasted ``seconds``."""
    if connected_at is None:
        return False
    return now - connected_at >= seconds


def ws_is_healthy(connected_at: float | None, now: float) -> bool:
    """Whether a connection opened at ``connected_at`` lasted long enough to count."""
    return _uptime_at_least(connected_at, now, WS_HEALTHY_UPTIME)


def ws_warning_is_rearmed(connected_at: float | None, now: float) -> bool:
    """Whether that connection lasted long enough for its end to be a new outage."""
    return _uptime_at_least(connected_at, now, WS_WARN_REARM_UPTIME)


def fallback_resync_is_due(last_resync: float | None, now: float) -> bool:
    """Whether the standing-condition safety net may spend another snapshot."""
    if last_resync is None:
        return True
    return now - last_resync >= FALLBACK_RESYNC_INTERVAL


def replay_deltas(
    snapshot: dict[str, AlarmHub],
    deltas: Iterable[tuple[str, dict[str, Any]]],
) -> dict[str, AlarmHub]:
    """Layer deltas that raced a REST request over the snapshot it returned.

    Every delta buffered while the request was in flight happened after that
    snapshot was taken, so it wins, and they are applied in arrival order.
    ``isAlarmHub`` is judged once at the end rather than per delta: dropping a
    hub the moment one frame disowns it also drops every later frame for it --
    including the one that says the console changed its mind -- so a pair that
    cancels out used to publish the hub as gone until the next poll.

    Ids the snapshot does not carry are skipped, and that is the only filtering
    a delta gets. REST is authoritative about which hubs exist, and a hub the
    deltas leave as a non-alarm-hub drops out here exactly as on the poll path.
    """
    merged = dict(snapshot)
    for hub_id, item in deltas:
        hub = merged.get(hub_id)
        if hub is None:
            continue
        merged[hub_id] = hub.with_delta(item)
    return {hub_id: hub for hub_id, hub in merged.items() if hub.is_alarm_hub}


def hubs_by_id(hubs: Iterable[AlarmHub]) -> dict[str, AlarmHub]:
    """Index a REST snapshot's alarm hubs by id, skipping ids we cannot key on.

    ``id`` is untrusted console input on this path exactly as it is on the
    WebSocket one, and here it is used as a dict key: an unhashable value (a
    list, say) raised TypeError inside ``_async_update_data``, which is not one
    of the two exceptions that path maps, so a single unreadable device failed
    the whole refresh and reached the user as "Unexpected error fetching data"
    with a traceback. Skipping it costs that hub's entities and nothing else.
    """
    indexed: dict[str, AlarmHub] = {}
    for hub in hubs:
        if not hub.is_alarm_hub:
            continue
        if not isinstance(hub.id, str):
            # Standing condition -- the console returns the same object every
            # poll -- so debug, like the other console-shape complaints.
            _LOGGER.debug("Skipping an alarm hub whose id we cannot use: %r", hub.id)
            continue
        indexed[hub.id] = hub
    return indexed


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
        self._ws_down = False
        self._ws_backoff = BACKOFF_INITIAL
        self._ws_connected_at: float | None = None
        # Deltas applied while a REST request is in flight, replayed over the
        # snapshot it returns. One buffer is enough because only one request can
        # be running: every refresh path holds the debouncer's lock.
        self._pending_deltas: deque[tuple[str, dict[str, Any]]] = deque(
            maxlen=MAX_PENDING_DELTAS
        )
        self._deltas_evicted = False
        # Set while the snapshot an eviction forced is queued, so that snapshot
        # cannot force one of its own (see ``_async_update_data``).
        self._eviction_resync_pending = False
        self._rest_in_flight = False
        self._last_fallback_resync: float | None = None

    async def _async_update_data(self) -> dict[str, AlarmHub]:
        self._pending_deltas.clear()
        self._deltas_evicted = False
        is_eviction_resync = self._eviction_resync_pending
        self._eviction_resync_pending = False
        self._rest_in_flight = True
        try:
            hubs = await self.client.async_get_alarm_hubs()
        except AlarmHubAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AlarmHubConnectionError as err:
            raise UpdateFailed(f"Error talking to UniFi Protect: {err}") from err
        finally:
            # Buffered deltas never outlive the request they raced, failures
            # included: they are already in the cache, and replaying them over
            # some later snapshot would revert whatever happened since.
            self._rest_in_flight = False
            pending = tuple(self._pending_deltas)
            self._pending_deltas.clear()
            if self._deltas_evicted and not is_eviction_resync:
                # An edge we dropped is an edge nobody re-reports, and the reply
                # about to be published predates it, so the cache is about to go
                # wrong with nothing to notice: the scheduled poll is up to five
                # minutes away. Queue a snapshot that postdates the loss instead
                # -- it waits on the refresh lock this request is holding.
                #
                # One follow-up per request that lost a delta, and none from a
                # follow-up. Whatever overran the buffer is still overrunning it
                # while the resync is in flight, so a re-entrant version of this
                # chains GET after GET off a single poll. It also matters that
                # this runs on the failing endings above: one retry after a poll
                # that failed or was refused is reasonable, a chain of them is a
                # reauth flow being restarted in a loop.
                self._eviction_resync_pending = True
                self.hass.async_create_task(self.async_refresh())
            self._deltas_evicted = False
        return replay_deltas(hubs_by_id(hubs), pending)

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
        state fresh meanwhile. A connection that stayed up (see
        ``ws_is_healthy``) restarts the ramp, so a socket that ran for hours
        before dropping retries immediately instead of inheriting an old delay.
        The drop that begins an outage is logged at warning level — until it
        reconnects the integration is quietly reduced to five-minute polling,
        which is the failure users cannot otherwise see — while the retries
        that follow stay at debug, so a flapping console cannot fill the log.
        Re-arming that warning takes a longer connection than restarting the
        ramp does (see ``ws_warning_is_rearmed``), so a console flapping around
        the healthy threshold cannot warn on every cycle.

        Each ``reason`` says what actually happened: the client returns only on
        a graceful close and raises on every other ending, so a console that
        vanished mid-stream reads as dropped rather than as a tidy close.
        """
        while True:
            self._ws_connected_at = None
            try:
                await self.client.async_subscribe_devices(
                    self._on_ws_frame, self._on_ws_connected
                )
                reason = "closed by the console"
            except asyncio.CancelledError:
                raise
            except AlarmHubAuthError as err:
                # The key was revoked or rotated, so retrying cannot fix it; the
                # REST poll hits the same 401 and raises ConfigEntryAuthFailed,
                # which is what surfaces it to the user for now.
                # TODO: escalate straight to a reauth flow from here, once the
                # config flow has a reauth step to escalate to.
                reason = f"rejected the API key ({err})"
            except Exception as err:
                reason = f"dropped ({err})"
            now = self.hass.loop.time()
            if ws_is_healthy(self._ws_connected_at, now):
                # A real connection just ended: start the ramp over.
                self._ws_backoff = BACKOFF_INITIAL
            if ws_warning_is_rearmed(self._ws_connected_at, now):
                # ...and it lasted long enough to have been a recovery, so let
                # this outage be reported even if an earlier one already was.
                self._ws_down = False
            delay = self._ws_backoff
            _LOGGER.log(
                logging.DEBUG if self._ws_down else logging.WARNING,
                "UniFi Protect WebSocket %s; falling back to REST polling until it"
                " reconnects (retrying in %.0fs)",
                reason,
                delay,
            )
            self._ws_down = True
            self._ws_backoff = next_backoff(delay)
            await asyncio.sleep(delay)

    @callback
    def _on_ws_connected(self) -> None:
        """The devices WebSocket is up: start the uptime clock, and resync."""
        self._ws_connected_at = self.hass.loop.time()
        if not self._ws_down:
            # First connect of the session, moments after setup took its own
            # snapshot: nothing has been missed, so skip the duplicate request.
            return
        _LOGGER.info("UniFi Protect WebSocket reconnected; real-time updates resumed")
        # Whatever changed while the socket was down was never delivered, so take
        # a full snapshot rather than trusting the cache. Debounced, because a
        # console that completes the upgrade and drops the socket straight away
        # reconnects on every backoff tick, and an unbounded resync here would
        # buy a snapshot per flap — the amplification the frame paths are already
        # throttled against. Nothing cancels the debouncer any more (``_publish``
        # replaced ``async_set_updated_data``, which used to), so a deferred call
        # is no longer droppable and the resync always lands.
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_ws_frame(self, frame: dict[str, Any]) -> None:
        """Apply an alarm-hub frame to the cached state, or resync if we cannot.

        An ``update`` for a hub we already hold carries just the fields that
        changed, so merging it in and publishing synchronously reproduces every
        edge the hub reported: a zone that opens and closes within a couple of
        seconds still lands as two state changes at the right times. Re-polling
        instead would lose them — the request-refresh debouncer holds a
        ten-second cooldown, and the snapshot it eventually fetched would show
        the zone closed again, as if nothing had happened.

        Anything else needs a full snapshot, and the two kinds ask for one
        differently. A hub added or removed is a rare, discrete event that a
        single request settles, so it is spent immediately. An id we do not
        hold, an item we cannot read, a frame type we do not know: those are
        standing conditions — the console reports the same id, in the same
        shape, under the same type, for every frame it sends — so they go
        through ``_request_snapshot_throttled`` instead of asking forever at
        the debouncer's ten-second floor.
        """
        frame_type = frame.get("type")
        if frame_type in ("add", "remove"):
            # A hub adopted or removed. Rare, and worth a request at once: a
            # newly adopted hub appearing within ten seconds instead of five
            # minutes is the behaviour someone is standing there waiting for.
            self._request_snapshot()
            return
        if frame_type != "update":
            # A type this integration was not written against, or a frame with
            # no type at all. Naming the two that earn the immediate request is
            # the point: gating on "not an update" put every unknown type
            # through that door, so a console sending a keepalive every ten
            # seconds spent a REST GET on each one.
            self._request_snapshot_throttled()
            return
        item = frame.get("item")
        if not isinstance(item, dict):
            self._request_snapshot_throttled()
            return
        hub_id = item.get("id")
        if not isinstance(hub_id, str):
            # The console's payload is untrusted input: an id we cannot use as
            # a dict key (a list, say) would otherwise raise in the WS reader.
            self._request_snapshot_throttled()
            return
        if self._rest_in_flight:
            # Newer than the snapshot already on its way, so keep it to replay
            # over the reply; without that the reply reverts it silently.
            #
            # Buffered before the cache is consulted, because whether a delta
            # matters is a question about the incoming snapshot rather than
            # about what we hold now. An id missing from the cache may be the
            # hub that snapshot is introducing, and a delta that moves nothing
            # here may be the correction that undoes one already buffered.
            # ``replay_deltas`` decides against the snapshot, which is the only
            # place the question can be answered. Traffic that turns out not to
            # matter does spend slots, but overflow is no longer silent -- an
            # eviction forces a resync.
            self._buffer_delta(hub_id, item)
        hubs = self.data
        if not hubs or hub_id not in hubs:
            # Usually a second linkstation that /v1/alarm-hubs filters out, or
            # one this handler dropped itself for the same reason. Either way
            # the id keeps arriving and no snapshot will ever contain it.
            self._request_snapshot_throttled()
            return
        current = hubs[hub_id]
        updated = current.with_delta(item)
        if updated != current:
            hubs = {**hubs, hub_id: updated}
            if not updated.is_alarm_hub:
                # The filter the poll path applies, applied here too: a device
                # that stopped being an alarm hub goes away instead of staying
                # live and reporting until a poll drops it.
                del hubs[hub_id]
            self._publish(hubs)
            return
        # Nothing we model moved. That is usually a housekeeping frame (uptime
        # and friends) or the hub re-reporting a status it already holds, both
        # routine traffic that has to cost nothing. It can also mean the console
        # sends hub state in a shape we do not parse, so keep a safety net — on
        # the same throttle, since that condition stands until someone changes
        # the code and re-asking at frame rate diagnoses it no better.
        if "alarmHub" not in item and "state" not in item:
            return
        if self._request_snapshot_throttled():
            _LOGGER.debug("Alarm-hub frame matched nothing we parse: %s", item)

    @callback
    def _buffer_delta(self, hub_id: str, item: dict[str, Any]) -> None:
        """Hold a delta that raced the in-flight request, to replay over its reply."""
        if len(self._pending_deltas) == MAX_PENDING_DELTAS:
            # The append below drops the oldest entry — the first zone that
            # tripped — and the reply is about to revert it. Record that so the
            # request can follow itself with a snapshot taken after the loss.
            self._deltas_evicted = True
        self._pending_deltas.append((hub_id, item))

    @callback
    def _request_snapshot(self) -> None:
        """Ask for a full REST snapshot, debounced, for a frame we cannot apply."""
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _request_snapshot_throttled(self) -> bool:
        """Ask for a snapshot for a standing condition, at most once per interval.

        The debouncer alone does not bound these: its cooldown only spaces them
        ten seconds apart, and a condition that never clears then asks forever —
        a console reporting a hub the REST filter drops turns a five-minute poll
        into sixty. Returns whether this call spent a request, so a caller can
        keep its own diagnostics to the same rhythm.
        """
        now = self.hass.loop.time()
        if not fallback_resync_is_due(self._last_fallback_resync, now):
            return False
        self._last_fallback_resync = now
        self._request_snapshot()
        return True

    @callback
    def _publish(self, hubs: dict[str, AlarmHub]) -> None:
        """Publish pushed state to entities without disturbing the poll schedule.

        Deliberately not ``async_set_updated_data``: that re-arms the
        ``SCAN_INTERVAL`` timer and cancels the request-refresh debouncer on
        every call. At frame rate the first starves the reconciling poll — the
        only thing that heals a frame we never got, or a zone the hub deleted —
        and the second disarms the cooldown that bounds fallback REST traffic.
        A push is not a poll: it neither counts as one nor postpones one.

        ``last_update_success`` is left alone for the same reason, and it costs
        something real: while REST is failing every entity reads unavailable
        even though deltas are still arriving. Writing it was worse. It is the
        flag Home Assistant latches on — after an auth failure it deliberately
        does not re-arm the poll timer, so a frame that set it True would leave
        the integration reporting hours-old values as live with nothing left to
        correct it — and it is also the log-once guard, so resetting it turns
        one standing REST outage into an error line per failed poll. Staying
        available while push is live is an entity-level statement ("the socket
        is delivering"), not a claim that a poll succeeded; expressing it
        belongs in entity.py, which a later stage owns.
        """
        self.data = hubs
        self.async_update_listeners()

    async def async_shutdown(self) -> None:
        """Cancel the WS task and shut the coordinator down cleanly."""
        if self._ws_task is not None:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
        await super().async_shutdown()
