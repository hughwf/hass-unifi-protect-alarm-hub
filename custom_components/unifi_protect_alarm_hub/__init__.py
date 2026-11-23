"""The UniFi Protect Alarm Hub integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AlarmHubApiClient
from .config_flow import async_migrate_unique_id
from .const import PLATFORMS
from .coordinator import AlarmHubCoordinator
from .entity import async_migrate_hub_identity

type AlarmHubConfigEntry = ConfigEntry[AlarmHubCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AlarmHubConfigEntry) -> bool:
    """Set up UniFi Protect Alarm Hub from a config entry."""
    session = async_get_clientsession(hass, verify_ssl=entry.data[CONF_VERIFY_SSL])
    client = AlarmHubApiClient(
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_API_KEY],
        session,
    )
    coordinator = AlarmHubCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    # Here rather than in ``async_migrate_entry`` because the mac an entry is
    # keyed on only exists once the console has answered, and the first refresh
    # is the answer -- no second request, and a console that was unreachable is
    # retried by setup instead of failing a migration HA will not run again.
    async_migrate_unique_id(hass, entry, coordinator.data.values())
    # And before the platforms read the registry, for the same reason: they seed
    # hub identity from what is on disk, so a row re-filed afterwards would be
    # one the entities of this run had already been built against.
    async_migrate_hub_identity(hass, entry, coordinator.data)

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start real-time WebSocket push (best-effort; REST polling is the fallback).
    coordinator.start_ws()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AlarmHubConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded
