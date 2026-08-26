"""Config-flow tests: the three ways in, and the strings they name.

Every result these tests produce goes through ``_assert_translated``, so a step,
error or abort reason added without a translation fails the test that exercises
it rather than reaching a user as a raw key.
"""

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import unifi_protect_alarm_hub
from custom_components.unifi_protect_alarm_hub.api import (
    AlarmHubAuthError,
    AlarmHubConnectionError,
)
from custom_components.unifi_protect_alarm_hub.config_flow import is_legacy_unique_id
from custom_components.unifi_protect_alarm_hub.const import DOMAIN
from custom_components.unifi_protect_alarm_hub.logic import mac_key
from custom_components.unifi_protect_alarm_hub.models import AlarmHub

COMPONENT = Path(unifi_protect_alarm_hub.__file__).parent
STRINGS = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
TRANSLATIONS = json.loads(
    (COMPONENT / "translations" / "en.json").read_text(encoding="utf-8")
)

MAC = "AABBCCDDEEFF"
MAC_ID = "aabbccddeeff"
# Lower than MAC_ID, so a console carrying both is keyed on this one.
OTHER_MAC = "112233445566"

# What a console reports for a hub it has adopted but not yet read the mac of.
# Every console mid-adoption reports the same one, which is what stops it being
# an identity -- see ``logic.UNUSABLE_MACS``.
PLACEHOLDER_MAC = "000000000000"

# The address every entry in this module lives at, and the id one created before
# 0.3.0 carries for it. Chosen so that stripping every character a mac may be
# separated by leaves exactly twelve hex digits -- ``192.168.1.50:443`` reduces
# to ``192168150443``, which is a well-formed mac key naming no hardware
# anywhere. An entry id read that way is an ownership claim on hardware that
# does not exist, and it took both recovery flows down. So the legacy-keyed
# tests below are written on an address that has that property rather than
# passing because the one in the fixture happened not to; ``192.168.0.103``, the
# address they used to use, reduces to thirteen digits and quietly exercised
# nothing.
HOST = "192.168.1.50"
OTHER_HOST = "192.168.1.60"
LEGACY_ID = f"{HOST}:443"

USER_INPUT = {
    CONF_HOST: HOST,
    CONF_PORT: 443,
    CONF_API_KEY: "k",
    CONF_VERIFY_SSL: False,
}


def _hub(mac: str = MAC, hub_id: str = "hub-1", is_alarm_hub: bool = True) -> AlarmHub:
    return AlarmHub.from_json(
        {"id": hub_id, "mac": mac, "state": "CONNECTED", "isAlarmHub": is_alarm_hub}
    )


def _entry(unique_id: str | None = MAC_ID, **data: object) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, unique_id=unique_id, data={**USER_INPUT, **data}
    )


def _owns_hub(hass, entry: MockConfigEntry, identifier: str, name: str) -> None:
    """Give ``entry`` the device row it gets from publishing entities for a hub.

    The identifier is ``logic.device_identifier``'s answer for that hub: the mac
    as the console spelled it, or the hub id when the console gave no mac. It is
    the only durable record of which hardware an entry owns -- an entry that
    cannot reach its console never migrates its unique_id, so on exactly the
    entries that need reconfigure, the id says nothing and this says everything.
    """
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, identifier)}, name=name
    )


def _assert_translated(result: dict) -> None:
    """Every key a flow result names must exist where HA reads translations.

    ``translations/en.json`` is the only file loaded for a custom integration --
    ``helpers/translation.py`` builds ``file_path / "translations" /
    f"{language}.json"`` and never opens ``strings.json`` -- so a key that only
    reached the latter shows the user the key itself.
    """
    config = TRANSLATIONS["config"]
    if result["type"] == FlowResultType.FORM:
        assert result["step_id"] in config["step"]
        for error in (result["errors"] or {}).values():
            assert error in config["error"]
    elif result["type"] == FlowResultType.ABORT:
        assert result["reason"] in config["abort"]


def _suggested(result: dict) -> dict[str, object]:
    """The values a re-shown form offers, keyed by field name."""
    return {
        str(marker): marker.description["suggested_value"]
        for marker in result["data_schema"].schema
        if isinstance(marker, vol.Marker) and marker.description
    }


@contextmanager
def _console(hubs: list[AlarmHub] | None = None, side_effect: Exception | None = None):
    """Patch the client the flow validates through."""
    with patch(
        "custom_components.unifi_protect_alarm_hub.config_flow.AlarmHubApiClient"
    ) as cls:
        cls.return_value.async_get_alarm_hubs = AsyncMock(
            side_effect=side_effect, return_value=hubs if hubs is not None else [_hub()]
        )
        yield cls


async def _configure(hass, flow_id, user_input, *, reload=False, **console):
    """Submit one step, optionally letting the reload it triggers land."""
    with (
        _console(**console),
        patch(
            "custom_components.unifi_protect_alarm_hub.async_setup_entry",
            return_value=True,
        ) as setup,
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, user_input)
        await hass.async_block_till_done()
    _assert_translated(result)
    if reload:
        assert setup.await_count == 1
    return result


async def _user_flow(hass, user_input=USER_INPUT, **console):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    _assert_translated(result)
    return await _configure(hass, result["flow_id"], user_input, **console)


def test_strings_and_translations_stay_in_sync():
    """The two hand-maintained copies must not drift.

    Only ``translations/en.json`` is read at runtime; ``strings.json`` is what a
    reviewer looks at. A key in one and not the other is invisible until a user
    hits that path.
    """
    assert STRINGS == TRANSLATIONS


async def test_user_flow_success(hass):
    result = await _user_flow(hass)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == HOST
    assert result["result"].unique_id == MAC_ID


async def test_user_flow_invalid_auth(hass):
    result = await _user_flow(hass, side_effect=AlarmHubAuthError("bad"))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass):
    result = await _user_flow(hass, side_effect=AlarmHubConnectionError("down"))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_unknown_error(hass):
    result = await _user_flow(hass, side_effect=Exception("boom"))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_user_flow_keeps_what_was_typed_after_an_error(hass):
    """A failed submit must not cost the user their API key."""
    result = await _user_flow(hass, side_effect=AlarmHubConnectionError("down"))
    assert _suggested(result) == USER_INPUT


@pytest.mark.parametrize(
    "hubs",
    [
        pytest.param([], id="no devices at all"),
        pytest.param([_hub(is_alarm_hub=False)], id="a device that is not a hub"),
        pytest.param([_hub(hub_id=["not", "a", "key"])], id="an id we cannot key on"),
    ],
)
async def test_user_flow_refuses_a_console_with_nothing_to_control(hass, hubs):
    """The flow's predicate is the coordinator's, so no entry can load empty.

    ``isAlarmHub: false`` passed the old ``if not hubs`` check and produced an
    entry with zero entities and a green success dialog.
    """
    result = await _user_flow(hass, hubs=hubs)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "no_alarm_hub"}
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize(
    ("typed", "host", "port"),
    [
        pytest.param(HOST, HOST, 443, id="plain"),
        pytest.param(f"  {HOST} ", HOST, 443, id="padded"),
        pytest.param(f"https://{HOST}", HOST, 443, id="scheme"),
        pytest.param("https://protect.lan/protect/", "protect.lan", 443, id="path"),
        pytest.param(f"{HOST}:8443", HOST, 8443, id="embedded port"),
        pytest.param("fd00::1", "[fd00::1]", 443, id="ipv6 literal"),
        # Already normalised: reauth and reconfigure re-run this over stored
        # data, so a second pass has to be a no-op.
        pytest.param("[fd00::1]", "[fd00::1]", 443, id="ipv6 bracketed"),
        pytest.param(
            "https://[fd00::1]:8443/protect", "[fd00::1]", 8443, id="ipv6 url"
        ),
    ],
)
async def test_user_flow_normalises_the_address(hass, typed, host, port):
    """What was pasted has to reach the client as something it can build a URL from.

    Asserted on the constructor call as well as on the stored data: normalising
    only on the way to storage would validate one address and then poll another.
    """
    with (
        _console() as cls,
        patch(
            "custom_components.unifi_protect_alarm_hub.async_setup_entry",
            return_value=True,
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"], {**USER_INPUT, CONF_HOST: typed}
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert cls.call_args.args[:2] == (host, port)
    assert (result["data"][CONF_HOST], result["data"][CONF_PORT]) == (host, port)


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "   ",
        "https://",
        f"{HOST}:donkey",
        f"{HOST}:99999",
        # In range as far as ``urlsplit`` is concerned, and handed straight back
        # -- where it beats the port box, an embedded port being the more
        # specific of the two. Nothing serves on port 0; it is the value a
        # *listener* passes to be given one. So this slip alone reached the
        # console layer and came back as "Failed to connect", blaming the
        # network for a typo in the field the user is looking at, while both of
        # its neighbours here already named it.
        f"{HOST}:0",
    ],
)
async def test_user_flow_names_an_unusable_address(hass, typed):
    """These used to reach the console layer and come back as "cannot connect"."""
    with _console() as cls:
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"], {**USER_INPUT, CONF_HOST: typed}
        )
    _assert_translated(result)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_host"}
    assert cls.call_count == 0


async def test_user_flow_reads_the_host_a_url_parser_reads(hass):
    """The host has to be the one every URL parser finds, not the last one there.

    Normalising by ``split("//", 1)[-1]`` took the *tail*, so
    "192.168.1.1//evil.example" resolved to evil.example -- while
    ``urlsplit("//192.168.1.1//evil.example").hostname`` is 192.168.1.1, and
    what follows is a path. Driven through the flow rather than asserted on the
    function, because the damage is where the API key goes: the client was built
    against the wrong host and that host was then stored.
    """
    with (
        _console() as cls,
        patch(
            "custom_components.unifi_protect_alarm_hub.async_setup_entry",
            return_value=True,
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"], {**USER_INPUT, CONF_HOST: "192.168.1.1//evil.example"}
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert cls.call_args.args[:2] == ("192.168.1.1", 443)
    assert result["data"][CONF_HOST] == "192.168.1.1"


@pytest.mark.parametrize(
    "typed",
    [
        pytest.param("192.168.1.1@evil.example", id="userinfo"),
        pytest.param("user:pw@192.168.1.1", id="userinfo with a password"),
        pytest.param("192.168.1.1。evil.example", id="ideographic full stop"),
        pytest.param("192.168.1.1．evil.example", id="fullwidth full stop"),
        pytest.param("192.168.1.1｡evil.example", id="halfwidth ideographic stop"),
        pytest.param("192.168.1.1\nevil.example", id="a newline urlsplit deletes"),
        pytest.param("192.168.1.1 evil.example", id="a space"),
        pytest.param("192.168.1.1\\evil.example", id="a backslash"),
    ],
)
async def test_user_flow_refuses_an_address_that_names_a_host_nobody_typed(hass, typed):
    """Each of these resolves somewhere other than where it reads.

    Everything before an "@" is userinfo, so ``192.168.1.1@evil.example`` is a
    request to evil.example. The three stops are folded to "." by yarl's IDNA
    pass at request time, so the host that reaches the wire is
    ``192.168.1.1.evil.example`` -- a domain its owner controls. ``urlsplit``
    deletes tab, CR and LF before it parses, so a pasted newline silently joins
    two labels into one. The last two reach nothing, which is a milder fault of
    the same kind and answered the same way.

    Refused on the form, and the client is never built: an address the user
    cannot see in what they typed must not be reachable at all, and least of all
    with their API key attached.
    """
    with _console() as cls:
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"], {**USER_INPUT, CONF_HOST: typed}
        )
    _assert_translated(result)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_host"}
    assert cls.call_count == 0
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_user_flow_rejects_the_same_console_at_another_address(hass):
    """The bug F3 describes: added by IP, then by hostname, twice over.

    The addresses differ, so a host:port key let the second entry through -- and
    it set up with no entities of its own, every unique_id already taken.
    """
    _entry().add_to_hass(hass)
    result = await _user_flow(hass, {**USER_INPUT, CONF_HOST: "protect.lan"})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize(
    "mac",
    [
        pytest.param("", id="not populated yet"),
        pytest.param("aabbcc", id="too short to be a mac"),
        pytest.param("not a mac at all", id="not a mac at all"),
        pytest.param(PLACEHOLDER_MAC, id="the mid-adoption placeholder"),
        pytest.param("00:00:00:00:00:00", id="the placeholder, separated"),
        pytest.param("ff:ff:ff:ff:ff:ff", id="broadcast"),
    ],
)
async def test_user_flow_falls_back_to_the_address_without_a_usable_mac(hass, mac):
    """A console that will not say what it is still gets an entry, keyed weakly.

    Keying two consoles on one string would be worse than keying on the address,
    which at least refuses the same address twice. Setup upgrades the key once a
    real mac appears.

    The placeholder is the case that matters: it is not blank, it is the right
    length and it is what *every* console mid-adoption reports, so a length
    check alone let it through as an identity -- and ``logic`` had already named
    it as one hub cannot own. A short or garbled mac is the same failure with a
    less specific cause, and broadcast is the other value a field holds when
    nothing was ever written to it.
    """
    result = await _user_flow(hass, hubs=[_hub(mac=mac)])
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == LEGACY_ID


async def test_two_consoles_mid_adoption_each_get_their_own_entry(hass):
    """The placeholder is shared, so keying on it made the second console a dup.

    Both consoles are real, different and separately addressable; the second
    aborted with "already configured" and could not be added at all until its
    hub finished adopting.
    """
    first = await _user_flow(hass, hubs=[_hub(mac=PLACEHOLDER_MAC, hub_id="hub-1")])
    assert first["type"] == FlowResultType.CREATE_ENTRY

    second = await _user_flow(
        hass,
        {**USER_INPUT, CONF_HOST: OTHER_HOST},
        hubs=[_hub(mac=PLACEHOLDER_MAC, hub_id="hub-9")],
    )
    assert second["type"] == FlowResultType.CREATE_ENTRY
    assert {entry.unique_id for entry in hass.config_entries.async_entries(DOMAIN)} == {
        LEGACY_ID,
        f"{OTHER_HOST}:443",
    }


@pytest.mark.parametrize(
    "listed", [(MAC, OTHER_MAC), (OTHER_MAC, MAC)], ids=["lowest first", "lowest last"]
)
async def test_the_console_id_does_not_depend_on_the_order_hubs_are_listed_in(
    hass, listed
):
    """A console with two hubs answers in whatever order it likes, and does vary.

    The flow that adds the entry and the setup that later migrates it read
    separate REST responses, so an id taken from the first hub in the list is an
    id that can change between them -- the entry would be re-keyed under the
    other hub, and ``async_entry_for_domain_unique_id`` would stop finding it
    under the id the flow's duplicate check just used.
    """
    result = await _user_flow(
        hass,
        hubs=[_hub(mac=listed[0], hub_id="hub-1"), _hub(mac=listed[1], hub_id="hub-2")],
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == OTHER_MAC


async def test_reauth_replaces_the_key_and_reloads(hass):
    """A rotated key is recoverable without deleting the entry."""
    entry = _entry()
    entry.add_to_hass(hass)
    started = await entry.start_reauth_flow(hass)
    _assert_translated(started)
    assert started["step_id"] == "reauth_confirm"

    result = await _configure(
        hass, started["flow_id"], {CONF_API_KEY: "fresh"}, reload=True
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "fresh"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reauth_stays_on_the_form_while_the_key_is_wrong(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    started = await entry.start_reauth_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {CONF_API_KEY: "still-wrong"},
        side_effect=AlarmHubAuthError("nope"),
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_API_KEY] == "k"


async def test_reauth_refuses_a_key_for_another_console(hass):
    """A key that works but is for different hardware must not be adopted.

    The entry's entities all belong to the hub it was set up with; pointing it
    at another console would leave every one of them reporting the wrong device.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    started = await entry.start_reauth_flow(hass)
    result = await _configure(
        hass, started["flow_id"], {CONF_API_KEY: "other"}, hubs=[_hub(mac=OTHER_MAC)]
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_API_KEY] == "k"
    assert entry.unique_id == MAC_ID


async def test_reauth_adopts_the_mac_of_a_pre_migration_entry(hass):
    """An entry still keyed on host:port names no console, so it cannot mismatch.

    Aborting here would make reauth impossible for exactly the installs that
    predate mac keys -- the ones most likely to need it.
    """
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    started = await entry.start_reauth_flow(hass)
    result = await _configure(
        hass, started["flow_id"], {CONF_API_KEY: "fresh"}, reload=True
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == MAC_ID


async def test_reauth_renormalises_the_address_it_just_validated(hass):
    """Reauth stores the whole validated payload, not only the key it asked for.

    The host it validated against is ``normalise_host``'s answer for what the
    entry holds, so an entry set up before that normalisation existed -- host
    and port fused into one field, a scheme still on the front -- is polling an
    address the client has to re-parse on every request. This is the one moment
    we know the normalised form answers, because we just used it.
    """
    entry = _entry(**{CONF_HOST: f"https://{HOST}:8443/protect"})
    entry.add_to_hass(hass)
    started = await entry.start_reauth_flow(hass)
    result = await _configure(
        hass, started["flow_id"], {CONF_API_KEY: "fresh"}, reload=True
    )
    assert result["type"] == FlowResultType.ABORT
    assert entry.data[CONF_API_KEY] == "fresh"
    assert entry.data[CONF_HOST] == HOST
    assert entry.data[CONF_PORT] == 8443


async def test_reauth_accepts_a_console_that_retired_the_hub_it_was_keyed_on(hass):
    """A replaced hub is not a replaced console, and the entry is keyed on a hub.

    The id is the lowest mac of the hubs the console had when the entry was
    made. RMA one of them, or retire the one that happened to hold the lower
    mac, and that id matches nothing the console now reports -- so both recovery
    paths aborted with "that key is for a different console" about the console
    that had set the entry up and would set it up again. The abort also removes
    the reauth flow and the repair issue with it, and an entry in SETUP_ERROR is
    never retried, so the false diagnosis was also the last word.

    The entry still owns the hub that stayed. That overlap is the relationship,
    and the id follows the hubs that are actually there.
    """
    entry = _entry(unique_id=OTHER_MAC)
    entry.add_to_hass(hass)
    _owns_hub(hass, entry, OTHER_MAC, "Shed Hub")
    _owns_hub(hass, entry, MAC, "Hall Hub")

    started = await entry.start_reauth_flow(hass)
    result = await _configure(
        hass, started["flow_id"], {CONF_API_KEY: "fresh"}, reload=True
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "fresh"
    assert entry.unique_id == MAC_ID


async def test_reauth_works_while_the_hub_is_unadopted(hass):
    """The key is the subject; a console with no hub can still hand one over."""
    entry = _entry()
    entry.add_to_hass(hass)
    started = await entry.start_reauth_flow(hass)
    result = await _configure(
        hass, started["flow_id"], {CONF_API_KEY: "fresh"}, hubs=[], reload=True
    )
    assert result["type"] == FlowResultType.ABORT
    assert entry.data[CONF_API_KEY] == "fresh"
    assert entry.unique_id == MAC_ID


async def test_entry_supports_reconfigure(hass):
    """What the frontend actually tests before offering the option."""
    entry = _entry()
    entry.add_to_hass(hass)
    assert entry.supports_reconfigure


async def test_reconfigure_follows_the_console_to_a_new_address(hass):
    """The DHCP-lease case, fixed in place rather than by re-adding the entry."""
    entry = _entry()
    entry.add_to_hass(hass)
    started = await entry.start_reconfigure_flow(hass)
    _assert_translated(started)
    assert started["step_id"] == "reconfigure"

    result = await _configure(
        hass,
        started["flow_id"],
        {**USER_INPUT, CONF_HOST: f"https://{OTHER_HOST}:8443/"},
        reload=True,
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == OTHER_HOST
    assert entry.data[CONF_PORT] == 8443
    assert entry.unique_id == MAC_ID
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_offers_the_current_settings(hass):
    """Including the key: UniFi OS shows an API key once, at creation.

    A form that blanked it would make changing a port cost a new key.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    started = await entry.start_reconfigure_flow(hass)
    assert _suggested(started) == USER_INPUT


async def test_reconfigure_validates_before_it_saves(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    started = await entry.start_reconfigure_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {**USER_INPUT, CONF_HOST: OTHER_HOST},
        side_effect=AlarmHubConnectionError("down"),
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_HOST] == HOST


async def test_reconfigure_refuses_a_console_with_no_hub(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    started = await entry.start_reconfigure_flow(hass)
    result = await _configure(
        hass, started["flow_id"], {**USER_INPUT, CONF_HOST: OTHER_HOST}, hubs=[]
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "no_alarm_hub"}
    assert entry.data[CONF_HOST] == HOST


async def test_reconfigure_refuses_to_point_at_a_different_console(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    started = await entry.start_reconfigure_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {**USER_INPUT, CONF_HOST: OTHER_HOST},
        hubs=[_hub(mac=OTHER_MAC)],
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "different_console"
    assert entry.data[CONF_HOST] == HOST


async def test_reconfigure_refuses_hardware_a_legacy_entry_does_not_own(hass):
    """The entry this step exists for is the one whose id proves nothing.

    An entry that cannot reach its console never runs the migration, so it still
    carries ``host:port`` -- and unreachable is exactly why someone opens
    Reconfigure. Read as "names no console", such an entry was adopted onto
    whatever answered: type your *other* console's address and the flow reported
    success, the entry moved, a second device row appeared, and the device and
    entities it left behind sat at None for good, with every automation written
    against them silently never firing again.

    Its device rows name the hardware it owns whatever its id says, and none of
    them is on this console.
    """
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    _owns_hub(hass, entry, MAC, "Hall Hub")

    started = await entry.start_reconfigure_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {**USER_INPUT, CONF_HOST: OTHER_HOST},
        hubs=[_hub(mac=OTHER_MAC, hub_id="hub-9")],
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "different_console"
    assert entry.data[CONF_HOST] == HOST
    assert entry.unique_id == LEGACY_ID
    assert [
        device.identifiers
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        )
    ] == [{(DOMAIN, MAC)}]


@pytest.mark.parametrize(
    "filed_as",
    [
        pytest.param(MAC, id="as the console spelled it"),
        pytest.param("aa:bb:cc:dd:ee:ff", id="in the other spelling"),
    ],
)
async def test_reconfigure_follows_a_legacy_entry_to_a_new_address(hass, filed_as):
    """And the case the step exists for must survive the refusal above.

    Same console, new address, on an entry that never migrated because it has
    not reached that console since it moved. The registry holds the mac in
    whatever case the console used on the day the device row was written, and
    the same console spelling it the other way today is the same hub.
    """
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    _owns_hub(hass, entry, filed_as, "Hall Hub")

    started = await entry.start_reconfigure_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {**USER_INPUT, CONF_HOST: OTHER_HOST},
        reload=True,
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == OTHER_HOST
    assert entry.unique_id == MAC_ID


async def test_reconfigure_follows_a_console_past_a_device_row_naming_no_hub(hass):
    """An identifier from before hub identity was hardened must not lock anyone out.

    A release before ``logic.hub_keys`` filed the device under the raw mac even
    when the console had not populated one, so an install from that era can
    carry a device row identified by the empty string. It names no hub, so it
    can never match one -- and read as evidence it would say this entry owns
    hardware that is not on the console it is already pointed at, refusing the
    move for an entry that is following its own console.
    """
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    _owns_hub(hass, entry, "", "Alarm Hub")

    started = await entry.start_reconfigure_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {**USER_INPUT, CONF_HOST: OTHER_HOST},
        reload=True,
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == OTHER_HOST
    assert entry.unique_id == MAC_ID


@pytest.mark.parametrize(
    "stranger_hubs",
    [
        pytest.param(
            [
                _hub(mac="998877665544", hub_id="hub-9"),
                _hub(mac=PLACEHOLDER_MAC, hub_id="hub-8"),
            ],
            id="one-hub-still-adopting",
        ),
        pytest.param(
            [_hub(mac="998877665544", hub_id="hub-9"), _hub(mac="", hub_id="hub-8")],
            id="one-hub-with-no-mac-at-all",
        ),
    ],
)
async def test_reconfigure_refuses_a_stranger_that_has_a_hub_still_adopting(
    hass, stranger_hubs
):
    """One unreadable hub must not disarm the guard for every readable one.

    Mac evidence is only inconclusive when the console cannot answer in macs at
    all. Discarding it whenever *any* hub is mid-adoption emptied the evidence
    for the ordinary install -- rows filed under a real mac, which is what every
    released version wrote -- and an empty set short-circuits the overlap check,
    so a stranger only had to have one hub still adopting to be accepted.
    """
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    _owns_hub(hass, entry, MAC, "Hall Hub")

    started = await entry.start_reconfigure_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {**USER_INPUT, CONF_HOST: OTHER_HOST},
        hubs=stranger_hubs,
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "different_console"
    assert entry.data[CONF_HOST] == HOST


async def test_reauth_refuses_a_stranger_that_has_a_hub_still_adopting(hass):
    """The same guard, on the path where aborting also drops the repair issue."""
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    _owns_hub(hass, entry, MAC, "Hall Hub")

    started = await entry.start_reauth_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {CONF_API_KEY: "other-key"},
        hubs=[
            _hub(mac="998877665544", hub_id="hub-9"),
            _hub(mac=PLACEHOLDER_MAC, hub_id="hub-8"),
        ],
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"


async def test_reconfigure_refuses_another_console_with_no_mac_to_show(hass):
    """Two consoles mid-adoption are not one console, and share no identity.

    Neither offers a mac worth keying on, so the flow has nothing to compare and
    used to keep the entry's own id and move it anyway -- the same orphaning as
    above, on the pair of consoles least able to prove they are different.
    """
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    _owns_hub(hass, entry, "hub-1", "Hall Hub")

    started = await entry.start_reconfigure_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {**USER_INPUT, CONF_HOST: OTHER_HOST},
        hubs=[_hub(mac=PLACEHOLDER_MAC, hub_id="hub-9")],
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "different_console"
    assert entry.data[CONF_HOST] == HOST


async def test_reconfigure_follows_a_console_that_has_no_mac_to_show(hass):
    """The hub id is the identity when the mac is not, and it still matches.

    A console with nothing keyable about it is the strongest form of "we cannot
    tell", so refusing every one of them would take reconfigure away from the
    installs mid-adoption -- which are the ones being set up right now.
    """
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    _owns_hub(hass, entry, "hub-1", "Hall Hub")

    started = await entry.start_reconfigure_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {**USER_INPUT, CONF_HOST: OTHER_HOST},
        hubs=[_hub(mac=PLACEHOLDER_MAC, hub_id="hub-1")],
        reload=True,
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == OTHER_HOST
    assert entry.unique_id == LEGACY_ID


@pytest.mark.parametrize(
    ("filed_as", "reports"),
    [
        pytest.param(PLACEHOLDER_MAC, PLACEHOLDER_MAC, id="still mid-adoption"),
        pytest.param("FFFFFFFFFFFF", "FFFFFFFFFFFF", id="erased flash"),
        pytest.param("aabbcc", "aabbcc", id="a mac that is not one"),
        pytest.param("", "", id="no mac at all"),
        pytest.param(PLACEHOLDER_MAC, MAC, id="the real mac has since arrived"),
        pytest.param("", MAC, id="and for a hub that never had one"),
    ],
)
async def test_reconfigure_follows_a_console_it_could_not_be_filed_against(
    hass, filed_as, reports
):
    """Both halves of the ownership test have to be generated by one rule.

    They were not. A hub was filed under its raw mac whenever that was a
    non-empty string -- placeholder included -- while the console side offered a
    mac only when ``logic.mac_key`` accepted one. For any present-but-unusable
    mac the two sets could not intersect *by construction*, so an entry pointed
    at its own unchanged console, reporting the same hub under the same id, was
    told it was a different console. Every time, with no way back but deleting
    the entry and every entity id in it.

    It did not heal either: the placeholder a row was filed under is not
    something the console still says once it has read the hub's real mac, so the
    sets stayed disjoint afterwards too. Rows filed under a string that names no
    hardware are left out of the comparison entirely for that reason -- they are
    still that entry's devices, they are just no evidence about which console.
    """
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    _owns_hub(hass, entry, filed_as, "Hall Hub")

    started = await entry.start_reconfigure_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {**USER_INPUT, CONF_HOST: OTHER_HOST},
        hubs=[_hub(mac=reports)],
        reload=True,
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == OTHER_HOST


async def test_reauth_accepts_a_key_for_the_console_it_could_not_be_filed_against(hass):
    """The same disjointness, on the path where being turned away is near fatal.

    An entry in SETUP_ERROR is never retried and aborting reauth takes its
    repair issue with it, so "that key is for a different console" about the
    console that set the entry up left the user with a revoked key, no entities
    and nothing to click.
    """
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    _owns_hub(hass, entry, PLACEHOLDER_MAC, "Hall Hub")

    started = await entry.start_reauth_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {CONF_API_KEY: "fresh"},
        hubs=[_hub(mac=PLACEHOLDER_MAC)],
        reload=True,
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "fresh"


async def test_a_row_naming_no_hardware_does_not_excuse_the_rows_beside_it(hass):
    """Leaving the unreadable rows out must not disarm the check for the rest.

    An entry with a placeholder row *and* an ordinary one still owns the
    ordinary one, and that is evidence enough. Read the other way -- any
    unreadable row means "no evidence" -- a two-hub install adopted while one of
    them was mid-adoption could be moved onto a stranger console, which is the
    orphaning this test exists to refuse.
    """
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    _owns_hub(hass, entry, PLACEHOLDER_MAC, "New Hub")
    _owns_hub(hass, entry, MAC, "Hall Hub")

    started = await entry.start_reconfigure_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {**USER_INPUT, CONF_HOST: OTHER_HOST},
        hubs=[_hub(mac=OTHER_MAC, hub_id="hub-9")],
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "different_console"
    assert entry.data[CONF_HOST] == HOST


def test_an_address_shaped_id_can_reduce_to_a_well_formed_mac_key():
    """The trap the shape check exists for, stated as an equality.

    ``mac_key`` strips every character a mac may be separated by, and an address
    is mostly separators, so a common one comes back as twelve hex digits --
    accepted, indistinguishable downstream from a mac a console reported, and
    naming no hardware on earth. Nothing later can undo that, so the two shapes
    are told apart by shape before either is read as an identity.
    """
    assert mac_key(LEGACY_ID) == "192168150443"
    assert is_legacy_unique_id(LEGACY_ID)
    assert not is_legacy_unique_id(MAC_ID)


# A console whose mac is readable, and one still mid-adoption. Both are run
# against the phantom below: the first is the only one that isolates it (the
# second is also caught by the mid-adoption rule in ``_unique_id_for``), and the
# second is the population for which the phantom is *permanent*, since an entry
# whose console offers no usable mac is never re-keyed off ``host:port``.
PHANTOM_CONSOLES = [
    pytest.param(MAC, MAC_ID, id="a console whose mac is readable"),
    pytest.param(PLACEHOLDER_MAC, LEGACY_ID, id="and one still mid-adoption"),
]


@pytest.mark.parametrize(("reports", "keyed_on"), PHANTOM_CONSOLES)
async def test_reauth_does_not_read_a_legacy_id_as_hardware_the_entry_owns(
    hass, reports, keyed_on
):
    """The phantom, on the path where being turned away is close to fatal.

    An entry created before 0.3.0 is keyed ``host:port``, and on a great many
    real addresses that string reduces to exactly twelve hex digits. Read as a
    mac it is an ownership claim on hardware nobody has, so it can never
    intersect what a console reports -- and it is the *only* thing on the owned
    side here, because the entry's own device row names no hardware and is
    deliberately left out. Disjoint, so the flow aborted ``unique_id_mismatch``
    about the console that set the entry up.

    Which takes the repair issue down with it, on an entry HA never retries:
    a revoked key, no entities, and nothing left to click.
    """
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    _owns_hub(hass, entry, PLACEHOLDER_MAC, "Hall Hub")

    started = await entry.start_reauth_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {CONF_API_KEY: "fresh"},
        hubs=[_hub(mac=reports, hub_id="hub-1")],
        reload=True,
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "fresh"
    assert entry.unique_id == keyed_on


@pytest.mark.parametrize(("reports", "keyed_on"), PHANTOM_CONSOLES)
async def test_reconfigure_does_not_read_a_legacy_id_as_hardware_the_entry_owns(
    hass, reports, keyed_on
):
    """The same phantom on the step the legacy population actually reaches for.

    An entry that cannot reach its console never migrates, and unreachable is
    why someone opens Reconfigure -- so the entries carrying an address-shaped
    id are precisely the ones typing a new address here, and every one of them
    on such an address was told it had found a different console.
    """
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    _owns_hub(hass, entry, PLACEHOLDER_MAC, "Hall Hub")

    started = await entry.start_reconfigure_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {**USER_INPUT, CONF_HOST: OTHER_HOST},
        hubs=[_hub(mac=reports, hub_id="hub-1")],
        reload=True,
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == OTHER_HOST
    assert entry.unique_id == keyed_on


async def test_reconfigure_follows_its_own_console_through_a_re_adoption(hass):
    """A console cannot be told apart by a mac it is not currently reporting.

    The entry's row is filed under its hub's real mac; the console re-adopts
    that hub and reports the placeholder for it until it has read the mac again.
    ``names_hardware`` keeps the placeholder off the console side, so the two
    sets went disjoint -- and reconfigure told a user following their own
    unchanged console that they had typed a different one, which is the same
    class of false negative as the row-filed-under-a-placeholder case beside it.

    A mac the entry owns is unreadable here, not unmatched, so it stops being
    evidence for as long as any hub on the console has no usable mac. What is
    given up is telling two mid-adoption consoles apart by a mac neither of them
    reports, which was never a comparison; what is kept is every hub-id row,
    which still refuses a stranger.
    """
    entry = _entry(unique_id=MAC_ID)
    entry.add_to_hass(hass)
    _owns_hub(hass, entry, MAC, "Hall Hub")

    started = await entry.start_reconfigure_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {**USER_INPUT, CONF_HOST: OTHER_HOST},
        hubs=[_hub(mac=PLACEHOLDER_MAC, hub_id="hub-1")],
        reload=True,
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == OTHER_HOST
    assert entry.unique_id == MAC_ID


async def test_a_malformed_identifier_does_not_take_a_recovery_flow_down(hass):
    """Identifiers come off disk with no arity checked, and this unpacks them.

    ``DeviceRegistry._async_load_data`` rebuilds each one as
    ``tuple(iden)`` and validates nothing, so a hand-edited or partially
    restored registry can hold a one-element identifier. Unpacked into
    ``domain, identifier`` that raises ValueError out of the ownership test,
    which ends the flow -- on an entry whose recovery it is.
    """
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, MAC), (DOMAIN,)},
        name="Hall Hub",
    )

    started = await entry.start_reauth_flow(hass)
    result = await _configure(
        hass, started["flow_id"], {CONF_API_KEY: "fresh"}, reload=True
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == MAC_ID


async def test_reconfigure_refuses_a_console_another_entry_already_holds(hass):
    """Two entries for one console is the state this scheme exists to prevent."""
    other = MockConfigEntry(
        domain=DOMAIN, unique_id=OTHER_MAC, data={**USER_INPUT, CONF_HOST: "other.lan"}
    )
    other.add_to_hass(hass)
    entry = _entry(unique_id=LEGACY_ID)
    entry.add_to_hass(hass)
    started = await entry.start_reconfigure_flow(hass)
    result = await _configure(
        hass,
        started["flow_id"],
        {**USER_INPUT, CONF_HOST: "other.lan"},
        hubs=[_hub(mac=OTHER_MAC)],
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.unique_id == LEGACY_ID
