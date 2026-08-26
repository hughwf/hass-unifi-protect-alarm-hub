"""Tests for the battery sensors: creation, the enum's closed set, and voltage."""

from __future__ import annotations

from homeassistant.const import STATE_UNKNOWN

from custom_components.unifi_protect_alarm_hub.sensor import (
    BATTERY_STATUS_OPTIONS,
    battery_status_option,
    battery_voltage,
)
from platform_common import (  # noqa: F401  (unload_entries is an autouse fixture)
    MAC,
    FakeConsole,
    entity_ids,
    hub_devices,
    hub_frame,
    hub_json,
    poll,
    setup_integration,
    unique_ids,
    unload_entries,
)

STATUS = "sensor.alarm_hub_kit_backup_battery_status"
VOLTAGE = "sensor.alarm_hub_kit_backup_battery_voltage"


def _without_battery() -> dict:
    payload = hub_json()
    del payload["alarmHub"]["battery"]
    return payload


# --- pure helpers ---


def test_battery_status_option_passes_the_values_we_model():
    assert battery_status_option("ok") == "ok"
    assert battery_status_option("low") == "low"
    assert battery_status_option("critical") == "critical"


def test_battery_status_option_folds_case():
    # The design spec writes the value set in a case the lowercase options do
    # not contain, and a value outside ``options`` raises inside the listener.
    assert battery_status_option("OK") == "ok"
    assert battery_status_option("Critical") == "critical"


def test_everything_unreadable_becomes_one_no_reading():
    """ "The hub cannot tell" and "we have no value" render identically anyway.

    Home Assistant shows a ``None`` state as ``unknown``, so an ``unknown``
    option would have been the same string for both with nothing able to tell
    them apart. There is one way to say it, and it is the one HA already owns.
    """
    assert battery_status_option("UNKNOWN") is None
    assert battery_status_option("degraded") is None
    assert battery_status_option(7) is None
    assert battery_status_option({"status": "ok"}) is None
    assert battery_status_option(None) is None


def test_the_options_are_only_states_a_battery_can_be_in():
    assert BATTERY_STATUS_OPTIONS == ["ok", "low", "critical"]
    assert "unknown" not in BATTERY_STATUS_OPTIONS


def test_every_value_we_publish_is_actually_an_option():
    # Anything outside the closed set raises in ``SensorEntity.state``, and it
    # raises inside the coordinator listener -- which stops every later update.
    for raw in ("ok", "OK", "low", "UNKNOWN", "degraded", 7, None):
        option = battery_status_option(raw)
        assert option is None or option in BATTERY_STATUS_OPTIONS


def test_battery_voltage_reads_numbers_and_numeric_strings():
    assert battery_voltage(12.6) == 12.6
    assert battery_voltage(12) == 12.0
    assert battery_voltage("12.6") == 12.6


def test_battery_voltage_reports_no_reading_for_anything_else():
    assert battery_voltage(None) is None
    assert battery_voltage("n/a") is None
    assert battery_voltage({"volts": 12.6}) is None
    assert battery_voltage(float("nan")) is None
    assert battery_voltage(float("inf")) is None


def test_battery_voltage_refuses_a_json_boolean():
    """``bool`` is a subclass of ``int``, so ``true`` used to read as 1.0 V.

    On a VOLTAGE sensor carrying a MEASUREMENT state_class that number goes
    into long-term statistics, where it is indistinguishable from a real
    reading of a battery about to fail.
    """
    assert battery_voltage(True) is None
    assert battery_voltage(False) is None


# --- entities ---


async def test_a_battery_reported_later_gets_its_sensors(hass):
    """``alarm_hub_battery`` is optional and can start being reported at any time."""
    console = FakeConsole(_without_battery())
    entry = await setup_integration(hass, console)
    assert entity_ids(hass, "sensor") == []

    console.payloads[0]["alarmHub"]["battery"] = {
        "connection": "connected",
        "voltage": 12.4,
        "batteryStatus": "low",
    }
    await poll(hass, entry)

    assert entity_ids(hass, "sensor") == [STATUS, VOLTAGE]
    assert hass.states.get(STATUS).state == "low"
    assert hass.states.get(VOLTAGE).state == "12.4"


async def test_a_battery_and_a_mac_arriving_together_stay_on_the_hub_we_have(hass):
    """This is the platform whose ``build`` returns nothing for some hubs.

    Which is why identity is settled from the whole snapshot before anything is
    built. Learned only from the entities it creates, this platform's map never
    hears about a hub with no battery -- so when one reports a battery and a mac
    in the same poll, it mints a device from the mac while the other platforms
    are still using the id they learned when the hub arrived. The hub splits in
    two and the battery sensors land on the half nothing else is on.

    The hub is adopted after setup deliberately. Adopted before it, the registry
    seeding covers for the missing pass -- another platform registers the device
    first and this one reads the identity back out of the registry -- and the
    config flow lets someone finish with nothing adopted yet, so that is not the
    order to test.
    """
    console = FakeConsole()
    entry = await setup_integration(hass, console)
    payload = hub_json(mac="")
    del payload["alarmHub"]["battery"]
    console.payloads.append(payload)
    await poll(hass, entry)
    assert entity_ids(hass, "sensor") == []

    console.payloads[0]["mac"] = MAC
    console.payloads[0]["alarmHub"]["battery"] = {
        "connection": "connected",
        "voltage": 12.4,
        "batteryStatus": "ok",
    }
    await poll(hass, entry)

    assert len(hub_devices(hass, entry)) == 1
    assert {"ah1_battery_status", "ah1_battery_voltage"} <= unique_ids(hass)
    assert hass.states.get(STATUS).state == "ok"


async def test_a_battery_the_hub_stops_reporting_stops_reading_ok(hass):
    """``battery`` is optional, and a delta may null it away (``keeps_hub_shape``).

    A backup battery the console has stopped describing is exactly the case
    where a confident "ok" is a lie nobody can see through -- and it is the one
    reading somebody checks before assuming the alarm survives a power cut.
    """
    console = FakeConsole(hub_json())
    await setup_integration(hass, console)
    assert hass.states.get(STATUS).state == "ok"

    console.push(hub_frame(alarmHub={"battery": None}))
    await hass.async_block_till_done()

    assert hass.states.get(STATUS).state == STATE_UNKNOWN


async def test_an_unmodelled_battery_status_does_not_freeze_the_entity(hass):
    """One value outside ``options`` used to stop the state advancing for good.

    ``SensorEntity.state`` raises when the value is not in the option list, and
    it raises inside the coordinator listener -- so the exception did not just
    skip that update, it took every update behind it with it. The second half
    of this test is the part that matters: the entity has to still be moving.
    """
    payload = hub_json()
    payload["alarmHub"]["battery"]["batteryStatus"] = "UNKNOWN"
    console = FakeConsole(payload)
    entry = await setup_integration(hass, console)

    assert hass.states.get(STATUS).state == "unknown"
    assert hass.states.get(STATUS).attributes["options"] == BATTERY_STATUS_OPTIONS

    console.payloads[0]["alarmHub"]["battery"]["batteryStatus"] = "critical"
    await poll(hass, entry)

    assert hass.states.get(STATUS).state == "critical"


async def test_a_boolean_voltage_does_not_publish_a_confident_reading(hass):
    """The whole path, because 1.0 V is a plausible number and a total lie."""
    payload = hub_json()
    payload["alarmHub"]["battery"]["voltage"] = True
    await setup_integration(hass, FakeConsole(payload))

    assert hass.states.get(VOLTAGE).state == "unknown"


async def test_a_non_numeric_voltage_reports_no_reading(hass):
    """A voltage sensor is numeric, so an unreadable value raised on first write.

    That write is during setup, so the entity never got a state at all -- not a
    gap in the graph, a permanently blank sensor.
    """
    payload = hub_json()
    payload["alarmHub"]["battery"]["voltage"] = "n/a"
    console = FakeConsole(payload)
    entry = await setup_integration(hass, console)

    assert hass.states.get(VOLTAGE).state == "unknown"

    console.payloads[0]["alarmHub"]["battery"]["voltage"] = 12.1
    await poll(hass, entry)

    assert hass.states.get(VOLTAGE).state == "12.1"


async def test_the_voltage_sensor_keeps_long_term_statistics(hass):
    """Without a state_class the recorder keeps five days and no statistics.

    Volts sagging over months is the one graph a backup battery is for.
    """
    console = FakeConsole(hub_json())
    await setup_integration(hass, console)

    assert hass.states.get(VOLTAGE).attributes["state_class"] == "measurement"
