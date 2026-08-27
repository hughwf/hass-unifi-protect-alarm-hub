"""Tests for the binary_sensor platform: what gets created, and when it changes.

Everything here runs against a real config-entry setup (see
``platform_common``), because the questions being asked -- does an entity exist,
is it available, what is its friendly name -- are questions about the entity
registry and the state machine, not about the objects underneath them.
"""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er

from custom_components.unifi_protect_alarm_hub.const import DOMAIN
from platform_common import (  # noqa: F401  (unload_entries is an autouse fixture)
    MAC,
    FakeConsole,
    entity_ids,
    hub_frame,
    hub_json,
    poll,
    published_states,
    setup_integration,
    unique_ids,
    unload_entries,
)

# The error entity_platform logs when two entities claim one unique_id. It is
# only a log line -- the second entity is dropped and the count never changes --
# so a duplicate-prevention test that watched the count alone would pass either
# way.
DUPLICATE_UNIQUE_ID = "does not generate unique IDs"

HALLWAY = "binary_sensor.alarm_hub_kit_hallway"

# Every unique_id one ordinary hub produces. Spelled out as literals built from
# the raw MAC, because that is the upgrade contract: these exact strings are in
# the entity registry of every existing install, and routing unique_ids through
# ``device_identifier`` is only safe while it keeps producing them.
EXPECTED_UNIQUE_IDS = {
    f"{MAC}_zone_4",
    f"{MAC}_zone_4_fault",
    f"{MAC}_zone_6",
    f"{MAC}_zone_6_fault",
    f"{MAC}_tamper",
    f"{MAC}_armed",
    f"{MAC}_connectivity",
    f"{MAC}_battery_connection",
    f"{MAC}_battery_status",
    f"{MAC}_battery_voltage",
    f"{MAC}_output_1",
}


async def test_a_hub_adopted_after_setup_gets_its_entities(hass):
    """The config flow lets setup finish with nothing adopted yet.

    Which used to mean the entry loaded empty and stayed empty: adopting the
    hub filled ``coordinator.data`` and created nothing, so the integration
    looked broken until someone reloaded it by hand.
    """
    console = FakeConsole()
    entry = await setup_integration(hass, console)
    assert entity_ids(hass, "binary_sensor") == []

    console.payloads.append(hub_json())
    await poll(hass, entry)

    assert entity_ids(hass, "binary_sensor") == [
        "binary_sensor.alarm_hub_kit_armed",
        "binary_sensor.alarm_hub_kit_backup_battery_connection",
        "binary_sensor.alarm_hub_kit_connectivity",
        "binary_sensor.alarm_hub_kit_garage_entry",
        "binary_sensor.alarm_hub_kit_garage_entry_fault",
        "binary_sensor.alarm_hub_kit_hallway",
        "binary_sensor.alarm_hub_kit_hallway_fault",
        "binary_sensor.alarm_hub_kit_tamper",
    ]


async def test_an_ordinary_hub_keeps_the_unique_ids_it_has_always_had(hass):
    """The upgrade contract, end to end and across all three platforms.

    unique_ids are now composed from ``logic.device_identifier`` rather than
    from ``hub.mac`` directly, which is what stops a blank or missing mac from
    silently reissuing every id. That change is only safe because the identifier
    of a hub with a usable mac *is* that mac: anything else would strand every
    registry entry on every existing install, reissue the entity ids with _2
    suffixes, and leave every alarm automation pointing at nothing.
    """
    await setup_integration(hass, FakeConsole(hub_json()))

    assert unique_ids(hass) == EXPECTED_UNIQUE_IDS


async def test_a_zone_wired_in_later_gets_entities(hass):
    """Wiring a new contact is the ordinary case, not an edge one."""
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    assert "binary_sensor.alarm_hub_kit_back_door" not in entity_ids(
        hass, "binary_sensor"
    )

    console.payloads[0]["alarmHub"]["input"]["7"] = {
        "enable": "on",
        "status": "alarm",
        "inputType": "ENTRY",
        "name": "Back Door",
    }
    await poll(hass, entry)

    state = hass.states.get("binary_sensor.alarm_hub_kit_back_door")
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes["device_class"] == "door"
    assert hass.states.get("binary_sensor.alarm_hub_kit_back_door_fault") is not None


async def test_a_battery_and_cover_reported_later_get_their_entities(hass):
    """``battery`` and ``cover`` are optional sections that turn up late.

    Gated at setup, a hub that only started reporting a backup battery -- or
    one fitted afterwards -- never got a tamper or battery-connection sensor.
    """
    payload = hub_json()
    del payload["alarmHub"]["battery"]
    del payload["alarmHub"]["cover"]
    console = FakeConsole(payload)
    entry = await setup_integration(hass, console)
    assert hass.states.get("binary_sensor.alarm_hub_kit_tamper") is None
    assert (
        hass.states.get("binary_sensor.alarm_hub_kit_backup_battery_connection") is None
    )

    console.payloads[0]["alarmHub"]["cover"] = {"status": "open", "distance": 0}
    console.payloads[0]["alarmHub"]["battery"] = {
        "connection": "connected",
        "voltage": 12.4,
        "batteryStatus": "ok",
    }
    await poll(hass, entry)

    assert hass.states.get("binary_sensor.alarm_hub_kit_tamper").state == STATE_ON
    assert (
        hass.states.get("binary_sensor.alarm_hub_kit_backup_battery_connection").state
        == STATE_ON
    )


async def test_an_intact_case_reads_clear_and_an_opened_one_reads_tampered(hass):
    """Both halves of the tamper sensor, against the wire values the spec gives.

    The fixture used to send ``closed``, which ``logic.cover_is_on`` rightly
    refuses, so every Tier-2 test ran against a tamper sensor stuck at
    ``unknown`` and nothing ever exercised an intact case at all.
    """
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    assert hass.states.get("binary_sensor.alarm_hub_kit_tamper").state == STATE_OFF

    console.payloads[0]["alarmHub"]["cover"]["status"] = "open"
    await poll(hass, entry)

    assert hass.states.get("binary_sensor.alarm_hub_kit_tamper").state == STATE_ON


async def test_a_cover_the_hub_stops_reporting_stops_reading_clear(hass):
    """``cover`` is optional, and a delta may null it away (``keeps_hub_shape``).

    The section is how the hub describes its case; without one there is nothing
    to read, and "Clear" on a tamper sensor is the reassurance nobody may get
    for free. The pure predicate has always said so -- this is the entity
    saying it, which is where an automation reads it.
    """
    console = FakeConsole(hub_json())
    await setup_integration(hass, console)
    assert hass.states.get("binary_sensor.alarm_hub_kit_tamper").state == STATE_OFF

    console.push(hub_frame(alarmHub={"cover": None}))
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.alarm_hub_kit_tamper").state == STATE_UNKNOWN


async def test_a_zone_the_hub_reports_as_disabled_is_created_disabled(hass):
    """``enable: off`` is the hub saying nothing is wired to that channel.

    Its status means nothing in particular, and on the ``door`` device_class the
    entity would publish that nothing as a confident "Closed". Created disabled,
    it is there for anyone who wants it and silent for everyone else.
    """
    payload = hub_json()
    payload["alarmHub"]["input"]["6"]["enable"] = "off"
    await setup_integration(hass, FakeConsole(payload))

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{MAC}_zone_6")
    assert entity_id is not None
    assert registry.async_get(entity_id).disabled_by is (
        er.RegistryEntryDisabler.INTEGRATION
    )
    assert hass.states.get(entity_id) is None
    # ...while the channel the hub does have wired stays enabled and reporting.
    assert hass.states.get(HALLWAY).state == STATE_OFF


async def test_repeated_polls_do_not_create_a_second_set_of_entities(
    hass, caplog: pytest.LogCaptureFixture
):
    """The reconcile pass runs on every update, so it has to be idempotent."""
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    before = entity_ids(hass, "binary_sensor")
    assert before  # or this compares an empty list against an empty list

    caplog.clear()
    await poll(hass, entry)
    await poll(hass, entry)

    assert entity_ids(hass, "binary_sensor") == before
    assert DUPLICATE_UNIQUE_ID not in caplog.text


async def test_re_adopting_the_hub_leaves_every_entity_still_reporting(
    hass, caplog: pytest.LogCaptureFixture
):
    """Re-adopting changes the hub's id; the MAC the entities are built on does not.

    Two halves have to hold together, and the first without the second is worse
    than neither. The reconcile pass must not build a second set of entities for
    the same hardware -- it dedups on unique_id, so it does not. But the
    entities that already exist must also *find* the hub again: bound to the id
    they were constructed with, they all went unavailable the moment the id
    changed, permanently, with live state one dict entry away and no
    replacement ever built precisely because the dedup was working.

    Nothing in the entity list or the log says any of that, which is why this
    asserts on what the entities publish. Home Assistant drops unavailable
    entities from service calls, so the siren silently accepting
    ``switch.turn_on`` and sending nothing was part of the same failure.
    """
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    before = entity_ids(hass, "binary_sensor")

    caplog.clear()
    console.payloads[0]["id"] = "ah2"
    await poll(hass, entry)

    assert entity_ids(hass, "binary_sensor") == before
    assert DUPLICATE_UNIQUE_ID not in caplog.text
    assert unique_ids(hass) == EXPECTED_UNIQUE_IDS

    published = published_states(hass, "binary_sensor", "switch", "sensor")
    assert STATE_UNAVAILABLE not in published.values(), published
    assert published[HALLWAY] == STATE_OFF

    # ...and they track the hub afterwards, rather than merely surviving once.
    console.payloads[0]["alarmHub"]["input"]["4"]["status"] = "alarm"
    await poll(hass, entry)

    assert hass.states.get(HALLWAY).state == STATE_ON


async def test_a_mac_that_arrives_after_the_first_poll_does_not_duplicate_anything(
    hass,
):
    """Mid-adoption, /v1/alarm-hubs can answer before the mac is populated.

    Built from the raw mac, the entities from that snapshot carried unique_ids
    with an empty one; when the mac turned up, the reconcile pass saw eleven
    unique_ids it had never added and built a whole second set. The hub ended up
    with two of everything, permanently, the first set still pointing at the
    same live hub. A delta that nulls the mac does the same thing in reverse.
    """
    payload = hub_json()
    payload["mac"] = ""
    console = FakeConsole(payload)
    entry = await setup_integration(hass, console)
    before = unique_ids(hass)
    assert before  # the macless hub is identified by its id and does get entities

    console.payloads[0]["mac"] = MAC
    await poll(hass, entry)

    assert unique_ids(hass) == before
    assert hass.states.get(HALLWAY).state == STATE_OFF


async def test_a_mac_that_stops_being_reported_does_not_duplicate_anything(hass):
    """The same defect in reverse, and the one a WebSocket delta can cause.

    ``deep_merge`` treats a null as a removal, so ``{"mac": null}`` leaves the
    hub with an empty mac -- and with identity derived fresh each time, that is
    a brand-new device with a brand-new set of unique_ids, while the entities
    that were already there can no longer find their hub at all.
    """
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    assert unique_ids(hass) == EXPECTED_UNIQUE_IDS

    console.payloads[0]["mac"] = ""
    await poll(hass, entry)

    assert unique_ids(hass) == EXPECTED_UNIQUE_IDS
    assert hass.states.get(HALLWAY).state == STATE_OFF


async def test_a_zone_that_disappears_goes_unavailable_and_keeps_its_entity(hass):
    """A poll that comes back short must not delete anybody's entities.

    REST is authoritative about what exists, but "authoritative" is not
    "infallible": a mid-adoption snapshot, or a console that briefly omits a
    section, would take the registry entry with it -- and with that the
    entity_id every alarm automation is written against. Unavailable is
    reversible and testable; deleted is neither.
    """
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    registry = er.async_get(hass)

    del console.payloads[0]["alarmHub"]["input"]["4"]
    await poll(hass, entry)

    assert hass.states.get(HALLWAY).state == STATE_UNAVAILABLE
    assert (
        registry.async_get_entity_id("binary_sensor", DOMAIN, f"{MAC}_zone_4")
        == HALLWAY
    )

    console.payloads[0]["alarmHub"]["input"]["4"] = {
        "enable": "on",
        "status": "alarm",
        "inputType": "MOTION",
        "name": "Hallway",
    }
    await poll(hass, entry)

    assert hass.states.get(HALLWAY).state == STATE_ON


async def test_the_connectivity_sensor_survives_the_hub_going_offline(hass):
    """The entity that reports the outage must not be erased by it.

    ``entity.py`` takes every entity unavailable while the hub is not
    CONNECTED, which is right for a door contact nobody can read and exactly
    wrong here: this sensor's whole job is to say "off". It opts out via
    ``_survives_hub_offline``, and this is the only place that contract is
    exercised against a real entity.
    """
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    assert hass.states.get("binary_sensor.alarm_hub_kit_connectivity").state == STATE_ON

    console.payloads[0]["state"] = "DISCONNECTED"
    await poll(hass, entry)

    assert hass.states.get("binary_sensor.alarm_hub_kit_connectivity").state == (
        STATE_OFF
    )
    # ...while everything that reads through the hub does go unavailable.
    assert hass.states.get(HALLWAY).state == STATE_UNAVAILABLE


async def test_renaming_a_zone_moves_the_friendly_name_not_the_entity_id(hass):
    """Renaming a zone in the UniFi app is how a door gets labelled.

    Read once in __init__, the name was frozen for the life of the entity. The
    entity_id is the opposite case: it is what automations reference, so it
    stays where it was.
    """
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    assert (
        hass.states.get(HALLWAY).attributes["friendly_name"] == "Alarm Hub Kit Hallway"
    )

    console.payloads[0]["alarmHub"]["input"]["4"]["name"] = "Upstairs Landing"
    await poll(hass, entry)

    state = hass.states.get(HALLWAY)
    assert state.attributes["friendly_name"] == "Alarm Hub Kit Upstairs Landing"
    assert (
        hass.states.get("binary_sensor.alarm_hub_kit_hallway_fault").attributes[
            "friendly_name"
        ]
        == "Alarm Hub Kit Upstairs Landing Fault"
    )


async def test_re_typing_a_zone_moves_its_device_class(hass):
    """``inputType`` decides whether the frontend says open/closed or detected.

    Moving a channel from a door contact to a PIR is a wiring change someone
    makes in the app; captured at construction, the entity kept rendering as
    the old kind indefinitely.
    """
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    assert (
        hass.states.get("binary_sensor.alarm_hub_kit_garage_entry").attributes[
            "device_class"
        ]
        == "door"
    )

    console.payloads[0]["alarmHub"]["input"]["6"]["inputType"] = "MOTION"
    await poll(hass, entry)

    assert (
        hass.states.get("binary_sensor.alarm_hub_kit_garage_entry").attributes[
            "device_class"
        ]
        == "motion"
    )
