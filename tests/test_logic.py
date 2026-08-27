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

# What a console reports for a hub it has adopted but not yet read the mac of.
# Every console mid-adoption reports this same value, which is what stops it
# being an identity -- see ``logic.UNUSABLE_MACS``.
PLACEHOLDER_MAC = "000000000000"


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
        mac="AABBCCDDEEFF",
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
    "mac", ["AABBCCDDEEFF", "aabbccddeeff", "AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff"]
)
def test_the_device_identifier_of_a_hub_with_a_mac_is_that_mac_verbatim(mac):
    """The upgrade contract, and the reason unique_ids may be routed through it.

    Every unique_id is now ``entity_unique_id(device_identifier(...), ...)``
    rather than ``f"{hub.mac}_..."``. Existing installs hold the mac-built ids
    in their entity registry, so a hub the console describes normally must keep
    producing exactly those: an identifier that merely *usually* equals the mac
    would orphan every entity on upgrade -- registry entries stranded, entity
    ids reissued with _2 suffixes, and every automation written against the old
    ones quietly pointing at nothing.

    Verbatim rather than normalised, so this holds without reading a registry at
    all. ``HubDeviceIds`` would keep an install's ids anyway once it has seen
    the device row; minting the same string the released 0.2 minted means it
    does not have to.
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


@pytest.mark.parametrize(
    "mac",
    [
        pytest.param("", id="never populated"),
        pytest.param(None, id="missing"),
        pytest.param(["AABBCCDDEEFF"], id="not a string"),
        pytest.param(42, id="not a string either"),
        pytest.param(PLACEHOLDER_MAC, id="the mid-adoption placeholder"),
        pytest.param("FF:FF:FF:FF:FF:FF", id="erased flash"),
        pytest.param("aabbcc", id="not a mac"),
    ],
)
def test_device_identifier_falls_back_when_the_mac_is_unusable(mac):
    """``mac`` is unvalidated console JSON and it is the whole device identity.

    A list is unhashable and raised TypeError inside ``DeviceInfo(identifiers=)``,
    failing platform setup; an empty string collapses two hubs onto one identity.
    ``hub_id`` is already validated as a usable string by the coordinator.

    The placeholders matter most, because they are the ones that *look* usable.
    A hub minted under ``000000000000`` stops answering to it the moment the
    console reads its real mac, and the device registry holds only the string
    that was chosen -- so the next restart found nothing tying the two together
    and built the hub a second device, eleven registry entries becoming
    twenty-two with ``_2`` suffixes on the live half. A hub id is reported for
    the whole life of the adoption, so an identity minted from one cannot go
    stale that way.
    """
    assert logic.device_identifier(_hub(mac=mac), "ah1") == "ah1"


def test_device_identifier_without_a_hub():
    assert logic.device_identifier(None, "ah1") == "ah1"


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        pytest.param("AABBCCDDEEFF", True, id="a mac"),
        pytest.param("ah1", True, id="a hub id"),
        pytest.param("aabbcc", True, id="a string we cannot classify"),
        pytest.param("", False, id="the empty string 0.2 filed a mac-less hub under"),
        pytest.param(PLACEHOLDER_MAC, False, id="the mid-adoption placeholder"),
        pytest.param("00:00:00:00:00:00", False, id="the same, spelled out"),
        pytest.param("ffffffffffff", False, id="erased flash"),
    ],
)
def test_names_hardware_admits_everything_but_an_unwritten_field(identity, expected):
    """The line the config flow's ownership test is drawn on.

    A string every console in the world reports for every hub it has not read
    yet cannot say *which* console is in front of us. Offered by one side of
    that comparison only, it refuses a working install its own console forever;
    offered by both, it hands an entry to a stranger. Anything else -- a mac, a
    hub id, or a string we cannot classify -- is at least somebody's own.
    """
    assert logic.names_hardware(identity) is expected


def test_identity_aliases_only_bridge_the_two_spellings_of_a_mac():
    assert logic.identity_aliases("AABBCCDDEEFF") == ("aabbccddeeff",)
    assert logic.identity_aliases("aa:bb:cc:dd:ee:ff") == ("aabbccddeeff",)
    assert logic.identity_aliases("aabbccddeeff") == ()
    assert logic.identity_aliases("ah1") == ()
    assert logic.identity_aliases(PLACEHOLDER_MAC) == ()
    assert logic.identity_aliases("") == ()


def test_own_identities_reads_a_registry_row_that_was_never_validated():
    """The device registry restores identifiers with no arity check at all.

    ``_async_load_data`` rebuilds each one as ``tuple(iden)`` straight out of
    JSON, so a hand-edited or partially restored ``core.device_registry`` can
    hold a one- or three-element identifier -- and ``for domain, identifier in
    row.identifiers`` raises ValueError on it, inside a platform setup that
    swallows the exception and loads the entry with no entities at all.
    """
    assert logic.own_identities([("d", "AABBCCDDEEFF")], "d") == ("AABBCCDDEEFF",)
    assert logic.own_identities([("other", "AABBCCDDEEFF")], "d") == ()
    assert logic.own_identities([("d",), ("d", "x"), ("d", "y", "z")], "d") == ("x",)
    assert logic.own_identities([], "d") == ()


def _ids(*known: str) -> logic.HubDeviceIds:
    return logic.HubDeviceIds(known)


def test_hub_keys_offers_the_id_first_and_every_mac_a_row_may_hold():
    """Recognition, not minting: whatever 0.2 filed a hub under must be offered.

    A hub whose mac was present but unusable was filed under that mac verbatim,
    and a mac-less one under the empty string. Left out here, neither can ever
    be matched to the device row it created -- so the hub mints a second device
    and the eleven entities on the first are unavailable for good.
    """
    assert logic.hub_keys(_hub(mac="AABBCCDDEEFF"), "ah1") == (
        "ah1",
        "AABBCCDDEEFF",
        "aabbccddeeff",
    )
    assert logic.hub_keys(_hub(mac="aabbccddeeff"), "ah1") == ("ah1", "aabbccddeeff")
    assert logic.hub_keys(_hub(mac=PLACEHOLDER_MAC), "ah1") == ("ah1", PLACEHOLDER_MAC)
    assert logic.hub_keys(_hub(mac="aabbcc"), "ah1") == ("ah1", "aabbcc")
    assert logic.hub_keys(_hub(mac=""), "ah1") == ("ah1", "")
    assert logic.hub_keys(_hub(mac=["AABBCCDDEEFF"]), "ah1") == ("ah1",)
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
    before = {"ah1": _hub(id="ah1", mac="AABBCCDDEEFF")}
    assert devices.resolve(before["ah1"], "ah1") == "AABBCCDDEEFF"

    after = {"ah2": _hub(id="ah2", mac="AABBCCDDEEFF")}

    assert devices.resolve(after["ah2"], "ah2") == "AABBCCDDEEFF"
    # ...and the entity finds it again, under the key the API now needs.
    assert devices.find(after, "AABBCCDDEEFF") == ("ah2", after["ah2"])


def test_a_mac_that_arrives_late_does_not_mint_a_second_device():
    """Mid-adoption, /v1/alarm-hubs can answer before the mac is populated.

    Derived fresh each time, identity flips from the id to the mac the moment
    it appears, every unique_id under the hub changes with it, and the hub ends
    up with two full sets of entities -- the first still pointing at the same
    live hardware, permanently.
    """
    devices = _ids()
    assert devices.resolve(_hub(id="ah1", mac=""), "ah1") == "ah1"

    hubs = {"ah1": _hub(id="ah1", mac="AABBCCDDEEFF")}

    assert devices.resolve(hubs["ah1"], "ah1") == "ah1"
    assert devices.find(hubs, "ah1") == ("ah1", hubs["ah1"])


def test_a_mac_that_stops_being_reported_does_not_mint_a_second_device():
    """The same defect in reverse: a delta that nulls ``mac``."""
    devices = _ids()
    assert devices.resolve(_hub(id="ah1", mac="AABBCCDDEEFF"), "ah1") == "AABBCCDDEEFF"

    hubs = {"ah1": _hub(id="ah1", mac="")}

    assert devices.resolve(hubs["ah1"], "ah1") == "AABBCCDDEEFF"
    assert devices.find(hubs, "AABBCCDDEEFF") == ("ah1", hubs["ah1"])


def test_identities_already_in_the_device_registry_are_reused():
    """The seed is what makes the decision survive a restart.

    A hub adopted before its mac was populated carries id-based unique_ids for
    good. Recomputing identity from a snapshot that now has a mac would strand
    every one of them.
    """
    devices = _ids("ah1")
    hubs = {"ah1": _hub(id="ah1", mac="AABBCCDDEEFF")}

    assert devices.resolve(hubs["ah1"], "ah1") == "ah1"


def test_an_ordinary_hub_on_an_existing_install_keeps_its_mac_identity():
    devices = _ids("AABBCCDDEEFF")

    assert devices.resolve(_hub(id="ah1", mac="AABBCCDDEEFF"), "ah1") == "AABBCCDDEEFF"


@pytest.mark.parametrize(
    "filed_as",
    [
        pytest.param("", id="the empty string, for a console that gave no mac"),
        pytest.param(PLACEHOLDER_MAC, id="the placeholder, adopted mid-adoption"),
        pytest.param("aabbcc", id="something that is not a mac at all"),
    ],
)
def test_an_identity_that_names_no_hardware_still_wins_if_it_is_on_disk(filed_as):
    """Rule one: what the registry holds wins, however odd it looks.

    The released 0.2 filed a hub under ``data.get("mac", "")`` verbatim, so real
    installs hold device rows -- and eleven entity unique_ids apiece -- under
    every one of these. Recognising only the strings this integration would
    choose today reads such an install as a hub it has never seen: a second
    device, eleven new entities taking ``_2`` entity ids, and the originals
    unavailable for good with every automation against them silently dead.

    An ugly identity that does not move beats a principled one that does.
    """
    devices = _ids(filed_as)
    hubs = {"ah1": _hub(id="ah1", mac=filed_as)}

    assert devices.resolve(hubs["ah1"], "ah1") == filed_as
    assert devices.find(hubs, filed_as) == ("ah1", hubs["ah1"])


def test_an_exact_spelling_on_disk_outranks_another_row_it_is_the_alias_of():
    """Aliases are seeded first so exact spellings can overwrite them.

    The pathological install that somehow holds both spellings as two separate
    rows: ``AA:BB:CC:DD:EE:FF`` aliases to ``aabbccddeeff``, which is also a row
    in its own right. A hub reporting ``aabbccddeeff`` has to reach *that* row
    and not the one it is merely the alias of, or eleven entities move house on
    an upgrade -- so the alias goes in first and the exact spelling lands on top
    of it. Seeded the other way round nothing complains and every such hub is
    quietly re-filed.
    """
    devices = _ids("AA:BB:CC:DD:EE:FF", "aabbccddeeff")

    assert devices.resolve(_hub(id="ah1", mac="aabbccddeeff"), "ah1") == "aabbccddeeff"

    other = _ids("AA:BB:CC:DD:EE:FF", "aabbccddeeff")

    assert (
        other.resolve(_hub(id="ah2", mac="AA:BB:CC:DD:EE:FF"), "ah2")
        == "AA:BB:CC:DD:EE:FF"
    )


def test_a_console_that_changes_the_spelling_of_a_mac_keeps_one_device():
    """One piece of hardware written two ways is still one piece of hardware.

    The registry holds whatever case the console used the day the row was
    written, and the same console is free to use the other one today. Compared
    as raw strings that is a hub nobody has seen, so it mints a second device
    and abandons eleven entities -- across a restart, where nothing in this
    process remembers the first spelling.
    """
    for filed_as, reports in (
        ("AABBCCDDEEFF", "aa:bb:cc:dd:ee:ff"),
        ("aa:bb:cc:dd:ee:ff", "AABBCCDDEEFF"),
    ):
        devices = _ids(filed_as)
        hubs = {"ah1": _hub(id="ah1", mac=reports)}

        assert devices.resolve(hubs["ah1"], "ah1") == filed_as
        assert devices.find(hubs, filed_as) == ("ah1", hubs["ah1"])


def test_a_second_hub_does_not_inherit_a_device_by_reporting_no_mac_either():
    """A key that names no hardware is never evidence of a re-adoption.

    Two hubs mid-adoption report the same placeholder, and every mac-less hub on
    every console reports the same empty string, so a hub carrying one is not
    the hub that carried it before under a new id. The live-owner contest alone
    would let it be: with the first hub absent -- rebooting, removed, replaced
    -- the claim dissolved and the newcomer resolved onto an identity whose
    eleven entities describe somebody else's hardware.
    """
    for filed_as in ("", PLACEHOLDER_MAC):
        devices = _ids(filed_as)  # what a 0.2 install left behind
        devices.observe({"ah1": _hub(id="ah1", mac=filed_as)})
        assert devices.resolve(_hub(id="ah1", mac=filed_as), "ah1") == filed_as

        replacement = {"ah2": _hub(id="ah2", mac=filed_as)}
        devices.observe(replacement)

        assert devices.resolve(replacement["ah2"], "ah2") == "ah2"
        assert devices.find(replacement, filed_as) is None


@pytest.mark.parametrize(
    "filed_as", ["", PLACEHOLDER_MAC], ids=["no mac at all", "the placeholder"]
)
def test_a_row_naming_no_hardware_is_open_to_whichever_hub_asks_first(filed_as):
    """The seed's deliberate trade, pinned so it stays deliberate.

    ``__init__`` seeds ``_by_key`` from the registry but never ``_owner``, so at
    process start an identity on disk has no owner and the first hub that offers
    the string resolves onto it. For an identity that names hardware that is
    exactly right -- only its own hub reports that mac. For one that does not,
    every mid-adoption hub on every console reports the same string, so the hub
    that asks first is not necessarily the hub the row was written for: if the
    true owner is missing from the first snapshot after a restart, a stranger
    inherits eleven entity unique_ids describing somebody else's hardware.

    Nothing in either payload can tell the two apart, and refusing the match
    would stop a pre-0.3 install ever finding its own row again -- which is the
    upgrade this seed exists for. So the trade is taken knowingly, and
    ``entity.async_migrate_hub_identity`` is what shrinks it: a single-hub
    install is re-filed under an identity that *does* name hardware on its first
    setup, after which there is nothing here for anyone to inherit. What is left
    exposed is an entry holding a second device row, where the migration refuses
    to guess.

    The case where the true owner *was* seen first is not this, and is refused:
    see ``test_a_second_hub_does_not_inherit_a_device_by_reporting_no_mac_either``.
    """
    devices = _ids(filed_as)  # what a 0.2 install left behind
    stranger = {"ah9": _hub(id="ah9", mac=filed_as)}
    devices.observe(stranger)

    assert devices.resolve(stranger["ah9"], "ah9") == filed_as


def test_two_hubs_on_one_console_stay_two_devices():
    devices = _ids()
    hubs = {
        "ah1": _hub(id="ah1", mac="AABBCCDDEEFF"),
        "ah2": _hub(id="ah2", mac="112233445566"),
    }
    for hub_id, hub in hubs.items():
        devices.resolve(hub, hub_id)

    assert devices.find(hubs, "112233445566") == ("ah2", hubs["ah2"])
    assert devices.find(hubs, "ffeeddccbbaa") is None
    assert devices.find({}, "AABBCCDDEEFF") is None


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


REPEATED_MAC = "AABBCCDDEEFF"


def test_two_hubs_reporting_one_mac_stay_two_devices():
    """A mac that names two hubs at once names neither of them.

    A console bug can repeat a real mac. Both hubs then took the same identity,
    so the second was refused every unique_id it asked for and got no entities
    at all -- and the set that survived answered from whichever hub ``find``
    reached first, which is REST list order. That is the serious half: an entity
    confidently publishing another physical hub's zone.
    """
    devices = _ids()
    hubs = {
        "ah1": _hub(id="ah1", mac=REPEATED_MAC),
        "ah2": _hub(id="ah2", mac=REPEATED_MAC),
    }
    devices.observe(hubs)

    assert devices.resolve(hubs["ah1"], "ah1") == REPEATED_MAC
    assert devices.resolve(hubs["ah2"], "ah2") == "ah2"
    # ...and each device finds its own hub, in either order the console lists
    # them: matching on the contested key made the first entry answer for both.
    backwards = dict(reversed(list(hubs.items())))
    for snapshot in (hubs, backwards):
        assert devices.find(snapshot, REPEATED_MAC) == ("ah1", hubs["ah1"])
        assert devices.find(snapshot, "ah2") == ("ah2", hubs["ah2"])


def test_two_hubs_mid_adoption_never_contest_a_placeholder_at_all():
    """The placeholder is settled before the contest, by not being an identity.

    Every console reports the same one, so it was never a mac two hubs happened
    to share -- it is what a mac field holds before anybody read it. Minting it
    left the first hub holding a string it stops answering to the moment the
    console fills the field in; each hub taking its own id instead is stable for
    both, and needs no contest to be.
    """
    devices = _ids()
    hubs = {
        "ah1": _hub(id="ah1", mac=PLACEHOLDER_MAC),
        "ah2": _hub(id="ah2", mac=PLACEHOLDER_MAC),
    }
    devices.observe(hubs)

    assert devices.resolve(hubs["ah1"], "ah1") == "ah1"
    assert devices.resolve(hubs["ah2"], "ah2") == "ah2"
    for snapshot in (hubs, dict(reversed(list(hubs.items())))):
        assert devices.find(snapshot, "ah1") == ("ah1", hubs["ah1"])
        assert devices.find(snapshot, "ah2") == ("ah2", hubs["ah2"])


def test_a_placeholder_identity_survives_the_mac_it_was_standing_in_for():
    """B2: the reload after the console finally reads the hub's mac.

    Minted from the placeholder, the identity was a string nothing answered to
    once the real mac arrived -- in-process the hub id held the two together,
    but the registry records only the identity, so the next restart seeded
    ``{'000000000000'}``, matched nothing, and minted a second device. Eleven
    registry entries became twenty-two. Minted from the hub id, the key the
    identity *is* is reported for the life of the adoption.
    """
    devices = _ids()
    assert devices.resolve(_hub(id="ah1", mac=PLACEHOLDER_MAC), "ah1") == "ah1"

    # The mac arrives, in this process...
    grown_up = {"ah1": _hub(id="ah1", mac="AABBCCDDEEFF")}
    assert devices.resolve(grown_up["ah1"], "ah1") == "ah1"

    # ...and again on the next run, seeded from what the registry now holds.
    restarted = _ids("ah1")
    assert restarted.resolve(grown_up["ah1"], "ah1") == "ah1"
    assert restarted.find(grown_up, "ah1") == ("ah1", grown_up["ah1"])


def test_a_contested_mac_does_not_change_sides_on_the_next_poll():
    """Identity is decided once -- an entity captures its device at construction.

    A rule that re-decided per snapshot would not merely pick a side, it would
    hand each hub's entities to the other hub's device on the poll after that.
    """
    devices = _ids()
    hubs = {
        "ah1": _hub(id="ah1", mac=REPEATED_MAC),
        "ah2": _hub(id="ah2", mac=REPEATED_MAC),
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
    devices = _ids(REPEATED_MAC, "ah2")  # what the first run left behind
    hubs = {
        "ah2": _hub(id="ah2", mac=REPEATED_MAC),
        "ah1": _hub(id="ah1", mac=REPEATED_MAC),
    }
    devices.observe(hubs)

    assert devices.resolve(hubs["ah1"], "ah1") == REPEATED_MAC
    assert devices.resolve(hubs["ah2"], "ah2") == "ah2"


def test_a_re_adoption_still_takes_over_the_identity_it_already_had():
    """The claimed-mac rule must not fire on the case the scheme exists for.

    A re-adoption *is* a mac appearing under a second id; what makes it not a
    collision is that the first id is gone. Refusing it would split one hub into
    two devices -- the same damage as collapsing two hubs into one, in reverse.
    """
    devices = _ids()
    devices.observe({"ah1": _hub(id="ah1", mac="AABBCCDDEEFF")})

    after = {"ah2": _hub(id="ah2", mac="AABBCCDDEEFF")}
    devices.observe(after)

    assert devices.resolve(after["ah2"], "ah2") == "AABBCCDDEEFF"
    assert devices.find(after, "AABBCCDDEEFF") == ("ah2", after["ah2"])


def test_a_hub_that_comes_back_does_not_share_the_identity_it_lost():
    """The ordering the contest never saw: each hub alone, then both together.

    The existing coverage runs it the other way -- both hubs present, then one
    alone -- and that order never puts a hub in front of an identity that has
    moved on. This one does. B takes the mac; B misses a snapshot and C,
    reporting the same mac, is read as B re-adopted, which is the inference this
    whole scheme exists to make and which moves the identity to C; then B comes
    back.

    B's mac is contested and dropped, but its own id still pointed at the
    identity it used to have, and a hub's own id was exempted from the contest
    outright -- true of the *key*, since no other hub can present it, and
    nothing at all about the *device* behind it. So both live hubs resolved to
    one identity: one device row for two hubs, an entity built and named for B
    publishing C's zone while B's own zone read normal. That is the exact
    failure ``HubDeviceIds`` claims to prevent.

    Whoever is recorded as the owner keeps the identity; the other mints its own
    id, which is what ``device_identifier`` would give it anyway.
    """
    devices = _ids()
    first = {"ah1": _hub(id="ah1", mac=REPEATED_MAC)}
    devices.observe(first)
    assert devices.resolve(first["ah1"], "ah1") == REPEATED_MAC

    alone = {"ah2": _hub(id="ah2", mac=REPEATED_MAC)}
    devices.observe(alone)
    assert devices.resolve(alone["ah2"], "ah2") == REPEATED_MAC

    both = {"ah1": first["ah1"], "ah2": alone["ah2"]}
    devices.observe(both)

    assert devices.resolve(both["ah2"], "ah2") == REPEATED_MAC
    assert devices.resolve(both["ah1"], "ah1") == "ah1"
    # ...and each device still finds its own hub, in either listed order --
    # including the one whose every key had to be given up, which is a device
    # nothing answers for if the fallback is not recorded as a key too.
    for snapshot in (both, dict(reversed(list(both.items())))):
        assert devices.find(snapshot, REPEATED_MAC) == ("ah2", both["ah2"])
        assert devices.find(snapshot, "ah1") == ("ah1", both["ah1"])


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
        "ah1": _hub(id="ah1", mac=REPEATED_MAC),
        "ah2": _hub(id="ah2", mac=REPEATED_MAC),
    }
    devices.observe(hubs)

    alone = {"ah2": hubs["ah2"]}
    devices.observe(alone)

    assert devices.find(alone, REPEATED_MAC) is None
    assert devices.find(alone, "ah2") == ("ah2", alone["ah2"])
    # ...and the device comes back the moment its own hub does.
    devices.observe(hubs)
    assert devices.find(hubs, REPEATED_MAC) == ("ah1", hubs["ah1"])


def test_hub_device_name_uses_the_console_name():
    assert logic.hub_device_name(_hub(name="Hall Hub"), "AABBCCDDEEFF") == "Hall Hub"


@pytest.mark.parametrize("name", ["", None, ["Hall Hub"]])
def test_hub_device_name_stays_distinct_when_the_console_gave_none(name):
    """The old "Alarm Hub" default only fired when the hub itself was missing.

    A hub present but unnamed left the device nameless, and ``has_entity_name``
    then degraded every entity id to a bare generic -- ``binary_sensor.tamper``.
    """
    assert (
        logic.hub_device_name(_hub(name=name), "AABBCCDDEEFF")
        == "Alarm Hub AABBCCDDEEFF"
    )
    assert logic.hub_device_name(None, "ah1") == "Alarm Hub ah1"
