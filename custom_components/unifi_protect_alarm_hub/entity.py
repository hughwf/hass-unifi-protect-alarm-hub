"""Shared entity base for the UniFi Protect Alarm Hub, and the platform seam.

Availability here answers one question -- can this entity still vouch for what
it is about to report? -- and three separate failures answer it no: the hub has
dropped out of the snapshot, the console says the hub itself is offline, or
nothing has confirmed the state recently enough to keep standing behind it.
All three end in *unavailable* rather than *unknown*, deliberately. "Unknown"
says the device is present and its value is indeterminate; none of these can
claim the device is present. Unavailable is Home Assistant's word for "we
cannot see this", it leaves a gap in history instead of a value, and it makes
``state == 'on'`` and ``state == 'off'`` conditions *both* false, which is what
an alarm automation needs from a sensor nobody can read.

The reconcile pass the three platforms share lives here too, for the same
reason the base entity does: which device an entity belongs to, the unique_id
built from that identity, and the lifecycle window in which entities may be
created at all are one question. Split across three platform files, device
identity was hardened while entity identity was not.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import logic
from .const import DOMAIN, MANUFACTURER, MODEL, SCAN_INTERVAL
from .coordinator import AlarmHubCoordinator
from .models import AlarmHub

# How long a delivered update stands in for a successful poll: the poll interval
# itself. Any longer and a push would hold entities available past the point a
# working poll would already have refreshed them -- the stale-but-available state
# the coordinator stopped writing ``last_update_success`` to avoid. Any shorter
# and an ordinary REST hiccup would blink every entity out between pushes.
PUSH_FRESHNESS_WINDOW = SCAN_INTERVAL.total_seconds()

# The config-entry states in which a platform may still be handed new entities.
#
# Home Assistant flips the entry to UNLOAD_IN_PROGRESS *before* it calls
# ``async_unload_entry``, and so before ``async_unload_platforms`` resets the
# platforms (``config_entries.ConfigEntry.async_unload``), while the reconcile
# listener stays subscribed right through it: ``async_on_unload`` callbacks run
# only once unloading has finished. A frame landing in that window used to add
# an entity behind ``EntityPlatform.async_reset``'s snapshot of what to remove,
# so the entity outlived the entry -- still in the state machine, still holding
# a listener on a shut-down coordinator, publishing whatever it last read for
# good -- and it kept the unique_id its own replacement needed, which Home
# Assistant then refuses on the next setup with "does not generate unique IDs".
#
# Written as the states that may add rather than as the one state that may not,
# so that NOT_LOADED -- an entry already unloaded, poked by whatever is left
# holding the old coordinator -- is inert for the same reason and not by luck.
ADDING_ENTRY_STATES = frozenset(
    {ConfigEntryState.SETUP_IN_PROGRESS, ConfigEntryState.LOADED}
)


@callback
def async_hub_device_ids(hass: HomeAssistant, entry: ConfigEntry) -> logic.HubDeviceIds:
    """The device identities this entry has already assigned, from the registry.

    Seeding matters across a restart. The identity a hub *would* be given can
    differ from the one its entities were registered under -- a hub adopted
    while the console had not populated its mac keeps id-based unique_ids for
    good -- and the device registry is the record of what was decided. Rebuilt
    per platform rather than shared, because all three read the same registry
    and the same snapshot and so reach the same answer; sharing it would need
    entry-wide mutable state for no gain.
    """
    return logic.HubDeviceIds(
        identifier
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        )
        for domain, identifier in device.identifiers
        if domain == DOMAIN
    )


@callback
def async_reconcile_on_update(
    entry: ConfigEntry,
    coordinator: AlarmHubCoordinator,
    devices: logic.HubDeviceIds,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[str, AlarmHub], Iterable[Entity]],
) -> None:
    """Create entities for what exists now, and for whatever appears later.

    Setup is not a census. The config flow deliberately lets someone finish with
    no hub adopted yet, a zone gets wired in long after the integration was
    installed, and ``battery``/``cover`` only appear once the console reports
    them -- so a single pass over ``coordinator.data`` at setup leaves an entry
    that can load empty and stay that way until a manual reload. Reconciling on
    every coordinator update instead is the pattern Home Assistant's own
    integrations use: a listener that adds what is missing, plus one immediate
    call for what is already there.

    Deduplicated on unique_id -- the identity Home Assistant itself refuses
    duplicates on -- rather than on the hub id, because the hub id is only the
    coordinator's dict key: re-adopting a hub changes it while the device
    identity every unique_id is built from stays put. Keyed on the id, a
    re-adoption would ask for a second full set of entities for the same
    hardware. Candidates are built and then filtered, so the unique_id compared
    here is the one the entity would actually register with and the two cannot
    drift apart; constructing an entity touches nothing outside itself, so the
    ones that lose the filter cost a few short-lived objects and no more.

    Nothing is ever *removed*, and that is deliberate. What we hold is one REST
    poll's opinion, which a mid-adoption snapshot or a frame that briefly
    disowns a hub can shrink; deleting the entity would take its registry entry
    with it, and with that the user's customisations, its area, and the
    entity_id every alarm automation is written against -- silently, leaving the
    automation to simply never fire again. A zone that goes away reads
    ``unavailable`` instead, which is loud, reversible, and something an
    automation can test for. Deletion belongs to the user removing the device,
    not to a poll that came back short.
    """
    added: set[str | None] = set()

    @callback
    def _reconcile() -> None:
        if entry.state not in ADDING_ENTRY_STATES:
            # Teardown has begun, and an entity added now outlives the entry.
            # See ADDING_ENTRY_STATES.
            return
        # Before anything is built, and over the whole snapshot at once: a mac
        # that has just appeared is learned as another key for a device we
        # already named rather than read as a device we have never seen, and a
        # mac two hubs both report is recognised as contested while both of them
        # are still in front of us. Neither is visible from one hub alone, and
        # ``build`` does not see every hub -- sensor.py returns nothing for a hub
        # with no battery, so that platform would never learn such a hub at all.
        devices.observe(coordinator.data)
        candidates = [
            entity
            for hub_id, hub in coordinator.data.items()
            for entity in build(hub_id, hub)
        ]
        new = [entity for entity in candidates if entity.unique_id not in added]
        added.update(entity.unique_id for entity in new)
        async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_reconcile))
    _reconcile()


class AlarmHubBaseEntity(CoordinatorEntity[AlarmHubCoordinator]):
    """Base entity bound to one alarm hub device."""

    _attr_has_entity_name = True

    # Set on entities that exist to report the hub's own reachability. Taking
    # them down with it would erase the one indicator that could say why
    # everything else went unavailable.
    _survives_hub_offline: bool = False

    def __init__(
        self,
        coordinator: AlarmHubCoordinator,
        devices: logic.HubDeviceIds,
        hub_id: str,
    ) -> None:
        super().__init__(coordinator)
        # Only ever the id this entity was *built* for. Everything afterwards
        # resolves through ``_device_id`` instead -- see ``hub`` and ``hub_id``.
        self._built_for = hub_id
        self._devices = devices
        hub = coordinator.data.get(hub_id)
        self._device_id = devices.resolve(hub, hub_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=logic.hub_device_name(hub, self._device_id),
        )
        # The hub object this entity has already counted, and when it arrived.
        #
        # Per hub, not per snapshot: ``_publish`` rebuilds the whole dict for
        # any hub's frame, so timing the dict let one chatty hub vouch for every
        # silent one on the same console. A poll rebuilds every hub object and
        # so delivers for all of them, which is right -- REST answered for all
        # of them -- while a frame replaces exactly the hub it was about.
        #
        # Seeded with the object we were built from and the moment we were
        # built, because that object *is* a delivery: an entity a push created
        # during a REST outage otherwise read unavailable while holding the
        # status the very frame that created it had just carried.
        self._delivered: AlarmHub | None = hub
        self._delivered_at: float = coordinator.hass.loop.time()
        self._expiry_unsub: CALLBACK_TYPE | None = None

    @property
    def hub(self) -> AlarmHub | None:
        """The live hub for this device, under whatever id it carries now.

        See ``logic.HubDeviceIds.find``: the coordinator's key changes when a
        hub is re-adopted, and an entity that resolved by it died for good.
        """
        found = self._devices.find(self.coordinator.data, self._device_id)
        return found[1] if found is not None else None

    @property
    def hub_id(self) -> str:
        """The id the API is addressed by for this device, right now.

        A command built from the id captured at construction would be sent to a
        device the console no longer has. Falls back to that captured id only
        when the device is absent from the snapshot altogether, where there is
        nothing better and the entity is unavailable anyway.
        """
        found = self._devices.find(self.coordinator.data, self._device_id)
        return found[0] if found is not None else self._built_for

    @property
    def available(self) -> bool:
        """See the module docstring for why each of these ends in unavailable.

        ``super().available`` is exactly ``coordinator.last_update_success``, so
        on its own it reads unavailable through any REST outage even while the
        WebSocket is delivering fresh state -- the cost stage 1 accepted when it
        stopped writing that flag from the push path. ``push_is_fresh`` is what
        pays it back: a recent delivery *for this hub* is an entity-level
        statement that the socket is working, not a claim that a poll succeeded.
        """
        hub = self.hub
        if hub is None:
            return False
        if not self._survives_hub_offline and logic.hub_is_connected(hub) is not True:
            return False
        return super().available or logic.push_is_fresh(
            self._delivered_at,
            self.coordinator.hass.loop.time(),
            PUSH_FRESHNESS_WINDOW,
        )

    async def async_added_to_hass(self) -> None:
        """Arm the expiry for an entity born while polling was already failing.

        ``_rearm_freshness_expiry`` otherwise runs only from a coordinator
        update, and this entity's delivery -- the frame that created it -- came
        before it had a ``hass`` to schedule anything on. Without this, a zone
        wired in during a console outage would publish that first status for as
        long as the socket then stayed quiet, with nothing left to retire it.
        """
        await super().async_added_to_hass()
        self._rearm_freshness_expiry()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Time this hub's delivery, re-arm its expiry, then write the state out.

        The identity check is what separates a delivery from a bare
        notification, and it is made against *this hub's* object rather than the
        snapshot dict. Every path that publishes -- pushed delta or completed
        poll -- rebuilds the hubs it touched, while a poll that *failed* leaves
        the old objects in place and still notifies listeners once, on the edge
        into failure. Counting that edge as a delivery would keep every entity
        available for a further window on the strength of a REST failure;
        counting another hub's frame as one would do the same on the strength of
        a device this entity knows nothing about.
        """
        hub = self.hub
        if hub is not None and hub is not self._delivered:
            self._delivered = hub
            self._delivered_at = self.coordinator.hass.loop.time()
        self._rearm_freshness_expiry()
        super()._handle_coordinator_update()

    @callback
    def _rearm_freshness_expiry(self) -> None:
        """Schedule the re-evaluation that lets a delivery stop counting as fresh.

        Nothing else would do it. A poll that fails while the previous one had
        already failed does not notify listeners at all (see
        ``DataUpdateCoordinator._async_refresh``), so an entity held available by
        a push has no cue to stop -- and a socket that goes quiet after pushing
        an "alarm" would strand that alarm on screen indefinitely.

        Only armed while polling is failing, because only then does availability
        consult the clock: while REST works the timer would fire on every quiet
        window for nothing, and the poll that breaks is itself the notification
        that arms this.
        """
        self._cancel_freshness_expiry()
        if self.coordinator.last_update_success:
            return
        lapsed = self.coordinator.hass.loop.time() - self._delivered_at
        self._expiry_unsub = async_call_later(
            self.hass, max(PUSH_FRESHNESS_WINDOW - lapsed, 0), self._freshness_lapsed
        )

    @callback
    def _cancel_freshness_expiry(self) -> None:
        if self._expiry_unsub is not None:
            self._expiry_unsub()
            self._expiry_unsub = None

    @callback
    def _freshness_lapsed(self, _now: datetime) -> None:
        """Nothing has confirmed this state for a window; say so -- if it is so.

        A timer firing is not proof its deadline passed: asyncio runs a handle
        up to ``_clock_resolution`` early by design, and anything that moves the
        clock in coarser steps lands it earlier still. Taking the callback at
        its word was the failure this whole mechanism exists to prevent, in
        miniature -- it dropped ``_expiry_unsub`` and wrote a state that was
        still fresh, leaving the entity with nothing scheduled to revisit it.
        Nothing else would: a poll that fails behind an already-failed poll
        notifies no listeners at all. So the entity kept publishing that reading
        as live for as long as the socket stayed quiet, which is to say for good.

        Re-arming only while the delivery is still fresh is what keeps that from
        becoming a busy loop: once the window really has passed there is nothing
        left to wait for, and the next delivery arms the timer again.
        """
        self._expiry_unsub = None
        if logic.push_is_fresh(
            self._delivered_at,
            self.coordinator.hass.loop.time(),
            PUSH_FRESHNESS_WINDOW,
        ):
            self._rearm_freshness_expiry()
            return
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Drop the timer with the entity, or a reload leaves one behind per entity."""
        self._cancel_freshness_expiry()
        await super().async_will_remove_from_hass()
