"""Tests for the coordinator: backoff helpers and WebSocket frame handling."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from copy import deepcopy
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.unifi_protect_alarm_hub import coordinator as coordinator_module
from custom_components.unifi_protect_alarm_hub import logic
from custom_components.unifi_protect_alarm_hub.api import (
    AlarmHubAuthError,
    AlarmHubConnectionError,
)
from custom_components.unifi_protect_alarm_hub.const import DOMAIN
from custom_components.unifi_protect_alarm_hub.coordinator import (
    BACKOFF_CAP,
    FALLBACK_RESYNC_INTERVAL,
    MAX_PENDING_DELTAS,
    WS_HEALTHY_UPTIME,
    WS_WARN_REARM_UPTIME,
    AlarmHubCoordinator,
    fallback_resync_is_due,
    hubs_by_id,
    next_backoff,
    replay_deltas,
    ws_is_healthy,
    ws_warning_is_rearmed,
)
from custom_components.unifi_protect_alarm_hub.models import AlarmHub


def test_next_backoff_grows_by_doubling():
    assert next_backoff(1.0) == 2.0
    assert next_backoff(2.0) == 4.0
    assert next_backoff(4.0) == 8.0
    assert next_backoff(8.0) == 16.0


def test_next_backoff_caps_at_max():
    assert next_backoff(32.0) == BACKOFF_CAP
    assert next_backoff(60.0) == BACKOFF_CAP
    assert next_backoff(1000.0) == BACKOFF_CAP
    assert BACKOFF_CAP == 60.0


def test_next_backoff_zero_or_negative_starts_at_one():
    # A reset (prev <= 0) should restart the ramp at the floor, not stay at 0.
    assert next_backoff(0.0) == 1.0
    assert next_backoff(-5.0) == 1.0


def test_ws_is_healthy_only_for_a_connection_that_lasted():
    assert ws_is_healthy(None, 1000.0) is False  # never got connected
    assert ws_is_healthy(1000.0, 1000.5) is False  # accepted, dropped at once
    assert ws_is_healthy(1000.0, 1000.0 + WS_HEALTHY_UPTIME) is True
    assert ws_is_healthy(1000.0, 5000.0) is True


def test_ws_warning_is_rearmed_asks_more_than_ws_is_healthy_does():
    # The flap guard's own threshold must not be a hole in it: a connection can
    # be long enough to restart the ramp and still too short to warn about.
    assert WS_WARN_REARM_UPTIME > WS_HEALTHY_UPTIME
    assert ws_warning_is_rearmed(None, 1000.0) is False
    assert ws_warning_is_rearmed(1000.0, 1000.0 + WS_HEALTHY_UPTIME) is False
    assert ws_warning_is_rearmed(1000.0, 1000.0 + WS_WARN_REARM_UPTIME) is True


def test_fallback_resync_is_due_only_once_the_interval_has_passed():
    assert fallback_resync_is_due(None, 1000.0) is True  # never asked
    assert fallback_resync_is_due(1000.0, 1000.5) is False  # frame rate
    assert fallback_resync_is_due(1000.0, 1000.0 + FALLBACK_RESYNC_INTERVAL) is True


# --- WebSocket frame handling ---

HUB_JSON = {
    "id": "ah1",
    "modelKey": "linkstation",
    "name": "Alarm Hub Kit",
    "mac": "AABBCCDDEEFF",
    "state": "CONNECTED",
    "isAlarmHub": True,
    "alarmHub": {
        "armed": "on",
        "input": {
            "4": {
                "enable": "on",
                "status": "normal",
                "inputType": "MOTION",
                "name": "Hallway",
            },
            "6": {
                "enable": "on",
                "status": "normal",
                "inputType": "ENTRY",
                "name": "Garage Entry",
            },
        },
    },
}


def _frame(zone: int, status: str, ftype: str = "update", hub_id: str = "ah1") -> dict:
    """A devices-WS frame carrying one zone's new status."""
    return {
        "type": ftype,
        "item": {
            "id": hub_id,
            "modelKey": "linkstation",
            "alarmHub": {"input": {str(zone): {"status": status}}},
        },
    }


def _hub_json(hub_id: str) -> dict[str, Any]:
    """A second console device of the same shape, sharing nothing with HUB_JSON."""
    return {**deepcopy(HUB_JSON), "id": hub_id, "name": f"Alarm Hub {hub_id}"}


def _ownership_frame(is_alarm_hub: bool, hub_id: str = "ah1") -> dict:
    """A frame changing only whether the console calls this device an alarm hub."""
    return {
        "type": "update",
        "item": {"id": hub_id, "modelKey": "linkstation", "isAlarmHub": is_alarm_hub},
    }


def _make_coordinator(hass, client) -> AlarmHubCoordinator:
    """A coordinator wired to ``client``, with nothing fetched yet."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "h",
            CONF_PORT: 443,
            CONF_API_KEY: "k",
            CONF_VERIFY_SSL: False,
        },
    )
    entry.add_to_hass(hass)
    return AlarmHubCoordinator(hass, entry, client)


@pytest.fixture
async def coordinator(hass):
    """A coordinator holding one hub, with a mocked client."""
    client = MagicMock()
    client.async_get_alarm_hubs = AsyncMock(return_value=[AlarmHub.from_json(HUB_JSON)])
    coord = _make_coordinator(hass, client)
    await coord.async_refresh()
    yield coord
    await coord.async_shutdown()


def test_replay_deltas_layers_a_frame_over_a_fresh_snapshot():
    snapshot = {"ah1": AlarmHub.from_json(HUB_JSON)}

    merged = replay_deltas(snapshot, [("ah1", _frame(6, "alarm")["item"])])

    assert merged["ah1"].alarm_hub_inputs[6].status == "alarm"
    assert merged["ah1"].alarm_hub_inputs[4].status == "normal"
    assert snapshot["ah1"].alarm_hub_inputs[6].status == "normal"  # not mutated


def test_replay_deltas_skips_a_hub_the_snapshot_does_not_carry():
    # REST decides which hubs exist: a delta must not resurrect one it dropped.
    snapshot = {"ah1": AlarmHub.from_json(HUB_JSON)}

    delta = _frame(6, "alarm", hub_id="gone")["item"]

    merged = replay_deltas(snapshot, [("gone", delta)])

    assert list(merged) == ["ah1"]


def test_replay_deltas_drops_a_hub_a_delta_disowns():
    snapshot = {"ah1": AlarmHub.from_json(HUB_JSON)}

    merged = replay_deltas(snapshot, [("ah1", {"id": "ah1", "isAlarmHub": False})])

    assert merged == {}


def test_replay_deltas_lets_a_later_delta_undo_an_earlier_disown():
    """A hub is judged on where its deltas leave it, not on the first of them.

    Dropping it the moment one frame says ``isAlarmHub: false`` also drops every
    frame behind it, so the console taking that back a moment later never lands
    and a pair that cancels out publishes the hub as gone.
    """
    snapshot = {"ah1": AlarmHub.from_json(HUB_JSON)}

    merged = replay_deltas(
        snapshot,
        [
            ("ah1", _ownership_frame(False)["item"]),
            ("ah1", _ownership_frame(True)["item"]),
        ],
    )

    assert list(merged) == ["ah1"]
    assert merged["ah1"].alarm_hub_inputs[6].name == "Garage Entry"


def test_hubs_by_id_indexes_the_alarm_hubs_a_snapshot_carries():
    hub = AlarmHub.from_json(HUB_JSON)
    not_a_hub = AlarmHub.from_json({**_hub_json("ah2"), "isAlarmHub": False})

    assert hubs_by_id([hub, not_a_hub]) == {"ah1": hub}


def test_hubs_by_id_skips_an_id_it_cannot_key_on():
    """One unreadable device costs its own entities, not the whole refresh.

    ``id`` is whatever the console put in the JSON, and here it is used as a
    dict key: a list there raised TypeError inside ``_async_update_data``, which
    is neither of the two exceptions that path maps, so a single malformed
    device failed the poll and reached the user as "Unexpected error fetching
    data" with a traceback and every hub gone. The WebSocket path has always
    guarded the same value.
    """
    good = AlarmHub.from_json(HUB_JSON)
    unusable = AlarmHub.from_json({**_hub_json("ah2"), "id": ["ah2"]})

    assert hubs_by_id([unusable, good]) == {"ah1": good}


async def test_a_hub_with_an_unusable_id_does_not_fail_the_poll(hass):
    """End to end: the rest of the console still gets through."""
    client = MagicMock()
    client.async_get_alarm_hubs = AsyncMock(
        return_value=[
            AlarmHub.from_json({**_hub_json("ah2"), "id": ["ah2"]}),
            AlarmHub.from_json(HUB_JSON),
        ]
    )
    coord = _make_coordinator(hass, client)

    await coord.async_refresh()

    assert coord.last_update_success is True
    assert list(coord.data) == ["ah1"]
    await coord.async_shutdown()


async def test_short_zone_pulse_survives_a_busy_hub(hass, coordinator):
    """Regression for #3: a brief door pulse must not be swallowed.

    Motion on the hallway zone three seconds before the door opened used to take
    the request-refresh debouncer's immediate slot; the open and the close then
    both fell inside its ten-second cooldown, and the single snapshot that
    followed read the door as closed again — so Home Assistant recorded nothing
    and automations never fired. Every edge must reach entities instead.
    """
    seen: list[bool] = []
    coordinator.async_add_listener(
        lambda: seen.append(
            logic.zone_is_on(coordinator.data["ah1"].alarm_hub_inputs[6])
        )
    )

    coordinator._on_ws_frame(_frame(4, "alarm"))  # 16:53:06 hallway motion
    coordinator._on_ws_frame(_frame(6, "alarm"))  # 16:53:09 entry opened
    coordinator._on_ws_frame(_frame(6, "normal"))  # 16:53:12 entry closed

    assert seen == [False, True, False]
    # ...and none of it needed a REST round trip (1 = the initial refresh).
    assert coordinator.client.async_get_alarm_hubs.await_count == 1


async def test_update_frame_keeps_the_rest_of_the_hub(coordinator):
    coordinator._on_ws_frame(_frame(6, "alarm"))

    hub = coordinator.data["ah1"]
    assert hub.alarm_hub_inputs[6].status == "alarm"
    assert hub.alarm_hub_inputs[6].name == "Garage Entry"
    assert hub.alarm_hub_inputs[4].status == "normal"
    assert hub.alarm_hub_armed == "on"
    assert hub.name == "Alarm Hub Kit"


async def test_unknown_hub_id_falls_back_to_a_refresh(hass, coordinator):
    coordinator._on_ws_frame(_frame(6, "alarm", hub_id="somebody-elses-hub"))
    await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count == 2


@pytest.mark.parametrize("ftype", ["add", "remove"])
async def test_add_and_remove_frames_fall_back_to_a_refresh(hass, coordinator, ftype):
    # A hub appearing or going away changes more than the cache can express.
    coordinator._on_ws_frame(_frame(6, "alarm", ftype=ftype))
    await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count == 2


async def test_frame_without_a_usable_item_falls_back_to_a_refresh(hass, coordinator):
    coordinator._on_ws_frame({"type": "update", "item": {"modelKey": "linkstation"}})
    await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count == 2


async def test_housekeeping_frame_costs_nothing(hass, coordinator):
    # Frames that touch none of the hub state we model must not cause a request.
    coordinator._on_ws_frame(
        {
            "type": "update",
            "item": {"id": "ah1", "modelKey": "linkstation", "uptime": 12345},
        }
    )
    await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count == 1


async def test_unparsed_hub_payload_falls_back_to_a_refresh(hass, coordinator):
    """A frame carrying hub state we cannot read must not be silently dropped.

    The merge assumes the WebSocket sends the same shape as the REST snapshot.
    If a console ever disagrees, re-polling keeps the entity correct instead of
    leaving it stale until the next scheduled poll.
    """
    coordinator._on_ws_frame(
        {
            "type": "update",
            "item": {
                "id": "ah1",
                "modelKey": "linkstation",
                "alarmHub": {"inputs": [{"id": 6, "status": "alarm"}]},
            },
        }
    )
    await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count == 2


async def test_reconnect_resyncs(hass, coordinator):
    """Frames sent while the socket was down are gone: resync in full."""
    coordinator._ws_down = True
    coordinator._ws_backoff = BACKOFF_CAP

    coordinator._on_ws_connected()
    await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count == 2
    # The uptime clock starts now; whether this connection counts as a recovery
    # is decided by how long it lasts (see ws_is_healthy).
    assert coordinator._ws_connected_at is not None


async def test_a_flapping_console_does_not_buy_a_snapshot_per_reconnect(
    hass, coordinator
):
    """A socket that opens and dies repeatedly must not drive REST at flap rate.

    The reconnect resync is the one REST path with a real reason to skip the
    frame throttle, so its bound is the debouncer: a console that completes the
    upgrade and drops it immediately reconnects on every backoff tick, and one
    full snapshot per flap is the amplification every other path is bounded
    against.
    """
    before = coordinator.client.async_get_alarm_hubs.await_count

    for _ in range(12):
        coordinator._ws_down = True
        coordinator._on_ws_connected()
        await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count - before <= 2


async def test_shutdown_cancels_the_listener(hass):
    """Unload must leave no listener running: nothing else cancels this task."""
    started = asyncio.Event()

    async def _listen(_on_frame, _on_connected=None):
        started.set()
        await asyncio.Event().wait()

    client = MagicMock()
    client.async_get_alarm_hubs = AsyncMock(return_value=[])
    client.async_subscribe_devices = AsyncMock(side_effect=_listen)
    coord = _make_coordinator(hass, client)
    await coord.async_refresh()
    coord.start_ws()
    async with asyncio.timeout(5):
        await started.wait()
    task = coord._ws_task

    await coord.async_shutdown()

    assert task.done()
    assert coord._ws_task is None


async def test_the_first_connect_does_not_resync(hass, coordinator):
    """Setup polled moments ago, so the opening connect has missed nothing."""
    coordinator._on_ws_connected()
    await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count == 1
    assert coordinator._ws_connected_at is not None


async def test_reconnect_resync_is_not_swallowed_by_the_delta_behind_it(
    hass, coordinator, freezer
):
    """The resync recovers what the outage swallowed, so it must always land.

    A live cooldown defers it rather than dropping it: the debouncer is what
    bounds a flapping console to one snapshot per cooldown, and nothing cancels
    it any more — ``_publish`` replaced the ``async_set_updated_data`` call that
    used to. A delta arriving right behind the reconnect must not change that.
    """
    # Something has already spent the debouncer's immediate slot.
    coordinator._on_ws_frame(_frame(6, "alarm", hub_id="somebody-elses-hub"))
    await hass.async_block_till_done()
    polls = coordinator.client.async_get_alarm_hubs.await_count

    coordinator._ws_down = True
    coordinator._on_ws_connected()
    await hass.async_block_till_done()
    # ...and an ordinary delta lands right behind the reconnect.
    coordinator._on_ws_frame(_frame(6, "alarm"))
    await hass.async_block_till_done()

    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count == polls + 1


async def test_frame_with_an_unusable_id_falls_back_to_a_refresh(hass, coordinator):
    """An id that is not a string would raise where we look the hub up."""
    coordinator._on_ws_frame(
        {"type": "update", "item": {"id": ["ah1"], "modelKey": "linkstation"}}
    )
    await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count == 2


async def test_a_hub_that_stops_being_an_alarm_hub_is_dropped(hass, coordinator):
    """The poll path filters on isAlarmHub; the push path must agree."""
    coordinator._on_ws_frame(
        {
            "type": "update",
            "item": {"id": "ah1", "modelKey": "linkstation", "isAlarmHub": False},
        }
    )
    await hass.async_block_till_done()

    assert "ah1" not in coordinator.data


async def test_idempotent_frames_do_not_drive_rest_traffic(hass, coordinator, freezer):
    """A hub re-reporting a status it already holds is routine traffic.

    It reaches the unparsed-payload safety net — the merge changes nothing —
    and used to spend a REST request every time: the real deltas in between
    cancelled the debouncer that was meant to bound it, and even on its own the
    net asks again the moment the ten-second cooldown lapses.
    """
    idempotent = {
        "type": "update",
        "item": {"id": "ah1", "modelKey": "linkstation", "alarmHub": {"armed": "on"}},
    }
    polls = coordinator.client.async_get_alarm_hubs.await_count

    for i in range(20):  # a few minutes of hub chatter, ten seconds apart
        coordinator._on_ws_frame(_frame(6, "alarm" if i % 2 else "normal"))
        await hass.async_block_till_done()
        coordinator._on_ws_frame(idempotent)
        await hass.async_block_till_done()
        freezer.tick(timedelta(seconds=11))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count - polls <= 1


async def test_unparsed_payload_is_asked_about_again_after_the_throttle(
    hass, coordinator, freezer
):
    """The safety net keeps working, just on its own clock rather than the hub's."""
    unparsed = {
        "type": "update",
        "item": {
            "id": "ah1",
            "modelKey": "linkstation",
            "alarmHub": {"inputs": [{"id": 6, "status": "alarm"}]},
        },
    }
    coordinator._on_ws_frame(unparsed)
    await hass.async_block_till_done()
    coordinator._on_ws_frame(unparsed)
    await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count == 2

    # A few minutes later, with the shape still unparsed, it asks once more.
    freezer.tick(timedelta(seconds=FALLBACK_RESYNC_INTERVAL + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    coordinator._on_ws_frame(unparsed)
    await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count == 3


async def test_a_stream_of_deltas_does_not_postpone_the_scheduled_poll(
    hass, coordinator, freezer
):
    """The poll is what heals a frame we never got, so pushes must not delay it."""
    coordinator.async_add_listener(lambda: None)
    polls = coordinator.client.async_get_alarm_hubs.await_count

    for i in range(6):  # twelve minutes, a real delta every two
        freezer.tick(timedelta(minutes=2))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        coordinator._on_ws_frame(_frame(6, "alarm" if i % 2 else "normal"))
        await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count - polls >= 2


async def test_ws_auth_rejection_is_reported_and_retried(hass, coordinator, caplog):
    """A rejected key ends the socket; the loop reports it and keeps retrying."""
    coordinator.client.async_subscribe_devices = AsyncMock(
        side_effect=AlarmHubAuthError("Auth failed (401)")
    )

    task = hass.async_create_task(coordinator._ws_listen())
    for _ in range(5):
        await asyncio.sleep(0)
    still_listening = not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert still_listening
    assert "rejected the API key" in caplog.text


# --- Deltas racing a REST snapshot ---


def _gated_client(gate: asyncio.Event) -> MagicMock:
    """A client whose snapshot resolves only once ``gate`` is set.

    It always returns the hub as HUB_JSON describes it, which is the point: a
    console captures its snapshot when the request arrives, so a reply that
    lands later still describes the world as it was before anything happened.
    """
    client = MagicMock()

    async def snapshot():
        await gate.wait()
        return [AlarmHub.from_json(HUB_JSON)]

    client.async_get_alarm_hubs = AsyncMock(side_effect=snapshot)
    return client


async def test_delta_survives_a_snapshot_that_was_already_in_flight(hass):
    """A frame that lands mid-request is newer than the reply, and must win.

    Otherwise the zone silently reverts when the reply arrives; worse, it never
    settles on the new value, so Home Assistant records no state change at all
    and nothing an automation listens for ever fires.
    """
    gate = asyncio.Event()
    coord = _make_coordinator(hass, _gated_client(gate))
    gate.set()
    await coord.async_refresh()

    gate.clear()
    refresh = hass.async_create_task(coord.async_refresh())
    await asyncio.sleep(0)  # let it reach the awaited request
    coord._on_ws_frame(_frame(6, "alarm"))
    assert coord.data["ah1"].alarm_hub_inputs[6].status == "alarm"

    gate.set()
    await refresh

    assert coord.data["ah1"].alarm_hub_inputs[6].status == "alarm"
    # Only the raced zone is held over: the rest of the snapshot is authoritative.
    assert coord.data["ah1"].alarm_hub_inputs[4].status == "normal"
    await coord.async_shutdown()


async def test_a_buffered_delta_does_not_replay_onto_a_later_snapshot(hass):
    """The buffer covers exactly the request it raced, and no later one.

    By the next poll the console has already said what the zone is doing now,
    so replaying an old delta over that would revert real state.
    """
    gate = asyncio.Event()
    coord = _make_coordinator(hass, _gated_client(gate))
    gate.set()
    await coord.async_refresh()

    gate.clear()
    refresh = hass.async_create_task(coord.async_refresh())
    await asyncio.sleep(0)
    coord._on_ws_frame(_frame(6, "alarm"))
    gate.set()
    await refresh
    assert coord.data["ah1"].alarm_hub_inputs[6].status == "alarm"

    # The zone closed again, and the next poll is the first to hear about it.
    coord.client.async_get_alarm_hubs = AsyncMock(
        return_value=[AlarmHub.from_json(HUB_JSON)]
    )
    await coord.async_refresh()

    assert coord.data["ah1"].alarm_hub_inputs[6].status == "normal"
    await coord.async_shutdown()


async def test_a_failed_snapshot_leaves_the_delta_buffer_empty(hass):
    """A request that failed still owned the buffer: nothing carries over."""
    gate = asyncio.Event()
    coord = _make_coordinator(hass, _gated_client(gate))
    gate.set()
    await coord.async_refresh()

    async def failing_snapshot():
        await gate.wait()
        raise AlarmHubConnectionError("console went away")

    gate.clear()
    coord.client.async_get_alarm_hubs = AsyncMock(side_effect=failing_snapshot)
    refresh = hass.async_create_task(coord.async_refresh())
    await asyncio.sleep(0)
    coord._on_ws_frame(_frame(6, "alarm"))
    gate.set()
    await refresh

    assert not coord._pending_deltas
    # The failed poll left the cache alone, so the delta is already in it.
    assert coord.data["ah1"].alarm_hub_inputs[6].status == "alarm"
    await coord.async_shutdown()


async def test_a_pushed_frame_reaches_the_cache_without_faking_a_poll(
    hass, coordinator
):
    """Push updates the cache; it does not get to say the REST poll succeeded.

    ``last_update_success`` is Home Assistant's record of the last poll, and it
    latches: after an auth failure the interval is deliberately left unarmed, so
    a frame that flipped it True would leave the integration reporting ever
    older REST state as live with nothing scheduled to correct it. The price is
    that entities read unavailable while REST is down even though deltas keep
    arriving — an entity-level statement that entity.py owns, not this.
    """
    coordinator.client.async_get_alarm_hubs = AsyncMock(
        side_effect=AlarmHubConnectionError("console went away")
    )
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False

    for i in range(10):
        coordinator._on_ws_frame(_frame(6, "alarm" if i % 2 else "normal"))

    assert coordinator.last_update_success is False
    assert coordinator.data["ah1"].alarm_hub_inputs[6].status == "alarm"


async def test_pushed_frames_do_not_silence_the_rest_failure_log(
    hass, coordinator, caplog
):
    """``last_update_success`` is also HA's log-once latch for a standing outage.

    ``_async_refresh`` logs a failure only ``if self.last_update_success``, so
    resetting it from the push path turned one console reboot into an ERROR per
    failed poll — a wall of identical lines around the one that mattered.
    """
    coordinator.client.async_get_alarm_hubs = AsyncMock(
        side_effect=AlarmHubConnectionError("console went away")
    )

    for _ in range(7):  # half an hour of failed polls with the hub still chatty
        await coordinator.async_refresh()
        coordinator._on_ws_frame(_frame(6, "alarm"))
        coordinator._on_ws_frame(_frame(6, "normal"))

    errors = [rec for rec in caplog.records if rec.levelno == logging.ERROR]
    assert len(errors) == 1


async def test_frames_cannot_revive_a_run_stranded_by_an_auth_failure(
    hass, coordinator, freezer
):
    """After an auth failure there is no poll left to undo the failure with.

    ``_async_refresh`` re-arms the interval only ``if not auth_failed``, so once
    a 401 has landed the WebSocket is the only thing still running. It must not
    be able to report success on its own: that is how two hours of frames used
    to leave every entity presenting two-hour-old REST values as live.
    """
    coordinator.async_add_listener(lambda: None)
    coordinator.client.async_get_alarm_hubs = AsyncMock(
        side_effect=AlarmHubAuthError("Auth failed (401)")
    )
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False
    polls = coordinator.client.async_get_alarm_hubs.await_count

    for _ in range(24):  # two hours of hub chatter and no working poll
        coordinator._on_ws_frame(_frame(6, "alarm"))
        coordinator._on_ws_frame(_frame(6, "normal"))
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert coordinator.client.async_get_alarm_hubs.await_count == polls


async def test_the_pending_delta_buffer_is_bounded_and_overflow_forces_a_resync(hass):
    """A request that hangs against a chatty console must not grow the buffer.

    Overflow is not free, though: the evicted frame is an edge nobody will
    re-report, and the reply about to be published predates it, so the cache
    silently goes wrong until a poll up to five minutes away. The request that
    lost it has to follow itself with a snapshot instead.
    """
    gate = asyncio.Event()
    coord = _make_coordinator(hass, _gated_client(gate))
    gate.set()
    await coord.async_refresh()
    polls = coord.client.async_get_alarm_hubs.await_count

    gate.clear()
    refresh = hass.async_create_task(coord.async_refresh())
    await asyncio.sleep(0)
    for i in range(MAX_PENDING_DELTAS * 3):
        coord._on_ws_frame(_frame(6, "alarm" if i % 2 else "normal"))

    assert len(coord._pending_deltas) == MAX_PENDING_DELTAS

    gate.set()
    await refresh
    assert not coord._pending_deltas  # spent with the request that owned it

    await hass.async_block_till_done()
    assert coord.client.async_get_alarm_hubs.await_count == polls + 2
    await coord.async_shutdown()


async def test_a_correction_that_raced_a_snapshot_reaches_the_replay(hass):
    """Both halves of a disown-and-take-it-back pair have to survive the race.

    Buffering only deltas that changed the cache looked like a way to stop
    pointless traffic spending slots, but whether a delta matters cannot be
    judged against the cache — only against the snapshot coming back. Here the
    first frame disowns the hub and drops it, so the correction a moment later
    arrives for an id we no longer hold and was filtered out as "did not
    apply". The replay then applied the disown alone, and two frames that
    cancel each other out published the alarm panel as gone until the next poll.
    """
    gate = asyncio.Event()
    coord = _make_coordinator(hass, _gated_client(gate))
    gate.set()
    await coord.async_refresh()
    # Something else is already holding the fallback throttle, so the replay is
    # the only thing that can put this right before the scheduled poll.
    coord._on_ws_frame(_frame(4, "alarm", hub_id="second-linkstation"))
    await hass.async_block_till_done()

    gate.clear()
    refresh = hass.async_create_task(coord.async_refresh())
    await asyncio.sleep(0)
    coord._on_ws_frame(_ownership_frame(False))
    assert "ah1" not in coord.data  # the push path drops it, as the poll would
    coord._on_ws_frame(_ownership_frame(True))

    gate.set()
    await refresh

    assert "ah1" in coord.data
    assert coord.data["ah1"].alarm_hub_inputs[6].name == "Garage Entry"
    await hass.async_block_till_done()
    await coord.async_shutdown()


async def test_a_trip_on_a_hub_the_snapshot_is_about_to_reveal_survives(hass):
    """A newly adopted hub's first zone trip must not rest on the throttle.

    The hub is not in the cache yet — the request in flight is the one that
    introduces it — so its delta reads as an id we do not hold and was dropped
    rather than buffered. The zone then published normal while the console said
    alarm, and the only thing that would have noticed shares the five-minute
    fallback throttle, which an unrelated standing condition can already be
    holding: no recovery snapshot at all until the scheduled poll.
    """
    client = MagicMock()
    client.async_get_alarm_hubs = AsyncMock(return_value=[AlarmHub.from_json(HUB_JSON)])
    coord = _make_coordinator(hass, client)
    await coord.async_refresh()
    coord._on_ws_frame(_frame(4, "alarm", hub_id="second-linkstation"))
    await hass.async_block_till_done()

    gate = asyncio.Event()

    async def two_hubs():
        await gate.wait()
        return [AlarmHub.from_json(HUB_JSON), AlarmHub.from_json(_hub_json("ah2"))]

    client.async_get_alarm_hubs = AsyncMock(side_effect=two_hubs)
    refresh = hass.async_create_task(coord.async_refresh())
    await asyncio.sleep(0)
    coord._on_ws_frame(_frame(6, "alarm", hub_id="ah2"))  # adopted, and tripped
    gate.set()
    await refresh

    assert coord.data["ah2"].alarm_hub_inputs[6].status == "alarm"
    assert coord.data["ah1"].alarm_hub_inputs[6].status == "normal"
    await coord.async_shutdown()


async def test_the_snapshot_an_eviction_forces_cannot_force_another(hass):
    """The overflow guarantee is one follow-up per losing request, not a cascade.

    Whatever overran the buffer is still overrunning it while the follow-up is
    in flight, so a follow-up allowed to force its own turned a single poll into
    a run of back-to-back GETs. The guarantee itself stays — the dropped edge is
    still chased once — and on a failing or key-rejected poll, where this path
    also runs, one retry stays one retry instead of a reauth restarted in a loop.
    """
    floods_left = 0
    client = MagicMock()

    async def flooding_snapshot():
        nonlocal floods_left
        if floods_left:
            floods_left -= 1
            for i in range(MAX_PENDING_DELTAS * 2):
                coord._on_ws_frame(_frame(6, "alarm" if i % 2 else "normal"))
        return [AlarmHub.from_json(HUB_JSON)]

    client.async_get_alarm_hubs = AsyncMock(side_effect=flooding_snapshot)
    coord = _make_coordinator(hass, client)
    await coord.async_refresh()  # quiet: just loads the hub into the cache

    floods_left = 5  # every request from here overruns the buffer
    polls = client.async_get_alarm_hubs.await_count
    await coord.async_refresh()
    await hass.async_block_till_done()

    assert client.async_get_alarm_hubs.await_count == polls + 2
    await coord.async_shutdown()


async def test_a_finished_request_stops_owning_the_delta_buffer(hass, coordinator):
    """``_rest_in_flight`` has to fall with the request that raised it.

    Left set, every frame the hub sends between polls takes a buffer slot with
    no request to replay it over. Nothing downstream can see that today, because
    the next request clears the buffer before it starts — which is why this is
    asserted directly rather than through behaviour: the two halves of that pair
    only hold the line together, and the day either moves, a poll replays frames
    that predate it over its own reply and reverts whatever REST just changed.
    """
    assert coordinator._rest_in_flight is False

    coordinator._on_ws_frame(_frame(6, "alarm"))
    assert not coordinator._pending_deltas

    # The failing ending gets there through the same finally.
    coordinator.client.async_get_alarm_hubs = AsyncMock(
        side_effect=AlarmHubConnectionError("console went away")
    )
    await coordinator.async_refresh()

    assert coordinator._rest_in_flight is False


# --- Fallback snapshots: rare events vs standing conditions ---


async def test_a_hub_we_do_not_hold_stops_asking_for_snapshots(
    hass, coordinator, freezer
):
    """An id REST never returns is a standing condition, not an event.

    A console reporting a second linkstation that ``/v1/alarm-hubs`` filters out
    — isAlarmHub false — sends that id for every frame it ever sends, as does
    one this handler dropped for the same reason. Unthrottled, each frame asked
    for a snapshot, bounded only by the debouncer's ten-second floor: sixty-odd
    REST GETs in the ten minutes ``SCAN_INTERVAL`` budgets two for, converging
    on nothing, because no snapshot can ever contain that id.
    """
    polls = coordinator.client.async_get_alarm_hubs.await_count

    for _ in range(60):  # ten minutes of frames, ten seconds apart
        coordinator._on_ws_frame(_frame(6, "alarm", hub_id="second-linkstation"))
        await hass.async_block_till_done()
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count - polls <= 3


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(
            {"type": "heartbeat", "item": {"id": "ah1", "modelKey": "linkstation"}},
            id="a-type-we-were-not-written-against",
        ),
        pytest.param(
            {"item": {"id": "ah1", "modelKey": "linkstation"}},
            id="no-type-at-all",
        ),
        pytest.param(
            {"type": "update", "item": ["not", "an", "object"]},
            id="an-item-we-cannot-read",
        ),
        pytest.param(
            {"type": "update", "item": {"id": ["ah1"], "modelKey": "linkstation"}},
            id="an-id-we-cannot-key-on",
        ),
    ],
)
async def test_the_frames_we_cannot_apply_stop_asking_for_snapshots(
    hass, coordinator, freezer, frame
):
    """Only ``add`` and ``remove`` earn a snapshot each; the rest wait their turn.

    The carve-out used to be spelled "not an update", which is wider than the
    two events it was written for: any type this integration was not written
    against — a keepalive, or a frame carrying no type at all — went through the
    immediate door and spent sixty REST GETs in the ten minutes ``SCAN_INTERVAL``
    budgets two for, converging on nothing, because no snapshot changes what the
    console puts on its frames. The two payload shapes below it are the same
    standing condition one level in, and were never held to the throttle by a
    test either.
    """
    polls = coordinator.client.async_get_alarm_hubs.await_count

    for _ in range(60):  # ten minutes of frames, ten seconds apart
        coordinator._on_ws_frame(frame)
        await hass.async_block_till_done()
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count - polls <= 3


async def test_the_standing_conditions_share_one_throttle(hass, coordinator, freezer):
    """One clock between them, or two problems just take turns spending it.

    An unknown id and a payload shape we cannot parse are the same kind of
    trouble — the console will keep sending both — so a console with both must
    not cost twice as many snapshots as a console with either.
    """
    polls = coordinator.client.async_get_alarm_hubs.await_count

    coordinator._on_ws_frame(_frame(6, "alarm", hub_id="second-linkstation"))
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=11))  # past the debouncer's own cooldown
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    coordinator._on_ws_frame(
        {
            "type": "update",
            "item": {
                "id": "ah1",
                "modelKey": "linkstation",
                "alarmHub": {"inputs": [{"id": 6, "status": "alarm"}]},
            },
        }
    )
    await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count == polls + 1


@pytest.mark.parametrize("ftype", ["add", "remove"])
async def test_a_hub_added_or_removed_still_resyncs_at_once(
    hass, coordinator, freezer, ftype
):
    """Rare, discrete events must not queue behind the standing-condition clock.

    One snapshot settles them, and a newly adopted hub showing up in ten seconds
    rather than five minutes is what someone is standing there waiting for.
    """
    coordinator._on_ws_frame(_frame(6, "alarm", hub_id="second-linkstation"))
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=11))  # let the debouncer's cooldown lapse
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    polls = coordinator.client.async_get_alarm_hubs.await_count

    coordinator._on_ws_frame(_frame(6, "alarm", ftype=ftype))
    await hass.async_block_till_done()

    assert coordinator.client.async_get_alarm_hubs.await_count == polls + 1


async def test_start_ws_keeps_the_listener_it_already_has(hass, coordinator):
    """A second call must not orphan the first listener.

    ``async_shutdown`` cancels the one task the coordinator holds, so a call
    that overwrote that reference would leave the previous listener running
    against a config entry on its way out — and a second subscription on the
    console reporting frames into a coordinator nobody is polling any more.
    """
    subscribed = asyncio.Event()

    async def never_ends(on_frame, on_connected=None):
        subscribed.set()
        await asyncio.Event().wait()

    coordinator.client.async_subscribe_devices = AsyncMock(side_effect=never_ends)

    coordinator.start_ws()
    listener = coordinator._ws_task
    coordinator.start_ws()
    await subscribed.wait()

    assert coordinator._ws_task is listener
    assert coordinator.client.async_subscribe_devices.await_count == 1


# --- The reconnect loop itself ---


class _ListenEnded(Exception):
    """Ends the reconnect loop from inside the fake sleep, once the script runs out."""


class _WsHarness:
    """Drives ``_ws_listen`` through a scripted sequence of connection attempts.

    Each entry is ``(uptime, ending)``: how many seconds the console held the
    socket open, or None for an attempt that never connected, and how it ended —
    None for the graceful return the client makes on a clean close, otherwise
    the exception it raised. Uptime is applied by backdating the clock the loop
    reads, so a connection can last ten minutes without the test taking them,
    and every delay the loop would have slept is recorded instead of slept.
    """

    def __init__(
        self,
        coordinator: AlarmHubCoordinator,
        script: list[tuple[float | None, Exception | None]],
    ) -> None:
        self.coordinator = coordinator
        self.script = script
        self.delays: list[float] = []
        self.attempts = 0

    async def _subscribe(
        self,
        on_frame: Callable[[dict[str, Any]], None],
        on_connected: Callable[[], None] | None = None,
    ) -> None:
        uptime, ending = self.script[self.attempts]
        self.attempts += 1
        if uptime is not None and on_connected is not None:
            on_connected()
            self.coordinator._ws_connected_at -= uptime
        if ending is not None:
            raise ending

    async def _sleep(self, delay: float) -> None:
        self.delays.append(delay)
        if self.attempts >= len(self.script):
            raise _ListenEnded

    async def run(self) -> None:
        """Run the loop to the end of the script, in no time at all."""
        self.coordinator.client.async_subscribe_devices = self._subscribe
        # Neither fake ever awaits anything, so the whole loop runs without
        # yielding: nothing else can observe the patched sleep.
        with (
            patch.object(coordinator_module.asyncio, "sleep", self._sleep),
            contextlib.suppress(_ListenEnded),
        ):
            await self.coordinator._ws_listen()


def _outage_records(caplog) -> list[logging.LogRecord]:
    """The one line per reconnect attempt that reports the socket being down."""
    return [
        rec
        for rec in caplog.records
        if "falling back to REST polling" in rec.getMessage()
    ]


def _dropped(message: str = "no route to host") -> AlarmHubConnectionError:
    return AlarmHubConnectionError(message)


async def test_reconnect_backoff_ramps_and_a_healthy_connection_restarts_it(
    hass, coordinator, caplog
):
    """The reconnect policy the docstring promises, driven end to end.

    Three failures ramp 1, 2, 4 and are reported once, not three times. The
    connection after them lasts long enough to count as a working link, so the
    ramp starts over and its ending is a new outage worth reporting again.
    """
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    harness = _WsHarness(
        coordinator,
        [
            (None, _dropped()),
            (None, _dropped()),
            (None, _dropped()),
            (999.0, _dropped("connection reset by peer")),
            (None, _dropped()),
            (None, _dropped()),
        ],
    )

    await harness.run()
    await hass.async_block_till_done()

    assert harness.delays == [1.0, 2.0, 4.0, 1.0, 2.0, 4.0]
    assert [rec.levelno for rec in _outage_records(caplog)] == [
        logging.WARNING,
        logging.DEBUG,
        logging.DEBUG,
        logging.WARNING,
        logging.DEBUG,
        logging.DEBUG,
    ]


async def test_a_console_flapping_at_the_healthy_threshold_stays_quiet(
    hass, coordinator, caplog
):
    """The flap guard must not have a hole at its own threshold.

    A console that holds the socket a little past ``WS_HEALTHY_UPTIME`` and then
    drops it is still flapping, only more slowly. Restarting the ramp for it is
    right — the link did work — but re-arming the warning on the same threshold
    put a WARNING in the log every cycle, which is the noise the latch exists to
    suppress, reappearing at the boundary of the rule that suppresses it.
    """
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    just_healthy = WS_HEALTHY_UPTIME + 1
    harness = _WsHarness(
        coordinator,
        [
            (None, _dropped()),
            (just_healthy, _dropped("connection reset by peer")),
            (just_healthy, _dropped("connection reset by peer")),
            (just_healthy, _dropped("connection reset by peer")),
        ],
    )

    await harness.run()
    await hass.async_block_till_done()

    assert harness.delays == [1.0, 1.0, 1.0, 1.0]  # the ramp still restarts...
    assert [rec.levelno for rec in _outage_records(caplog)] == [
        logging.WARNING,
        logging.DEBUG,
        logging.DEBUG,
        logging.DEBUG,
    ]


async def test_the_outage_line_says_how_the_socket_ended(hass, coordinator, caplog):
    """Three endings a user has to tell apart from the log alone.

    A tidy close, a key the console refuses, and a link that broke mid-stream
    call for different responses; one shared "disconnected" would hide which.
    """
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    long_enough = WS_WARN_REARM_UPTIME + 1
    harness = _WsHarness(
        coordinator,
        [
            (long_enough, None),
            (long_enough, AlarmHubAuthError("Auth failed (401)")),
            (long_enough, _dropped("Connection reset by peer")),
        ],
    )

    await harness.run()
    await hass.async_block_till_done()

    reasons = [rec.getMessage() for rec in _outage_records(caplog)]
    assert "closed by the console" in reasons[0]
    assert "rejected the API key (Auth failed (401))" in reasons[1]
    assert "dropped (Connection reset by peer)" in reasons[2]
