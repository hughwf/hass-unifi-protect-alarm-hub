import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_protect_alarm_hub.api import AlarmHubAuthError
from custom_components.unifi_protect_alarm_hub.const import DOMAIN
from custom_components.unifi_protect_alarm_hub.coordinator import AlarmHubCoordinator
from custom_components.unifi_protect_alarm_hub.models import AlarmHub

MAC = "AABBCCDDEEFF"
MAC_ID = "aabbccddeeff"
OTHER_MAC = "112233445566"
# What the flow wrote before entries were keyed on the hub's mac.
LEGACY_UNIQUE_ID = "h:443"


def _entry(unique_id: str | None = None, host: str = "h") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        data={
            CONF_HOST: host,
            CONF_PORT: 443,
            CONF_API_KEY: "k",
            CONF_VERIFY_SSL: False,
        },
    )


def _hub(mac: str = MAC, hub_id: str = "hub-1") -> AlarmHub:
    return AlarmHub.from_json(
        {"id": hub_id, "mac": mac, "state": "CONNECTED", "isAlarmHub": True}
    )


async def _stay_connected(_cb, _on_connected=None):
    await asyncio.Event().wait()  # "connected" until cancelled


@contextmanager
def _console(hubs=(), subscribe=_stay_connected, poll_error=None):
    """Patch the client setup builds, so no socket is ever opened."""
    with patch("custom_components.unifi_protect_alarm_hub.AlarmHubApiClient") as cls:
        cls.return_value.async_get_alarm_hubs = AsyncMock(
            return_value=list(hubs), side_effect=poll_error
        )
        cls.return_value.async_subscribe_devices = AsyncMock(side_effect=subscribe)
        yield cls


def _reauth_flows(hass) -> list[str]:
    return [
        flow["context"]["entry_id"]
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == "reauth"
    ]


async def test_setup_and_unload(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    async def _block(_cb, _on_connected=None):
        await asyncio.Event().wait()  # stay "connected" until cancelled

    with patch("custom_components.unifi_protect_alarm_hub.AlarmHubApiClient") as cls:
        cls.return_value.async_get_alarm_hubs = AsyncMock(return_value=[])
        # WS subscribe is mocked so setup never opens a real socket.
        cls.return_value.async_subscribe_devices = AsyncMock(side_effect=_block)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data is not None
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_setup_starts_ws_listener(hass):
    """The WS background task runs and calls async_subscribe_devices."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.unifi_protect_alarm_hub.AlarmHubApiClient") as cls:
        cls.return_value.async_get_alarm_hubs = AsyncMock(return_value=[])
        subscribed = asyncio.Event()

        async def fake_subscribe(_cb, _on_connected=None):
            subscribed.set()
            await asyncio.Event().wait()  # stay "connected" until cancelled

        cls.return_value.async_subscribe_devices = AsyncMock(side_effect=fake_subscribe)

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        async with asyncio.timeout(5):
            await subscribed.wait()
        assert cls.return_value.async_subscribe_devices.await_count >= 1

        # Unload must cancel the WS task cleanly (no error raised).
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_setup_takes_a_single_snapshot(hass):
    """Setup polls, then the socket connects: that connect must not poll again.

    A resync is for a RE-connect, where frames were missed while the socket was
    down. On the first one the snapshot setup just took is still current.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.unifi_protect_alarm_hub.AlarmHubApiClient") as cls:
        cls.return_value.async_get_alarm_hubs = AsyncMock(return_value=[])

        async def fake_subscribe(_cb, on_connected=None):
            if on_connected is not None:
                on_connected()
            await asyncio.Event().wait()  # stay "connected" until cancelled

        cls.return_value.async_subscribe_devices = AsyncMock(side_effect=fake_subscribe)

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert cls.return_value.async_get_alarm_hubs.await_count == 1

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_setup_succeeds_even_if_ws_errors(hass):
    """A WS that keeps failing must not break setup/unload."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.unifi_protect_alarm_hub.AlarmHubApiClient") as cls:
        cls.return_value.async_get_alarm_hubs = AsyncMock(return_value=[])
        cls.return_value.async_subscribe_devices = AsyncMock(
            side_effect=RuntimeError("ws down")
        )
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data is not None
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_setup_migrates_a_pre_mac_unique_id(hass):
    """An entry created by the old flow must end up keyed on the hub's mac.

    Leaving live installs on ``host:port`` would keep them addable a second time
    under another address -- the duplicate the new key exists to refuse -- and
    would make the flow's dedup disagree with what those entries actually hold.
    """
    entry = _entry(unique_id=LEGACY_UNIQUE_ID)
    entry.add_to_hass(hass)
    with _console(hubs=[_hub()]):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.unique_id == MAC_ID
        assert hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, MAC_ID) is (
            entry
        )
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_setup_does_not_re_key_an_already_migrated_entry(hass):
    """Only the exact legacy string is ever overwritten, so this runs once.

    A hub swapped for another one -- a new mac on the same console -- must not
    move an entry that already has an identity: its entities belong to what it
    was set up with, and an id that changes under them is not an id.
    """
    entry = _entry(unique_id=LEGACY_UNIQUE_ID)
    entry.add_to_hass(hass)
    with _console(hubs=[_hub()]):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.unique_id == MAC_ID
    with _console(hubs=[_hub(mac=OTHER_MAC)]):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.unique_id == MAC_ID

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_setup_leaves_a_duplicate_entry_on_its_old_key(hass, caplog):
    """Someone who already added one console twice must not end up with a collision.

    Re-keying both onto the same mac would leave HA's index answering for only
    one of them; the old key is wrong but at least it is theirs.

    The log line has to say *which* two. Every entry this integration creates is
    titled "UniFi Protect Alarm Hub", so naming both by title told the user to
    remove one of two identical names and gave them no way to tell which was
    which -- the addresses are what they recognise a console by, and the entry
    id is what the frontend puts in the URL of the entry's own page.
    """
    holder = _entry(unique_id=MAC_ID, host="protect.lan")
    holder.add_to_hass(hass)
    entry = _entry(unique_id="192.168.0.9:443", host="192.168.0.9")
    entry.add_to_hass(hass)
    assert holder.title == entry.title
    with _console(hubs=[_hub()]):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.unique_id == "192.168.0.9:443"
        assert hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, MAC_ID) is (
            holder
        )
        warning = next(
            record.getMessage()
            for record in caplog.records
            if "Not re-keying" in record.getMessage()
        )
        assert "192.168.0.9" in warning and entry.entry_id in warning
        assert "protect.lan" in warning and holder.entry_id in warning

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    "mac",
    [
        pytest.param("", id="not populated yet"),
        pytest.param("000000000000", id="the mid-adoption placeholder"),
        pytest.param("ff:ff:ff:ff:ff:ff", id="broadcast"),
    ],
)
async def test_setup_keeps_the_old_key_until_a_mac_is_readable(hass, mac):
    """A hub answering before its mac is populated defers the migration.

    Keying on any of these would hand every such console one identity, so the
    entry stays as it was and the next start tries again. The placeholder is the
    one that got through a length check: it is what a console reports for a hub
    it has adopted but not yet read, so a live install that migrated onto it
    would be stuck there for good -- the migration only ever overwrites an
    address-shaped id -- while every other console mid-adoption collided with it.
    """
    entry = _entry(unique_id=LEGACY_UNIQUE_ID)
    entry.add_to_hass(hass)
    with _console(hubs=[_hub(mac=mac)]):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.unique_id == LEGACY_UNIQUE_ID

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_setup_can_still_migrate_an_entry_whose_address_has_changed(hass):
    """The migration guard must recognise the id it wrote, not rebuild it.

    Rebuilt from current data, it asks "is this id ``{host}:{port}`` as they
    stand *now*" -- and reconfigure moves the host while reauth renormalises it.
    Either one leaves an entry whose id no longer equals the string being
    derived, so the guard stops matching and the entry can never migrate again:
    still keyed on an address, still addable a second time under another one,
    permanently, on every start for the life of the install.
    """
    entry = _entry(unique_id="192.168.0.9:443")
    entry.add_to_hass(hass)
    assert entry.data[CONF_HOST] == "h"
    with _console(hubs=[_hub()]):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.unique_id == MAC_ID

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_a_poll_that_loses_the_key_asks_for_a_new_one(hass):
    """A revoked key after setup must reach the user, not just the log.

    The coordinator is built here rather than through ``async_setup`` on
    purpose: HA sets the ``current_entry`` ContextVar only while it is running
    setup, and ``DataUpdateCoordinator._async_refresh`` escalates with ``if
    self.config_entry: async_start_reauth(...)``. A coordinator that leans on
    that ContextVar has no entry anywhere else, so the escalation silently does
    nothing -- and no test that built one could see it.
    """
    entry = _entry(unique_id=MAC_ID)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_alarm_hubs = AsyncMock(side_effect=AlarmHubAuthError("revoked"))

    await AlarmHubCoordinator(hass, entry, client).async_refresh()
    await hass.async_block_till_done()

    assert _reauth_flows(hass) == [entry.entry_id]


async def test_a_websocket_that_loses_the_key_asks_for_a_new_one(hass):
    """The socket sees a revoked key first; waiting for the poll wastes minutes.

    Reconnecting cannot fix a key the console has rejected, and HA drops the
    request while a flow for this entry is already open, so the retry loop
    cannot stack them up.
    """
    entry = _entry(unique_id=MAC_ID)
    entry.add_to_hass(hass)
    with _console(subscribe=AlarmHubAuthError("revoked")):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert _reauth_flows(hass) == [entry.entry_id]

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_setup_that_loses_the_key_asks_for_a_new_one(hass):
    """The reproduction: an entry in SETUP_ERROR used to leave no way back.

    HA turns the ConfigEntryAuthFailed the first refresh raises into
    ``async_start_reauth``. With no reauth step to start, that raised UnknownStep
    out of the task it runs in -- which also meant the repair notification HA
    creates once the flow is open was never reached, so the user was left with a
    broken entry, no flow, no issue, and a traceback per failed refresh.
    """
    entry = _entry(unique_id=MAC_ID)
    entry.add_to_hass(hass)
    with _console(poll_error=AlarmHubAuthError("revoked")):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert _reauth_flows(hass) == [entry.entry_id]
    assert ir.async_get(hass).async_get_issue(
        HOMEASSISTANT_DOMAIN, f"config_entry_reauth_{DOMAIN}_{entry.entry_id}"
    )
