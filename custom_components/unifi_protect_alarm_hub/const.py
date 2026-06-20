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

# Safety-net resync; real-time updates arrive via the devices WebSocket.
SCAN_INTERVAL = timedelta(minutes=5)

MANUFACTURER = "Ubiquiti"
MODEL = "Alarm Hub"
