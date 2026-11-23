"""Tests for the alarm panel: arming through Alarm Manager webhooks.

The Alarm Hub is stuck in Global Alarm Manager mode, where the API cannot set the
arm profile, so the panel fires user-configured webhooks and reads the result
back from the hub. The console here is the fake from ``platform_common`` with
each webhook given an *effect* -- what the real console would do to the hub --
so a test states "arming Night lights zone 4" instead of poking state by hand.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntityFeature as Feature,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError

from custom_components.unifi_protect_alarm_hub.alarm_control_panel import LEARN_WINDOW
from custom_components.unifi_protect_alarm_hub.api import (
    AlarmHubAuthError,
    AlarmHubConnectionError,
)
from custom_components.unifi_protect_alarm_hub.const import (
    CONF_ARM_AWAY_TRIGGER,
    CONF_ARM_NIGHT_TRIGGER,
    CONF_DISARM_TRIGGER,
    DOMAIN,
)
from custom_components.unifi_protect_alarm_hub.profiles import SAVE_DELAY
from platform_common import (  # noqa: F401  (unload_entries is an autouse fixture)
    FakeConsole,
    advance,
    entity_ids,
    hub_json,
    poll,
    restart,
    setup_integration,
    unload_entries,
)

PANEL = "alarm_control_panel.alarm_hub_kit"

ALL_TRIGGERS = {
    CONF_ARM_AWAY_TRIGGER: "away-id",
    CONF_ARM_NIGHT_TRIGGER: "night-id",
    CONF_DISARM_TRIGGER: "disarm-id",
}

# What the two profiles light up on the standard two-zone hub: Away watches the
# hallway motion sensor and the garage door, Night only the door.
AWAY_ZONES = {"4", "6"}
NIGHT_ZONES = {"6"}


def _arm(zones: set[str]):
    """The console's effect for a webhook that arms a profile lighting ``zones``."""

    def effect(payload: dict[str, Any]) -> None:
        hub = payload["alarmHub"]
        hub["armed"] = "on"
        for zone_id, zone in hub["input"].items():
            zone["triggerOnCurrentArmingProfile"] = "on" if zone_id in zones else "off"

    return effect


def _disarm(payload: dict[str, Any]) -> None:
    payload["alarmHub"]["armed"] = "off"


def _console(*, armed: str = "off") -> FakeConsole:
    payload = hub_json()
    payload["alarmHub"]["armed"] = armed
    console = FakeConsole(payload)
    console.webhook_effects = {
        "away-id": _arm(AWAY_ZONES),
        "night-id": _arm(NIGHT_ZONES),
        "disarm-id": _disarm,
    }
    return console


async def _call(hass, service: str) -> None:
    await hass.services.async_call(
        "alarm_control_panel", service, {"entity_id": PANEL}, blocking=True
    )
    await hass.async_block_till_done()


def _state(hass) -> str:
    return hass.states.get(PANEL).state


async def test_no_panel_until_a_webhook_is_configured(hass):
    """Without a Trigger ID the panel could do nothing, and would only duplicate
    the *Armed* binary sensor, so it is not created."""
    await setup_integration(hass, _console())
    assert entity_ids(hass, "alarm_control_panel") == []


async def test_a_configured_webhook_creates_the_panel(hass):
    await setup_integration(hass, _console(), ALL_TRIGGERS)
    assert entity_ids(hass, "alarm_control_panel") == [PANEL]


async def test_features_follow_which_webhooks_are_configured(hass):
    """Only actions this panel can actually perform are offered."""
    await setup_integration(hass, _console(), {CONF_ARM_NIGHT_TRIGGER: "night-id"})
    features = hass.states.get(PANEL).attributes["supported_features"]
    assert features == Feature.ARM_NIGHT


async def test_blank_options_are_treated_as_unconfigured(hass):
    """A cleared field must not count as a Trigger ID."""
    await setup_integration(hass, _console(), {CONF_ARM_AWAY_TRIGGER: "  "})
    assert entity_ids(hass, "alarm_control_panel") == []


async def test_disarmed_and_armed_states_come_from_the_hub(hass):
    console = _console(armed="off")
    entry = await setup_integration(hass, console, ALL_TRIGGERS)
    assert _state(hass) == "disarmed"

    console.payloads[0]["alarmHub"]["armed"] = "on"
    await poll(hass, entry)
    assert _state(hass) == "armed_away"  # profile never seen: the default


async def test_an_armed_field_the_hub_did_not_send_reads_unknown(hass):
    """Not "disarmed": nobody should be told the house is unarmed because the
    console left a field out."""
    console = _console()
    entry = await setup_integration(hass, console, ALL_TRIGGERS)
    del console.payloads[0]["alarmHub"]["armed"]
    await poll(hass, entry)
    assert _state(hass) == STATE_UNKNOWN


async def test_the_panel_goes_unavailable_with_the_hub(hass):
    console = _console()
    entry = await setup_integration(hass, console, ALL_TRIGGERS)
    console.payloads[0]["state"] = "DISCONNECTED"
    await poll(hass, entry)
    assert _state(hass) == STATE_UNAVAILABLE


async def test_arming_away_fires_its_webhook(hass):
    console = _console()
    await setup_integration(hass, console, ALL_TRIGGERS)
    await _call(hass, "alarm_arm_away")
    assert console.webhooks == ["away-id"]
    assert _state(hass) == "armed_away"


async def test_disarming_fires_its_webhook(hass, freezer):
    console = _console(armed="on")
    await setup_integration(hass, console, ALL_TRIGGERS)
    await _call(hass, "alarm_disarm")
    assert console.webhooks == ["disarm-id"]
    assert _state(hass) == "disarmed"


async def test_a_profile_armed_from_here_is_recognised_afterwards(hass, freezer):
    """The point of the fingerprint: once Night has been armed from Home
    Assistant, Night armed from a fob later still reads as Night."""
    console = _console()
    entry = await setup_integration(hass, console, ALL_TRIGGERS)

    await _call(hass, "alarm_arm_away")
    await _call_disarm_then(hass, freezer, "alarm_arm_night")
    assert _state(hass) == "armed_night"

    # Someone disarms at the keypad and re-arms Away there, then Night there.
    _disarm(console.payloads[0])
    await poll(hass, entry)
    _arm(AWAY_ZONES)(console.payloads[0])
    await poll(hass, entry)
    assert _state(hass) == "armed_away"
    _arm(NIGHT_ZONES)(console.payloads[0])
    await poll(hass, entry)
    assert _state(hass) == "armed_night"


async def _call_disarm_then(hass, freezer, service: str) -> None:
    """Disarm and arm again, past the refresh cooldown between the two."""
    await advance(hass, freezer, 11)
    await _call(hass, "alarm_disarm")
    await advance(hass, freezer, 11)
    await _call(hass, service)


async def test_switching_profile_while_armed_does_not_mislabel_the_old_one(
    hass, freezer
):
    """Arm Night *from Away*: the first read can still be Away's zones, because
    the hub has been told but has not switched yet. Filing that as Night would
    mislabel the profile that was working."""
    console = _console()
    entry = await setup_integration(hass, console, ALL_TRIGGERS)
    await _call(hass, "alarm_arm_away")  # learns Away = "4,6"
    assert _state(hass) == "armed_away"

    # The hub accepts the Night webhook but is slow to act on it.
    console.webhook_effects["night-id"] = lambda payload: None
    await advance(hass, freezer, 11)
    await _call(hass, "alarm_arm_night")
    assert _state(hass) == "armed_away"  # still Away's zones: not learned as Night

    # Now it switches; Night is learned from the real change.
    _arm(NIGHT_ZONES)(console.payloads[0])
    await poll(hass, entry)
    assert _state(hass) == "armed_night"

    # And Away is still Away.
    _arm(AWAY_ZONES)(console.payloads[0])
    await poll(hass, entry)
    assert _state(hass) == "armed_away"


async def test_an_arm_that_never_took_effect_stops_waiting(hass, freezer):
    """A refused arm must not leave the panel waiting to label the next profile
    someone happens to arm."""
    console = _console()
    entry = await setup_integration(hass, console, ALL_TRIGGERS)
    console.webhook_effects["night-id"] = lambda payload: None  # never arms
    await _call(hass, "alarm_arm_night")
    assert _state(hass) == "disarmed"

    await advance(hass, freezer, LEARN_WINDOW + 1)
    _arm(AWAY_ZONES)(console.payloads[0])  # someone arms Away at the keypad
    await poll(hass, entry)
    # Had the pending Night still been waiting, "4,6" would now be filed as Night.
    assert _state(hass) == "armed_away"


async def test_learned_profiles_survive_a_restart(hass, hass_storage, freezer):
    console = _console()
    entry = await setup_integration(hass, console, ALL_TRIGGERS)
    await _call(hass, "alarm_arm_night")
    assert _state(hass) == "armed_night"

    await advance(hass, freezer, SAVE_DELAY + 1)  # let the delayed save land
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}.profiles"]["data"]
    assert list(stored.values()) == [{"6": "armed_night"}]

    await restart(hass, entry, console)
    assert _state(hass) == "armed_night"  # not back to the Away default


async def test_only_night_configured_defaults_an_unknown_profile_to_night(hass):
    """Calling it Away would name a mode this panel cannot even command."""
    console = _console(armed="on")
    await setup_integration(hass, console, {CONF_ARM_NIGHT_TRIGGER: "night-id"})
    assert _state(hass) == "armed_night"


async def test_an_unconfigured_action_says_so(hass):
    await setup_integration(hass, _console(), {CONF_ARM_AWAY_TRIGGER: "away-id"})
    with pytest.raises(HomeAssistantError, match="disarm"):
        await _call(hass, "alarm_disarm")


async def test_a_rejected_key_is_reported_not_swallowed(hass):
    console = _console()
    await setup_integration(hass, console, ALL_TRIGGERS)
    console.webhook_error = AlarmHubAuthError("Auth failed (401)")
    with pytest.raises(HomeAssistantError, match="rejected the API key"):
        await _call(hass, "alarm_arm_away")
    assert console.webhooks == []


async def test_a_webhook_the_console_refuses_points_at_the_trigger_id(hass):
    """A wrong Trigger ID is the likeliest mistake; the error should say so."""
    console = _console()
    await setup_integration(hass, console, ALL_TRIGGERS)
    console.webhook_error = AlarmHubConnectionError("HTTP 404 from /v1/alarm-manager")
    with pytest.raises(HomeAssistantError, match="Trigger ID"):
        await _call(hass, "alarm_arm_away")
