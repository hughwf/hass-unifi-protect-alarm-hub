"""Config flow for the UniFi Protect Alarm Hub.

Three ways in, one validation path. Adding a console, replacing a rotated API
key and moving an entry to a new address all ask the same question -- does this
address plus this key reach a console with an alarm hub on it? -- so they share
``_async_validate`` and differ only in what they do with the answer. The two
recovery paths exist because a LAN integration outlives its own configuration:
a key gets revoked, a DHCP lease moves the console, and until now neither had a
way back that did not go through deleting the entry and every entity id in it.

An entry is keyed on its alarm hub's mac rather than on the address it was
reached at, because an address is not an identity: the same console added once
by IP and once by hostname produced two entries, the second setting up
"successfully" with no entities, colliding on every unique_id and running a
second WebSocket against the console. Entries created before that carry
``host:port``; ``async_migrate_unique_id`` re-keys them from setup.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AlarmHubApiClient, AlarmHubAuthError, AlarmHubConnectionError
from .const import DEFAULT_PORT, DEFAULT_VERIFY_SSL, DOMAIN
from .coordinator import hubs_by_id
from .logic import hub_keys, identity_aliases, mac_key, names_hardware, own_identities
from .models import AlarmHub

_LOGGER = logging.getLogger(__name__)

# The scheme a browser address arrives with, taken off so what remains can be
# read as a netloc. Matched from the front rather than split on "//", because
# ``split("//", 1)[-1]`` took the *tail*: "192.168.1.1//evil.example" normalised
# to the host ``evil.example``, which no URL parser reads there and which the
# user's API key was then sent to.
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)

# What a host may contain once ``urlsplit`` has lowercased it. IPv4 and IPv6
# literals, DNS names, ``.local`` names and the underscores some DHCP servers
# hand out all fit; the point is what does not.
#
# Deliberately narrower than "whatever yarl accepts", because yarl IDNA-folds
# the host at request time and several characters fold into a dot: a host typed
# as "192.168.1.1<U+3002>evil.example" (U+FF0E and U+FF61 alike) leaves here
# looking like one label and reaches the wire as ``192.168.1.1.evil.example``.
# An address that resolves somewhere the user cannot see in what they typed has
# to be refused where they *can* see it -- on the form, with an error.
_HOSTNAME = re.compile(r"[a-z0-9._-]+")

# ``urlsplit`` deletes these from the string before it parses it (WHATWG says
# so; see ``urllib.parse._UNSAFE_URL_BYTES_TO_REMOVE``), which would let a
# pasted "192.168.1.1\nevil.example" be *parsed* as a single host nobody typed.
# Refused instead: deletion is a reinterpretation, and this one is invisible.
_DELETED_BY_URLSPLIT = ("\t", "\r", "\n")

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_API_KEY): str,
        vol.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
    }
)

# Reauth replaces the key and nothing else: the console it belongs to is the one
# the entry already points at, and offering the address again would invite
# someone to move the entry from a dialog that cannot check the move.
STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_API_KEY): str})


def _is_ipv6(host: str) -> bool:
    """Whether ``host`` is a bare IPv6 literal, brackets not included."""
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
        return False


def normalise_host(raw: str) -> tuple[str, int | None] | None:
    """Parse what was pasted into a host a URL can be built from, and a port.

    Every shape below used to reach ``AlarmHubApiClient`` verbatim and come back
    as a bare "Failed to connect": an address copied from a browser became
    ``URL("https://https//host:443/...")``, an IPv6 literal raised ValueError
    out of yarl because the text after the last colon is not a port, and a
    pasted ``host:8443`` went out as a hostname with a colon in it while the
    port stayed at 443.

    The invariant that matters more than any of those: the host returned here is
    a case-folded *substring* of what the user typed, delimited exactly where a
    URL parser delimits one. Reaching it by splitting on "//" and keeping the
    tail did not hold that -- ``192.168.1.1//evil.example`` came back as
    ``evil.example``, a host that appears nowhere in any parse of that text, and
    the API key went to it. So the delimiting is done by ``urlsplit``, and
    everything ``urlsplit`` would quietly *reinterpret* rather than delimit --
    userinfo in front of the host, characters it deletes, characters yarl will
    later fold into a dot -- is refused rather than resolved.

    Returns the port only when the host carried one, so the caller can tell "no
    port given" from "port 443", and None when nothing usable is left -- an
    empty string, a port that is not a number, a host we will not stand behind
    -- so the flow can say which of the two things the user typed is wrong
    instead of blaming the network.
    """
    text = raw.strip()
    if any(char in text for char in _DELETED_BY_URLSPLIT):
        return None
    try:
        parsed = urlsplit(f"//{_SCHEME.sub('', text, count=1)}")
        if _is_ipv6(parsed.netloc):
            # An unbracketed IPv6 literal is read off the netloc rather than
            # through ``hostname``, which takes the first colon of ``fd00::1``
            # for the port separator and hands back the host ``fd00``.
            return f"[{parsed.netloc}]", None
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        # A port that is not a number or is out of range, or characters that
        # cannot appear in a netloc at all.
        return None
    if not hostname:
        return None
    if port == 0:
        # ``urlsplit`` reads 0 as a port in range and hands it over, and it then
        # overrides the port box because an embedded port is the more specific
        # of the two. Nothing serves on port 0 -- it is the "pick one for me"
        # value a *listener* passes -- so a slipped keystroke came back as
        # "Failed to connect" from the network, blaming the console for a typo
        # in the field the user is looking at. The two neighbouring mistakes,
        # ``:donkey`` and ``:99999``, already land on the form; this one is
        # refused with them.
        return None
    if parsed.username is not None or parsed.password is not None:
        # ``user@host`` is a host away from where it reads: everything before
        # the "@" is credentials and ``192.168.1.1@evil.example`` targets
        # evil.example. Nobody types credentials into an address field on
        # purpose, and guessing which half they meant is not ours to do.
        return None
    if ":" in hostname:
        # ``hostname`` strips the brackets an IPv6 literal arrived in; the URL
        # the client builds needs them back, or the port reads as another hextet.
        return (f"[{hostname}]", port) if _is_ipv6(hostname) else None
    if not _HOSTNAME.fullmatch(hostname):
        return None
    return hostname, port


def console_unique_id(hubs: Iterable[AlarmHub]) -> str | None:
    """The identity an entry keys on: the lowest alarm-hub mac, or None.

    Lowest rather than first, so a console that lists two hubs in whatever order
    it likes gives the same answer to the flow that adds it and to the setup
    that later migrates it -- an identity that depends on list order is not one.
    Normalised, so ``AA:BB:...`` and ``aabb...`` cannot be read as two consoles.

    None when no hub offers a usable mac -- see ``logic.mac_key``, which is the
    one place in this integration that decides what a mac is not. The caller
    decides what to do with that; what it must not do is invent an id that
    changes next time.
    """
    macs = {key for hub in hubs if (key := mac_key(hub.mac)) is not None}
    return min(macs) if macs else None


def legacy_unique_id(data: Mapping[str, Any]) -> str:
    """The address-shaped unique id entries created before 0.3.0 carry."""
    return f"{data[CONF_HOST]}:{data[CONF_PORT]}"


def is_legacy_unique_id(unique_id: str | None) -> bool:
    """Whether ``unique_id`` is one of the address-shaped ids from before 0.3.0.

    By shape, rather than by rebuilding it from ``entry.data`` and comparing:
    the data it was built from does not stay put. Reconfigure follows a console
    to a new address and reauth rewrites a stored host that normalises
    differently, and either leaves an entry whose id no longer equals the string
    ``legacy_unique_id`` now derives. Asked that way, such an entry could never
    migrate again -- not on any later start, for the life of the install --
    while remaining addable a second time under another address, which is the
    duplicate mac keys exist to refuse.

    The shape is decidable because there are only two: this integration has ever
    written ``host:port`` and a mac key, and a mac key is twelve hex digits with
    every separator stripped, so it cannot contain a colon. A port always can.
    """
    return unique_id is not None and ":" in unique_id


@callback
def async_migrate_unique_id(
    hass: HomeAssistant, entry: ConfigEntry, hubs: Iterable[AlarmHub]
) -> None:
    """Re-key an entry that predates mac-based identity, once the mac is visible.

    Called from setup rather than from ``async_migrate_entry`` because the new
    id does not exist until the console has answered: a migration hook that
    needs the network turns a console that is merely slow to boot into
    MIGRATION_ERROR, a state ``ConfigEntry.async_migrate`` does not retry before
    the next restart, while a setup that cannot reach the console is retried for
    free. Nothing in ``data`` changes, so there is no schema version to bump --
    only the key HA indexes the entry under.

    Idempotent by construction: the only ids it will overwrite are the
    address-shaped ones the old flow wrote, and a mac key is never one of those
    (see ``is_legacy_unique_id``). An entry whose console another entry already
    holds is left alone -- that duplicate is what the new scheme exists to
    prevent, and re-keying both onto one id would leave HA's index answering for
    only one of them.
    """
    if not is_legacy_unique_id(entry.unique_id):
        return
    unique_id = console_unique_id(hubs)
    if unique_id is None:
        return
    other = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
    if other is not None:
        # Named by address and entry id, not by title: every entry this
        # integration creates is titled "UniFi Protect Alarm Hub", so a message
        # built from titles asked the user to remove one of two identical names
        # and gave them nothing to tell the two apart. The address is what they
        # recognise the console by, and the entry id is what the frontend puts
        # in the URL of the entry's own page.
        _LOGGER.warning(
            "Not re-keying the entry for %s (%s): this console is already configured"
            " as the entry for %s (%s). Remove one of the two -- they hold a WebSocket"
            " each and contend for every entity id",
            entry.data[CONF_HOST],
            entry.entry_id,
            other.data[CONF_HOST],
            other.entry_id,
        )
        return
    hass.config_entries.async_update_entry(entry, unique_id=unique_id)


@callback
def _async_owned_hub_keys(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """Every string ``entry`` already knows one of its *own* hubs by.

    The device registry is the record. Each row this entry owns is a hub it
    published entities for, filed under the identity ``logic.HubDeviceIds``
    settled on. That evidence survives what ``unique_id`` cannot: an entry that
    has not reached its console since 0.3.0 still carries ``host:port``, which
    names no console at all, and that is exactly the population reconfigure
    exists to serve.

    Its own key counts too, for the entry whose very first setup failed and so
    has no device rows to speak for it yet.

    Its own key counts only when it *is* a key. An address-shaped id is not one
    and must never be read as one: ``mac_key`` strips every character a mac can
    be separated by, and an address is mostly separators, so
    ``192.168.1.50:443`` reduces to ``192168150443`` -- twelve hex digits, a
    well-formed mac key naming no hardware anywhere on earth. Offered here it
    cannot intersect anything the console reports, which turns every legacy
    entry on such an address into a permanent ``unique_id_mismatch`` on reauth
    and ``different_console`` on reconfigure -- and precisely on the population
    that has no other way back, because an entry that never reaches its console
    never migrates and so carries that id for good. ``is_legacy_unique_id``
    decides the shape, and says why the two shapes are always separable.

    Each identity is offered in every spelling it answers to (see
    ``logic.identity_aliases``), and the identities that name no hardware are
    not offered at all -- neither here nor by ``_console_hub_keys``, which is
    what keeps the two halves of this comparison generated by one rule. Read
    both ways, an identity a console cannot be told apart by is worse than no
    evidence: offered on one side only it refuses a working install its own
    console for good, and offered on both it hands the entry to a stranger.
    """
    own = None if is_legacy_unique_id(entry.unique_id) else mac_key(entry.unique_id)
    keys = set() if own is None else {own}
    for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id):
        for identifier in own_identities(device.identifiers, DOMAIN):
            if names_hardware(identifier):
                keys.update((identifier, *identity_aliases(identifier)))
    return keys


def _console_hub_keys(hubs: Mapping[str, AlarmHub]) -> set[str]:
    """Every string the console now in front of us can be recognised by.

    ``logic.hub_keys`` and nothing else, so that whatever a hub would be filed
    under is a string this offers. Restating the rule here is what made the two
    halves disjoint by construction: a hub whose mac was present but unusable
    was filed under that mac and then never offered under it, so an entry
    pointed at its own unchanged console was refused as a different one --
    every time, for good, with no way back but deleting the entry.

    Filtered by ``logic.names_hardware`` for the reason ``_async_owned_hub_keys``
    gives, and symmetrically with it: the hub ids survive that filter (the
    coordinator has already validated them, and they are what the registry holds
    for any hub whose mac the console never gave), the placeholder and the empty
    string do not.
    """
    return {
        key
        for hub_id, hub in hubs.items()
        for key in hub_keys(hub, hub_id)
        if names_hardware(key)
    }


class _ValidationFailed(Exception):
    """Carries the error key a failed validation should show on the form."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class AlarmHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for UniFi Protect Alarm Hub."""

    VERSION = 1

    async def _async_validate(
        self, user_input: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, AlarmHub]]:
        """Reach the console ``user_input`` describes and list its alarm hubs.

        Returns the data to store -- with the host and port as they were
        normalised, since those are what every later request has to use -- and
        the hubs, filtered by ``hubs_by_id``: the coordinator's own predicate,
        imported rather than restated so the flow cannot accept a console the
        integration will then publish nothing from. Checking ``if not hubs``
        against an unfiltered list is how a console answering with
        ``isAlarmHub: false`` used to pass here and yield zero entities.
        """
        normalised = normalise_host(user_input[CONF_HOST])
        if normalised is None:
            raise _ValidationFailed("invalid_host")
        host, embedded_port = normalised
        # A port pasted into the host field wins over the port box: it is the
        # more specific of the two, and it is what someone copying a working
        # browser address has in hand while the box still shows its default.
        port = user_input[CONF_PORT] if embedded_port is None else embedded_port
        data = {**user_input, CONF_HOST: host, CONF_PORT: port}
        client = AlarmHubApiClient(
            host,
            port,
            data[CONF_API_KEY],
            async_get_clientsession(self.hass, verify_ssl=data[CONF_VERIFY_SSL]),
        )
        try:
            hubs = await client.async_get_alarm_hubs()
        except AlarmHubAuthError as err:
            raise _ValidationFailed("invalid_auth") from err
        except AlarmHubConnectionError as err:
            raise _ValidationFailed("cannot_connect") from err
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Unexpected error validating UniFi Protect")
            raise _ValidationFailed("unknown") from err
        return data, hubs_by_id(hubs)

    @callback
    def _unique_id_for(
        self, entry: ConfigEntry, hubs: dict[str, AlarmHub], *, mismatch: str
    ) -> str | None:
        """The id ``entry`` should carry now, aborting if it may not carry it.

        Two ways it may not: the console in front of us is not hardware this
        entry owns any of, or another entry already holds it. Either would end
        with an entry pointing at a console whose entities it does not own --
        the first orphans the ones it does own, the second leaves two entries
        contending for one console's entity ids, which is the state F3's
        duplicate entries left people in.

        "Not hardware this entry owns" is decided by *overlap*, not by whether
        the id still matches. Comparing ids got both directions wrong. An entry
        still carrying ``host:port`` was read as naming no console, so it was
        adopted onto whatever answered -- and an entry that cannot reach its
        console never migrates, so the legacy-keyed population is precisely the
        one reconfigure serves: typing the address of a *different* console
        moved the entry there, silently, leaving its device row and every entity
        on it stranded at None with every automation against them dead. In the
        other direction the id is one hub's mac, the lowest, so replacing that
        hub or retiring it from a two-hub console made a console the entry
        plainly owns look like a stranger -- and the abort then removed the
        reauth flow and its repair issue from an entry that is never retried.

        Overlap answers both, because the hubs an entry has device rows for are
        the hardware it owns, whatever its id says. Mismatch now means no
        relationship at all.

        Three ways there is simply no evidence, and none of them is a mismatch:
        a console reporting no hubs (un-adopted, mid-reboot -- reauth is about
        the key and must still work), an entry that owns nothing yet (its first
        setup failed, so there is nothing for a new console to fail to match),
        and an entry whose device rows are all filed under something that names
        no hardware -- an install adopted mid-adoption, whose rows say
        ``000000000000`` or the empty string. Those rows are still that entry's
        devices and it keeps them, but they say nothing about *which* console,
        so ``_async_owned_hub_keys`` leaves them out and this reads as an entry
        with no evidence rather than one owning hardware it cannot find here.

        A console with no usable mac still keeps whatever identity the entry
        already had, because an identity we cannot read is not the same as one
        that changed -- but only after the overlap check, or two consoles that
        are both mid-adoption would be interchangeable.

        A mac the entry owns is likewise unreadable, not unmatched, but only
        while this console cannot answer in macs *at all*. The console's own hub
        reports the placeholder for a while after a re-adoption; ``names_hardware``
        keeps that out of ``_console_hub_keys``, so the two sets went disjoint on
        an entry pointed at its own unchanged console at an unchanged address,
        and reconfigure named the one thing that had not happened.

        ``all`` rather than ``any``, and the distinction is the whole guard: one
        mid-adoption hub beside a readable one used to discard every mac the
        entry owned, which empties the evidence for the ordinary install -- rows
        filed under a real mac -- and an empty set short-circuits the overlap
        check entirely. A stranger only had to have one hub still adopting to be
        accepted silently. A console that reports even one usable mac can be
        compared; what is given up is telling two wholly mid-adoption consoles
        apart by a mac neither of them is reporting, which was never a
        comparison either.

        ``AbortFlow`` rather than a returned reason, matching how HA's own
        ``_abort_if_unique_id_*`` helpers say no from inside a check.
        """
        owned = _async_owned_hub_keys(self.hass, entry)
        if hubs and all(mac_key(hub.mac) is None for hub in hubs.values()):
            owned = {key for key in owned if mac_key(key) is None}
        if owned and hubs and owned.isdisjoint(_console_hub_keys(hubs)):
            raise AbortFlow(mismatch)
        unique_id = console_unique_id(hubs.values())
        if unique_id is None:
            return entry.unique_id
        registered = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, unique_id
        )
        if registered not in (None, entry):
            raise AbortFlow("already_configured")
        return unique_id

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a console, refusing one this integration would find nothing on.

        A console with no adopted alarm hub is turned away on the form rather
        than through ``async_abort``: it is a state the user can change in
        another browser tab and retry from here, whereas an abort ends the flow
        and asks for every field again. What it can no longer do is finish --
        the green success dialog over an entry with zero entities, explained
        only in the log, is the failure this replaces. Adopting a hub after
        setup still needs no reload; adopting one during setup now needs one
        more Submit.
        """
        errors: dict[str, str] = {}
        data: dict[str, Any] | None = None
        if user_input is not None:
            try:
                data, hubs = await self._async_validate(user_input)
            except _ValidationFailed as err:
                errors["base"] = err.key
            else:
                if hubs:
                    # Without a usable mac the address is all we have; it is a
                    # weaker key, but it still refuses the same console twice at
                    # the same address, and setup upgrades it when a mac appears.
                    await self.async_set_unique_id(
                        console_unique_id(hubs.values()) or legacy_unique_id(data)
                    )
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="UniFi Protect Alarm Hub", data=data
                    )
                errors["base"] = "no_alarm_hub"

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                # Whatever survived validation, so a retry is one click rather
                # than a re-typed API key -- and so a host that was normalised
                # comes back in the form it will actually be used in.
                STEP_USER_SCHEMA,
                data or user_input,
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Entry point for the reauth HA starts when a refresh gets 401/403.

        Without this step ``async_start_reauth`` raised UnknownStep out of the
        task it runs in, which took the repair notification down with it -- and
        since no flow was ever created, the "already in progress" guard never
        tripped, so every subsequent failed refresh raised it again. A revoked
        key had no recovery short of deleting the entry and adding it back.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a new API key for the console this entry already points at.

        The key is validated against that console before it is stored, and the
        console is required to share at least one hub with the entry (see
        ``_unique_id_for``): a key that works but belongs to a different console
        would otherwise reload the entry onto hardware whose entities it does
        not own. Sharing a hub rather than still matching the id is what keeps a
        replaced or retired hub from reading as a replaced console, on the one
        path where being turned away is close to fatal -- an entry in
        SETUP_ERROR is never retried, and aborting this flow takes its repair
        issue with it. A console whose hub has been un-adopted still accepts a
        new key -- reauth is about the key, and refusing it would leave the user
        in a loop with no way to fix the credential.
        """
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data, hubs = await self._async_validate({**entry.data, **user_input})
            except _ValidationFailed as err:
                errors["base"] = err.key
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    # ``data_updates``, not the key alone: ``_async_validate``
                    # has just re-normalised the stored host, and an entry set
                    # up before that normalisation existed is one whose host is
                    # worth fixing while we are certain the fixed one answers.
                    data_updates=data,
                    unique_id=self._unique_id_for(
                        entry, hubs, mismatch="unique_id_mismatch"
                    ),
                )

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=STEP_REAUTH_SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the address, port, key or SSL setting of an existing entry.

        A LAN integration outlives DHCP leases and hostname changes, and this
        step is what makes the frontend offer the option at all
        (``ConfigEntry.supports_reconfigure`` is a ``hasattr`` on it). Without
        it the only way to follow a console that moved was to delete the entry,
        taking every entity id -- and every automation written against one --
        with it.

        Validated before anything is saved, and it updates the entry in place
        rather than creating a second one, so a typo costs a retry instead of a
        broken entry.

        Following a console is the whole point, but *moving* an entry to another
        console is not, and the address field cannot tell the two apart on its
        own. A console sharing no hub with what this entry already owns is
        refused (see ``_unique_id_for``): updating the entry in place is exactly
        what makes that dangerous, because the devices and entities it owned
        stay behind, still in the registry, still named in automations, with
        nothing left to answer for them.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data, hubs = await self._async_validate(user_input)
            except _ValidationFailed as err:
                errors["base"] = err.key
            else:
                if hubs:
                    return self.async_update_reload_and_abort(
                        entry,
                        data=data,
                        # A different reason from reauth's, because a different
                        # thing went wrong: nobody typed a key here, they typed
                        # an address, and telling them their key is for another
                        # console names neither the mistake nor the way out.
                        unique_id=self._unique_id_for(
                            entry, hubs, mismatch="different_console"
                        ),
                    )
                errors["base"] = "no_alarm_hub"

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                # Including the API key: UniFi OS shows a key once, at creation,
                # so a form that blanks it would make changing a port cost a new
                # key. It is already stored in plain text in .storage, and this
                # dialog is admin-only.
                STEP_USER_SCHEMA,
                {**entry.data, **(user_input or {})},
            ),
            errors=errors,
        )
