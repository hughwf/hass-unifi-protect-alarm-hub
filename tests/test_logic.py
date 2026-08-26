"""Tier-1 pure-logic tests (pytest only)."""

from __future__ import annotations

import pytest

from custom_components.unifi_protect_alarm_hub import logic
from custom_components.unifi_protect_alarm_hub.models import (
    AlarmHub,
    Battery,
    Cover,
    InputZone,
    OutputChannel,
)


def _zone(**kw) -> InputZone:
    base = dict(
        zone_id=1,
        enable="on",
        type="nc",
        status="normal",
        input_type="ENTRY",
        name=None,
        last_triggered_at=None,
        camera_id=None,
    )
    base.update(kw)
    return InputZone(**base)


def _output(**kw) -> OutputChannel:
    base = dict(
        output_id=1,
        active="off",
        enable="on",
        status="dry",
        name=None,
        delay=None,
        duration=None,
    )
    base.update(kw)
    return OutputChannel(**base)


def _hub(state="CONNECTED", **kw) -> AlarmHub:
    base = dict(
        id="ah1",
        name="Hub",
        mac="AABBCC",
        state=state,
        is_alarm_hub=True,
        alarm_hub_armed="on",
        alarm_hub_battery=Battery("connected", "on", 13.2, "ok"),
        alarm_hub_cover=Cover("open", 5),
        alarm_hub_inputs={1: _zone()},
        alarm_hub_outputs={1: _output()},
    )
    base.update(kw)
    return AlarmHub(**base)


def test_zone_is_on_only_when_alarm():
    assert logic.zone_is_on(_zone(status="alarm")) is True
    assert logic.zone_is_on(_zone(status="normal")) is False


@pytest.mark.parametrize("status", ["fault", "short", "cut", "unknown", None])
def test_zone_is_on_never_calls_an_unreadable_loop_closed(status):
    """A wire we cannot read is not a secure door.

    ``cut`` is the textbook defeat for a wired contact and ``None`` arrives on
    any zone first seen through a partial WebSocket frame. Both used to map to
    False, which on the ``door`` device_class renders as "Closed" -- an
    affirmative all-clear that every ``state == 'off'`` alarm condition passes.
    """
    assert logic.zone_is_on(_zone(status=status)) is None


def test_zone_fault_is_on():
    for s in ("fault", "short", "cut"):
        assert logic.zone_fault_is_on(_zone(status=s)) is True
    for s in ("normal", "alarm"):
        assert logic.zone_fault_is_on(_zone(status=s)) is False


@pytest.mark.parametrize("status", ["unknown", None])
def test_zone_fault_is_on_does_not_certify_an_unreadable_loop(status):
    # Only the two statuses that describe an intact loop may say "no fault".
    assert logic.zone_fault_is_on(_zone(status=status)) is None


def test_zone_device_class_mapping():
    assert logic.zone_device_class(_zone(input_type="MOTION")) == "motion"
    assert logic.zone_device_class(_zone(input_type="ENTRY")) == "door"
    assert logic.zone_device_class(_zone(input_type="SMOKE")) == "smoke"
    assert logic.zone_device_class(_zone(input_type="GLASS_BREAK")) == "sound"
    assert logic.zone_device_class(_zone(input_type="EMERGENCY_BUTTON")) == "safety"
    assert logic.zone_device_class(_zone(input_type=None)) == "safety"
    assert logic.zone_device_class(_zone(input_type="unknown")) == "safety"


def test_zone_enabled_default():
    assert logic.zone_enabled_default(_zone(enable="on")) is True
    assert logic.zone_enabled_default(_zone(enable="off")) is False


def test_zone_name_fallback():
    assert logic.zone_name(_zone(name="Front Door"), 3) == "Front Door"
    assert logic.zone_name(_zone(name=None), 3) == "Zone 3"


def test_unique_ids():
    assert logic.entity_unique_id("M", "tamper") == "M_tamper"
    assert logic.zone_unique_id("M", 2) == "M_zone_2"
    assert logic.zone_fault_unique_id("M", 2) == "M_zone_2_fault"
    assert logic.output_unique_id("M", 5) == "M_output_5"


@pytest.mark.parametrize(
    "mac", ["AABBCCDDEEFF", "aabbccddeeff", "AA:BB:CC:DD:EE:FF", "0"]
)
def test_the_device_identifier_of_a_hub_with_a_mac_is_that_mac_verbatim(mac):
    """The upgrade contract, and the reason unique_ids may be routed through it.

    Every unique_id is now ``entity_unique_id(device_identifier(...), ...)``
    rather than ``f"{hub.mac}_..."``. Existing installs hold the mac-built ids
    in their entity registry, so a hub the console describes normally must keep
    producing exactly those: an identifier that merely *usually* equals the mac
    would orphan every entity on upgrade -- registry entries stranded, entity
    ids reissued with _2 suffixes, and every automation written against the old
    ones quietly pointing at nothing. Only the blank / missing / non-string
    cases, which never made a usable id in the first place, may differ.
    """
    assert logic.device_identifier(_hub(mac=mac), "ah1") == mac
    assert logic.zone_unique_id(mac, 4) == f"{mac}_zone_4"


def test_output_is_on_and_name():
    assert logic.output_is_on(_output(active="on")) is True
    assert logic.output_is_on(_output(active="off")) is False
    assert logic.output_name(_output(name="Siren"), 1) == "Siren"
    assert logic.output_name(_output(name=None), 1) == "Output 1"


@pytest.mark.parametrize("active", ["unknown", None])
def test_output_is_on_does_not_report_a_siren_it_cannot_read_as_off(active):
    assert logic.output_is_on(_output(active=active)) is None


def test_output_confirms_only_when_the_hub_reports_what_was_asked_for():
    assert logic.output_confirms(_output(active="on"), True) is True
    assert logic.output_confirms(_output(active="off"), False) is True
    assert logic.output_confirms(_output(active="off"), True) is False
    assert logic.output_confirms(_output(active="on"), False) is False


@pytest.mark.parametrize(
    "output", [_output(active="unknown"), _output(active=None), None]
)
def test_output_confirms_refuses_to_read_silence_as_confirmation(output):
    """A relay the hub will not describe has not confirmed anything.

    This is what retires the switch's optimistic value early, so an unreadable
    ``active`` counting as agreement would drop the expected state in favour of
    the ``unknown`` it was standing in for -- turning a command that worked into
    a switch that says it cannot tell.
    """
    assert logic.output_confirms(output, True) is False
    assert logic.output_confirms(output, False) is False


def test_hub_predicates():
    assert logic.hub_is_connected(_hub("CONNECTED")) is True
    assert logic.hub_is_connected(_hub("DISCONNECTED")) is False
    assert logic.armed_is_on("on") is True
    assert logic.armed_is_on("off") is False
    assert logic.cover_is_on(Cover("open", 0)) is True
    assert logic.cover_is_on(Cover("close", 0)) is False
    assert logic.battery_connected_is_on(Battery("connected", None, None, "ok")) is True
    assert (
        logic.battery_connected_is_on(Battery("disconnected", None, None, None))
        is False
    )


def test_hub_predicates_stay_silent_on_values_they_cannot_read():
    """Every other on/off predicate has ``zone_is_on``'s shape, and its trap.

    A hub state that never arrived is not "disconnected", a hub that stopped
    reporting a cover is not an intact case, and "Clear" on a tamper sensor is
    exactly the reassurance nobody should get for free.
    """
    # Only a hub that reported *no* state is unknown: DISCONNECTED, rebooting
    # and mid-adoption are all states, and False is right for all of them.
    assert logic.hub_is_connected(_hub(None)) is None
    assert logic.hub_is_connected(_hub(["CONNECTED"])) is None
    assert logic.armed_is_on(None) is None
    assert logic.armed_is_on("unknown") is None
    assert logic.cover_is_on(None) is None
    assert logic.cover_is_on(Cover(None, 0)) is None
    assert logic.battery_connected_is_on(None) is None
    assert logic.battery_connected_is_on(Battery(None, None, None, None)) is None


@pytest.mark.parametrize("status", ["closed", "opened", "OPEN", "tamper", ""])
def test_cover_is_on_refuses_a_status_the_console_never_sends(status):
    """``close`` and ``open`` are the whole vocabulary, and the set is closed.

    Widening it is the easy mistake -- ``closed`` reads like the obvious synonym
    -- and every value admitted that way becomes an affirmative "Clear" on a
    tamper sensor from a string the console did not mean as one. The fixture
    made exactly that error in the other direction, and nothing failed.
    """
    assert logic.cover_is_on(Cover(status, 0)) is None


def test_push_is_fresh_times_the_delivery_not_the_socket():
    assert logic.push_is_fresh(1000.0, 1000.0, 300.0) is True
    assert logic.push_is_fresh(1000.0, 1299.0, 300.0) is True
    # At exactly one window it has stopped standing in for the poll it replaced.
    assert logic.push_is_fresh(1000.0, 1300.0, 300.0) is False
    assert logic.push_is_fresh(1000.0, 9999.0, 300.0) is False


def test_device_identifier_prefers_the_mac():
    assert logic.device_identifier(_hub(mac="AABBCCDDEEFF"), "ah1") == "AABBCCDDEEFF"


@pytest.mark.parametrize("mac", ["", None, ["AABBCC"], 42])
def test_device_identifier_falls_back_when_the_mac_is_unusable(mac):
    """``mac`` is unvalidated console JSON and it is the whole device identity.

    A list is unhashable and raised TypeError inside ``DeviceInfo(identifiers=)``,
    failing platform setup; an empty string collapses two hubs onto one identity.
    ``hub_id`` is already validated as a usable string by the coordinator.
    """
    assert logic.device_identifier(_hub(mac=mac), "ah1") == "ah1"


def test_device_identifier_without_a_hub():
    assert logic.device_identifier(None, "ah1") == "ah1"


def _ids(*known: str) -> logic.HubDeviceIds:
    return logic.HubDeviceIds(known)


def test_hub_keys_offers_the_mac_first_and_the_id_always():
    assert logic.hub_keys(_hub(mac="AABBCC"), "ah1") == ("AABBCC", "ah1")
    assert logic.hub_keys(_hub(mac=""), "ah1") == ("ah1",)
    assert logic.hub_keys(_hub(mac=["AABBCC"]), "ah1") == ("ah1",)
    assert logic.hub_keys(None, "ah1") == ("ah1",)


def test_a_re_adopted_hub_keeps_the_device_it_already_was():
    """Re-adoption issues a new device id for the same hardware.

    An entity built when the hub was "ah1" must still resolve after the console
    starts calling it "ah2": resolving by the coordinator's key instead left
    every entity on the device permanently unavailable with live state one dict
    entry away, and the reconcile pass -- correctly refusing to duplicate a
    device whose unique_ids it already holds -- never built a replacement.
    """
    devices = _ids()
    before = {"ah1": _hub(id="ah1", mac="AABBCC")}
    assert devices.resolve(before["ah1"], "ah1") == "AABBCC"

    after = {"ah2": _hub(id="ah2", mac="AABBCC")}

    assert devices.resolve(after["ah2"], "ah2") == "AABBCC"
    # ...and the entity finds it again, under the key the API now needs.
    assert devices.find(after, "AABBCC") == ("ah2", after["ah2"])


def test_a_mac_that_arrives_late_does_not_mint_a_second_device():
    """Mid-adoption, /v1/alarm-hubs can answer before the mac is populated.

    Derived fresh each time, identity flips from the id to the mac the moment
    it appears, every unique_id under the hub changes with it, and the hub ends
    up with two full sets of entities -- the first still pointing at the same
    live hardware, permanently.
    """
    devices = _ids()
    assert devices.resolve(_hub(id="ah1", mac=""), "ah1") == "ah1"

    hubs = {"ah1": _hub(id="ah1", mac="AABBCC")}

    assert devices.resolve(hubs["ah1"], "ah1") == "ah1"
    assert devices.find(hubs, "ah1") == ("ah1", hubs["ah1"])


def test_a_mac_that_stops_being_reported_does_not_mint_a_second_device():
    """The same defect in reverse: a delta that nulls ``mac``."""
    devices = _ids()
    assert devices.resolve(_hub(id="ah1", mac="AABBCC"), "ah1") == "AABBCC"

    hubs = {"ah1": _hub(id="ah1", mac="")}

    assert devices.resolve(hubs["ah1"], "ah1") == "AABBCC"
    assert devices.find(hubs, "AABBCC") == ("ah1", hubs["ah1"])


def test_identities_already_in_the_device_registry_are_reused():
    """The seed is what makes the decision survive a restart.

    A hub adopted before its mac was populated carries id-based unique_ids for
    good. Recomputing identity from a snapshot that now has a mac would strand
    every one of them.
    """
    devices = _ids("ah1")
    hubs = {"ah1": _hub(id="ah1", mac="AABBCC")}

    assert devices.resolve(hubs["ah1"], "ah1") == "ah1"


def test_an_ordinary_hub_on_an_existing_install_keeps_its_mac_identity():
    devices = _ids("AABBCC")

    assert devices.resolve(_hub(id="ah1", mac="AABBCC"), "ah1") == "AABBCC"


def test_two_hubs_on_one_console_stay_two_devices():
    devices = _ids()
    hubs = {"ah1": _hub(id="ah1", mac="AABBCC"), "ah2": _hub(id="ah2", mac="DDEEFF")}
    for hub_id, hub in hubs.items():
        devices.resolve(hub, hub_id)

    assert devices.find(hubs, "DDEEFF") == ("ah2", hubs["ah2"])
    assert devices.find(hubs, "GGHHII") is None
    assert devices.find({}, "AABBCC") is None


def test_two_macless_hubs_do_not_collapse_into_one_device():
    devices = _ids()

    assert devices.resolve(_hub(id="ah1", mac=""), "ah1") == "ah1"
    assert devices.resolve(_hub(id="ah2", mac=""), "ah2") == "ah2"


def test_a_macless_hub_that_is_re_adopted_is_a_new_device():
    """The honest limit of following a device across a re-adoption.

    With no usable mac the identity *is* the id, so nothing in the payload ties
    the new id to the old one and a looser match would merge hubs that are not
    the same hardware. The old entities stay unavailable until the user removes
    the device.
    """
    devices = _ids()
    assert devices.resolve(_hub(id="ah1", mac=""), "ah1") == "ah1"

    hubs = {"ah2": _hub(id="ah2", mac="")}

    assert devices.resolve(hubs["ah2"], "ah2") == "ah2"
    assert devices.find(hubs, "ah1") is None


PLACEHOLDER_MAC = "000000000000"


def test_two_hubs_reporting_one_mac_stay_two_devices():
    """A mac that names two hubs at once names neither of them.

    The console reports a placeholder ``000000000000`` mid-adoption, and a
    console bug can repeat a real mac. Both hubs then took the same identity, so
    the second was refused every unique_id it asked for and got no entities at
    all -- and the set that survived answered from whichever hub ``find``
    reached first, which is REST list order. That is the serious half: an entity
    confidently publishing another physical hub's zone.
    """
    devices = _ids()
    hubs = {
        "ah1": _hub(id="ah1", mac=PLACEHOLDER_MAC),
        "ah2": _hub(id="ah2", mac=PLACEHOLDER_MAC),
    }
    devices.observe(hubs)

    assert devices.resolve(hubs["ah1"], "ah1") == PLACEHOLDER_MAC
    assert devices.resolve(hubs["ah2"], "ah2") == "ah2"
    # ...and each device finds its own hub, in either order the console lists
    # them: matching on the contested key made the first entry answer for both.
    backwards = dict(reversed(list(hubs.items())))
    for snapshot in (hubs, backwards):
        assert devices.find(snapshot, PLACEHOLDER_MAC) == ("ah1", hubs["ah1"])
        assert devices.find(snapshot, "ah2") == ("ah2", hubs["ah2"])


def test_a_contested_mac_does_not_change_sides_on_the_next_poll():
    """Identity is decided once -- an entity captures its device at construction.

    A rule that re-decided per snapshot would not merely pick a side, it would
    hand each hub's entities to the other hub's device on the poll after that.
    """
    devices = _ids()
    hubs = {
        "ah1": _hub(id="ah1", mac=PLACEHOLDER_MAC),
        "ah2": _hub(id="ah2", mac=PLACEHOLDER_MAC),
    }
    devices.observe(hubs)
    decided = {hub_id: devices.resolve(hub, hub_id) for hub_id, hub in hubs.items()}

    for _ in range(3):
        devices.observe(hubs)

    assert {
        hub_id: devices.resolve(hub, hub_id) for hub_id, hub in hubs.items()
    } == decided


def test_a_contested_mac_keeps_its_side_across_a_restart():
    """The seed says which identities exist, never which hub each belongs to.

    So after a restart the pair has to be sorted out again from the snapshot,
    and the console is free to list them the other way round. Reading the mac
    first, the hub whose own id is one of the seeded identities took the *mac*
    identity instead -- and with it every entity the other hub registered under
    it. The id is unique to one hub by construction; the mac is only unique
    while the console is telling the truth, so the id is what settles it.
    """
    devices = _ids(PLACEHOLDER_MAC, "ah2")  # what the first run left behind
    hubs = {
        "ah2": _hub(id="ah2", mac=PLACEHOLDER_MAC),
        "ah1": _hub(id="ah1", mac=PLACEHOLDER_MAC),
    }
    devices.observe(hubs)

    assert devices.resolve(hubs["ah1"], "ah1") == PLACEHOLDER_MAC
    assert devices.resolve(hubs["ah2"], "ah2") == "ah2"


def test_a_re_adoption_still_takes_over_the_identity_it_already_had():
    """The claimed-mac rule must not fire on the case the scheme exists for.

    A re-adoption *is* a mac appearing under a second id; what makes it not a
    collision is that the first id is gone. Refusing it would split one hub into
    two devices -- the same damage as collapsing two hubs into one, in reverse.
    """
    devices = _ids()
    devices.observe({"ah1": _hub(id="ah1", mac="AABBCC")})

    after = {"ah2": _hub(id="ah2", mac="AABBCC")}
    devices.observe(after)

    assert devices.resolve(after["ah2"], "ah2") == "AABBCC"
    assert devices.find(after, "AABBCC") == ("ah2", after["ah2"])


def test_a_contested_mac_is_not_answered_by_the_hub_that_merely_shares_it():
    """A hub dropping out of one snapshot must not hand its device to the other.

    Skipping a contested key only while another *live* hub owned it left the
    crossing one poll away: with the owner absent -- rebooting, mid-adoption, or
    briefly disowned by a delta -- the contest dissolved and the surviving hub
    matched for the absent hub's device. Its entities then published the other
    physical hub's zones, and ``AlarmHubBaseEntity.hub_id`` resolves through the
    same call, so a ``switch.turn_on`` on the absent hub's siren was addressed
    to the surviving one. A snapshot the owner is missing from is no evidence
    that an unrelated hub is it.
    """
    devices = _ids()
    hubs = {
        "ah1": _hub(id="ah1", mac=PLACEHOLDER_MAC),
        "ah2": _hub(id="ah2", mac=PLACEHOLDER_MAC),
    }
    devices.observe(hubs)

    alone = {"ah2": hubs["ah2"]}
    devices.observe(alone)

    assert devices.find(alone, PLACEHOLDER_MAC) is None
    assert devices.find(alone, "ah2") == ("ah2", alone["ah2"])
    # ...and the device comes back the moment its own hub does.
    devices.observe(hubs)
    assert devices.find(hubs, PLACEHOLDER_MAC) == ("ah1", hubs["ah1"])


def test_hub_device_name_uses_the_console_name():
    assert logic.hub_device_name(_hub(name="Hall Hub"), "AABBCC") == "Hall Hub"


@pytest.mark.parametrize("name", ["", None, ["Hall Hub"]])
def test_hub_device_name_stays_distinct_when_the_console_gave_none(name):
    """The old "Alarm Hub" default only fired when the hub itself was missing.

    A hub present but unnamed left the device nameless, and ``has_entity_name``
    then degraded every entity id to a bare generic -- ``binary_sensor.tamper``.
    """
    assert logic.hub_device_name(_hub(name=name), "AABBCC") == "Alarm Hub AABBCC"
    assert logic.hub_device_name(None, "ah1") == "Alarm Hub ah1"
