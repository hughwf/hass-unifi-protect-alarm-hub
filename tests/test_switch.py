"""Tests for the switch platform: creation, optimistic state, and error surfacing."""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError

from custom_components.unifi_protect_alarm_hub.api import (
    AlarmHubAuthError,
    AlarmHubConnectionError,
)
from custom_components.unifi_protect_alarm_hub.const import SCAN_INTERVAL
from custom_components.unifi_protect_alarm_hub.switch import OPTIMISTIC_WINDOW
from platform_common import (  # noqa: F401  (unload_entries is an autouse fixture)
    FakeConsole,
    advance,
    armed_call_later_handles,
    entity_ids,
    hub_json,
    poll,
    setup_integration,
    unload_entries,
)

SIREN = "switch.alarm_hub_kit_siren"

# How long the switch may stand behind a command nobody has confirmed. A
# literal, and used as one below: every deadline test advancing by the imported
# symbol passes whatever the symbol says, so widening the window to the poll
# interval -- five minutes of claiming a siren is running -- broke nothing.
OPTIMISTIC_SECONDS = 10


async def _switch(hass, service: str, entity_id: str = SIREN) -> None:
    await hass.services.async_call(
        "switch", service, {"entity_id": entity_id}, blocking=True
    )
    await hass.async_block_till_done()


def _latent_console() -> FakeConsole:
    """A hub that accepts the command but has not moved the relay *yet*.

    Which is what real hardware does: the REST GET ``_async_trigger`` fires
    straight after the command races the relay, and the console answers from the
    state it had a moment ago. A fixture that applies the command to its own
    payload synchronously hides every defect in the optimistic path, because
    the truth always agrees with the guess.
    """
    console = FakeConsole(hub_json())
    console.relay_follows_command = False
    return console


async def test_an_output_wired_in_later_gets_a_switch(hass):
    """Same reconcile contract as the other platforms, on the output map."""
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    assert entity_ids(hass, "switch") == [SIREN]

    console.payloads[0]["alarmHub"]["output"]["2"] = {
        "active": "on",
        "enable": "on",
        "status": "normal",
        "name": "Strobe",
    }
    await poll(hass, entry)

    assert hass.states.get("switch.alarm_hub_kit_strobe").state == STATE_ON


async def test_the_first_command_shows_before_the_hub_has_moved_the_relay(hass):
    """The whole point of the optimistic write, on the command that needs it most.

    ``_async_trigger`` follows the command with ``async_request_refresh``, whose
    debouncer is immediate -- so on the first command the REST GET runs inside
    the service call, and it races the relay. Retiring the optimistic value on
    that update meant it never survived to be seen: ``switch.turn_on`` left the
    siren reading "off", which is exactly "the hub refused it", on a hub that
    had accepted it.
    """
    console = _latent_console()
    await setup_integration(hass, console)
    assert hass.states.get(SIREN).state == STATE_OFF

    await _switch(hass, "turn_on")

    assert console.triggered == [("ah1", 1, True)]
    assert hass.states.get(SIREN).state == STATE_ON


async def test_a_second_command_inside_the_cooldown_shows_at_once(hass):
    """Turn on, then straight off: the toggle must not spring back to on.

    The request-refresh debouncer holds a ten-second cooldown, so the second
    command rendered from a snapshot taken before it, and the frontend showed
    the switch reverting -- which reads as the hub having refused it.
    """
    console = _latent_console()
    await setup_integration(hass, console)

    await _switch(hass, "turn_on")
    assert hass.states.get(SIREN).state == STATE_ON

    await _switch(hass, "turn_off")

    assert console.triggered == [("ah1", 1, True), ("ah1", 1, False)]
    assert hass.states.get(SIREN).state == STATE_OFF


def test_the_window_is_ten_seconds_and_not_the_poll_interval():
    """The module's own argument, pinned as a number.

    The window is tied to the request-refresh cooldown because the refresh a
    command fires is what settles the question, and it is deliberately *not*
    tied to the poll interval: with the socket down the next reconciling poll is
    five minutes away, and five minutes is not a length of time a switch may
    claim a siren is running on the strength of a command nobody confirmed.
    """
    assert OPTIMISTIC_WINDOW == OPTIMISTIC_SECONDS
    assert OPTIMISTIC_WINDOW < SCAN_INTERVAL.total_seconds()


async def test_what_we_assumed_expires_when_the_hub_never_confirms_it(hass, freezer):
    """Optimism is a stand-in for the truth, never a replacement for it.

    A console that accepts a command and never acts on it never sends an
    agreeing update, so "hold until an update reflects it" on its own would
    leave the switch claiming a siren is running with nothing able to correct
    it -- the worst possible thing for the entity a panic automation drives to
    be stuck on. Inside the window the expected state deliberately stands, even
    against a poll that disagrees; at the end of it the hub's own reading wins
    outright.

    Both edges are pinned against the literal, so lengthening the window fails
    here as well as at the constant.
    """
    console = _latent_console()
    entry = await setup_integration(hass, console)

    await _switch(hass, "turn_on")
    await poll(hass, entry)  # the hub still says the relay is off
    assert hass.states.get(SIREN).state == STATE_ON

    await advance(hass, freezer, OPTIMISTIC_SECONDS - 1)
    assert hass.states.get(SIREN).state == STATE_ON

    await advance(hass, freezer, 2)

    assert hass.states.get(SIREN).state == STATE_OFF


async def test_a_command_is_followed_by_a_request_for_what_the_relay_did(hass, freezer):
    """The optimism is bounded, so something has to fetch the truth inside it.

    That is the ``async_request_refresh`` at the end of ``_async_trigger``.
    Without it a hub that took the command keeps being rendered from the
    pre-command snapshot: the expected state lapses after ten seconds and the
    switch drops back to "off" while the siren is running, and stays there
    until the next scheduled poll up to five minutes later.
    """
    console = FakeConsole(hub_json())  # a prompt hub: the relay has moved
    await setup_integration(hass, console)
    polls_before = console.polls

    await _switch(hass, "turn_on")
    assert console.polls == polls_before + 1

    await advance(hass, freezer, OPTIMISTIC_SECONDS + 1)

    assert hass.states.get(SIREN).state == STATE_ON


async def test_a_connection_failure_reaches_the_user_as_a_reason(hass):
    """``AlarmHubConnectionError`` is not a HomeAssistantError.

    Left to escape, Home Assistant renders it as "Unknown error occurred" with
    a traceback, and the automation that called the service aborts without
    saying why.
    """
    console = _latent_console()
    await setup_integration(hass, console)
    console.trigger_error = AlarmHubConnectionError("Timed out")

    with pytest.raises(HomeAssistantError) as caught:
        await _switch(hass, "turn_on")

    assert "Could not reach UniFi Protect" in str(caught.value)
    assert "Timed out" in str(caught.value)
    # Nothing claimed: the command never reached the hub, so there is nothing
    # to expect of it, and a siren the user thinks is running is worse than an
    # error they can see.
    assert hass.states.get(SIREN).state == STATE_OFF


async def test_an_auth_failure_says_what_to_do_about_it(hass):
    console = _latent_console()
    await setup_integration(hass, console)
    console.trigger_error = AlarmHubAuthError("Auth failed (401)")

    with pytest.raises(HomeAssistantError) as caught:
        await _switch(hass, "turn_off")

    assert "rejected the API key" in str(caught.value)
    assert "reconfigure" in str(caught.value)
    assert hass.states.get(SIREN).state == STATE_OFF  # nothing claimed either


async def test_an_output_that_disappears_goes_unavailable(hass):
    """A relay the snapshot no longer describes is one nobody can command.

    Availability has to say so: Home Assistant drops unavailable entities from
    service calls, so a switch that stayed "available" over a missing output
    would accept ``turn_on`` and send it into nothing.
    """
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    assert hass.states.get(SIREN).state == STATE_OFF

    del console.payloads[0]["alarmHub"]["output"]["1"]
    await poll(hass, entry)

    assert hass.states.get(SIREN).state == STATE_UNAVAILABLE


async def test_a_command_after_a_re_adoption_is_addressed_to_the_new_id(hass):
    """Re-adoption issues a new device id, and the API is addressed by id.

    The switch captured the id it was built with, so after a re-adoption the
    command went to a device the console no longer has -- and the console
    answers that with a 404, not with the siren.
    """
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)

    console.payloads[0]["id"] = "ah2"
    await poll(hass, entry)
    await _switch(hass, "turn_on")

    assert console.triggered == [("ah2", 1, True)]
    assert hass.states.get(SIREN).state == STATE_ON


async def test_the_optimism_deadline_does_not_outlive_the_entity(hass):
    """Every command arms a ten-second reference to the switch.

    Left behind, a reload issued moments after a command holds a dead entity
    for the rest of the window -- and Home Assistant's own cleanup check fails
    a test on any timer still on the loop, which is the same thing an
    integration leaves behind in a running instance.
    """
    console = _latent_console()
    entry = await setup_integration(hass, console)

    await _switch(hass, "turn_on")
    assert armed_call_later_handles(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert armed_call_later_handles(hass) == []


async def test_renaming_an_output_moves_the_friendly_name(hass):
    console = FakeConsole(hub_json())
    entry = await setup_integration(hass, console)
    assert hass.states.get(SIREN).attributes["friendly_name"] == "Alarm Hub Kit Siren"

    console.payloads[0]["alarmHub"]["output"]["1"]["name"] = "Outdoor Sounder"
    await poll(hass, entry)

    assert (
        hass.states.get(SIREN).attributes["friendly_name"]
        == "Alarm Hub Kit Outdoor Sounder"
    )
