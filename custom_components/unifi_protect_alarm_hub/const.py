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

# REST polling is the primary update mechanism for v0.1.
SCAN_INTERVAL = timedelta(seconds=15)

MANUFACTURER = "Ubiquiti"
MODEL = "Alarm Hub"
