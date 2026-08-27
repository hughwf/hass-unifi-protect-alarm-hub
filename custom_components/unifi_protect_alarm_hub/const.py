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

# The WebSocket is the primary *trigger* and REST the primary *source*: on the
# hardware this has been measured against, every alarm-hub frame is a bare
# timestamp, so the socket says when to read and REST says what changed. This
# poll is neither -- it is the reconciling fallback (and the initial load),
# covering a frame that never arrived and a socket that is down, so it can be
# infrequent.
SCAN_INTERVAL = timedelta(minutes=5)

MANUFACTURER = "Ubiquiti"
MODEL = "Alarm Hub"
