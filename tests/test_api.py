"""Tier-1 tests for the API client using a fake aiohttp session."""

from __future__ import annotations

import aiohttp
import pytest

from custom_components.unifi_protect_alarm_hub.api import (
    AlarmHubApiClient,
    AlarmHubAuthError,
    AlarmHubConnectionError,
    is_alarm_hub_frame,
)


class _FakeResp:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self._exc:
            raise self._exc
        return self._resp


def _client(session):
    return AlarmHubApiClient("h", 443, "key", session)


async def test_get_alarm_hubs_parses_models():
    payload = [
        {
            "id": "ah1",
            "mac": "M",
            "state": "CONNECTED",
            "isAlarmHub": True,
            "alarmHub": {"input": {"1": {"status": "alarm"}}},
        }
    ]
    client = _client(_FakeSession(_FakeResp(200, payload)))
    hubs = await client.async_get_alarm_hubs()
    assert len(hubs) == 1
    assert hubs[0].id == "ah1"
    assert hubs[0].alarm_hub_inputs[1].status == "alarm"


async def test_401_raises_auth_error():
    client = _client(_FakeSession(_FakeResp(401)))
    with pytest.raises(AlarmHubAuthError):
        await client.async_get_alarm_hubs()


async def test_403_raises_auth_error():
    client = _client(_FakeSession(_FakeResp(403)))
    with pytest.raises(AlarmHubAuthError):
        await client.async_get_alarm_hubs()


async def test_500_raises_connection_error():
    client = _client(_FakeSession(_FakeResp(500)))
    with pytest.raises(AlarmHubConnectionError):
        await client.async_get_alarm_hubs()


async def test_non_list_response_returns_empty():
    client = _client(_FakeSession(_FakeResp(200, {"unexpected": "object"})))
    assert await client.async_get_alarm_hubs() == []


async def test_client_error_raises_connection_error():
    client = _client(_FakeSession(exc=aiohttp.ClientError("boom")))
    with pytest.raises(AlarmHubConnectionError):
        await client.async_get_alarm_hubs()


# --- is_alarm_hub_frame() (pure helper, tier-1) ---


def test_is_alarm_hub_frame_true_for_linkstation():
    raw = '{"type": "update", "item": {"id": "x", "modelKey": "linkstation"}}'
    assert is_alarm_hub_frame(raw) is True


def test_is_alarm_hub_frame_false_for_other_models():
    for model in ("bridge", "chime", "camera", "sensor"):
        raw = '{"type": "update", "item": {"id": "x", "modelKey": "%s"}}' % model
        assert is_alarm_hub_frame(raw) is False


def test_is_alarm_hub_frame_ignores_type_field():
    # add/remove of a linkstation should still trigger a refresh.
    for ftype in ("add", "remove", "update"):
        raw = '{"type": "%s", "item": {"modelKey": "linkstation"}}' % ftype
        assert is_alarm_hub_frame(raw) is True


def test_is_alarm_hub_frame_false_for_malformed_json():
    assert is_alarm_hub_frame("not json") is False
    assert is_alarm_hub_frame("") is False
    assert is_alarm_hub_frame("{") is False


def test_is_alarm_hub_frame_false_for_missing_or_bad_item():
    assert is_alarm_hub_frame('{"type": "update"}') is False
    assert is_alarm_hub_frame('{"item": null}') is False
    assert is_alarm_hub_frame('{"item": {"id": "x"}}') is False
    assert is_alarm_hub_frame('"a string"') is False
    assert is_alarm_hub_frame("[1, 2, 3]") is False


async def test_trigger_output_posts_enable_body():
    session = _FakeSession(_FakeResp(200))
    client = _client(session)
    await client.async_trigger_output("ah1", 2, True)
    method, url, kw = session.calls[-1]
    assert method == "POST"
    assert url.endswith("/v1/alarm-hubs/ah1/outputs/2/trigger")
    assert kw["json"] == {"enable": True}
    assert kw["headers"]["X-API-KEY"] == "key"


# --- async_subscribe_devices() (fake-WS message dispatch) ---


class _FakeMsg:
    def __init__(self, type_, data=None):
        self.type = type_
        self.data = data


class _FakeWS:
    def __init__(self, messages):
        self._messages = messages

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield m


class _WSFakeSession:
    def __init__(self, messages):
        self._messages = messages
        self.connect_calls = []

    def ws_connect(self, url, **kw):
        self.connect_calls.append((url, kw))
        return _FakeWS(self._messages)


def _text(payload):
    return _FakeMsg(aiohttp.WSMsgType.TEXT, payload)


async def test_subscribe_calls_back_only_for_linkstation_frames():
    messages = [
        _text('{"item": {"modelKey": "bridge"}}'),
        _text('{"item": {"modelKey": "linkstation"}}'),
        _text('{"item": {"modelKey": "chime"}}'),
        _text('{"item": {"modelKey": "linkstation"}}'),
    ]
    session = _WSFakeSession(messages)
    client = _client(session)

    hits = []
    await client.async_subscribe_devices(lambda: hits.append(1))

    assert len(hits) == 2
    url, kw = session.connect_calls[-1]
    assert url.endswith("/v1/subscribe/devices")
    assert kw["headers"]["X-API-KEY"] == "key"


async def test_subscribe_stops_on_error_frame():
    messages = [
        _text('{"item": {"modelKey": "linkstation"}}'),
        _FakeMsg(aiohttp.WSMsgType.ERROR),
        _text('{"item": {"modelKey": "linkstation"}}'),
    ]
    session = _WSFakeSession(messages)
    client = _client(session)

    hits = []
    await client.async_subscribe_devices(lambda: hits.append(1))

    # The frame after ERROR must not be processed; loop returns so caller reconnects.
    assert len(hits) == 1


async def test_subscribe_ignores_non_text_frames():
    messages = [
        _FakeMsg(aiohttp.WSMsgType.PING),
        _text('{"item": {"modelKey": "linkstation"}}'),
        _FakeMsg(aiohttp.WSMsgType.PONG),
    ]
    session = _WSFakeSession(messages)
    client = _client(session)

    hits = []
    await client.async_subscribe_devices(lambda: hits.append(1))

    assert len(hits) == 1
