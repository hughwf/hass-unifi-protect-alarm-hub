"""Scaffolding shared by the platform and entity test modules.

These tests stand the integration up for real -- config entry, coordinator,
entity platforms, entity registry -- against a fake console, so what they assert
is entity state and registry content rather than the shape of an object.

Both update paths are drivable: ``poll`` runs the reconciling REST request, and
``FakeConsole.push`` delivers a devices-WS frame through the real coordinator
callback. A test that only wants the REST path simply never pushes.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.async_ import get_scheduled_timer_handles
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.unifi_protect_alarm_hub.const import DOMAIN, SCAN_INTERVAL
from custom_components.unifi_protect_alarm_hub.models import AlarmHub

MAC = "AABBCCDDEEFF"


def hub_json(
    hub_id: str = "ah1",
    *,
    mac: Any = MAC,
    name: Any = "Alarm Hub Kit",
    state: Any = "CONNECTED",
) -> dict[str, Any]:
    """One adopted alarm hub: two zones, one output, a battery and a cover.

    A fresh dict every call, so a test can edit it the way someone edits a hub.
    Wire values are the ones the design spec documents -- notably the cover's
    ``close``, not ``closed``: the fixture used to send a value
    ``logic.cover_is_on`` does not accept, so every test that touched the tamper
    sensor was reading ``unknown`` and no test ever exercised an intact case.
    """
    return {
        "id": hub_id,
        "modelKey": "linkstation",
        "name": name,
        "mac": mac,
        "state": state,
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
            "output": {
                "1": {
                    "active": "off",
                    "enable": "on",
                    "status": "normal",
                    "name": "Siren",
                },
            },
            "battery": {
                "connection": "connected",
                "charging": "no",
                "voltage": 12.6,
                "batteryStatus": "ok",
            },
            "cover": {"status": "close", "distance": 3},
        },
    }


def zone_frame(status: str, hub_id: str = "ah1", zone_id: str = "6") -> dict[str, Any]:
    """A devices-WS delta carrying one zone's new status."""
    return {
        "type": "update",
        "item": {
            "id": hub_id,
            "modelKey": "linkstation",
            "alarmHub": {"input": {zone_id: {"status": status}}},
        },
    }


def new_zone_frame(
    zone_id: str, name: str, status: str = "alarm", hub_id: str = "ah1"
) -> dict[str, Any]:
    """A delta that wires a contact in, tripped on arrival."""
    return {
        "type": "update",
        "item": {
            "id": hub_id,
            "modelKey": "linkstation",
            "alarmHub": {
                "input": {
                    zone_id: {
                        "enable": "on",
                        "status": status,
                        "inputType": "ENTRY",
                        "name": name,
                    }
                }
            },
        },
    }


def hub_frame(hub_id: str = "ah1", **fields: Any) -> dict[str, Any]:
    """A devices-WS delta carrying top-level hub fields (``state`` and friends)."""
    return {
        "type": "update",
        "item": {"id": hub_id, "modelKey": "linkstation", **fields},
    }


class FakeConsole:
    """A console whose answers a test edits between polls, and can push frames.

    ``payloads`` is what ``/v1/alarm-hubs`` returns; edit it in place to stage a
    hub being adopted, a zone being wired in, a rename, or a battery first being
    reported. Set ``rest_error`` to take the REST endpoint down without touching
    the socket.
    """

    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.payloads: list[dict[str, Any]] = list(payloads)
        self.triggered: list[tuple[str, int, bool]] = []
        self.trigger_error: Exception | None = None
        self.rest_error: Exception | None = None
        self.polls = 0
        # Whether the relay has moved by the time the next REST answer is built.
        # Real hardware often has not: the GET ``_async_trigger`` fires races the
        # relay, and the console answers from the state it had a moment ago.
        self.relay_follows_command = True
        self._on_frame: Any = None

    async def async_get_alarm_hubs(self) -> list[AlarmHub]:
        self.polls += 1
        if self.rest_error is not None:
            raise self.rest_error
        # Parsed from a copy: the coordinator keeps the payload it parsed, and
        # a test editing its own dict afterwards must not reach into a snapshot
        # that has already been published.
        return [AlarmHub.from_json(deepcopy(item)) for item in self.payloads]

    async def async_trigger_output(
        self, hub_id: str, output_id: int, enable: bool
    ) -> None:
        """Accept the command; move the relay only if this console is prompt."""
        if self.trigger_error is not None:
            raise self.trigger_error
        self.triggered.append((hub_id, output_id, enable))
        if not self.relay_follows_command:
            return
        for payload in self.payloads:
            if payload.get("id") == hub_id:
                outputs = payload["alarmHub"]["output"]
                outputs[str(output_id)]["active"] = "on" if enable else "off"

    def push(self, frame: dict[str, Any]) -> None:
        """Deliver one devices-WS frame to the coordinator's real callback."""
        assert self._on_frame is not None, "the WebSocket has not subscribed yet"
        self._on_frame(frame)


def add_entry(hass: HomeAssistant) -> MockConfigEntry:
    """The config entry this integration's tests use, added but not set up.

    Split out so a test can put registry rows on it *before* the first setup
    reads them -- which is what an upgrade is: the entry and its devices and
    entities are already on disk, written by a release whose identity rule was
    not this one, and setup has to recognise them.
    """
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
    return entry


async def start(
    hass: HomeAssistant, entry: MockConfigEntry, console: FakeConsole
) -> None:
    """Set ``entry`` up against ``console`` and wait for its entities."""
    await _async_setup(hass, entry, console)


async def setup_integration(
    hass: HomeAssistant, console: FakeConsole
) -> MockConfigEntry:
    """Set the integration up against ``console`` and wait for its entities."""
    entry = add_entry(hass)
    await _async_setup(hass, entry, console)
    return entry


# What a release registered for the standard ``hub_json`` hub: eleven entities,
# each ``f"{identity}_{suffix}"``, on one device row filed under that identity.
# The released 0.2 built both from ``hub.mac`` verbatim -- the raw string out of
# console JSON, placeholder or empty included -- which is why an upgrade has to
# recognise strings this integration would never choose today.
RELEASED_ENTITIES = (
    ("binary_sensor", "zone_4"),
    ("binary_sensor", "zone_4_fault"),
    ("binary_sensor", "zone_6"),
    ("binary_sensor", "zone_6_fault"),
    ("binary_sensor", "tamper"),
    ("binary_sensor", "armed"),
    ("binary_sensor", "connectivity"),
    ("binary_sensor", "battery_connection"),
    ("sensor", "battery_status"),
    ("sensor", "battery_voltage"),
    ("switch", "output_1"),
)


def as_an_earlier_release_left_it(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    identity: str,
    *,
    name: str = "Alarm Hub Kit",
) -> dr.DeviceEntry:
    """Fill both registries the way a release with a different identity rule did.

    An upgrade never starts from an empty registry, and every failure this
    guards is invisible from one: the ids are already on disk, the device row is
    already there, and what setup must not do is decide the hub is called
    something else. Written here rather than by running the old code so the
    strings are visible in the test -- the scratch upgrade matrix checks them
    against what 0.2 and the two staged commits actually produce.
    """
    # The entry itself is part of what the earlier release left: its config flow
    # always wrote f"{host}:{port}". Leaving unique_id None made every upgrade
    # test run against a shape no released install has -- async_migrate_unique_id
    # returned immediately, so the re-key and the device re-file were never
    # exercised together, and a guard that read that id as a mac went unnoticed
    # for four rounds.
    hass.config_entries.async_update_entry(
        entry, unique_id=f"{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}"
    )
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, identity)}, name=name
    )
    registry = er.async_get(hass)
    for domain, suffix in RELEASED_ENTITIES:
        registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{identity}_{suffix}",
            config_entry=entry,
            device_id=device.id,
            # One disabled row, because a disabled entity is still the user's:
            # leaving its unique_id behind strands it the moment they enable it.
            disabled_by=(
                er.RegistryEntryDisabler.INTEGRATION if suffix == "zone_4" else None
            ),
        )
    return device


async def restart(
    hass: HomeAssistant, entry: MockConfigEntry, console: FakeConsole
) -> None:
    """Take the entry down and set the *same* entry up again.

    The same entry deliberately: both registries are keyed on the entry id, so a
    second ``MockConfigEntry`` would be a different install with nothing
    recorded, and anything that reads what a previous run decided -- which is
    the whole of ``async_hub_device_ids`` -- would look correct by having
    nothing to read.
    """
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await _async_setup(hass, entry, console)


async def _async_setup(
    hass: HomeAssistant, entry: MockConfigEntry, console: FakeConsole
) -> None:
    client = MagicMock()
    client.async_get_alarm_hubs = AsyncMock(side_effect=console.async_get_alarm_hubs)
    client.async_trigger_output = AsyncMock(side_effect=console.async_trigger_output)

    async def _subscribe(on_frame, on_connected=None) -> None:
        console._on_frame = on_frame
        if on_connected is not None:
            on_connected()
        await asyncio.Event().wait()  # stay "connected" until unload cancels it

    client.async_subscribe_devices = AsyncMock(side_effect=_subscribe)

    with patch(
        "custom_components.unifi_protect_alarm_hub.AlarmHubApiClient",
        return_value=client,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Run the reconciling REST poll and let the platforms settle.

    ``async_refresh`` rather than ``async_request_refresh``: the scheduled poll
    does not go through the ten-second debouncer, and neither should a test
    standing in for it.
    """
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def advance(hass: HomeAssistant, freezer, seconds: float) -> None:
    """Move the clock, fire whatever fell due, and let it settle."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def failed_poll(hass: HomeAssistant, freezer) -> None:
    """Let one scheduled poll come round and fail (see ``rest_error``).

    The edge into failure is the one notification listeners get; a poll that
    fails behind an already-failed poll notifies nobody at all.
    """
    await advance(hass, freezer, SCAN_INTERVAL.total_seconds() + 1)


def entity_ids(hass: HomeAssistant, domain: str) -> list[str]:
    """Every entity this integration publishes in ``domain``, in a stable order.

    Ownership comes from the registry rather than from an entity_id prefix, so a
    hub the console named something other than "Alarm Hub ..." is not quietly
    skipped -- which would let a multi-hub test assert over one hub's entities
    while believing it covered both.
    """
    owned = {
        entry.entity_id
        for entry in er.async_get(hass).entities.values()
        if entry.platform == DOMAIN
    }
    return sorted(
        state.entity_id
        for state in hass.states.async_all(domain)
        if state.entity_id in owned
    )


def unique_ids(hass: HomeAssistant) -> set[str]:
    """Every unique_id this integration holds in the entity registry.

    The identity Home Assistant itself refuses duplicates on, and the one an
    existing install already has on disk -- so it is what both the upgrade
    contract and every hub-splitting failure are asserted against.
    """
    return {
        entry.unique_id
        for entry in er.async_get(hass).entities.values()
        if entry.platform == DOMAIN
    }


def hub_devices(hass: HomeAssistant, entry: MockConfigEntry) -> list[dr.DeviceEntry]:
    """The devices this entry has registered, which is one per hub."""
    return dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)


def armed_call_later_handles(hass: HomeAssistant) -> list[Any]:
    """The live ``async_call_later`` timers on the loop.

    They cannot be told apart by repr -- ``TimerHandle`` prints its callback
    without its arguments, so every one of them reads ``_run_async_call_action``
    -- which is why the tests using this assert on the count going to nothing
    rather than on finding a particular timer.
    """
    return [
        handle
        for handle in get_scheduled_timer_handles(hass.loop)
        if not handle.cancelled() and "_run_async_call_action" in repr(handle)
    ]


def published_states(hass: HomeAssistant, *domains: str) -> dict[str, str]:
    """Every entity this integration publishes in ``domains``, and its state."""
    return {
        entity_id: hass.states.get(entity_id).state
        for domain in domains
        for entity_id in entity_ids(hass, domain)
    }


@pytest.fixture(autouse=True)
async def unload_entries(hass: HomeAssistant):
    """Unload whatever a test set up, so no coordinator timer outlives it.

    ``verify_cleanup`` fails a test on a lingering timer, and the poll schedule
    and the request-refresh cooldown are both timers the coordinator only drops
    when it is shut down.
    """
    yield
    for entry in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
