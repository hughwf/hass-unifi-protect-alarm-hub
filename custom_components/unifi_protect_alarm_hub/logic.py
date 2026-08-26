"""Pure derivation logic. No Home Assistant, no uiprotect — stdlib + local
models only — so it is unit-testable with plain pytest. device_class values are
plain strings matching HA's BinarySensorDeviceClass values; the entity layer
wraps them.

The rule running through the state predicates here: only a value we recognise
may produce a ``bool``. Every wire value comes from console JSON that nothing
validates, and the enums it draws from have an ``unknown`` member of their own,
so "not the value that means on" is never the same question as "off". These are
the functions an alarm automation reads through, and a ``False`` returned for a
status we cannot read is an affirmative all-clear nobody asked for; ``None``
(rendered by Home Assistant as ``unknown``) is how a state property says it does
not know, and it fails both ``state == 'on'`` and ``state == 'off'``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .models import AlarmHub, Battery, Cover, InputZone, OutputChannel

ZONE_FAULT_STATUSES = {"fault", "short", "cut"}

# The statuses that describe a loop the hub could actually read. Everything else
# -- the fault trio, the console's own ``unknown``, or no status at all on a zone
# first seen through a partial WebSocket frame -- says nothing about whether the
# contact is open or closed.
ZONE_INTACT_STATUSES = {"normal", "alarm"}

ZONE_DEVICE_CLASS: dict[str, str] = {
    "MOTION": "motion",
    "ENTRY": "door",
    "SMOKE": "smoke",
    "GLASS_BREAK": "sound",
    "EMERGENCY_BUTTON": "safety",
}
DEFAULT_ZONE_DEVICE_CLASS = "safety"


def zone_is_on(zone: InputZone) -> bool | None:
    """Whether the zone is triggered: True on alarm, False on normal, else None.

    Mapping everything that is not ``alarm`` to False is the most dangerous
    thing this integration could do. A cut loop is the textbook defeat for a
    wired contact, and on the ``door`` device_class a False renders as "Closed":
    the entity would affirmatively report a secure door on a severed circuit,
    and every ``state == 'off'`` condition in an alarm automation would pass.
    The separate fault diagnostic is no substitute -- it is a different entity,
    and nobody's door automation reads it.
    """
    if zone.status not in ZONE_INTACT_STATUSES:
        return None
    return zone.status == "alarm"


def zone_fault_is_on(zone: InputZone) -> bool | None:
    """Whether the zone wiring is faulted (fault/short/cut). None if unreadable.

    The mirror of ``zone_is_on``: the two statuses that describe an intact loop
    are the only ones entitled to say "no fault", so a status we do not
    recognise leaves the diagnostic unknown rather than healthy.
    """
    if zone.status in ZONE_FAULT_STATUSES:
        return True
    if zone.status in ZONE_INTACT_STATUSES:
        return False
    return None


def zone_device_class(zone: InputZone) -> str:
    """Map a zone's input_type to an HA binary_sensor device_class string."""
    if zone.input_type is None:
        return DEFAULT_ZONE_DEVICE_CLASS
    return ZONE_DEVICE_CLASS.get(zone.input_type, DEFAULT_ZONE_DEVICE_CLASS)


def zone_enabled_default(zone: InputZone) -> bool:
    """Whether this zone's entities are enabled by default (enable == 'on')."""
    return zone.enable == "on"


def zone_name(zone: InputZone, zone_id: int) -> str:
    """Zone name, or 'Zone {id}' when unnamed."""
    return zone.name or f"Zone {zone_id}"


def entity_unique_id(device_id: str, suffix: str) -> str:
    """Compose one entity's permanent registry identity for a hub device.

    Every unique_id in the integration is built here, and always from the same
    string ``HubDeviceIds.resolve`` puts in the DeviceInfo, so device identity
    and entity identity cannot drift apart. Built from the raw ``mac``, they
    did: ``mac`` is an unvalidated ``data.get("mac", "")`` from console JSON, so
    a mid-adoption snapshot that answers before it is populated -- or a delta
    that nulls it -- silently renamed every unique_id under the hub. Home
    Assistant answers that by registering a whole second set of entities, with
    the first set left pointing at the same hardware forever, and two hubs that
    both report a blank mac collide on every hub-level id instead.

    What makes this safe to adopt on an existing install is not the shape of
    the string but where it comes from: ``HubDeviceIds`` hands back the identity
    the device registry already holds for the hub whenever it holds one, so the
    ids users already have are the ids this still produces. Only a hub nothing
    has been recorded for gets a freshly minted one (see ``device_identifier``).
    """
    return f"{device_id}_{suffix}"


def zone_unique_id(device_id: str, zone_id: int) -> str:
    return entity_unique_id(device_id, f"zone_{zone_id}")


def zone_fault_unique_id(device_id: str, zone_id: int) -> str:
    return entity_unique_id(device_id, f"zone_{zone_id}_fault")


def output_unique_id(device_id: str, output_id: int) -> str:
    return entity_unique_id(device_id, f"output_{output_id}")


def output_is_on(output: OutputChannel) -> bool | None:
    """Whether the output relay is energised. None when ``active`` is unreadable.

    A siren switch reporting "Off" is what someone checks before assuming the
    siren is silent, so it may only say so when the hub said so.
    """
    if output.active not in ("on", "off"):
        return None
    return output.active == "on"


def output_confirms(output: OutputChannel | None, expected: bool) -> bool:
    """Whether the hub is now reporting the relay state a command asked for.

    Only a hub that says so retires the switch's optimistic value, so ``None``
    -- an ``active`` we cannot read, or an output that has dropped out of the
    snapshot -- deliberately does not count. An unreadable value is the hub
    declining to say what the relay is doing, which is the last thing that
    should be allowed to settle the question early.
    """
    return output is not None and output_is_on(output) is expected


def output_name(output: OutputChannel, output_id: int) -> str:
    """Output name, or 'Output {id}' when unnamed."""
    return output.name or f"Output {output_id}"


def hub_is_connected(hub: AlarmHub) -> bool | None:
    """Whether the console can see the hub. None only when it reported no state.

    Unlike the wired-periphery predicates, a state that is merely not CONNECTED
    -- disconnected, rebooting, mid-adoption -- is still a state, and False is
    the correct answer for all of them. Only a hub carrying no state at all, or
    one in a shape we do not parse, is genuinely unknown. Availability collapses
    the two (see ``AlarmHubBaseEntity.available``); the connectivity diagnostic
    does not, which is why the distinction lives here.
    """
    if not isinstance(hub.state, str):
        return None
    return hub.state == "CONNECTED"


def armed_is_on(armed: str | None) -> bool | None:
    """Whether the hub is armed. None when it did not say, or said something else."""
    if armed not in ("on", "off"):
        return None
    return armed == "on"


def cover_is_on(cover: Cover | None) -> bool | None:
    """Whether the tamper cover is open. None when we cannot tell.

    ``close`` is the only value that means "case intact". A hub that stopped
    reporting a cover at all is not the same thing, and "Clear" on a tamper
    sensor is exactly the reassurance nobody should get for free.
    """
    if cover is None or cover.status not in ("open", "close"):
        return None
    return cover.status == "open"


def battery_connected_is_on(battery: Battery | None) -> bool | None:
    """Whether the backup battery is connected. None when the hub did not say."""
    if battery is None or battery.connection not in ("connected", "disconnected"):
        return None
    return battery.connection == "connected"


def push_is_fresh(delivered_at: float, now: float, window: float) -> bool:
    """Whether the last state delivered *for one hub* is recent enough to stand.

    Only ever consulted once REST polling has failed, so what it times in
    practice is the push path. It times *deliveries* rather than the socket,
    because a socket that is connected but silent for hours is not evidence of
    anything -- and ``window`` is the poll interval, so a delivery never keeps
    an entity alive for longer than the poll it is standing in for would have.

    "For one hub" is the whole of it: the caller times each hub's own frames,
    not the arrival of any frame at all. Timed per socket, a chatty hub vouched
    for every silent one on the same console -- a second hub that had said
    nothing for twenty minutes kept publishing a confident "closed" because its
    neighbour was talking, which is exactly the evidence this refuses to accept
    from the socket itself.
    """
    return now - delivered_at < window


# A mac written the two ways one console writes it -- ``AA:BB:CC:DD:EE:FF`` in
# one payload and ``aabbccddeeff`` in the next -- is one piece of hardware, and
# read as two strings it is two identities. Twelve hex digits once whatever
# separated them is gone.
_MAC_SEPARATORS = re.compile(r"[^0-9a-f]")
_MAC_DIGITS = 12

# The macs that identify no hardware, so nothing may be identified by one. Both
# are what unwritten storage reads back as -- zeroed memory and erased flash --
# and the first is what a console reports for a hub whose mac it has not read
# yet. Every hub mid-adoption on every console reports that same value, so a
# scheme that accepted it as an identity would hand two different consoles one
# id -- and would hand the hub itself a name it stops answering to the moment
# the console fills the field in.
#
# These two and no more. A mac is a hardware address and "looks improbable" is
# not grounds to refuse one: ``22:22:22:22:22:22`` is a well-formed, locally
# administered unicast address a device may genuinely carry, and demoting a
# console that reports one to weaker address-based keying would cost a real
# install a stable identity in exchange for nothing anyone has seen. What these
# two have in common is not that they look odd, it is that they are what a field
# holds when no mac was ever written into it.
UNUSABLE_MACS = frozenset({"000000000000", "ffffffffffff"})


def _mac_digits(text: str) -> str:
    """``text`` reduced to the hex digits every spelling of a mac shares."""
    return _MAC_SEPARATORS.sub("", text.lower())


def mac_key(mac: Any) -> str | None:
    """Reduce a mac to the identity it compares by, or None if it is not one.

    ``mac`` arrives as an unvalidated ``data.get("mac", "")`` out of console
    JSON, so three shapes have to be turned away before anything is keyed on it:
    something that is not a string at all, something that is not a mac -- a hub
    answering before the console filled the field in, or a field holding who
    knows what -- and a mac every such console reports the same value for. All
    three are one failure, an "identity" two different consoles would share, and
    every caller answers it the same way: fall back to something weaker that is
    at least its own.

    This is the one place that decides what a mac is not, and every consumer
    asks it: the identity a new hub is minted under (``device_identifier``), the
    identity an entry is keyed on (``config_flow.console_unique_id``), and the
    spelling a recorded identity also answers to (``identity_aliases``). Two
    hubs that genuinely present one real mac is a different question, and
    ``HubDeviceIds`` settles that one by contest.
    """
    if not isinstance(mac, str):
        return None
    digits = _mac_digits(mac)
    if len(digits) != _MAC_DIGITS or digits in UNUSABLE_MACS:
        return None
    return digits


def names_hardware(identity: str) -> bool:
    """Whether an identity names a hub at all, or is only a field nobody filled.

    Two strings have been written into this integration's device registry that
    identify no hardware: the empty string, which is what a release before
    ``hub_keys`` filed a hub under when the console had not populated its mac,
    and the placeholder macs every console mid-adoption reports (see
    ``UNUSABLE_MACS``). Both are what an unwritten field reads back as, so every
    console in the world offers the same one and no two installs can be told
    apart by it.

    They are still perfectly good *device identities* -- an install that already
    holds one keeps it, because moving it would strand every entity registered
    under it -- but they are no evidence about *which console* is in front of
    us, and that is the one question they must never be allowed to answer. The
    config flow's ownership test is where the difference bites: comparing an
    entry's recorded identities against the console it is being pointed at, a
    placeholder on both sides is two strangers reading as one hub, and a
    placeholder on one side alone is an entry refused its own console forever.
    Neither side offers one, so neither can happen.
    """
    return bool(identity) and _mac_digits(identity) not in UNUSABLE_MACS


def identity_aliases(identity: str) -> tuple[str, ...]:
    """The other spellings an identity already on disk may be met under.

    A device row records the mac in whatever case the console used the day it
    was written, and the same console is free to use the other one today --
    ``AA:BB:CC:DD:EE:FF`` in one payload and ``aabbccddeeff`` in the next. Read
    as two strings that is two devices, so a recorded mac answers to its
    normalised form as well as to itself, and a recorded anything-else (a hub
    id, a placeholder, the empty string) answers only to itself.
    """
    key = mac_key(identity)
    return () if key is None or key == identity else (key,)


def own_identities(identifiers: Iterable[Any], domain: str) -> tuple[str, ...]:
    """The identities in a device row's ``identifiers`` that belong to ``domain``.

    ``(domain, identity)`` is the shape of every identifier this integration
    writes, and nothing enforces it on the way back in: the device registry
    stores identifiers as JSON arrays and restores them with
    ``{tuple(iden) for iden in device["identifiers"]}``, no arity check anywhere
    (see ``device_registry.DeviceRegistry._async_load_data``). A hand-edited or
    partially restored ``core.device_registry`` can therefore hold a one- or
    three-element identifier, and ``for domain, identifier in row.identifiers``
    raises ValueError on it.

    Which is not a crash anybody sees. Raised inside platform setup it is caught
    and logged, so the entry reports LOADED, publishes not one entity and leaves
    three tracebacks behind; raised inside the config flow's ownership test it
    takes a recovery flow down. Read by length instead, a malformed identifier
    is simply not one of ours.
    """
    return tuple(
        identifier[1]
        for identifier in identifiers
        if len(identifier) == 2 and identifier[0] == domain
    )


def hub_keys(hub: AlarmHub | None, hub_id: str) -> tuple[str, ...]:
    """Every string this hub could already be recorded under, best evidence first.

    Recognition, not minting -- ``device_identifier`` decides what a hub we have
    never seen is called, and these are the strings that say we *have* seen it.
    So the raw ``mac`` is offered whatever it looks like, including the two
    shapes ``mac_key`` refuses and the empty string, because every one of them
    has been written into a real user's device registry as an identity: the
    released 0.2 filed a hub under ``data.get("mac", "")`` verbatim, so a
    mac-less hub's device is on disk as ``(DOMAIN, "")`` and a hub adopted
    mid-adoption is on disk as its placeholder. Refusing to recognise those is
    not caution, it is minting a second device for hardware we already have
    entities for -- and leaving the first set unavailable for good.

    ``hub_id`` first, and always. The coordinator keys the snapshot by it, so it
    names exactly one hub in any snapshot, while a mac only names one while the
    console is telling the truth; on the one occasion the two disagree the id is
    the one to believe. It is also the only key a non-string ``mac`` leaves --
    a list is unhashable and raised ``TypeError`` inside
    ``DeviceInfo(identifiers=...)``, failing platform setup outright.

    Several keys rather than one is what lets a hub be followed while the
    console changes any of them: a re-adoption issues a new id and keeps the
    mac, a mid-adoption snapshot answers before the mac is populated and fills
    it in later, a delta can null it back out, and the spelling can change
    underneath all of that. See ``HubDeviceIds``.
    """
    keys = [hub_id]
    mac = hub.mac if hub is not None else None
    if isinstance(mac, str):
        keys.extend(key for key in (mac, mac_key(mac)) if key is not None)
    return tuple(dict.fromkeys(keys))


def device_identifier(hub: AlarmHub | None, hub_id: str) -> str:
    """The identity a hub is minted under, having never been recorded before.

    Only ever reached past ``HubDeviceIds``, which offers an identity already in
    the device registry first: what is on disk wins however odd it looks, and
    this decides the rest. Two rules and no more -- the mac when the console
    gives a usable one, the hub id when it does not.

    The mac in the console's own spelling, not ``mac_key``'s normalisation of
    it, even though ``mac_key`` is what decides whether there is one. The
    released 0.2 built every identity and every unique_id from the raw string,
    so minting it verbatim is what makes an upgrade a no-op *without needing
    evidence*: the ids a hub gets on a registry we have never read are already
    the ids that install holds. Normalising would make the upgrade contract
    depend entirely on the seed being there, for the sole benefit of a console
    that changes its spelling -- and that console is already handled, by
    ``identity_aliases``, on the side where it costs nothing.

    The hub id rather than an unusable mac is the whole of B2: a hub adopted
    while the console still reported ``000000000000`` took the placeholder as
    its identity, and the registry then recorded a string the hub stops
    answering to the moment its real mac appears. On the next restart nothing
    tied the two together and the hub got a second device, eleven registry
    entries becoming twenty-two. A hub id is reported for the life of the
    adoption, so an identity minted from one survives every restart -- and the
    two macs that are not identities are reported by every console mid-adoption
    alike, so a scheme that accepted one would hand two consoles the same id.
    """
    mac = hub.mac if hub is not None else None
    if mac is not None and mac_key(mac) is not None:
        return mac
    return hub_id


class HubDeviceIds:
    """Which device each hub is, decided once per device and then kept.

    The identity a hub *would* be given is not stable, and both directions it
    moves do permanent damage. A hub adopted while ``/v1/alarm-hubs`` had not
    yet populated its mac is identified by its id; when the mac turns up, every
    unique_id under it changes at once, so Home Assistant registers a second
    full set of entities and leaves the first pointing at the same live hardware
    for good -- eleven registry entries become twenty-two, with ``_2`` suffixes,
    and the automations are written against the half that no longer updates. A
    delta that nulls the mac does the same thing in reverse.

    So identity is assigned rather than derived: the first time a device is
    seen it takes ``device_identifier``, and after that every key it has ever
    appeared under -- mac and id both -- resolves back to that same string. A
    console that changes one of them changes nothing here, which is also what
    makes a re-adoption (new id, same mac) invisible.

    Seeded from the identifiers already in the device registry, so the decision
    survives a restart: recomputing it from a snapshot that now carries a mac
    would strand the id-based entities the last run created. That seed is the
    first of the three rules identity is decided by, and it outranks the other
    two -- an identity on disk wins whatever it looks like, a real mac, a
    placeholder or the empty string, because what makes an install stable is not
    that its identity is well-formed but that it does not move. ``hub_keys`` is
    correspondingly generous about what it will recognise a hub by, for exactly
    this reason.

    Only the chosen identity is recorded there, though, so the *other* keys a
    device has been seen under are learned again from scratch each run -- a
    console that has stopped reporting a mac entirely reconnects to its old,
    mac-named device only for as long as this process lives.

    A key is only worth following while it names one hub, and ``mac`` does not
    always: a console bug can repeat a real one, and every console mid-adoption
    reports the same placeholder. Both hubs then resolved to the same identity,
    so the second got no entities at all -- every unique_id was already taken --
    and the surviving set read whichever hub ``find`` reached first, which is
    REST list order. An entity confidently reporting a different physical hub's
    zone is worse than either hub being missing, so an identity belongs to
    exactly one hub id, and a second hub presenting a key that is already
    claimed falls back to its own id.

    "Already claimed" has to mean *by a hub that is still here*, or the fix
    would break the case the scheme exists for: a re-adoption is precisely a mac
    turning up under a new id, and refusing it there would split the device it
    is meant to hold together. So the claim is only contested while the hub that
    made it is in the same snapshot -- which is what ``observe`` is for, and why
    the reconcile pass hands over the whole snapshot before anything is built.
    A key that names no hardware is the exception, and ``_claimed_elsewhere``
    says why. That is a rule for *minting* an identity, and only that: looking
    one up goes by the owner recorded here rather than by who is live, or the
    crossing comes back the moment the owner misses a snapshot. See ``find``.

    The contest is on the identity a key leads to, never on the key alone --
    including the hub's own id, which is the one key no other hub can present
    and was for that reason exempted outright. Two live hubs shared one identity
    straight through the exemption: B takes mac M; B misses a snapshot and C,
    reporting M, is read as B re-adopted and takes M over; B comes back, its mac
    is contested and dropped, but its own id still pointed at M and the
    exemption waved it through. Both resolved to M -- one device row for two
    live hubs, an entity built and named for B publishing C's zone while B's own
    zone read normal. That an id names one hub is a fact about the key, and the
    failure was never about the key.

    Two limits are deliberate rather than fixed.

    A hub with no usable mac has only its id, so a re-adoption there really is a
    new device to us and the old entities stay unavailable until the user
    removes them. Nothing in the payload survives the change to tie the two
    together, and a looser match would merge hubs that are not the same
    hardware.

    And the seed puts identities that name no hardware into ``_by_key`` while
    deliberately leaving ``_owner`` empty, so at process start such a row has no
    owner and any hub offering that string can resolve onto it. That is what
    carries a pre-0.3 install -- a row filed under ``""`` or the placeholder --
    back onto its own hub, and it is the one place preferring the registry
    widens the surface rather than narrowing it: if the true owner is missing
    from the first snapshot after a restart, a *different* mid-adoption hub
    inherits the row and its eleven entity unique_ids. There is nothing in
    either payload to tell the two apart, and refusing the match instead would
    strand the upgrade it exists for, so the trade is taken knowingly.
    ``entity.async_migrate_hub_identity`` is what shrinks it: on the single-hub
    installs that make up nearly all of that population it rewrites the row to
    an identity that does name hardware, once, and after that there is nothing
    left for a stranger to inherit. What remains exposed is an entry that holds
    a second device row, where the migration refuses to guess.
    """

    def __init__(self, known: Iterable[str] = ()) -> None:
        recorded = list(known)
        # Each recorded identity answers to itself and to the other spelling a
        # mac can be met in, so a console that changes its mind about case finds
        # the device it already has rather than minting a second one. Aliases go
        # in first and exact spellings overwrite them: on the pathological
        # install that somehow holds both spellings as two rows, a hub reporting
        # one of them must reach that row and not the other.
        self._by_key: dict[str, str] = {
            alias: identity
            for identity in recorded
            for alias in identity_aliases(identity)
        }
        self._by_key.update({identity: identity for identity in recorded})
        # Which hub id each identity currently belongs to, and which hub ids the
        # last snapshot carried. Neither is seeded from the registry: it records
        # the identities that were decided, not the ids they were decided for.
        # An identity whose owner this process has not seen is therefore free to
        # be claimed, which is exactly right for the hub reappearing under a new
        # id after a restart.
        self._owner: dict[str, str] = {}
        self._live: frozenset[str] = frozenset()

    def observe(self, hubs: Mapping[str, AlarmHub]) -> None:
        """Settle every hub in one snapshot's identity, together.

        Two hubs reporting one mac can only be told from one hub reappearing
        under a new id by looking at the whole snapshot: the first has both ids
        in front of us at once, the second has lost the old one. Resolving hub
        by hub cannot make that distinction, so the snapshot is what defines
        which claims are contested.
        """
        self._live = frozenset(hubs)
        for hub_id, hub in hubs.items():
            self.resolve(hub, hub_id)

    def resolve(self, hub: AlarmHub | None, hub_id: str) -> str:
        """This hub's device identity, minting one only if it is genuinely new.

        The three rules, in order. Keys leading to a device another hub has the
        better claim to are dropped first, so everything below works from the
        keys that answer for *this* hub; what is left is looked up against the
        registry seed and whatever this process has learned since, best evidence
        first (see ``hub_keys``); and only a hub nothing answers for is minted,
        under ``device_identifier``.

        The mint is taken only if it survived the contest, which is what stops
        the second of two hubs sharing a mac from minting the identity the first
        one is already using. Every key can be dropped -- a hub whose id and mac
        both lead to a device that has moved on has nothing left to answer for
        it -- and the hub id is then the identity, which is what
        ``device_identifier`` would mint for it anyway and is a string no other
        hub reports.

        Only keys nobody has resolved before are recorded. A key that already
        answers is answering for a device that exists, and re-pointing it is how
        a hub takes another one's entities: the second of two hubs sharing a mac
        matched its own id, and then quietly moved the mac onto its own identity
        -- leaving the hub that owned it to mint a third device.
        """
        keys = [
            key
            for key in hub_keys(hub, hub_id)
            if not self._claimed_elsewhere(key, hub_id)
        ]
        minted = device_identifier(hub, hub_id)
        device_id = next(
            (self._by_key[key] for key in keys if key in self._by_key),
            minted if minted in keys else hub_id,
        )
        self._owner[device_id] = hub_id
        for key in keys:
            self._by_key.setdefault(key, device_id)
        # A hub id names one hub for the life of the adoption, so it points at
        # that hub's identity *now* -- including an identity just minted because
        # the one it used to point at has been taken over. ``setdefault`` above
        # cannot do that: the stale mapping is precisely what it preserves, and
        # the key is dropped from ``keys`` in that case anyway. Keeping it in
        # step is what makes "this hub's own id leads somewhere else" mean the
        # takeover happened rather than merely that a poll went oddly, and
        # ``find`` reads it -- an owner none of whose keys lead to its own
        # device is a device nothing answers for.
        self._by_key[hub_id] = device_id
        return device_id

    def _claimed_elsewhere(self, key: str, hub_id: str) -> bool:
        """Whether ``key`` resolves to a device another hub has the better claim to.

        Asked of the *identity* the key leads to, and of every key alike. A
        hub's own id used to be exempted outright, on the sound observation that
        the coordinator keys the snapshot by it so no other hub can present one
        -- and two live hubs shared an identity straight through that exemption,
        because what the id led to had been taken over while the hub was away
        (see the class docstring). No other hub presenting a key does not make
        the device behind it still ours.

        For a key that names hardware the claim only stands while its owner is
        in the same snapshot, because a mac turning up under a second id with
        the first id gone is a re-adoption, which is the case this whole scheme
        exists to follow. A key that names no hardware is never that: the
        placeholder and the empty string are what every console reports for
        every hub it has not read yet, so a second hub carrying one is never
        evidence that it is the first hub back under a new name. There the claim
        stands whether or not its owner is still here, and the newcomer mints
        its own identity rather than inheriting eleven entities describing
        somebody else's hardware.

        An unowned identity is never claimed, which is what the registry seed
        relies on and what ``resolve`` needs to be able to take an identity back
        after a restart -- and, for a key that names no hardware, is the trade
        the class docstring sets out.
        """
        device_id = self._by_key.get(key)
        owner = self._owner.get(device_id) if device_id is not None else None
        if owner is None or owner == hub_id:
            return False
        return owner in self._live or not names_hardware(key)

    def find(
        self, hubs: Mapping[str, AlarmHub], device_id: str
    ) -> tuple[str, AlarmHub] | None:
        """Locate the hub that *is* this device, under whatever it is called now.

        An entity belongs to a device, and a device is not the coordinator's
        dict key. Entities that resolved through the key they were built with
        found nothing ever again once a re-adoption changed it: permanently
        unavailable, live state one dict entry away, and no replacement built
        because the reconcile pass correctly refuses to duplicate a device whose
        unique_ids it already holds. Worse than a blank dashboard -- Home
        Assistant drops unavailable entities from service calls, so a panic
        automation's ``switch.turn_on`` on the siren returned having sent
        nothing at all.

        Returns the current key alongside the hub, because that key is what the
        API is addressed by: a command built from the id captured at
        construction goes to a device the console no longer has.

        Carrying one of a device's keys is not enough to *be* it: the match has
        to be the hub ``resolve`` last recorded as the owner. Two hubs reporting
        one mac both carry it, so matching on the key alone let whichever the
        snapshot listed first answer for the device -- reversing the console's
        order flipped what the tamper and door entities on it reported. Asking
        only that no *live* hub own the key left the same crossing one snapshot
        away: with the owner absent -- rebooting, mid-adoption, briefly disowned
        by a delta -- the contest dissolved, and the surviving hub answered for
        the absent one's device, down to the id its ``switch.turn_on`` was
        addressed to.

        Ownership rather than liveness is also what keeps a re-adoption
        working. There the owner is absent *and* the key has just been claimed
        by its own new id, so ``resolve`` has already moved the ownership over
        and this matches it; a hub that merely shares the key never gets it, and
        the device goes unavailable until its own hub is back in a snapshot.
        """
        for hub_id, hub in hubs.items():
            if self._owner.get(device_id) == hub_id and any(
                self._by_key.get(key) == device_id for key in hub_keys(hub, hub_id)
            ):
                return hub_id, hub
        return None


def hub_device_name(hub: AlarmHub | None, device_id: str) -> str:
    """The hub's device-registry name, never blank.

    ``has_entity_name`` composes entity ids as "<device> <entity>", so a device
    with no name degrades every one of them to a bare generic --
    ``binary_sensor.tamper`` rather than ``binary_sensor.hall_hub_tamper`` --
    and two unnamed hubs then collide over the lot. The identifier is the
    fallback for the same reason it identifies the device: it is there and it
    is distinct.
    """
    name = hub.name if hub is not None else None
    if isinstance(name, str) and name:
        return name
    return f"Alarm Hub {device_id}"
