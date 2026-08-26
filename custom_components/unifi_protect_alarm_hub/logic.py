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

from collections.abc import Iterable, Mapping

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
    string ``device_identifier`` puts in the DeviceInfo, so device identity and
    entity identity cannot drift apart. Built from the raw ``mac`` instead, they
    did: ``mac`` is an unvalidated ``data.get("mac", "")`` from console JSON, so
    a mid-adoption snapshot that answers before it is populated -- or a delta
    that nulls it -- silently renamed every unique_id under the hub. Home
    Assistant answers that by registering a whole second set of entities, with
    the first set left pointing at the same hardware forever, and two hubs that
    both report a blank mac collide on every hub-level id instead.

    ``device_identifier`` returns the raw ``mac`` verbatim whenever there is a
    usable one, which is what makes this safe to adopt on an existing install:
    the ids users already have are the ids this still produces.
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


def hub_keys(hub: AlarmHub | None, hub_id: str) -> tuple[str, ...]:
    """Every string this snapshot entry can be recognised by, best first.

    ``mac`` is typed ``str`` but arrives as an unchecked ``data.get("mac", "")``
    from console JSON, and two shapes of it do real damage. A non-string (a
    list, say) is unhashable and raised ``TypeError`` inside
    ``DeviceInfo(identifiers=...)``, failing platform setup outright; an empty
    one collapses every hub onto one identity, which Home Assistant answers by
    dropping the second hub's entities as duplicate unique ids. So only a usable
    mac is offered, and ``hub_id`` is always offered: the coordinator has
    already validated it as a usable string and keys the snapshot by it, so it
    is both present and unique exactly when ``mac`` is not.

    Two keys rather than one is what lets a hub be followed while the console
    changes either of them -- a re-adoption issues a new id and keeps the mac,
    a mid-adoption snapshot answers before the mac is populated and fills it in
    later, and a delta can null it back out. See ``HubDeviceIds``.
    """
    mac = hub.mac if hub is not None else None
    if isinstance(mac, str) and mac:
        return (mac, hub_id)
    return (hub_id,)


def device_identifier(hub: AlarmHub | None, hub_id: str) -> str:
    """The identity a hub would be given if we had never seen it before.

    The mac whenever the console gives a usable one, which is the whole upgrade
    contract: every unique_id in the integration is composed from this, and the
    ones existing installs already hold were built from the raw mac.
    """
    return hub_keys(hub, hub_id)[0]


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
    would strand the id-based entities the last run created. Only the chosen
    identity is recorded there, though, so the *other* keys a device has been
    seen under are learned again from scratch each run -- a console that has
    stopped reporting a mac entirely reconnects to its old, mac-named device
    only for as long as this process lives.

    A key is only worth following while it names one hub, and ``mac`` does not
    always: a console mid-adoption reports the placeholder ``000000000000``, and
    a console bug can repeat a real one. Both hubs then resolved to the same
    identity, so the second got no entities at all -- every unique_id was
    already taken -- and the surviving set read whichever hub ``find`` reached
    first, which is REST list order. An entity confidently reporting a different
    physical hub's zone is worse than either hub being missing, so an identity
    belongs to exactly one hub id, and a second hub presenting a mac that is
    already claimed falls back to its own id.

    "Already claimed" has to mean *by a hub that is still here*, or the fix
    would break the case the scheme exists for: a re-adoption is precisely a mac
    turning up under a new id, and refusing it there would split the device it
    is meant to hold together. So the claim is only contested while the hub that
    made it is in the same snapshot -- which is what ``observe`` is for, and why
    the reconcile pass hands over the whole snapshot before anything is built.
    That is a rule for *minting* an identity, and only that: looking one up goes
    by the owner recorded here rather than by who is live, or the crossing comes
    back the moment the owner misses a snapshot. See ``find``.

    The honest limit is a hub with no usable mac. Its only key is its id, so a
    re-adoption there really is a new device to us and the old entities stay
    unavailable until the user removes them. Nothing in the payload survives
    the change to tie the two together, and a looser match would merge hubs
    that are not the same hardware.
    """

    def __init__(self, known: Iterable[str] = ()) -> None:
        self._by_key: dict[str, str] = {key: key for key in known}
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

        Keys another live hub has claimed are dropped first, so the lookup and
        the mint below both work from the keys that name *this* hub. The lookup
        then reads them id first: a hub id is unique to one hub by construction,
        while a mac is only unique when the console is telling the truth, so on
        the one occasion the two disagree the id is the one to believe.

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
        device_id = next(
            (self._by_key[key] for key in reversed(keys) if key in self._by_key),
            keys[0],
        )
        self._owner[device_id] = hub_id
        for key in keys:
            self._by_key.setdefault(key, device_id)
        return device_id

    def _claimed_elsewhere(self, key: str, hub_id: str) -> bool:
        """Whether ``key`` resolves to a device another hub in the snapshot owns.

        A hub's own id is never contested. The coordinator keys the snapshot by
        it, so it names exactly one hub in any snapshot -- which is what leaves
        every hub one key of its own to fall back on, and why the filter above
        can never come back empty.
        """
        if key == hub_id:
            return False
        device_id = self._by_key.get(key)
        owner = self._owner.get(device_id) if device_id is not None else None
        return owner is not None and owner != hub_id and owner in self._live

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
