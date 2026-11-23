"""Constants for the UniFi Protect Alarm Hub integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "unifi_protect_alarm_hub"

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Options, not data: they are optional, they are edited long after setup, and
# changing one must not go through the credential/console-identity checks that
# ``reconfigure`` runs. Each is the Trigger ID of an Alarm Manager alarm whose
# Trigger is Webhook and whose Action is Arm (with a profile) or Disarm -- the
# only way to arm or disarm while the Global Alarm Manager is in force, because
# the Integration API refuses to set the arm profile directly in that mode.
CONF_ARM_AWAY_TRIGGER = "arm_away_trigger_id"
CONF_ARM_NIGHT_TRIGGER = "arm_night_trigger_id"
CONF_DISARM_TRIGGER = "disarm_trigger_id"

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
