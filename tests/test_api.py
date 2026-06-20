"""Tier-1 tests for the API client using a fake aiohttp session."""

from __future__ import annotations

import pytest

from custom_components.unifi_protect_alarm_hub.api import (
    AlarmHubApiClient,
    AlarmHubAuthError,
    AlarmHubConnectionError,
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


async def test_500_raises_connection_error():
    client = _client(_FakeSession(_FakeResp(500)))
    with pytest.raises(AlarmHubConnectionError):
        await client.async_get_alarm_hubs()


async def test_client_error_raises_connection_error():
    import aiohttp

    client = _client(_FakeSession(exc=aiohttp.ClientError("boom")))
    with pytest.raises(AlarmHubConnectionError):
        await client.async_get_alarm_hubs()


async def test_trigger_output_posts_enable_body():
    session = _FakeSession(_FakeResp(200))
    client = _client(session)
    await client.async_trigger_output("ah1", 2, True)
    method, url, kw = session.calls[-1]
    assert method == "POST"
    assert url.endswith("/v1/alarm-hubs/ah1/outputs/2/trigger")
    assert kw["json"] == {"enable": True}
    assert kw["headers"]["X-API-KEY"] == "key"
