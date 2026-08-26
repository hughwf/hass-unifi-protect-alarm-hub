"""Tier-1 tests for JSON parsing into lightweight models (pytest only)."""

from __future__ import annotations

from custom_components.unifi_protect_alarm_hub.models import (
    AlarmHub,
    deep_merge,
    keeps_hub_shape,
)

RAW = {
    "id": "ah1",
    "modelKey": "linkstation",
    "name": "Alarm Hub",
    "mac": "AABBCCDDEEFF",
    "state": "CONNECTED",
    "isAlarmHub": True,
    "alarmHub": {
        "armed": "on",
        "battery": {
            "connection": "connected",
            "charging": "on",
            "voltage": 13.2,
            "batteryStatus": "ok",
        },
        "cover": {"status": "open", "distance": 5},
        "input": {
            "1": {
                "enable": "on",
                "type": "nc",
                "status": "normal",
                "inputType": "ENTRY",
                "name": "Front Door",
                "lastTriggeredAt": 1700,
                "cameraId": "cam1",
            },
            "2": {"enable": "off", "type": "no", "status": "alarm"},
        },
        "output": {
            "1": {
                "active": "off",
                "enable": "on",
                "status": "dry",
                "name": "Siren",
                "delay": 0,
                "duration": 30,
            },
        },
    },
}


def test_parses_top_level_fields():
    hub = AlarmHub.from_json(RAW)
    assert hub.id == "ah1"
    assert hub.name == "Alarm Hub"
    assert hub.mac == "AABBCCDDEEFF"
    assert hub.state == "CONNECTED"
    assert hub.is_alarm_hub is True
    assert hub.alarm_hub_armed == "on"


def test_parses_inputs_keyed_by_int():
    hub = AlarmHub.from_json(RAW)
    zones = hub.alarm_hub_inputs
    assert set(zones) == {1, 2}
    z1 = zones[1]
    assert z1.status == "normal"
    assert z1.input_type == "ENTRY"
    assert z1.enable == "on"
    assert z1.type == "nc"
    assert z1.name == "Front Door"
    assert z1.last_triggered_at == 1700
    assert z1.camera_id == "cam1"
    # zone 2 has no inputType/name -> None
    assert zones[2].input_type is None
    assert zones[2].name is None
    assert zones[2].status == "alarm"


def test_parses_outputs_battery_cover():
    hub = AlarmHub.from_json(RAW)
    out = hub.alarm_hub_outputs[1]
    assert out.active == "off"
    assert out.status == "dry"
    assert out.name == "Siren"
    assert out.duration == 30
    assert hub.alarm_hub_battery.battery_status == "ok"
    assert hub.alarm_hub_battery.voltage == 13.2
    assert hub.alarm_hub_battery.connection == "connected"
    assert hub.alarm_hub_cover.status == "open"


def test_minimal_hub_without_subsections():
    hub = AlarmHub.from_json(
        {
            "id": "ah2",
            "name": None,
            "mac": "X",
            "state": "DISCONNECTED",
            "isAlarmHub": True,
            "alarmHub": {},
        }
    )
    assert hub.alarm_hub_armed is None
    assert hub.alarm_hub_battery is None
    assert hub.alarm_hub_cover is None
    assert hub.alarm_hub_inputs == {}
    assert hub.alarm_hub_outputs == {}


def test_missing_alarmhub_key():
    hub = AlarmHub.from_json(
        {"id": "x", "mac": "Y", "state": "CONNECTED", "isAlarmHub": True}
    )
    assert hub.alarm_hub_inputs == {}
    assert hub.alarm_hub_battery is None


# --- deep_merge() ---


def test_deep_merge_layers_nested_keys_without_dropping_siblings():
    base = {"a": 1, "n": {"x": {"p": 1, "q": 2}, "y": 3}}
    merged = deep_merge(base, {"n": {"x": {"q": 9}}})
    assert merged == {"a": 1, "n": {"x": {"p": 1, "q": 9}, "y": 3}}


def test_deep_merge_replaces_lists_and_scalars_wholesale():
    base = {"l": [1, 2, 3], "s": "old", "d": {"k": 1}}
    merged = deep_merge(base, {"l": [9], "s": "new", "d": "not a dict"})
    assert merged == {"l": [9], "s": "new", "d": "not a dict"}


def test_deep_merge_adds_keys_and_deletes_nulled_ones():
    merged = deep_merge({"a": 1}, {"b": {"c": 2}, "a": None})
    assert merged == {"b": {"c": 2}}


def test_deep_merge_null_deletes_a_nested_entry_outright():
    # Storing the None instead would leave a non-dict where a zone used to be,
    # so the next partial delta for that zone takes the replace branch.
    base = {"alarmHub": {"input": {"1": {"name": "Front"}, "2": {"name": "Back"}}}}
    merged = deep_merge(base, {"alarmHub": {"input": {"2": None}}})
    assert merged == {"alarmHub": {"input": {"1": {"name": "Front"}}}}


def test_deep_merge_null_for_an_absent_key_is_a_no_op():
    assert deep_merge({"a": 1}, {"b": None}) == {"a": 1}


def test_deep_merge_does_not_mutate_its_arguments():
    base = {"n": {"x": 1}}
    delta = {"n": {"y": 2}}
    deep_merge(base, delta)
    assert base == {"n": {"x": 1}}
    assert delta == {"n": {"y": 2}}


# --- AlarmHub.with_delta() ---


def test_with_delta_updates_one_zone_and_leaves_the_rest_alone():
    hub = AlarmHub.from_json(RAW)
    updated = hub.with_delta(
        {
            "id": "ah1",
            "modelKey": "linkstation",
            "alarmHub": {"input": {"1": {"status": "alarm", "lastTriggeredAt": 1800}}},
        }
    )
    z1 = updated.alarm_hub_inputs[1]
    assert z1.status == "alarm"
    assert z1.last_triggered_at == 1800
    # Everything the delta did not mention is carried over from the snapshot.
    assert z1.name == "Front Door"
    assert z1.input_type == "ENTRY"
    assert z1.enable == "on"
    assert updated.alarm_hub_inputs[2].status == "alarm"
    assert updated.alarm_hub_outputs[1].name == "Siren"
    assert updated.alarm_hub_battery.voltage == 13.2
    assert updated.name == "Alarm Hub"
    # The source hub is untouched: a merge returns a new snapshot.
    assert hub.alarm_hub_inputs[1].status == "normal"


def test_with_delta_is_chainable():
    # Consecutive frames (open, then closed) each build on the previous state.
    hub = AlarmHub.from_json(RAW)
    opened = hub.with_delta({"alarmHub": {"input": {"1": {"status": "alarm"}}}})
    closed = opened.with_delta({"alarmHub": {"input": {"1": {"status": "normal"}}}})
    assert opened.alarm_hub_inputs[1].status == "alarm"
    assert closed.alarm_hub_inputs[1].status == "normal"
    assert closed.alarm_hub_inputs[1].name == "Front Door"


def test_with_delta_accepts_a_whole_device_object():
    # If the console sends the full device instead of a delta, every field it
    # carries still wins.
    hub = AlarmHub.from_json(RAW)
    full = dict(RAW, state="DISCONNECTED")
    assert hub.with_delta(full).state == "DISCONNECTED"


def test_with_delta_adds_a_zone_that_was_not_in_the_snapshot():
    hub = AlarmHub.from_json(RAW)
    updated = hub.with_delta({"alarmHub": {"input": {"6": {"status": "alarm"}}}})
    assert updated.alarm_hub_inputs[6].status == "alarm"
    assert set(updated.alarm_hub_inputs) == {1, 2, 6}


# --- from_json() tolerance: it is parsed inside the WS read loop ---


def test_from_json_survives_a_non_dict_alarm_hub_section():
    # Seen as {"alarmHub": "unavailable"}: truthy, so an ``or {}`` guard lets it
    # through and the next .get() blows up the refresh or the socket.
    hub = AlarmHub.from_json(
        {"id": "x", "mac": "Y", "isAlarmHub": True, "alarmHub": "unavailable"}
    )
    assert hub.id == "x"
    assert hub.alarm_hub_inputs == {}
    assert hub.alarm_hub_battery is None


def test_from_json_survives_junk_in_every_section():
    for junk in ("", 0, [], ["a"], 1.5, True, {"nested": {"deep": None}}):
        hub = AlarmHub.from_json(
            {
                "id": "x",
                "mac": "Y",
                "alarmHub": {
                    "input": junk,
                    "output": junk,
                    "battery": junk,
                    "cover": junk,
                    "armed": junk,
                },
            }
        )
        assert hub.alarm_hub_inputs == {}
        assert hub.alarm_hub_outputs == {}


def test_zone_keys_that_only_look_numeric_are_skipped():
    # "²".isdigit() is True but int("²") raises, which would take out the parse.
    hub = AlarmHub.from_json(
        {
            "id": "x",
            "mac": "Y",
            "alarmHub": {
                "input": {
                    "1": {"status": "normal"},
                    "²": {"status": "alarm"},
                    "-1": {"status": "alarm"},
                    "1.0": {"status": "alarm"},
                }
            },
        }
    )
    assert set(hub.alarm_hub_inputs) == {1}


def test_zone_keys_too_long_to_convert_are_skipped():
    """int() refuses a decimal string past sys.get_int_max_str_digits() (4300).

    isdecimal() is true of it, so the guard lets it through and the ValueError
    takes out a parse that runs inside the WebSocket read loop.
    """
    hub = AlarmHub.from_json(
        {
            "id": "x",
            "mac": "Y",
            "alarmHub": {
                "input": {"1" * 5000: {"status": "alarm"}, "1": {"status": "normal"}},
                "output": {"9" * 5000: {"active": "on"}},
            },
        }
    )
    assert set(hub.alarm_hub_inputs) == {1}
    assert hub.alarm_hub_outputs == {}


def test_hub_is_hashable():
    # Frozen dataclasses generate __hash__, and an unhashable field turns any
    # set/dict use of a hub into a TypeError at the call site.
    hub = AlarmHub.from_json(RAW)
    assert len({hub, AlarmHub.from_json(RAW)}) == 1


# --- keeps_hub_shape() ---


def test_keeps_hub_shape_accepts_a_merge_that_only_refines():
    base = {"alarmHub": {"input": {"1": {"status": "normal"}}}}
    merged = {"alarmHub": {"input": {"1": {"status": "alarm"}}}}
    assert keeps_hub_shape(base, merged) is True


def test_keeps_hub_shape_rejects_a_section_that_stopped_being_a_mapping():
    # input and output are not optional on the model -- there is no hub without
    # them -- so neither replacing one nor dropping one is representable.
    base = {"alarmHub": {"input": {"1": {}}, "output": {"1": {}}}}
    assert keeps_hub_shape(base, {}) is False
    assert keeps_hub_shape(base, {"alarmHub": []}) is False
    assert (
        keeps_hub_shape(base, {"alarmHub": {"input": [], "output": {"1": {}}}}) is False
    )
    assert keeps_hub_shape(base, {"alarmHub": {"input": {"1": {}}}}) is False


def test_keeps_hub_shape_accepts_removing_an_optional_section():
    """battery and cover are ``| None`` on the model, so absence is a state.

    deep_merge documents null as the way a delta expresses removal, and refusing
    it here leaves the hub reporting a battery the console stopped describing.
    """
    base = {
        "alarmHub": {
            "input": {"1": {}},
            "battery": {"voltage": 13.2},
            "cover": {"status": "open"},
        }
    }
    assert keeps_hub_shape(base, {"alarmHub": {"input": {"1": {}}}}) is True


def test_keeps_hub_shape_still_rejects_junk_in_an_optional_section():
    # Removal is expressible; a list where the mapping was is not, and storing
    # it would make the next partial battery delta replace rather than merge.
    base = {"alarmHub": {"battery": {"voltage": 13.2}}}
    assert keeps_hub_shape(base, {"alarmHub": {"battery": ["nope"]}}) is False


def test_keeps_hub_shape_rejects_junk_in_an_optional_section_the_base_lacked():
    """Absent from the base is not a reason to skip the check.

    Zones default the other way round -- ``_entries_stay_mappings`` reads a key
    the base never had as a mapping, so a new one arriving as junk is rejected
    -- and a section the base never had is the same case one level up: nothing
    has vetted the value yet, which is exactly what the guard is for.
    """
    base = {"alarmHub": {"input": {"1": {}}}}
    for junk in ({"battery": "junk"}, {"cover": 7}):
        merged = {"alarmHub": {"input": {"1": {}}, **junk}}
        assert keeps_hub_shape(base, merged) is False


def test_keeps_hub_shape_rejects_an_entry_that_stopped_being_a_mapping():
    # The same poisoning one level deeper: a zone stored as junk makes the next
    # partial delta for it rebuild the zone from that delta alone.
    base = {"alarmHub": {"input": {"1": {"name": "Front Door"}}}}
    assert keeps_hub_shape(base, {"alarmHub": {"input": {"1": "junk"}}}) is False
    # A zone arriving for the first time has to arrive as a mapping too.
    assert keeps_hub_shape(base, {"alarmHub": {"input": {"1": {}, "6": 7}}}) is False


def test_keeps_hub_shape_tolerates_an_entry_that_was_already_junk():
    """A REST snapshot is stored as it arrived, junk entries included.

    Rejecting frames over junk that is already in the base would kill the push
    path for as long as it sits there -- worse than the damage it guards.
    """
    base = {"alarmHub": {"input": {"1": "junk", "2": {}}}}
    merged = {"alarmHub": {"input": {"1": "junk", "2": {"status": "alarm"}}}}
    assert keeps_hub_shape(base, merged) is True


def test_keeps_hub_shape_allows_sections_the_base_never_had():
    assert keeps_hub_shape({"id": "x"}, {"id": "x"}) is True
    assert keeps_hub_shape({"alarmHub": {}}, {"alarmHub": {}}) is True


# --- with_delta() refuses to poison the cache ---


def test_with_delta_ignores_a_frame_that_nulls_the_whole_hub_section():
    hub = AlarmHub.from_json(RAW)
    unchanged = hub.with_delta({"id": "ah1", "alarmHub": None})
    assert unchanged is hub
    assert set(unchanged.alarm_hub_inputs) == {1, 2}


def test_with_delta_ignores_a_frame_that_replaces_the_zone_map_with_a_list():
    hub = AlarmHub.from_json(RAW)
    assert hub.with_delta({"alarmHub": {"input": []}}) is hub


def test_a_rejected_frame_does_not_poison_later_merges():
    # The merged payload becomes the next delta's base, so a bad frame that was
    # stored would keep every later frame from finding the zones it updates.
    hub = AlarmHub.from_json(RAW)
    poisoned = hub.with_delta({"alarmHub": None})
    later = poisoned.with_delta({"alarmHub": {"input": {"1": {"status": "alarm"}}}})
    assert set(later.alarm_hub_inputs) == {1, 2}
    assert later.alarm_hub_inputs[1].status == "alarm"
    assert later.alarm_hub_inputs[1].name == "Front Door"
    assert later.alarm_hub_outputs[1].name == "Siren"


def test_with_delta_never_raises_on_a_hostile_item():
    hub = AlarmHub.from_json(RAW)
    for item in (None, "unavailable", [1, 2], 7, {"alarmHub": ["x"]}):
        assert hub.with_delta(item) is hub
    # A junk value inside a section we model is absorbed, not raised.
    assert hub.with_delta({"alarmHub": {"battery": {"voltage": ["nope"]}}})


def test_with_delta_ignores_a_frame_that_turns_a_zone_into_junk():
    hub = AlarmHub.from_json(RAW)
    assert hub.with_delta({"alarmHub": {"input": {"1": "junk"}}}) is hub
    assert hub.with_delta({"alarmHub": {"output": {"1": 7}}}) is hub
    # Including a zone we have never seen: adopting it would leave a non-dict
    # for the next delta on that zone to replace instead of merge into.
    assert hub.with_delta({"alarmHub": {"input": {"6": "junk"}}}) is hub


def test_with_delta_ignores_an_optional_section_that_first_arrives_as_junk():
    """A hub with no battery must not acquire one made of string.

    The module's rule is about what gets stored, not only about what parses:
    ``from_json`` reads the junk as "no battery" either way, but the merged
    payload is the next frame's base, so adopting it leaves the cache holding a
    document that has stopped describing a hub.
    """
    sections = {
        key: value
        for key, value in RAW["alarmHub"].items()
        if key not in ("battery", "cover")
    }
    hub = AlarmHub.from_json(dict(RAW, alarmHub=sections))
    assert hub.alarm_hub_battery is None
    assert hub.with_delta({"alarmHub": {"battery": "junk"}}) is hub
    assert hub.with_delta({"alarmHub": {"cover": 7}}) is hub


def test_a_zone_turned_into_junk_does_not_poison_later_merges():
    # Stored, the junk would take deep_merge's replace-wholesale branch on the
    # next partial delta, so zone 1 would come back with no name, no inputType
    # and no enable -- the binary_sensor losing its name and device_class.
    hub = AlarmHub.from_json(RAW)
    poisoned = hub.with_delta({"alarmHub": {"input": {"1": "junk"}}})
    later = poisoned.with_delta({"alarmHub": {"input": {"1": {"status": "alarm"}}}})
    assert later.alarm_hub_inputs[1].status == "alarm"
    assert later.alarm_hub_inputs[1].name == "Front Door"
    assert later.alarm_hub_inputs[1].input_type == "ENTRY"
    assert later.alarm_hub_inputs[1].enable == "on"


def test_with_delta_deletes_a_zone_the_console_nulls_out():
    hub = AlarmHub.from_json(RAW)
    updated = hub.with_delta({"alarmHub": {"input": {"2": None}}})
    assert set(updated.alarm_hub_inputs) == {1}
    # The deletion is real in the stored payload, not a None left lying in it.
    assert "2" not in updated.raw["alarmHub"]["input"]
    assert updated.alarm_hub_inputs[1].name == "Front Door"


def test_with_delta_removes_an_optional_section_the_console_nulls_out():
    # A hub that lost its battery pack reports it as null; keeping the last
    # reading would leave the sensor showing 13.2 V from hardware that is gone.
    hub = AlarmHub.from_json(RAW)
    updated = hub.with_delta({"alarmHub": {"battery": None, "cover": None}})
    assert updated.alarm_hub_battery is None
    assert updated.alarm_hub_cover is None
    assert "battery" not in updated.raw["alarmHub"]
    assert set(updated.alarm_hub_inputs) == {1, 2}


def test_a_removal_does_not_discard_the_rest_of_the_frame():
    # Rejection used to be whole-frame, so one null section silently dropped a
    # zone going into alarm that rode along in the same delta.
    hub = AlarmHub.from_json(RAW)
    updated = hub.with_delta(
        {"alarmHub": {"battery": None, "input": {"6": {"status": "alarm"}}}}
    )
    assert updated.alarm_hub_inputs[6].status == "alarm"
    assert updated.alarm_hub_battery is None
    assert updated.alarm_hub_inputs[1].name == "Front Door"


def test_a_junk_zone_entry_is_skipped_rather_than_parsed():
    """A non-mapping zone value must not reach InputZone.from_json.

    Reachable in normal operation, not hypothetically: a REST snapshot carrying
    junk in one zone is stored as it arrived and re-parsed on every later merge
    — inside the WebSocket reader, where a raise would drop the socket.
    """
    hub = AlarmHub.from_json(
        {
            "id": "ah1",
            "mac": "M",
            "isAlarmHub": True,
            "alarmHub": {"input": {"1": "junk", "2": {"status": "alarm"}}},
        }
    )

    assert set(hub.alarm_hub_inputs) == {2}
    assert hub.alarm_hub_inputs[2].status == "alarm"
