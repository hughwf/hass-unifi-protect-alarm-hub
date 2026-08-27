"""Tier-2 tests for the shared entity base and the platform seam it owns.

Everything here stands the integration up for real against a fake console and
reads ``hass.states`` and the registries, because every defect these cover is
about what an automation sees -- a state string, an availability flag, an
entity id -- rather than about a return value.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.unifi_protect_alarm_hub.api import AlarmHubConnectionError
from custom_components.unifi_protect_alarm_hub.const import DOMAIN, SCAN_INTERVAL
from platform_common import (  # noqa: F401  (unload_entries is an autouse fixture)
    MAC,
    RELEASED_ENTITIES,
    FakeConsole,
    add_entry,
    advance,
    armed_call_later_handles,
    as_an_earlier_release_left_it,
    failed_poll,
    hub_devices,
    hub_frame,
    hub_json,
    new_zone_frame,
    poll,
    published_states,
    restart,
    setup_integration,
    start,
    unique_ids,
    unload_entries,
    zone_frame,
)

GARAGE = "binary_sensor.alarm_hub_kit_garage_entry"
WINDOW = SCAN_INTERVAL.total_seconds()

# The mac a console reports for a hub whose own is not populated yet. Two hubs
# mid-adoption carry it at once, and it is a perfectly usable-looking string.
PLACEHOLDER_MAC = "000000000000"

# The error entity_platform logs when two entities claim one unique_id. Only a
# log line: the second entity is dropped and nothing else says so.
DUPLICATE_UNIQUE_ID = "does not generate unique IDs"


def _zone_entity_id(hass, device_id: str = MAC, zone_id: int = 6) -> str:
    """The zone binary_sensor's entity id, via the registry rather than a guess."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{device_id}_zone_{zone_id}"
    )
    assert entity_id is not None
    return entity_id


# --- S1: a wire we cannot read must not read as a secure door ---


@pytest.mark.parametrize("status", ["cut", None], ids=["cut-loop", "no-status-yet"])
async def test_an_unreadable_zone_does_not_publish_a_closed_door(hass, status):
    """The severed-loop case, end to end.

    A cut contact used to render as "off" on the ``door`` device_class -- which
    the UI shows as Closed and which every ``state == 'off'`` condition in an
    alarm automation passes. A zone first seen through a partial frame carries
    no status at all and did exactly the same.
    """
    payload = hub_json()
    zone = payload["alarmHub"]["input"]["6"]
    if status is None:
        del zone["status"]
    else:
        zone["status"] = status
    await setup_integration(hass, FakeConsole(payload))

    state = hass.states.get(GARAGE)

    assert state.attributes["device_class"] == "door"
    assert state.state == STATE_UNKNOWN


# --- S2: a hub the console cannot see must not keep publishing state ---


async def test_a_hub_that_goes_offline_takes_its_zones_with_it(hass):
    """Kill the hub -- power cut, RF link lost -- and the board must not stay green.

    The console is still reachable, so polling keeps succeeding and the cached
    zone state keeps being republished; nothing but this gate notices that the
    device those zones live on has stopped answering.
    """
    console = FakeConsole(hub_json())
    await setup_integration(hass, console)
    assert hass.states.get(GARAGE).state == STATE_OFF

    console.push(hub_frame(state="DISCONNECTED"))
    await hass.async_block_till_done()

    assert hass.states.get(GARAGE).state == STATE_UNAVAILABLE


async def test_a_hub_that_never_reported_a_state_is_not_assumed_alive(hass):
    await setup_integration(hass, FakeConsole(hub_json(state=None)))

    assert hass.states.get(GARAGE).state == STATE_UNAVAILABLE


# --- S3: availability must count the push path, without stranding on it ---


async def test_a_pushed_alarm_is_visible_while_rest_polling_is_failing(hass, freezer):
    """A live socket and a dead REST endpoint: the alarm still has to land.

    ``CoordinatorEntity.available`` is exactly ``last_update_success``, which
    stage 1 deliberately stopped writing from the push path -- so without a
    push-side signal here every entity reads unavailable through a REST outage
    and a real alarm arriving over the socket is invisible.
    """
    console = FakeConsole(hub_json())
    await setup_integration(hass, console)
    console.rest_error = AlarmHubConnectionError("no route")

    for _ in range(2):  # two failed polls, ten minutes of console outage
        await failed_poll(hass, freezer)
        # A failed poll is a notification, not a delivery. Counting it as one
        # would let every entity coast for a further window on the strength of
        # the REST failure itself.
        assert hass.states.get(GARAGE).state == STATE_UNAVAILABLE

    console.push(zone_frame("alarm"))
    await hass.async_block_till_done()

    assert hass.states.get(GARAGE).state == STATE_ON


async def test_a_delivery_stands_for_exactly_one_poll_interval(hass, freezer):
    """The window is the poll interval, and its *value* is what matters.

    Written against ``SCAN_INTERVAL`` rather than ``PUSH_FRESHNESS_WINDOW``,
    deliberately: a test that only uses the symbol passes whatever the symbol is
    set to, and multiplying that constant by four -- twenty minutes of a dead
    console reported as live state -- used to break nothing. Both edges are
    pinned here, so widening it and narrowing it both fail.

    The far edge is also the other half of the push bargain. A socket that
    pushed an alarm and then died is not evidence that the alarm is still true,
    and nothing else would ever revisit it: a poll that fails while the previous
    one already failed does not notify listeners at all.
    """
    console = FakeConsole(hub_json())
    await setup_integration(hass, console)
    console.rest_error = AlarmHubConnectionError("no route")
    await failed_poll(hass, freezer)

    console.push(zone_frame("alarm"))
    await hass.async_block_till_done()
    assert hass.states.get(GARAGE).state == STATE_ON

    await advance(hass, freezer, WINDOW - 1)
    assert hass.states.get(GARAGE).state == STATE_ON

    await advance(hass, freezer, 2)
    assert hass.states.get(GARAGE).state == STATE_UNAVAILABLE


async def test_an_expiry_that_fires_early_still_leaves_something_watching(
    hass, freezer
):
    """A timer firing is not proof that its deadline passed.

    asyncio may run a handle up to ``_clock_resolution`` early by design, and
    the test harness fires them up to half a second early for the same reason --
    so the callback lands while the delivery it was timing is still fresh. It
    used to clear its own handle and write the state anyway, which left the
    entity available on that reading with nothing scheduled to revisit it. A
    poll that fails behind an already-failed poll notifies no listeners, so
    nothing ever did: a pushed "alarm" stayed on the board for good.
    """
    console = FakeConsole(hub_json())
    await setup_integration(hass, console)
    console.rest_error = AlarmHubConnectionError("no route")
    await failed_poll(hass, freezer)

    console.push(zone_frame("alarm"))
    await hass.async_block_till_done()
    assert hass.states.get(GARAGE).state == STATE_ON

    # Half a second short of the window: the expiry fires, and it is wrong.
    await advance(hass, freezer, WINDOW - 0.5)
    assert hass.states.get(GARAGE).state == STATE_ON

    await advance(hass, freezer, 1)
    assert hass.states.get(GARAGE).state == STATE_UNAVAILABLE


async def test_a_zone_a_push_creates_during_an_outage_reports_what_it_carried(
    hass, freezer
):
    """A contact wired in mid-outage, tripped on arrival, must not read blank.

    The entity is created by the very frame that carries its status, so seeding
    its delivery clock as "nothing has ever arrived" left it unavailable while
    holding live data -- and nothing re-armed it, because the expiry is only
    armed from a coordinator update and this entity's update predated its own
    existence. A contact that trips on first report during a console outage
    showed nothing at all until some *other* frame happened along.
    """
    console = FakeConsole(hub_json())
    await setup_integration(hass, console)
    console.rest_error = AlarmHubConnectionError("no route")
    await failed_poll(hass, freezer)

    console.push(new_zone_frame("7", "Back Door", status="alarm"))
    await hass.async_block_till_done()

    back_door = hass.states.get("binary_sensor.alarm_hub_kit_back_door")
    assert back_door is not None
    assert back_door.state == STATE_ON

    # ...and that is a delivery, not an exemption: it lapses on the same clock.
    await advance(hass, freezer, WINDOW + 1)
    assert (
        hass.states.get("binary_sensor.alarm_hub_kit_back_door").state
        == STATE_UNAVAILABLE
    )


async def test_one_hubs_chatter_does_not_vouch_for_another_hubs_silence(hass, freezer):
    """Freshness is a statement about a hub, not about the socket.

    Timed on the identity of the whole snapshot dict -- which ``_publish``
    rebuilds for any hub's frame -- a chatty hub held every other hub on the
    console available indefinitely. With REST dead and hub B silent for twenty
    minutes, B's door contact kept publishing a confident "off" because A was
    talking. The module's own rationale ("a socket that is connected but silent
    for hours is not evidence of anything") applies per hub.
    """
    console = FakeConsole(
        hub_json("ah1"),
        hub_json("ah2", mac="112233445566", name="Shed Hub"),
    )
    await setup_integration(hass, console)
    shed = _zone_entity_id(hass, "112233445566")
    assert hass.states.get(shed).state == STATE_OFF

    console.rest_error = AlarmHubConnectionError("no route")
    await failed_poll(hass, freezer)

    # Only ah1 ever speaks again, and it never stops.
    for _ in range(4):
        await advance(hass, freezer, WINDOW - 10)
        console.push(zone_frame("alarm", hub_id="ah1"))
        await hass.async_block_till_done()
        console.push(zone_frame("normal", hub_id="ah1"))
        await hass.async_block_till_done()

    assert hass.states.get(GARAGE).state == STATE_OFF  # ah1 is genuinely live
    assert hass.states.get(shed).state == STATE_UNAVAILABLE


# --- S4: mac is unvalidated, and it is the whole device identity ---


async def test_a_hub_whose_mac_is_not_a_string_still_gets_its_entities(hass):
    """``identifiers={(DOMAIN, mac)}`` raised "unhashable type: 'list'".

    That is inside the entity constructor, so it took the whole platform setup
    down with it and the hub got no entities at all.
    """
    await setup_integration(hass, FakeConsole(hub_json(mac=["AA", "BB"])))

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "ah1")})
    assert device is not None
    entities = er.async_entries_for_device(er.async_get(hass), device.id)
    assert [entry for entry in entities if entry.unique_id.endswith("_zone_6")]


async def test_two_hubs_without_macs_keep_two_full_sets_of_entities(hass, caplog):
    """An absent mac made every hub the same device *and* the same entities.

    Device identity was hardened first and entity identity was not, which left
    the worse half of the defect: two distinct devices whose hub-level
    unique_ids -- tamper, armed, connectivity, battery, siren -- still collided
    on ``f"{mac}_..."``. The reconcile pass dedups on unique_id, so the second
    hub's entities were dropped before Home Assistant ever saw them, and there
    was not even a duplicate-unique_id line in the log to notice.
    """
    await setup_integration(
        hass,
        FakeConsole(hub_json("ah1", mac="", name="Hall Hub"), hub_json("ah2", mac="")),
    )

    devices = dr.async_get(hass)
    assert devices.async_get_device(identifiers={(DOMAIN, "ah1")}) is not None
    assert devices.async_get_device(identifiers={(DOMAIN, "ah2")}) is not None

    registered = unique_ids(hass)
    for suffix in ("tamper", "armed", "connectivity", "battery_connection", "output_1"):
        assert {f"ah1_{suffix}", f"ah2_{suffix}"} <= registered
    assert DUPLICATE_UNIQUE_ID not in caplog.text


@pytest.mark.parametrize("shed_first", [False, True], ids=["hall-first", "shed-first"])
async def test_two_hubs_reporting_one_mac_each_report_their_own_zones(
    hass, caplog, shed_first
):
    """One mac on two hubs, which a console placeholder or a bug both produce.

    Sharing an identity, the two hubs became one device: the second was refused
    every unique_id it asked for -- eleven "does not generate unique IDs" lines
    and no entities at all -- and the set that survived answered from whichever
    hub the REST list put first. That is the half that matters. Reversing the
    console's order flipped what the door contact on the surviving device
    reported, so an entity was publishing a different physical hub's zone with
    no indication anywhere that it was doing it.

    Asserted through the entity ids the hub *names* produce, because those
    follow the hardware whichever of the two ends up holding the mac identity.
    """
    hall = hub_json("ah1", mac=PLACEHOLDER_MAC, name="Hall Hub")
    shed = hub_json("ah2", mac=PLACEHOLDER_MAC, name="Shed Hub")
    shed["alarmHub"]["input"]["6"]["status"] = "alarm"
    payloads = [shed, hall] if shed_first else [hall, shed]

    entry = await setup_integration(hass, FakeConsole(*payloads))

    assert len(hub_devices(hass, entry)) == 2
    assert hass.states.get("binary_sensor.hall_hub_garage_entry").state == STATE_OFF
    assert hass.states.get("binary_sensor.shed_hub_garage_entry").state == STATE_ON
    assert DUPLICATE_UNIQUE_ID not in caplog.text


async def test_a_contested_mac_still_answers_for_its_own_hub_after_a_restart(hass):
    """The order the console lists two hubs in is not a promise, and it decides.

    Whichever of the pair is seen first takes the mac as its identity, so after
    a restart that hub can be the *second* entry -- and the contested mac then
    matches the first entry before the loop ever reaches its owner. Every entity
    on the mac-named device followed that match: the hall hub's door contact
    published the shed hub's zone, with the right name, on the right device,
    reporting the wrong building.
    """
    hall = hub_json("ah1", mac=PLACEHOLDER_MAC, name="Hall Hub")
    shed = hub_json("ah2", mac=PLACEHOLDER_MAC, name="Shed Hub")
    shed["alarmHub"]["input"]["6"]["status"] = "alarm"
    console = FakeConsole(hall, shed)
    entry = await setup_integration(hass, console)
    before = unique_ids(hass)
    assert hass.states.get("binary_sensor.hall_hub_garage_entry").state == STATE_OFF

    console.payloads.reverse()
    await restart(hass, entry, console)

    assert unique_ids(hass) == before
    assert len(hub_devices(hass, entry)) == 2
    assert hass.states.get("binary_sensor.hall_hub_garage_entry").state == STATE_OFF
    assert hass.states.get("binary_sensor.shed_hub_garage_entry").state == STATE_ON


@pytest.mark.parametrize(
    "shed_status", ["alarm", "normal"], ids=["shed-tripped", "shed-clear"]
)
async def test_a_contested_mac_reports_nothing_once_its_own_hub_is_absent(
    hass, shed_status
):
    """One of a mac-sharing pair drops out of a poll, and the other one is not it.

    The contest was only recognised while both hubs were in the snapshot, so a
    hub missing from one poll -- rebooting, mid-adoption, briefly disowned by a
    delta -- dissolved it, and the absent hub's device started answering from
    the hub that was still there. ``alarm`` is the loud half; ``normal`` is the
    worse one, because the absent hub's door contact then publishes a confident
    "off" -- an affirmative all-clear that every ``state == 'off'`` alarm
    condition passes, for a hub nobody can currently see.
    """
    hall = hub_json("ah1", mac=PLACEHOLDER_MAC, name="Hall Hub")
    shed = hub_json("ah2", mac=PLACEHOLDER_MAC, name="Shed Hub")
    shed["alarmHub"]["input"]["6"]["status"] = shed_status
    console = FakeConsole(hall, shed)
    entry = await setup_integration(hass, console)
    assert hass.states.get("binary_sensor.hall_hub_garage_entry").state == STATE_OFF

    del console.payloads[0]  # this poll does not carry the hall hub at all
    await poll(hass, entry)

    assert (
        hass.states.get("binary_sensor.hall_hub_garage_entry").state
        == STATE_UNAVAILABLE
    )

    # The push path resolves through the same call, so it crossed the same way.
    console.push(zone_frame("alarm", hub_id="ah2"))
    await hass.async_block_till_done()

    assert (
        hass.states.get("binary_sensor.hall_hub_garage_entry").state
        == STATE_UNAVAILABLE
    )
    assert hass.states.get("binary_sensor.shed_hub_garage_entry").state == STATE_ON


async def test_a_command_for_an_absent_hub_is_not_sent_to_the_one_sharing_its_mac(hass):
    """The same crossing, on the path that does something physical.

    ``AlarmHubBaseEntity.hub_id`` is resolved the same way, so once the contest
    dissolved the hall hub's siren switch addressed its commands to the shed
    hub's id: a panic automation's ``switch.turn_on`` sounding a siren in a
    different building, with nothing anywhere reporting that it had. Unavailable
    is the honest answer -- Home Assistant drops unavailable entities from
    service calls, so the command goes nowhere rather than somewhere wrong.
    """
    hall = hub_json("ah1", mac=PLACEHOLDER_MAC, name="Hall Hub")
    shed = hub_json("ah2", mac=PLACEHOLDER_MAC, name="Shed Hub")
    console = FakeConsole(hall, shed)
    entry = await setup_integration(hass, console)

    del console.payloads[0]  # this poll does not carry the hall hub at all
    await poll(hass, entry)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.hall_hub_siren"}, blocking=True
    )
    await hass.async_block_till_done()

    assert hass.states.get("switch.hall_hub_siren").state == STATE_UNAVAILABLE
    assert console.triggered == []

    # ...while the hub that is still here goes on taking its own commands.
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.shed_hub_siren"}, blocking=True
    )
    await hass.async_block_till_done()

    assert console.triggered == [("ah2", 1, True)]


async def test_a_mac_that_appears_while_the_integration_was_down_keeps_the_device(hass):
    """The seeding is only exercised across a restart, which is where it matters.

    A hub adopted before ``/v1/alarm-hubs`` populated its mac is identified by
    its id, and its entities are registered under that id for good. Identity
    recomputed from a fresh snapshot at the next startup would move to the mac
    the moment it appeared: the registry goes from one device to two, the eleven
    entities every automation is written against are stranded on the old one
    with nothing left to update them, and eleven more appear beside them.
    """
    console = FakeConsole(hub_json(mac=""))
    entry = await setup_integration(hass, console)
    before = unique_ids(hass)
    assert before  # a macless hub is identified by its id and does get entities
    assert len(hub_devices(hass, entry)) == 1

    console.payloads[0]["mac"] = MAC
    await restart(hass, entry, console)

    assert unique_ids(hass) == before
    assert len(hub_devices(hass, entry)) == 1
    assert hass.states.get(GARAGE).state == STATE_OFF


async def test_a_placeholder_mac_never_becomes_an_identity_to_lose(hass):
    """B2 end to end: adopt mid-adoption, let the mac land, restart.

    The placeholder looks like a mac and is not one -- it is what the console
    reports for a hub whose own it has not read. Taken as the identity, it was
    recorded in the device registry and then abandoned by the hub the moment the
    real mac appeared: the restart after that seeded ``{'000000000000'}``,
    recognised nothing, and minted a second device. The eleven entities every
    automation names were left on the first with nothing updating them, eleven
    more appeared with ``_2`` entity ids, and the live half was the half nobody
    had written an automation against.
    """
    console = FakeConsole(hub_json(mac=PLACEHOLDER_MAC))
    entry = await setup_integration(hass, console)
    before = unique_ids(hass)
    assert len(before) == 11
    assert [device.identifiers for device in hub_devices(hass, entry)] == [
        {(DOMAIN, "ah1")}
    ]

    console.payloads[0]["mac"] = MAC
    await poll(hass, entry)
    await restart(hass, entry, console)

    assert unique_ids(hass) == before
    assert len(hub_devices(hass, entry)) == 1
    assert hass.states.get(GARAGE).state == STATE_OFF


@pytest.mark.parametrize(
    ("filed_as", "reports"),
    [
        pytest.param("aabbcc", "aabbcc", id="a hub filed under a mac that is not one"),
        pytest.param(MAC, MAC, id="an ordinary hub"),
        pytest.param(MAC, "aa:bb:cc:dd:ee:ff", id="the same, spelled the other way"),
        pytest.param("aa:bb:cc:dd:ee:ff", MAC, id="and back again"),
    ],
)
async def test_an_upgrade_keeps_every_id_an_earlier_release_wrote(
    hass, filed_as, reports
):
    """The whole upgrade contract, from the registry an install already has.

    Released 0.2 filed a hub under ``hub.mac`` verbatim and built all eleven
    unique_ids from the same string, so the strings on disk include ones nothing
    would choose today. A rule that only recognises identities it would mint
    itself reads every one of those installs as a hub it has never seen -- a
    second device row, eleven new entities, the originals orphaned at
    ``unavailable`` for good, and the live ones taking ``_2`` entity ids that no
    automation names.

    So what is on disk wins, whatever it looks like, and it wins over an
    identity the console's *current* payload would otherwise justify.

    The two strings that name no hardware -- the empty string and the
    placeholder -- are the exception, and are not merely kept but re-filed once:
    see ``test_a_row_naming_no_hardware_is_re_filed_under_one_that_does``.
    ``aabbcc`` is not one of them. It is a mac nobody can use as an identity
    *across consoles*, but it is a string this console alone reports, so the row
    stays exactly where it is.
    """
    entry = add_entry(hass)
    device = as_an_earlier_release_left_it(hass, entry, filed_as)
    before = unique_ids(hass)
    entity_ids_before = {
        entry.unique_id: entry.entity_id
        for entry in er.async_entries_for_device(er.async_get(hass), device.id)
    }

    await start(hass, entry, FakeConsole(hub_json(mac=reports)))

    assert unique_ids(hass) == before
    assert [row.identifiers for row in hub_devices(hass, entry)] == [
        {(DOMAIN, filed_as)}
    ]
    assert {
        entry.unique_id: entry.entity_id
        for entry in er.async_entries_for_device(er.async_get(hass), device.id)
    } == entity_ids_before
    # ...and they are live, not merely still on disk.
    assert hass.states.get(entity_ids_before[f"{filed_as}_zone_6"]).state == STATE_OFF


@pytest.mark.parametrize(
    ("filed_as", "reports", "becomes"),
    [
        pytest.param("", "", "ah1", id="no mac at all, and still none"),
        pytest.param(PLACEHOLDER_MAC, PLACEHOLDER_MAC, "ah1", id="still mid-adoption"),
        pytest.param("", MAC, MAC, id="a mac-less hub whose mac has since arrived"),
        pytest.param(PLACEHOLDER_MAC, MAC, MAC, id="and one adopted mid-adoption"),
    ],
)
async def test_a_row_naming_no_hardware_is_re_filed_under_one_that_does(
    hass, filed_as, reports, becomes
):
    """The released mid-adoption install, migrated instead of abandoned.

    A device row records the identity and nothing else, so a hub is recognised
    by a string it still reports -- and the empty string and the placeholder are
    strings a console stops reporting the moment it reads the hub's real mac.
    Left alone, such a row is a time bomb with the fuse lit by the console:
    whichever restart follows the mac landing matches nothing, mints a second
    device, and leaves eleven entities with no state object while eleven more
    take ``_2`` entity ids that no automation names. Both halves of that are
    covered here -- the mac has not arrived yet in the first two cases and had
    already arrived in the last two, and neither ends in a second device.

    So the row is re-filed, once, under the identity a fresh install would mint
    for that hub today, and the eleven unique_ids built from the old string move
    with it. What a user can see does not move at all: same device row, same
    entity ids, same states, same customisations.

    The guess it makes is one row, one hub. That is not much of a guess -- the
    string on the row never named hardware, so there was never a second
    candidate -- and it is the same inference the registry seed already makes
    every process start, taken once and written down instead of re-taken on
    every restart. Where there is more than one of either, it refuses: see
    ``test_a_row_naming_no_hardware_is_left_alone_when_anything_is_ambiguous``.
    """
    entry = add_entry(hass)
    device = as_an_earlier_release_left_it(hass, entry, filed_as)
    registry = er.async_get(hass)
    before = {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_device(
            registry, device.id, include_disabled_entities=True
        )
    }
    assert len(before) == len(RELEASED_ENTITIES)

    await start(hass, entry, FakeConsole(hub_json(mac=reports)))

    moved = {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_device(
            registry, device.id, include_disabled_entities=True
        )
    }
    assert set(moved) == {f"{becomes}_{suffix}" for _, suffix in RELEASED_ENTITIES}
    assert unique_ids(hass) == set(moved)
    # The same eleven registry rows, on the same device row, under new keys --
    # which is the whole point: unique_id is the registry's plumbing and
    # entity_id is what every automation in the house is written against.
    assert sorted(moved.values()) == sorted(before.values())
    assert [row.identifiers for row in hub_devices(hass, entry)] == [
        {(DOMAIN, becomes)}
    ]
    assert hass.states.get(before[f"{filed_as}_zone_6"]).state == STATE_OFF

    # ...and it holds through the mac landing and the restart that used to be
    # where the second device appeared. The migration does not run twice: the
    # identity it moved to names hardware, which is the condition it tests.
    await restart(hass, entry, FakeConsole(hub_json(mac=MAC)))

    assert unique_ids(hass) == set(moved)
    assert [row.identifiers for row in hub_devices(hass, entry)] == [
        {(DOMAIN, becomes)}
    ]
    assert hass.states.get(before[f"{filed_as}_zone_6"]).state == STATE_OFF


@pytest.mark.parametrize(
    "reports",
    [
        pytest.param(MAC, id="the hub the other row is for"),
        pytest.param("112233445566", id="a hub neither row is for"),
    ],
)
async def test_a_row_naming_no_hardware_is_left_alone_when_anything_is_ambiguous(
    hass, reports
):
    """The limit that stays, pinned so it stays deliberate.

    Re-filing a row that names no hardware is only safe while there is exactly
    one row and exactly one hub, because then there is nothing to pair wrongly.
    A second row means the install has run more than one hub, and a snapshot
    missing one of them -- rebooting, un-adopted, mid-adoption, replaced -- would
    offer the survivor to a row that is not its own, handing eleven entities to
    different hardware. The second case here is that exactly: one hub, neither
    row's, and one unmatched row naming no hardware. Pairing them is the guess
    round 3 named and this still refuses to make; it is also the only one of the
    two the identifier-collision check does not catch on its own.

    Refusing leaves the pre-0.3 two-hub install where it was: recognised for as
    long as the console keeps reporting the string on the row, and a new device
    on the restart after it stops. Not a regression -- 0.2 recomputed identity
    from the payload on every start, so it moved on that same restart -- and not
    repeatable, because no row is created under either string any more (see
    ``logic.device_identifier``).
    """
    entry = add_entry(hass)
    as_an_earlier_release_left_it(hass, entry, "")
    as_an_earlier_release_left_it(hass, entry, MAC, name="Shed Hub")

    await start(hass, entry, FakeConsole(hub_json(mac=reports)))

    assert {"_armed", f"{MAC}_armed"} <= unique_ids(hass)
    assert {frozenset({(DOMAIN, "")}), frozenset({(DOMAIN, MAC)})} <= {
        frozenset(row.identifiers) for row in hub_devices(hass, entry)
    }


async def test_a_row_naming_no_hardware_is_left_alone_beside_a_second_hub(hass):
    """The other half of the ambiguity: one row, two hubs on the console.

    Which of them the row belongs to is exactly what a row naming no hardware
    does not say, so nothing is re-filed and the seed carries the install for as
    long as the console keeps reporting the string it was written under.
    """
    entry = add_entry(hass)
    as_an_earlier_release_left_it(hass, entry, PLACEHOLDER_MAC)

    await start(
        hass,
        entry,
        FakeConsole(hub_json(mac=PLACEHOLDER_MAC), hub_json("ah2", mac=MAC)),
    )

    assert f"{PLACEHOLDER_MAC}_armed" in unique_ids(hass)
    assert {frozenset(row.identifiers) for row in hub_devices(hass, entry)} == {
        frozenset({(DOMAIN, PLACEHOLDER_MAC)}),
        frozenset({(DOMAIN, MAC)}),
    }


async def test_a_row_naming_no_hardware_is_left_alone_if_the_new_ids_are_taken(hass):
    """All of it is planned before any of it is applied.

    ``async_update_entity`` raises on a unique_id already in use, so a partial
    plan would leave the row half-migrated -- some entities under the new
    identity, the rest under the old, and the device row under whichever of the
    two the last statement reached. The whole plan is built and checked before
    a line of it is applied, so a target that is taken calls the migration off
    rather than half-doing it: the install lands back on the pre-migration
    behaviour, which is the second device row asserted below, and not somewhere
    new.

    One stranded id is enough to prove it, and one is what a registry in this
    state holds: an entity from a run that already minted the second identity,
    left behind when its device row was deleted.
    """
    entry = add_entry(hass)
    device = as_an_earlier_release_left_it(hass, entry, PLACEHOLDER_MAC)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "binary_sensor", DOMAIN, f"{MAC}_zone_6", config_entry=entry
    )
    before = {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_device(
            registry, device.id, include_disabled_entities=True
        )
    }

    await start(hass, entry, FakeConsole(hub_json(mac=MAC)))

    assert {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_device(
            registry, device.id, include_disabled_entities=True
        )
    } == before
    assert {frozenset(row.identifiers) for row in hub_devices(hass, entry)} == {
        frozenset({(DOMAIN, PLACEHOLDER_MAC)}),
        frozenset({(DOMAIN, MAC)}),
    }


async def test_a_malformed_identifier_does_not_load_an_entry_with_no_entities(hass):
    """The device registry restores identifiers with no arity checked.

    ``DeviceRegistry._async_load_data`` rebuilds each one as ``tuple(iden)``
    straight out of JSON, so a hand-edited or partially restored
    ``core.device_registry`` can hold a one-element identifier -- and unpacking
    it into ``domain, identifier`` raises ValueError while the seed is being
    read. Raised there it is caught by platform setup and logged, so the entry
    reports LOADED, publishes not one entity, and says nothing about why: the
    worst shape a failure can take on an alarm integration, because every
    automation just stops without anything going unavailable to test for.
    """
    entry = add_entry(hass)
    device = as_an_earlier_release_left_it(hass, entry, MAC)
    dr.async_get(hass).async_update_device(
        device.id, new_identifiers={(DOMAIN, MAC), (DOMAIN,)}
    )

    await start(hass, entry, FakeConsole(hub_json(mac=MAC)))

    assert unique_ids(hass) == {f"{MAC}_{suffix}" for _, suffix in RELEASED_ENTITIES}
    assert hass.states.get(_zone_entity_id(hass)).state == STATE_OFF
    assert len(hub_devices(hass, entry)) == 1


async def test_an_unnamed_hub_does_not_take_generic_entity_ids(hass):
    """``has_entity_name`` composes "<device> <entity>", so a nameless device
    degrades every entity id to a bare generic and two hubs then fight over them.
    The old "Alarm Hub" default only fired when the hub object was missing
    entirely, never when the console simply sent no name.
    """
    await setup_integration(hass, FakeConsole(hub_json(name=None)))

    assert _zone_entity_id(hass) == "binary_sensor.alarm_hub_aabbccddeeff_garage_entry"


# --- S5: the reconcile listener outlives the platform it adds to ---


@asynccontextmanager
async def _slow_platform_reset():
    """Give ``EntityPlatform.async_reset`` one extra scheduling point.

    The teardown window is real but narrow: ``async_reset`` awaits each entity's
    removal in turn, and a frame has to land between the list it snapshots and
    the entities it is still working through. One ``sleep(0)`` is what a loaded
    event loop hands out for free, and it makes the window wide enough to hit
    from a test deterministically.
    """
    real_reset = EntityPlatform.async_reset

    async def slow_reset(self: EntityPlatform) -> None:
        await asyncio.sleep(0)
        await real_reset(self)

    with patch.object(EntityPlatform, "async_reset", slow_reset):
        yield


async def test_a_frame_during_teardown_does_not_orphan_an_entity(hass):
    """Teardown resets the platforms while the reconcile listener is still live.

    ``entry.async_on_unload`` runs *after* ``async_unload_platforms``, so a
    frame arriving mid-teardown asks a platform that is being reset for a new
    entity. It lands behind ``EntityPlatform.async_reset``'s snapshot of what to
    remove, and so outlives the entry: still in the state machine, still holding
    a listener on a shut-down coordinator, reporting a stale "on" for good.

    ``async_reset`` is given one extra scheduling point, which any loaded event
    loop provides for free. That widens the window; it does not invent it.

    An entity Home Assistant removed properly keeps its state row as
    ``unavailable`` -- that is how a registry-backed entity is retired -- so
    what is asserted is that *nothing still reports*. An orphan reports the
    status of the frame that made it, forever.
    """
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    assert published_states(hass, "binary_sensor")  # there is something to orphan

    async with _slow_platform_reset():
        unload = hass.async_create_task(
            hass.config_entries.async_unload(entry.entry_id)
        )
        for index in range(6):
            await asyncio.sleep(0)
            if not unload.done():
                console.push(new_zone_frame(str(20 + index), f"Ghost {index}"))
        assert await unload
        await hass.async_block_till_done()

    reporting = {
        entity_id: state
        for entity_id, state in published_states(hass, "binary_sensor").items()
        if state != STATE_UNAVAILABLE
    }
    assert reporting == {}


async def test_an_entry_reloaded_after_a_teardown_frame_gets_its_entities_back(hass):
    """The orphan's real cost: it blocked its own replacement.

    A ghost still holding a unique_id makes the next setup refuse the entity
    that should own it -- "Platform unifi_protect_alarm_hub does not generate
    unique IDs" -- so the zone comes back reporting whatever the ghost last
    read, from a coordinator that will never update again.
    """
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    assert published_states(hass, "binary_sensor")  # there is something to orphan

    async with _slow_platform_reset():
        unload = hass.async_create_task(
            hass.config_entries.async_unload(entry.entry_id)
        )
        for _ in range(4):
            await asyncio.sleep(0)
            if not unload.done():
                console.push(new_zone_frame("9", "Ghost Zone"))
        assert await unload
        await hass.async_block_till_done()

    console.payloads[0]["alarmHub"]["input"]["9"] = {
        "enable": "on",
        "status": "normal",
        "inputType": "ENTRY",
        "name": "Ghost Zone",
    }
    await setup_integration(hass, console)

    assert hass.states.get("binary_sensor.alarm_hub_kit_ghost_zone").state == STATE_OFF


async def test_the_reconcile_listener_does_not_outlive_the_entry(hass):
    """The subscription itself has to go, not merely fall silent.

    The teardown guard is what stops the orphan; dropping
    ``entry.async_on_unload`` around the subscription would still leave three
    closures -- one per platform -- attached to a coordinator the entry has
    finished with, and every reload would add three more.
    """
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    coordinator = entry.runtime_data
    assert coordinator._listeners  # the entities, plus the three reconcile passes

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert coordinator._listeners == {}


# --- S6: an entity's timers must not outlive it ---


async def test_the_expiry_timer_does_not_outlive_the_entity(hass, freezer):
    """An armed timer holds a five-minute reference to a dead entity.

    Harmless where it fires -- ``async_write_ha_state`` on a removed entity is
    a documented no-op -- but every reload of the integration would leave one
    behind per entity.
    """
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    console.rest_error = AlarmHubConnectionError("no route")
    await failed_poll(hass, freezer)
    console.push(zone_frame("alarm"))  # every entity arms its expiry timer
    await hass.async_block_till_done()
    assert armed_call_later_handles(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert armed_call_later_handles(hass) == []
