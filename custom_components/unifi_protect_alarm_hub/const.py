"""Constants for the UniFi Protect Alarm Hub integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "unifi_protect_alarm_hub"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SWITCH,
]

DEFAULT_PORT = 443
DEFAULT_VERIFY_SSL = False

# WebSocket push is the primary update path (v0.2); REST polling is a fallback
# safety net and the initial load, so it can be infrequent.
SCAN_INTERVAL = timedelta(minutes=5)

MANUFACTURER = "Ubiquiti"
MODEL = "Alarm Hub"
