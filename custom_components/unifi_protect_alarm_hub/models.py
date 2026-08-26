"""Lightweight dataclasses parsed from the UniFi Protect public-API JSON.

Pure: only stdlib. Attribute names deliberately mirror the shape the entity
layer consumes (``alarm_hub_inputs`` etc.) so platforms stay thin. All status /
type values are kept as their raw wire strings.

Each hub keeps the JSON it was parsed from, so a partial WebSocket frame can be
layered over it (``AlarmHub.with_delta``) without a REST round trip. That makes
parsing part of the cache: a merged payload becomes the base for every later
merge, so whatever this module keeps, it keeps for good. Hence the rule running
through it -- parse anything without raising, but refuse to *store* a payload
that has stopped describing a hub.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Sections that are shape rather than content. Individual zones and outputs come
# and go inside ``input``/``output``, but the maps themselves -- and the
# ``alarmHub`` object around them -- always exist: the model has no
# representation for a hub without them, so a frame that turns one into null or
# a list is malformed.
_REQUIRED_HUB_SECTIONS = ("input", "output")

# Sections a hub may genuinely not have, which the model types as optional
# (``alarm_hub_battery`` and ``alarm_hub_cover`` are ``| None``). "Absent" is a
# state we can represent, so the null that ``deep_merge`` documents as a removal
# is a legitimate delta here -- while anything else non-mapping still is not.
_REMOVABLE_HUB_SECTIONS = ("battery", "cover")

# Longest zone/output key ``_int_keyed`` will convert. ``int()`` raises
# ValueError on a decimal string longer than ``sys.get_int_max_str_digits()``
# (4300 by default), and this parse runs inside the WebSocket read loop, where
# raising drops the socket. Ids are one or two digits; nine is already far past
# any hub and nowhere near the limit.
_MAX_ID_KEY_DIGITS = 9


def deep_merge(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Return ``base`` with ``delta`` layered on top, recursing into nested dicts.

    Keys absent from ``delta`` keep their ``base`` value, so a partial frame like
    ``{"alarmHub": {"input": {"6": {"status": "alarm"}}}}`` updates one zone's
    status while its name, its type and every other zone survive untouched.
    Lists and scalars replace wholesale. Keys absent from ``base`` are stored
    by reference from ``delta``, so the caller's payload is aliased into the
    result: safe only because nothing here mutates a parsed dict. An in-place
    merge would corrupt every snapshot sharing that sub-tree. Neither argument is mutated.

    A ``null`` value deletes its key rather than being stored. Absence already
    means "unchanged", so null is the only way a delta can express removal, and
    storing the None would leave a hole for every later delta to merge into: a
    partial update would find a non-dict there, take the replace-wholesale
    branch and rebuild the entry from the delta alone. Deleting a key that is
    not present is a no-op.
    """
    merged = dict(base)
    for key, value in delta.items():
        if value is None:
            merged.pop(key, None)
            continue
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _entries_stay_mappings(
    base_section: dict[str, Any], merged_section: dict[str, Any]
) -> bool:
    """Whether every zone/output the merge left behind is still a mapping.

    Same damage as a section that stopped being a mapping, one level deeper: a
    zone that becomes ``"junk"`` parses as absent now and, because it is stored,
    makes the next partial delta for it take ``deep_merge``'s replace-wholesale
    branch -- so the zone comes back built from that delta alone, with no name,
    no ``inputType`` and no ``enable`` until a poll reconciles it.

    An entry that was already junk in ``base`` is left alone: a REST snapshot is
    stored as it arrived, and treating junk that is already there as a reason to
    reject frames would kill the push path for as long as it sits in the cache.
    A null entry never reaches here -- ``deep_merge`` deletes the key outright.
    """
    return all(
        isinstance(value, dict) or not isinstance(base_section.get(key, {}), dict)
        for key, value in merged_section.items()
    )


def keeps_hub_shape(base: dict[str, Any], merged: dict[str, Any]) -> bool:
    """Whether ``merged`` still models every section ``base`` did.

    A section that was a mapping and no longer is means the merge destroyed
    state rather than refining it: ``{"alarmHub": null}`` drops every zone, and
    ``{"alarmHub": {"input": []}}`` drops them and makes every later dict-shaped
    delta take the replace-wholesale branch. Since the merged payload becomes
    the next delta's base, adopting either would turn one bad frame into
    permanent damage.

    Removal is not destruction, though: ``battery`` and ``cover`` are optional on
    the model, so a delta that nulls one is expressing a hub that no longer has
    it, and rejecting that would keep reporting a battery the console stopped
    describing.
    """
    base_hub = base.get("alarmHub")
    if not isinstance(base_hub, dict):
        return True
    merged_hub = merged.get("alarmHub")
    if not isinstance(merged_hub, dict):
        return False
    for key in _REQUIRED_HUB_SECTIONS:
        base_section = base_hub.get(key)
        if not isinstance(base_section, dict):
            continue
        merged_section = merged_hub.get(key)
        if not isinstance(merged_section, dict):
            return False
        if not _entries_stay_mappings(base_section, merged_section):
            return False
    # ``get(key, {})`` for the same reason ``_entries_stay_mappings`` uses it: a
    # section the base never had defaults to a mapping, so one arriving as junk
    # is rejected rather than adopted. Defaulting the other way would skip the
    # guard exactly where nothing has vetted the value yet -- a hub with no
    # battery would store ``"battery": "junk"``, and the next partial battery
    # frame would take deep_merge's replace-wholesale branch. Only a section
    # already non-mapping in the base is left alone, as entries are.
    return all(
        isinstance(merged_hub[key], dict)
        for key in _REMOVABLE_HUB_SECTIONS
        if isinstance(base_hub.get(key, {}), dict) and key in merged_hub
    )


def _int_keyed(raw: Any, parse: Callable[[int, dict[str, Any]], Any]) -> dict[int, Any]:
    """Map a ``{"1": {...}}`` wire dict to ``{1: parse(1, {...})}``; skip junk.

    ``isdecimal`` rather than ``isdigit``: the latter is true of superscripts
    like ``"²"``, which ``int()`` then refuses. The length bound closes the other
    way ``int()`` can refuse a string of decimal digits (see
    ``_MAX_ID_KEY_DIGITS``), so no key shape gets past both checks and raises.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[int, Any] = {}
    for key, value in raw.items():
        if (
            isinstance(key, str)
            and len(key) <= _MAX_ID_KEY_DIGITS
            and key.isdecimal()
            and isinstance(value, dict)
        ):
            out[int(key)] = parse(int(key), value)
    return out


@dataclass(frozen=True)
class InputZone:
    zone_id: int
    enable: str | None
    type: str | None
    status: str | None
    input_type: str | None
    name: str | None
    last_triggered_at: int | None
    camera_id: str | None

    @classmethod
    def from_json(cls, zone_id: int, data: dict[str, Any]) -> InputZone:
        return cls(
            zone_id=zone_id,
            enable=data.get("enable"),
            type=data.get("type"),
            status=data.get("status"),
            input_type=data.get("inputType"),
            name=data.get("name"),
            last_triggered_at=data.get("lastTriggeredAt"),
            camera_id=data.get("cameraId"),
        )


@dataclass(frozen=True)
class OutputChannel:
    output_id: int
    active: str | None
    enable: str | None
    status: str | None
    name: str | None
    delay: int | None
    duration: int | None

    @classmethod
    def from_json(cls, output_id: int, data: dict[str, Any]) -> OutputChannel:
        return cls(
            output_id=output_id,
            active=data.get("active"),
            enable=data.get("enable"),
            status=data.get("status"),
            name=data.get("name"),
            delay=data.get("delay"),
            duration=data.get("duration"),
        )


@dataclass(frozen=True)
class Battery:
    connection: str | None
    charging: str | None
    voltage: float | None
    battery_status: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Battery:
        return cls(
            connection=data.get("connection"),
            charging=data.get("charging"),
            voltage=data.get("voltage"),
            battery_status=data.get("batteryStatus"),
        )


@dataclass(frozen=True)
class Cover:
    status: str | None
    distance: int | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Cover:
        return cls(status=data.get("status"), distance=data.get("distance"))


@dataclass(frozen=True)
class AlarmHub:
    id: str
    name: str | None
    mac: str
    state: str | None
    is_alarm_hub: bool
    alarm_hub_armed: str | None
    alarm_hub_battery: Battery | None
    alarm_hub_cover: Cover | None
    # ``frozen`` makes the dataclass generate __hash__ from the compared fields,
    # and a dict is unhashable, so the two zone maps are kept out of it: identity
    # and the scalar state carry the hash while equality still compares
    # everything. Hashing a subset of the compared fields stays consistent with
    # __eq__ -- equal hubs agree on that subset too.
    alarm_hub_inputs: dict[int, InputZone] = field(default_factory=dict, hash=False)
    alarm_hub_outputs: dict[int, OutputChannel] = field(
        default_factory=dict, hash=False
    )
    # The JSON this hub was parsed from, kept so a WebSocket frame can be merged
    # into it. Derived, not state: excluded from equality — and so from the hash
    # — and from repr.
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AlarmHub:
        """Parse one device object. Never raises, whatever the values turn out to be.

        A snapshot is parsed inside a refresh and a frame inside the WebSocket
        read loop, so anything that raises here fails the whole poll or drops
        the socket. Every section is type-checked instead of trusted, and one
        that is unusable reads as absent.
        """
        hub = data.get("alarmHub")
        if not isinstance(hub, dict):
            hub = {}
        battery = hub.get("battery")
        cover = hub.get("cover")
        return cls(
            id=data.get("id", ""),
            name=data.get("name"),
            mac=data.get("mac", ""),
            state=data.get("state"),
            is_alarm_hub=bool(data.get("isAlarmHub", False)),
            alarm_hub_armed=hub.get("armed"),
            alarm_hub_battery=Battery.from_json(battery)
            if isinstance(battery, dict)
            else None,
            alarm_hub_cover=Cover.from_json(cover) if isinstance(cover, dict) else None,
            alarm_hub_inputs=_int_keyed(hub.get("input"), InputZone.from_json),
            alarm_hub_outputs=_int_keyed(hub.get("output"), OutputChannel.from_json),
            raw=data,
        )

    def with_delta(self, item: dict[str, Any]) -> AlarmHub:
        """Return a new hub with a WebSocket ``item`` layered over this one.

        ``item`` is normally a partial delta, but a whole device object merges
        just as correctly -- every key it carries wins. What it cannot do is
        remove a zone by omitting it: absence means "unchanged", so only an
        explicit null deletes (see ``deep_merge``), and a zone the console
        dropped silently lives on here until the REST poll reconciles it.

        Never raises, and never adopts a merge that fails ``keeps_hub_shape``:
        the merged payload becomes this hub's new ``raw``, so keeping a
        malformed one would carry it into every future merge instead of letting
        it pass as a one-frame glitch. A rejected merge returns ``self``.
        """
        if not isinstance(item, dict):
            return self
        merged = deep_merge(self.raw, item)
        if not keeps_hub_shape(self.raw, merged):
            _LOGGER.debug("Discarding alarm-hub frame that unmakes the hub: %s", item)
            return self
        return AlarmHub.from_json(merged)
