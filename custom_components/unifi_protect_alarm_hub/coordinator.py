"""Data update coordinator for the UniFi Protect Alarm Hub.

Real-time updates come from the Protect devices WebSocket (see
``AlarmHubApiClient.async_subscribe_devices``), and a frame arrives as one of
two things. A frame that carries hub state is a delta: it is merged into the
cache and published synchronously, so every edge it describes reaches entities
at the moment it was reported, with no request at all. A frame that carries
none is a notification -- it says something happened on that hub and nothing
whatever about what -- and the only way to learn what is to read the hub over
REST, promptly, while whatever tripped is still tripped. That is not the
degenerate case it sounds like: on the one UP-AlarmHub-Kit console anyone has
measured it is *every* frame, a bare ``lastEvent`` timestamp beside the id.
REST polling at ``SCAN_INTERVAL`` is the reconciling fallback (and the initial
load), and a reconnect resyncs in full.

Push and poll race, so the two paths are kept from overwriting each other. A
snapshot describes the console as it was when its request went out, which makes
anything that lands while it is in flight newer than the reply -- and the two
kinds of frame need different things done about that. A delta is buffered and
replayed over the snapshot before it is published. A notification has no state
in it to replay; what it has is a claim that the console moved on after the
request went out, so the reply cannot be its answer and a read of its own is
still owed. Both are settled from the same ``finally``, on every ending the
request has -- though "settled" means the next read is *considered*, not that a
lost event is recovered: a notification read that itself fails clears the claim
without answering it, and the event waits for the next frame or the poll.
Released v0.2 lost it the same way, and by the time a retry could land the pulse
it reported is over.

A *delta* never touches the poll schedule, because the poll is what heals a
frame we never received -- a chatty hub must not be able to postpone it. A
notification does postpone it, and has to: it is answered by a full snapshot,
and re-arming the timer after one is the same thing the poll path does when it
takes one. Each postponement is a snapshot, so nothing goes unreconciled.

A frame the cache cannot absorb falls back to a snapshot, and how eagerly
depends on whether the thing that caused it will ever stop. A hub being adopted
or removed happens once and is settled by one request. A notification happens
once too, but its answer expires while you wait, so it is read at once and
coalesced rather than throttled (see ``NOTIFY_READ_COOLDOWN``). An id we do not
hold, a payload in a shape we cannot parse, a frame type this integration was
not written against: those are properties of the console rather than events --
each repeats for every frame it ever sends -- so they share a long throttle.
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
# without bound. Every update that could move the cache takes a slot, whether or
# not it turns out to (see ``_on_ws_frame``), and overflow is not free -- the
# dropped frame is an edge nobody re-reports, so an eviction forces one snapshot
# once the request it raced has landed.
MAX_PENDING_DELTAS = 64

# The item keys ``AlarmHub.from_json`` reads, minus ``id`` -- a frame is looked
# up by its id, so merging that back changes nothing. A frame carrying none of
# these cannot change the hub the cache holds: everything else in the payload is
# kept verbatim in ``raw`` and never parsed, so it cannot move anything now or
# become a merge base that moves something later.
#
# Being the parser's own list rather than a guess about which keys look
# important is what makes it safe to skip the buffer on the strength of it -- a
# delta left unbuffered is reverted, silently, by the reply it raced, and that
# is not a mistake worth risking on a hunch. It does mean the two files have to
# stay in step, so ``test_the_buffer_gate_knows_every_field_the_parser_reads``
# reads the list back out of ``from_json`` and fails if a field is added there
# and not here.
HUB_STATE_KEYS = frozenset({"name", "mac", "state", "isAlarmHub", "alarmHub"})

# Minimum gap between the snapshots the fallback paths spend (seconds). They all
# guard standing conditions -- an id the REST filter never returns, hub state in
# a shape we do not parse -- rather than events: asking once every few minutes
# diagnoses them as well as asking once per frame, and costs nothing in between.
FALLBACK_RESYNC_INTERVAL = 300.0

# How long after a notification read finishes before the next one may start
# (seconds).
#
# Notifications are not a standing condition and must not share the throttle
# above: each one is a discrete event, and its read is only true while the thing
# that caused it is still happening. The captured door pairs were about two
# seconds apart and the pulses behind #3 ran three to five, so the whole budget
# is a fraction of a second plus one LAN round trip -- ``REQUEST_TIMEOUT`` calls
# a healthy one well under a second. Ten seconds, which is what
# ``async_request_refresh`` costs, is not a slower version of this: it is long
# enough that the snapshot finds the door shut again, which is the bug.
#
# Measured from the *end* of the read, not from the frame that asked for it.
# That is the whole difference between a bound and a wish. Armed at arrival --
# which is what ``Debouncer(immediate=True)`` does, and what this used to be --
# the cooldown expires underneath any read longer than itself, and the console
# then sets the pace: against a sustained frame stream reads started 0.515s
# apart whatever they cost, which measured 2.00 GET/s at an 82% REST duty cycle
# with a 0.45s read -- and 0.303s apart, 3.31 GET/s at 94%, once the stream was
# fast enough to overrun the delta buffer as well and buy an eviction resync per
# read. A security integration holding the console's REST endpoint busy more or
# less permanently, with the coordinator's refresh lock saturated behind it.
# Armed at completion, a read and a cooldown strictly alternate, so the
# period is one read plus this and the ceiling is
# ``1 / (read + NOTIFY_READ_COOLDOWN)`` however fast the console talks. Measured
# on the same stream, consecutive reads now start 0.501s apart against an
# instant read (2.00 GET/s), 0.803s apart against a 0.3s read (1.25 GET/s) and
# 0.952s apart against a 0.45s read (1.05 GET/s) -- and the buffer-overrunning
# stream measures the same 1.25 GET/s as the ordinary one, because a
# notification no longer takes a delta slot. 2.00 GET/s is this path's ceiling,
# reached only by a console that answers instantly -- not the integration's whole
# REST rate. A stream of *state-carrying* frames fast enough to overrun
# ``MAX_PENDING_DELTAS`` inside one read window still buys an eviction resync per
# read, measured at 2.5 GET/s. No frame the captured hardware sends can do that,
# because every one of them is a notification and takes no slot.
#
# A frame arriving on a quiet hub is still read at once -- the request is on the
# wire before ``_on_ws_frame`` returns -- and a burst still costs two reads: the
# immediate one and one trailing read covering the rest of the burst.
NOTIFY_READ_COOLDOWN = 0.5


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


def carries_hub_state(item: dict[str, Any]) -> bool:
    """Whether a frame could move the cached hub, and so has an edge worth keeping.

    True of anything naming a field the parser reads (see ``HUB_STATE_KEYS``),
    whether or not that field turns out to differ -- that question belongs to
    the snapshot coming back, not to what we hold now. False of the frame the
    measured console actually sends, which is an id, a modelKey and a
    ``lastEvent`` timestamp: there is no state in it to lose to an eviction, so
    it must not spend a buffer slot that a real delta may need.
    """
    return not HUB_STATE_KEYS.isdisjoint(item)


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
        # ``config_entry`` explicitly, not by way of the ``current_entry``
        # ContextVar the base class falls back to. That ContextVar is set only
        # while HA is running setup, and ``_async_refresh`` escalates an auth
        # failure with ``if self.config_entry: async_start_reauth(...)`` -- so a
        # coordinator built outside that window silently loses the escalation,
        # and every test that built one proved nothing about it.
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
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
        # The notification path, in three fields (see ``_pump_notify_read``):
        # whether a frame is still owed a read that postdates it, whether the
        # read it is owed has already been queued, and the cooldown, whose
        # timer being alive is the whole of "too soon".
        #
        # Deliberately not a ``Debouncer``. Two of its properties are wrong
        # here, and neither is configurable. ``immediate=True`` arms the
        # cooldown when the frame arrives, so a read longer than the cooldown
        # outlives it: the timer fires, ``_handle_timer_finish`` finds the
        # execute lock held, clears the pending flag and returns, and nothing
        # re-arms -- the frame is gone, silently, and the longer the read the
        # wider that window. And ``_schedule_timer`` never cancels the handle it
        # replaces, so a frame arriving during a read and that read's own
        # ``finally`` each arm one; three live cooldown timers were measured on
        # a sustained stream, driving reads 0.5s apart whatever the read cost.
        self._notify_pending = False
        self._notify_read_queued = False
        self._notify_timer: asyncio.TimerHandle | None = None

    async def _async_update_data(self) -> dict[str, AlarmHub]:
        self._pending_deltas.clear()
        self._deltas_evicted = False
        is_eviction_resync = self._eviction_resync_pending
        self._eviction_resync_pending = False
        # The request goes out below, so its reply postdates every notification
        # received up to this line and is the answer to all of them. Cleared
        # here rather than where the read was asked for, so that a scheduled
        # poll, a reconnect resync or an add/remove snapshot settles a pending
        # notification too: they are the same full snapshot, and a second read
        # queued behind one of them would buy nothing.
        self._notify_pending = False
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
            # A notification that arrived while this request was in flight is
            # newer than the reply, so the reply is not its answer and a read of
            # its own is still owed. This is the one place that runs on every
            # ending -- reply, failure, refused key, cancellation -- which is
            # exactly why the follow-up is issued from here rather than left to
            # a cooldown timer armed when the frame arrived: that timer fires
            # while this request still holds the lock, and nothing re-arms it.
            self._pump_notify_read()
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
                # The key was revoked or rotated, so reconnecting cannot fix it:
                # ask the user for a new one instead of waiting up to five
                # minutes for the REST poll to reach the same conclusion. HA
                # drops the request if a reauth or reconfigure flow for this
                # entry is already open, so the retry loop cannot stack them up,
                # and the loop keeps running -- a key repaired by that flow
                # reloads the entry from under it.
                self.entry.async_start_reauth(self.hass)
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
        # Logged on every connect, not just a recovery: a healthy socket on a
        # quiet hub says nothing at all, so without this there is no way to tell
        # it apart from one that never opened -- which is the first thing to
        # establish when a frame someone expected did not arrive.
        _LOGGER.debug("UniFi Protect WebSocket connected")
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
        # throttled against.
        #
        # A deferred resync can still be cancelled: ``_async_refresh`` calls
        # ``self._debounced_refresh.async_cancel()`` at the top of every run,
        # notification reads included. It lands anyway, and not because nothing
        # cancels it — the thing that cancelled it is a full snapshot taken
        # after the reconnect, which is all the resync was ever for. What must
        # not happen is the deferred call being dropped by something that reads
        # nothing, and that is what ``_publish`` fixed: the delta path used to
        # call ``async_set_updated_data``, which cancels the debouncer without
        # going near the console.
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_ws_frame(self, frame: dict[str, Any]) -> None:
        """Apply an alarm-hub frame to the cached state, or go and read it.

        An ``update`` for a hub we already hold *may* carry the fields that
        changed. Where it does, merging it in and publishing synchronously
        reproduces every edge the hub reported: a zone that opens and closes
        within a couple of seconds lands as two state changes at the right
        times, for no requests at all. Re-polling instead would lose them — the
        request-refresh debouncer holds a ten-second cooldown, and the snapshot
        it eventually fetched would show the zone closed again, as if nothing
        had happened.

        Where it does not — no ``alarmHub``, no ``state``, nothing but an id
        and a timestamp, which is every frame the measured UP-AlarmHub-Kit
        firmware sends — the frame is a notification and there is no delta to
        apply. Dropping it, which is what this used to do, put the console's
        only real signal on the five-minute poll. It buys a prompt REST read
        instead: see ``_request_snapshot_promptly``.

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
        if self._rest_in_flight and carries_hub_state(item):
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
            #
            # A notification is the one frame that provably cannot: there is no
            # field in it the parser reads, so the replay has nothing to apply
            # and an eviction loses nothing (see ``carries_hub_state``). It used
            # to take a slot anyway, and a console sending more than
            # MAX_PENDING_DELTAS of them inside one read window then bought an
            # extra, un-cooled-down GET for an edge that was never there.
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
        if "alarmHub" not in item and "state" not in item:
            # The frame said nothing about hub state at all, so there was never
            # a delta here to lose. This used to return, on the reasoning that a
            # frame touching no state we model cannot be reporting a change to
            # it — true of a housekeeping frame, and false of the only frame a
            # real console sends. A UP-AlarmHub-Kit reports a door opening as
            # ``{"item": {"lastEvent": <ms>, "id": ..., "modelKey":
            # "linkstation"}, "type": "update"}`` and nothing else, in pairs a
            # couple of seconds apart as the door opens and shuts, so returning
            # here dropped every event the hardware has and left the five-minute
            # poll to notice — worse than the v0.2 behaviour it replaced.
            #
            # We cannot tell that apart from genuine housekeeping, and must not
            # try: guessing wrong costs an alarm panel a missed zone, while
            # guessing the other way costs a bounded GET. So ask, and ask fast.
            #
            # A wider test than ``carries_hub_state`` uses above, on purpose,
            # because it is a different question. That one asks whether the
            # parser reads any of these fields, and can answer for certain. This
            # one asks whether the console is telling us something happened, and
            # cannot: a frame carrying an ``uptime`` and nothing else is beyond
            # the reach of both, and gets no buffer slot (it provably moves
            # nothing) and a read anyway (it might mean everything).
            self._request_snapshot_promptly()
            return
        # The frame did carry hub state, and merging it moved nothing — so the
        # console has just told us the state and it matches. Nothing happened
        # that we cannot already see, which is why this one does *not* take the
        # notification path: it is the hub re-reporting a status it holds,
        # routine traffic that has to cost nothing. It can also mean the console
        # sends that state in a shape we do not parse, so keep a safety net — on
        # the fallback throttle, since that condition stands until someone
        # changes the code and re-asking at frame rate diagnoses it no better.
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
    def _request_snapshot_promptly(self) -> None:
        """Read the hub now, because a frame said something happened on it.

        Latency is the whole value of this path. A console that sends no state
        leaves the REST read as the only way to learn what changed, and the
        answer stops being true the moment the zone settles: a read that starts
        within the cooldown and finishes in a LAN round trip catches a two- or
        three-second door pulse, and one deferred to ``async_request_refresh``'s
        ten seconds does not — that is #3, and it is why this does not simply
        reuse it.

        Recording the frame and pumping is all this does: whether a read can
        start now, has to wait for the one already running, or has to wait for a
        cooldown is ``_pump_notify_read``'s question, asked again from every
        place an answer can change.
        """
        self._notify_pending = True
        self._pump_notify_read()

    @callback
    def _pump_notify_read(self) -> None:
        """Start the read a notification is owed, or leave it to whoever can.

        Called from the four places the answer can change: a frame arriving, a
        REST request ending, a notification read ending, and the cooldown after
        one lapsing. Each of the guards below names something that will pump
        again when it clears, so a frame recorded here is never left waiting on
        nobody -- which is the hole this replaced. Together they also make the
        rate a fact rather than a hope: a read is queued only with no read
        running, none queued and no cooldown live, and every read this path
        starts arms a cooldown as it ends, so reads and cooldowns on this path
        strictly alternate and the ceiling is one per
        read-plus-``NOTIFY_READ_COOLDOWN``.

        A read this path did *not* start -- the scheduled poll, a reconnect
        resync -- arms nothing, so a notification that raced one is answered the
        moment it lands rather than a cooldown later. That is the right way
        round (the frame is already as old as that request) and it cannot become
        a rate: those requests have bounds of their own, five minutes and ten
        seconds respectively.

        Coalescing falls out of ``_notify_pending`` being one flag: two hundred
        frames inside a read window are two hundred writes of ``True`` and one
        follow-up read, which is the same snapshot every one of them wanted.
        """
        if self._shutdown_requested or not self._notify_pending:
            return
        if self._notify_read_queued:
            # A read is on its way to the lock; its own ``finally`` pumps.
            return
        if self._rest_in_flight:
            # A request is on the wire. Its reply predates this frame, so it is
            # not the answer -- but ``_async_update_data``'s ``finally`` pumps
            # on every ending it has, whatever the request does and however long
            # it takes. That is the fix for the read-longer-than-the-cooldown
            # window, and it needs no timer at all.
            return
        if self._notify_timer is not None:
            # Cooling down after the last read; the timer pumps when it lapses.
            return
        self._notify_read_queued = True
        # Eagerly started, so on a quiet hub the request is on the wire before
        # ``_on_ws_frame`` returns -- there is no timer between the frame and
        # the GET, which is the latency this whole path exists for.
        self.hass.async_create_task(self._notify_read())

    async def _notify_read(self) -> None:
        """One REST read for the notification path, then its cooldown.

        ``async_refresh`` rather than ``async_request_refresh``: the shared
        request-refresh debouncer holds a ten-second cooldown, longer than the
        events this path exists to catch. The ``finally`` runs on every ending,
        so a read that failed or was cancelled still arms the cooldown and still
        hands on to whatever arrived while it ran.

        A failing read is otherwise treated exactly like a successful one: the
        cooldown is the only spacing, so a console that keeps talking while REST
        is down is re-read at the full rate rather than backing off. Bounded, and
        it recovers the moment REST does -- but worth knowing, because a revoked
        key that somehow leaves the socket up is read at 2 GET/s until the key is
        replaced. HA dedupes the reauth flow, so it is traffic, not a loop.
        """
        try:
            await self.async_refresh()
        finally:
            self._notify_read_queued = False
            self._start_notify_cooldown()
            self._pump_notify_read()

    @callback
    def _start_notify_cooldown(self) -> None:
        """Bar the next notification read for ``NOTIFY_READ_COOLDOWN`` seconds.

        There is never one of these alive already, and that is a property of the
        pump rather than of this: a read is queued only when the timer is None,
        and only a queued read gets here. Which is exactly what
        ``Debouncer._schedule_timer`` lacked -- it arms without cancelling, and
        it is reached both by a frame arriving during a read and by that read's
        own ``finally``, so cooldowns accumulated (three were measured at once)
        and reads went through at the rate of whichever expired first.

        Not armed at all once shutdown has been requested. This runs from a
        ``finally``, so it is reached by a read that was still out when the
        entry unloaded -- and a timer armed there is the orphan that outlives
        the config entry holding a coordinator whose session is closing.
        """
        if self._shutdown_requested:
            return
        self._notify_timer = self.hass.loop.call_later(
            NOTIFY_READ_COOLDOWN, self._end_notify_cooldown
        )

    @callback
    def _end_notify_cooldown(self) -> None:
        """The cooldown lapsed: read again if a frame is still owed one."""
        self._notify_timer = None
        self._pump_notify_read()

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
        is delivering"), not a claim that a poll succeeded, so entity.py is
        where it is expressed: ``AlarmHubBaseEntity.available`` falls back to
        how recently state was delivered *for its own hub*, for one poll
        interval, and arms a timer so a socket that goes quiet retires the
        reading rather than stranding it on screen.
        """
        self.data = hubs
        self.async_update_listeners()

    async def async_shutdown(self) -> None:
        """Cancel the WS task and shut the coordinator down cleanly."""
        # First, and before the socket goes. The base class sets
        # ``_shutdown_requested``, which is what ``_pump_notify_read`` refuses
        # on, so a frame arriving while the WS task is still being cancelled
        # below cannot arm a cooldown timer with nothing left to cancel it or
        # schedule a read against a client whose session is on its way out.
        await super().async_shutdown()
        if self._notify_timer is not None:
            self._notify_timer.cancel()
            self._notify_timer = None
        self._notify_pending = False
        if self._ws_task is not None:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
