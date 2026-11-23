"""What each arm profile looked like the last time this integration armed it.

The hub says whether it is armed, never which profile (see
``logic.profile_fingerprint``), so the only way to tell Away from Night later --
including when someone armed it from a fob or the Protect app -- is to have
watched each one being armed from here and remembered its fingerprint. That
memory has to outlive a restart, or every reboot turns an armed Night system
back into "armed, profile unknown" until someone arms it again.

Kept in Home Assistant's own ``Store`` rather than in ``entry.options``: it is
learned state, not something the user chose, and writing it into the options
would fire the update listener that reloads the entry.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1

# Learned state is cheap to rebuild and each write is a disk write, so a burst of
# updates is folded into one.
SAVE_DELAY = 10.0


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, dict[str, str]]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.profiles")


async def async_remove_profiles(hass: HomeAssistant, entry_id: str) -> None:
    """Delete an entry's remembered profiles along with the entry."""
    await _store(hass, entry_id).async_remove()


class ProfileBook:
    """Fingerprint -> profile, per hub device, persisted."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = _store(hass, entry_id)
        self._data: dict[str, dict[str, str]] = {}

    async def async_load(self) -> None:
        stored = await self._store.async_load()
        # Whatever is on disk is only trusted if it has the shape we wrote: a
        # hand-edited or truncated file must cost the memory, not the setup.
        if isinstance(stored, dict):
            self._data = {
                device: {
                    fingerprint: profile
                    for fingerprint, profile in profiles.items()
                    if isinstance(fingerprint, str) and isinstance(profile, str)
                }
                for device, profiles in stored.items()
                if isinstance(device, str) and isinstance(profiles, dict)
            }

    def for_device(self, device_id: str) -> dict[str, str]:
        """The live mapping for one hub. Read-only to callers; use ``learn``."""
        return self._data.setdefault(device_id, {})

    def learn(self, device_id: str, fingerprint: str, profile: str) -> None:
        profiles = self.for_device(device_id)
        if profiles.get(fingerprint) == profile:
            return
        profiles[fingerprint] = profile
        self._store.async_delay_save(lambda: self._data, SAVE_DELAY)
