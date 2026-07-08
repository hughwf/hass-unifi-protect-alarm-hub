import asyncio
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_protect_alarm_hub.const import DOMAIN


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "h",
            CONF_PORT: 443,
            CONF_API_KEY: "k",
            CONF_VERIFY_SSL: False,
        },
    )


async def test_setup_and_unload(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    async def _block(_cb):
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

        async def fake_subscribe(_cb):
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
