"""Tests for the alarm panel: arming through Alarm Manager webhooks.

The Alarm Hub is stuck in Global Alarm Manager mode, where the API cannot set the
arm profile, so the panel fires user-configured webhooks and reads the result
back from the hub. The console here is the fake from ``platform_common`` with
each webhook given an *effect* -- what the real console would do to the hub.
Arming Away and arming Night have the same effect on purpose: on real hardware
the hub reports ``armed`` and nothing that tells the two apart.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntityFeature as Feature,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError

from custom_components.unifi_protect_alarm_hub.alarm_control_panel import ARM_WINDOW
from custom_components.unifi_protect_alarm_hub.api import (
    AlarmHubAuthError,
    AlarmHubConnectionError,
)
from custom_components.unifi_protect_alarm_hub.const import (
    CONF_ARM_AWAY_TRIGGER,
    CONF_ARM_NIGHT_TRIGGER,
    CONF_DISARM_TRIGGER,
)
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


def _arm(payload: dict[str, Any]) -> None:
    payload["alarmHub"]["armed"] = "on"


def _disarm(payload: dict[str, Any]) -> None:
    payload["alarmHub"]["armed"] = "off"


def _console(*, armed: str = "off") -> FakeConsole:
    payload = hub_json()
    payload["alarmHub"]["armed"] = armed
    console = FakeConsole(payload)
    console.webhook_effects = {
        "away-id": _arm,
        "night-id": _arm,
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
    assert _state(hass) == "armed_away"  # not armed from here: the default


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


async def test_the_profile_armed_from_here_is_shown_until_disarmed(hass, freezer):
    console = _console()
    entry = await setup_integration(hass, console, ALL_TRIGGERS)

    await _call(hass, "alarm_arm_night")
    assert _state(hass) == "armed_night"
    await advance(hass, freezer, ARM_WINDOW + 1)
    await poll(hass, entry)
    assert _state(hass) == "armed_night"  # the label outlives the window

    _disarm(console.payloads[0])  # at the keypad
    await poll(hass, entry)
    assert _state(hass) == "disarmed"


async def test_armed_from_elsewhere_reads_as_the_default(hass, freezer):
    """The hub does not say which profile, so a keypad arm is only "armed"."""
    console = _console()
    entry = await setup_integration(hass, console, ALL_TRIGGERS)
    await _call(hass, "alarm_arm_night")
    await _call_disarm_then(hass, freezer, "alarm_arm_night")
    await advance(hass, freezer, ARM_WINDOW + 1)

    _disarm(console.payloads[0])
    await poll(hass, entry)
    _arm(console.payloads[0])  # someone arms at the keypad, long after
    await poll(hass, entry)
    assert _state(hass) == "armed_away"


async def _call_disarm_then(hass, freezer, service: str) -> None:
    """Disarm and arm again, past the refresh cooldown between the two."""
    await advance(hass, freezer, 11)
    await _call(hass, "alarm_disarm")
    await advance(hass, freezer, 11)
    await _call(hass, service)


async def test_switching_profile_from_here_while_armed(hass, freezer):
    console = _console()
    await setup_integration(hass, console, ALL_TRIGGERS)
    await _call(hass, "alarm_arm_away")
    assert _state(hass) == "armed_away"

    await advance(hass, freezer, 11)
    await _call(hass, "alarm_arm_night")
    assert _state(hass) == "armed_night"


async def test_an_arm_that_never_took_effect_stops_waiting(hass, freezer):
    """A refused arm must not leave the panel waiting to label the next profile
    someone happens to arm."""
    console = _console()
    entry = await setup_integration(hass, console, ALL_TRIGGERS)
    console.webhook_effects["night-id"] = lambda payload: None  # never arms
    await _call(hass, "alarm_arm_night")
    assert _state(hass) == "disarmed"

    await advance(hass, freezer, ARM_WINDOW + 1)
    _arm(console.payloads[0])  # someone arms at the keypad
    await poll(hass, entry)
    # Had the Night command still been waiting, this would now read Night.
    assert _state(hass) == "armed_away"


async def test_an_arm_the_hub_reports_late_is_seen_without_a_push(hass, freezer):
    """Arming sends no WebSocket frame, so the panel has to go back and look:
    left to the five-minute poll it would read disarmed, then uncredited."""
    console = _console()
    await setup_integration(hass, console, ALL_TRIGGERS)
    console.webhook_effects["night-id"] = lambda payload: None  # not yet
    await _call(hass, "alarm_arm_night")
    assert _state(hass) == "disarmed"

    _arm(console.payloads[0])  # the hub acts a moment later, and pushes nothing
    await advance(hass, freezer, 2)
    assert _state(hass) == "armed_night"


async def test_a_disarm_the_hub_reports_late_is_seen_without_a_push(hass, freezer):
    console = _console(armed="on")
    await setup_integration(hass, console, ALL_TRIGGERS)
    console.webhook_effects["disarm-id"] = lambda payload: None  # not yet
    await _call(hass, "alarm_disarm")
    assert _state(hass) == "armed_away"

    _disarm(console.payloads[0])
    await advance(hass, freezer, 2)
    assert _state(hass) == "disarmed"


async def test_follow_up_reads_stop_once_the_hub_has_answered(hass, freezer):
    console = _console()
    await setup_integration(hass, console, ALL_TRIGGERS)
    await _call(hass, "alarm_arm_night")  # the fake arms at once
    polls = console.polls
    await advance(hass, freezer, 30)
    assert console.polls == polls


async def test_one_command_does_not_label_a_second_spell(hass):
    """Armed Night from here, then disarmed and re-armed at the keypad while the
    window is still open: the second arm is not ours."""
    console = _console()
    entry = await setup_integration(hass, console, ALL_TRIGGERS)
    await _call(hass, "alarm_arm_night")
    assert _state(hass) == "armed_night"

    _disarm(console.payloads[0])
    await poll(hass, entry)
    _arm(console.payloads[0])
    await poll(hass, entry)
    assert _state(hass) == "armed_away"


async def test_a_disarm_withdraws_an_arm_still_waiting(hass, freezer):
    console = _console()
    entry = await setup_integration(hass, console, ALL_TRIGGERS)
    console.webhook_effects["night-id"] = lambda payload: None  # slow to act
    await _call(hass, "alarm_arm_night")
    await advance(hass, freezer, 11)
    await _call(hass, "alarm_disarm")

    _arm(console.payloads[0])  # armed at the keypad, inside the old window
    await poll(hass, entry)
    assert _state(hass) == "armed_away"


async def test_every_hub_credits_the_same_command(hass):
    """The Alarm Manager arms every hub at once; the panel beside the one that
    was pressed must not read Away while its neighbour reads Night."""
    second = hub_json("ah2", mac="112233445566", name="Second Hub")
    second["alarmHub"]["armed"] = "off"
    console = _console()
    console.payloads.append(second)
    await setup_integration(hass, console, ALL_TRIGGERS)
    panels = entity_ids(hass, "alarm_control_panel")
    assert len(panels) == 2

    await _call(hass, "alarm_arm_night")
    assert {hass.states.get(panel).state for panel in panels} == {"armed_night"}


async def test_the_credited_profile_survives_a_restart(hass):
    console = _console()
    entry = await setup_integration(hass, console, ALL_TRIGGERS)
    await _call(hass, "alarm_arm_night")
    assert _state(hass) == "armed_night"

    await restart(hass, entry, console)
    assert _state(hass) == "armed_night"  # not back to the Away default


async def test_a_restored_profile_does_not_outlive_a_disarm_it_missed(hass):
    """Disarmed while Home Assistant was down: the old label must not be waiting
    for whoever arms next."""
    console = _console()
    entry = await setup_integration(hass, console, ALL_TRIGGERS)
    await _call(hass, "alarm_arm_night")

    _disarm(console.payloads[0])
    await restart(hass, entry, console)
    assert _state(hass) == "disarmed"

    _arm(console.payloads[0])
    await poll(hass, entry)
    assert _state(hass) == "armed_away"


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
