"""Tier-1 tests for the API client using a fake aiohttp session."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from multidict import CIMultiDict
from yarl import URL

from custom_components.unifi_protect_alarm_hub import api
from custom_components.unifi_protect_alarm_hub.api import (
    REQUEST_TIMEOUT,
    WS_HEARTBEAT,
    AlarmHubApiClient,
    AlarmHubAuthError,
    AlarmHubConnectionError,
    alarm_hub_frame,
    ws_close_reason,
)


class _FakeResp:
    def __init__(self, status, payload=None, json_exc=None):
        self.status = status
        self._payload = payload
        self._json_exc = json_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        if self._json_exc is not None:
            raise self._json_exc
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


async def test_non_list_response_raises_connection_error():
    """Laundering it into [] publishes "no hubs" behind a successful refresh.

    Every entity then goes unavailable with last_update_success still True,
    where a failed refresh would have kept the last known state on screen.
    A 200 with an empty body is the real-world shape: json() returns None.
    """
    for payload in (None, {"unexpected": "object"}, "hubs"):
        client = _client(_FakeSession(_FakeResp(200, payload)))
        with pytest.raises(AlarmHubConnectionError):
            await client.async_get_alarm_hubs()


async def test_empty_hub_list_stays_a_valid_answer():
    # No hubs adopted is a fact, not a failure.
    client = _client(_FakeSession(_FakeResp(200, [])))
    assert await client.async_get_alarm_hubs() == []


async def test_timeout_raises_connection_error():
    """A console that accepts TCP and never answers trips the total timer.

    aiohttp raises the builtin TimeoutError there, which is not a ClientError,
    so without its own clause it escapes as "unknown error" with a traceback.
    """
    client = _client(_FakeSession(exc=TimeoutError()))
    with pytest.raises(AlarmHubConnectionError):
        await client.async_get_alarm_hubs()
    assert asyncio.TimeoutError is TimeoutError  # they are the same class


async def test_requests_carry_a_lan_sized_timeout():
    """Inheriting aiohttp's default would freeze the config flow for 5 minutes."""
    session = _FakeSession(_FakeResp(200, []))
    await _client(session).async_get_alarm_hubs()
    _method, _url, kw = session.calls[-1]
    assert kw["timeout"] is REQUEST_TIMEOUT
    assert 0 < REQUEST_TIMEOUT.total <= 30


async def test_malformed_json_body_raises_connection_error():
    # A body that claims JSON and is not: JSONDecodeError is a ValueError with
    # no ClientError in its MRO, so it walks straight past the aiohttp except.
    bad = json.JSONDecodeError("Expecting property name", "{not json", 1)
    client = _client(_FakeSession(_FakeResp(200, json_exc=bad)))
    with pytest.raises(AlarmHubConnectionError):
        await client.async_get_alarm_hubs()


async def test_client_error_raises_connection_error():
    client = _client(_FakeSession(exc=aiohttp.ClientError("boom")))
    with pytest.raises(AlarmHubConnectionError):
        await client.async_get_alarm_hubs()


async def test_a_closed_session_raises_connection_error():
    """HA can close its shared session while the coordinator is still live.

    aiohttp raises a bare RuntimeError("Session is closed") from
    ClientSession._request -- neither ClientError nor TimeoutError -- so without
    a clause of its own it reaches HA as "Unexpected error fetching ... data"
    with a traceback. A real session, because the exception type is the point.
    """
    session = aiohttp.ClientSession()
    await session.close()
    with pytest.raises(AlarmHubConnectionError, match="Session is closed"):
        await _client(session).async_get_alarm_hubs()


async def test_cancellation_is_not_mapped_to_a_connection_error():
    # CancelledError is a BaseException, so the RuntimeError clause must not
    # reach it: shutdown depends on it unwinding the coordinator's tasks.
    client = _client(_FakeSession(exc=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await client.async_get_alarm_hubs()


# --- alarm_hub_frame() (pure helper, tier-1) ---


def test_alarm_hub_frame_returns_the_frame_for_linkstation():
    raw = '{"type": "update", "item": {"id": "x", "modelKey": "linkstation"}}'
    assert alarm_hub_frame(raw) == {
        "type": "update",
        "item": {"id": "x", "modelKey": "linkstation"},
    }


def test_alarm_hub_frame_keeps_the_delta_payload():
    # The changed fields are what the coordinator merges, so they must survive.
    raw = (
        '{"type": "update", "item": {"id": "x", "modelKey": "linkstation",'
        ' "alarmHub": {"input": {"6": {"status": "alarm"}}}}}'
    )
    frame = alarm_hub_frame(raw)
    assert frame["item"]["alarmHub"]["input"]["6"]["status"] == "alarm"


def test_alarm_hub_frame_none_for_other_models():
    for model in ("bridge", "chime", "camera", "sensor"):
        raw = '{"type": "update", "item": {"id": "x", "modelKey": "%s"}}' % model
        assert alarm_hub_frame(raw) is None


def test_alarm_hub_frame_keeps_every_frame_type():
    # add/remove/update all reach the coordinator; it decides between merging
    # the delta and taking a full snapshot.
    for ftype in ("add", "remove", "update"):
        raw = '{"type": "%s", "item": {"modelKey": "linkstation"}}' % ftype
        assert alarm_hub_frame(raw)["type"] == ftype


def test_alarm_hub_frame_none_for_malformed_json():
    assert alarm_hub_frame("not json") is None
    assert alarm_hub_frame("") is None
    assert alarm_hub_frame("{") is None


def test_alarm_hub_frame_none_for_missing_or_bad_item():
    assert alarm_hub_frame('{"type": "update"}') is None
    assert alarm_hub_frame('{"item": null}') is None
    assert alarm_hub_frame('{"item": {"id": "x"}}') is None
    assert alarm_hub_frame('"a string"') is None
    assert alarm_hub_frame("[1, 2, 3]") is None


async def test_trigger_output_posts_enable_body():
    session = _FakeSession(_FakeResp(200))
    client = _client(session)
    await client.async_trigger_output("ah1", 2, True)
    method, url, kw = session.calls[-1]
    assert method == "POST"
    assert url.endswith("/v1/alarm-hubs/ah1/outputs/2/trigger")
    assert kw["json"] == {"enable": True}
    assert kw["headers"]["X-API-KEY"] == "key"


async def test_trigger_output_succeeds_on_a_2xx_with_an_unparseable_body():
    """The siren has already fired by the time the body is read.

    Consoles answer this endpoint with an empty octet-stream, which json()
    rejects on mimetype alone -- reporting a command that worked as failed.
    """
    for json_exc in (
        aiohttp.ContentTypeError(
            _request_info(),
            (),
            status=200,
            message="unexpected mimetype: application/octet-stream",
        ),
        json.JSONDecodeError("Expecting value", "", 0),
    ):
        session = _FakeSession(_FakeResp(200, json_exc=json_exc))
        await _client(session).async_trigger_output("ah1", 2, True)


async def test_trigger_output_still_reports_a_rejected_command():
    # Skipping the body must not turn every status into success.
    with pytest.raises(AlarmHubAuthError):
        await _client(_FakeSession(_FakeResp(403))).async_trigger_output("ah1", 2, True)
    with pytest.raises(AlarmHubConnectionError):
        await _client(_FakeSession(_FakeResp(500))).async_trigger_output("ah1", 2, True)


# --- async_subscribe_devices() (fake-WS message dispatch) ---


class _FakeMsg:
    def __init__(self, type_, data=None):
        self.type = type_
        self.data = data


class _FakeWS:
    """Stands in for ClientWebSocketResponse.

    ``close_code``/``exception()`` mirror what aiohttp leaves behind after the
    read loop ends: 1000 for a close the console asked for, 1006 when the TCP
    connection just died. Both endings fall out of ``async for`` identically,
    which is why the client has to look at them.
    """

    def __init__(self, messages, close_code=aiohttp.WSCloseCode.OK, exc=None):
        self._messages = messages
        self.close_code = close_code
        self._exc = exc

    def exception(self):
        return self._exc

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
    def __init__(
        self, messages, close_code=aiohttp.WSCloseCode.OK, exc=None, ws_exc=None
    ):
        self._messages = messages
        self._close_code = close_code
        self._exc = exc
        self._ws_exc = ws_exc
        self.connect_calls = []

    def ws_connect(self, url, **kw):
        self.connect_calls.append((url, kw))
        if self._exc is not None:
            raise self._exc
        return _FakeWS(self._messages, self._close_code, self._ws_exc)


class _HangingWSSession:
    """A console that accepts the TCP connection and never finishes the upgrade."""

    def ws_connect(self, url, **kw):
        return self

    async def __aenter__(self):
        await asyncio.Event().wait()

    async def __aexit__(self, *a):
        return False


class _SlowReadSession:
    """A socket that opens at once and then reads across several loop passes.

    The steady state of a healthy subscription: the upgrade is instant, the
    stream that follows is not, and every pass through the loop is a chance for
    a deadline that was left armed to fire.
    """

    close_code = aiohttp.WSCloseCode.OK

    def __init__(self, frames=3):
        self._left = frames

    def ws_connect(self, url, **kw):
        return self

    def exception(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Suspending is what gives a live timer its chance to run.
        await asyncio.sleep(0)
        if not self._left:
            raise StopAsyncIteration
        self._left -= 1
        return _text('{"item": {"modelKey": "linkstation"}}')


class _BlockedReadSession:
    """A socket that connects and then goes quiet -- the steady state to cancel."""

    close_code = None

    def ws_connect(self, url, **kw):
        return self

    def exception(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()


def _request_info(url="https://h:443/x"):
    # aiohttp's ClientResponseError.__str__ dereferences request_info, so the
    # fakes have to carry a real one or the mapping under test never runs.
    return aiohttp.RequestInfo(URL(url), "GET", CIMultiDict(), URL(url))


def _handshake_error(status):
    return aiohttp.WSServerHandshakeError(
        _request_info(), (), status=status, message="Invalid response status"
    )


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

    frames = []
    await client.async_subscribe_devices(frames.append)

    assert len(frames) == 2
    url, kw = session.connect_calls[-1]
    assert url.endswith("/v1/subscribe/devices")
    assert kw["headers"]["X-API-KEY"] == "key"


async def test_subscribe_raises_on_error_frame():
    messages = [
        _text('{"item": {"modelKey": "linkstation"}}'),
        _FakeMsg(aiohttp.WSMsgType.ERROR),
        _text('{"item": {"modelKey": "linkstation"}}'),
    ]
    session = _WSFakeSession(messages)
    client = _client(session)

    frames = []
    with pytest.raises(AlarmHubConnectionError):
        await client.async_subscribe_devices(frames.append)

    # The frame after ERROR must not be processed; the caller reconnects.
    assert len(frames) == 1


async def test_subscribe_reports_why_a_heartbeat_timeout_dropped_the_socket():
    """aiohttp signals a missed pong as an ERROR frame, not an exception.

    Read as an ordinary close it would be logged as "closed by the console",
    which is the opposite of what happened: the console answered nothing.
    """
    timeout = aiohttp.ServerTimeoutError("No PONG received after 15.0 seconds")
    session = _WSFakeSession([_FakeMsg(aiohttp.WSMsgType.ERROR, timeout)])

    with pytest.raises(AlarmHubConnectionError, match="No PONG received"):
        await _client(session).async_subscribe_devices(lambda _frame: None)


async def test_subscribe_ignores_non_text_frames():
    messages = [
        _FakeMsg(aiohttp.WSMsgType.PING),
        _text('{"item": {"modelKey": "linkstation"}}'),
        _FakeMsg(aiohttp.WSMsgType.PONG),
    ]
    session = _WSFakeSession(messages)
    client = _client(session)

    frames = []
    await client.async_subscribe_devices(frames.append)

    assert len(frames) == 1


async def test_subscribe_sets_a_heartbeat():
    """Without pings, a socket that dies silently never errors or reconnects."""
    session = _WSFakeSession([])
    await _client(session).async_subscribe_devices(lambda _frame: None)

    _url, kw = session.connect_calls[-1]
    assert kw["heartbeat"] == WS_HEARTBEAT
    assert WS_HEARTBEAT > 0


async def test_subscribe_reports_the_connection_before_any_frame():
    # The caller resyncs on connect, so it has to land before the first delta.
    session = _WSFakeSession([_text('{"item": {"modelKey": "linkstation"}}')])
    seen = []
    await _client(session).async_subscribe_devices(
        lambda _frame: seen.append("frame"), lambda: seen.append("connected")
    )
    assert seen == ["connected", "frame"]


async def test_subscribe_without_a_connected_callback():
    session = _WSFakeSession([_text('{"item": {"modelKey": "linkstation"}}')])
    frames = []
    await _client(session).async_subscribe_devices(frames.append)
    assert len(frames) == 1


async def test_subscribe_maps_a_rejected_key_to_auth_error():
    """A revoked key on the push path must stop the retry loop, not feed it.

    aiohttp reports it as WSServerHandshakeError, which IS a ClientError, so a
    blanket connection-error mapping would hide it and the coordinator would
    retry a dead key forever instead of asking the user to reauthenticate.
    """
    for status in (401, 403):
        session = _WSFakeSession([], exc=_handshake_error(status))
        with pytest.raises(AlarmHubAuthError):
            await _client(session).async_subscribe_devices(lambda _f: None)


async def test_subscribe_maps_other_handshake_failures_to_connection_error():
    session = _WSFakeSession([], exc=_handshake_error(500))
    with pytest.raises(AlarmHubConnectionError):
        await _client(session).async_subscribe_devices(lambda _f: None)


async def test_subscribe_maps_aiohttp_errors_to_connection_error():
    # The docstring has always promised this; there was no try/except at all.
    session = _WSFakeSession([], exc=aiohttp.ClientConnectionError("no route to host"))
    with pytest.raises(AlarmHubConnectionError, match="no route"):
        await _client(session).async_subscribe_devices(lambda _f: None)


async def test_subscribe_times_out_a_handshake_that_never_finishes(monkeypatch):
    """The socket is long-lived, but opening it is not: the deadline covers only
    the upgrade, so a console that stops answering cannot park the listener."""
    monkeypatch.setattr(api, "WS_CONNECT_TIMEOUT", 0.01)
    # A deadline of the test's own, well past the one under test: without it a
    # regression that drops the connect timeout would wedge CI on this line
    # instead of reporting a failure.
    async with asyncio.timeout(5):
        with pytest.raises(AlarmHubConnectionError):
            await _client(_HangingWSSession()).async_subscribe_devices(lambda _f: None)


async def test_the_connect_deadline_stops_at_the_open_socket(monkeypatch):
    """Disarming the deadline once the socket is up is load-bearing.

    Left armed it covers the read loop too, so WS_CONNECT_TIMEOUT would cancel
    every subscription ten seconds in: the listener would raise "Timed out
    opening the WebSocket" about a socket that opened fine, reconnect, and do it
    again forever -- push permanently degraded behind a reason string pointing
    at the handshake. A deadline of zero expires on the loop's next pass, so
    this asserts the disarming instead of racing a clock.
    """
    monkeypatch.setattr(api, "WS_CONNECT_TIMEOUT", 0)
    frames = []
    await _client(_SlowReadSession()).async_subscribe_devices(frames.append)
    assert len(frames) == 3


async def test_a_closed_session_raises_connection_error_on_the_ws_path():
    # ws_connect goes through ClientSession._request, so the same bare
    # RuntimeError lands here; the reconnect loop needs it as a connection
    # error, not as the raw exception the docstring promises never escapes.
    session = aiohttp.ClientSession()
    await session.close()
    with pytest.raises(AlarmHubConnectionError, match="Session is closed"):
        await _client(session).async_subscribe_devices(lambda _f: None)


async def test_cancelling_the_listener_is_not_mapped_to_a_connection_error():
    """Shutdown cancels the WS task while it is blocked reading.

    Mapped to AlarmHubConnectionError, the reconnect loop would treat teardown
    as a dropped socket and back off instead of stopping.
    """
    connected = asyncio.Event()
    task = asyncio.create_task(
        _client(_BlockedReadSession()).async_subscribe_devices(
            lambda _f: None, connected.set
        )
    )
    await connected.wait()  # the socket is up and the reader is blocked on it
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_subscribe_raises_when_the_socket_died_rather_than_closed():
    """A console reboot ends the read loop exactly like a graceful close.

    aiohttp turns the mid-read ClientError into a CLOSED message, so without
    the close-code check the caller is told the console hung up politely.
    """
    session = _WSFakeSession(
        [_text('{"item": {"modelKey": "linkstation"}}')],
        close_code=aiohttp.WSCloseCode.ABNORMAL_CLOSURE,
    )
    frames = []
    with pytest.raises(AlarmHubConnectionError, match="1006"):
        await _client(session).async_subscribe_devices(frames.append)
    # The frames that did arrive were still delivered.
    assert len(frames) == 1


async def test_subscribe_returns_quietly_on_a_graceful_close():
    for code in (None, 0, aiohttp.WSCloseCode.OK, aiohttp.WSCloseCode.GOING_AWAY):
        session = _WSFakeSession([], close_code=code)
        await _client(session).async_subscribe_devices(lambda _f: None)


async def test_subscribe_keeps_reading_when_the_frame_handler_raises():
    """One entity's bug must not read as a network fault.

    The callback reaches HA's async_update_listeners, which has no per-listener
    guard, so an exception there would unwind the reader and drop the socket.
    """
    messages = [
        _text('{"item": {"modelKey": "linkstation", "n": %d}}' % n) for n in range(3)
    ]
    seen = []

    def handler(frame):
        seen.append(frame["item"]["n"])
        if frame["item"]["n"] == 0:
            raise RuntimeError("entity blew up")

    await _client(_WSFakeSession(messages)).async_subscribe_devices(handler)
    assert seen == [0, 1, 2]


async def test_subscribe_keeps_reading_when_the_connect_handler_raises():
    """The connect callback needs the same isolation as the frame one.

    It schedules a resync through HA, so whatever that raises would otherwise
    escape async_subscribe_devices raw -- or, once mapped, read as a socket that
    failed to open, when in fact it opened and is delivering.
    """
    session = _WSFakeSession([_text('{"item": {"modelKey": "linkstation"}}')])
    frames = []

    def on_connected():
        raise RuntimeError("resync scheduling blew up")

    await _client(session).async_subscribe_devices(frames.append, on_connected)
    assert len(frames) == 1


async def test_a_callback_that_is_cancelled_still_unwinds_the_reader():
    """The isolation stops at Exception on purpose, and only this proves it.

    Shutdown ends the read loop by cancelling the task, and HA runs both
    callbacks synchronously inside it, so a CancelledError arriving while one is
    on the stack is teardown, not a misbehaving entity. Widened to
    BaseException, the guard would log it and read on: the socket would outlive
    the cancel and async_shutdown would wait on a task that never ends.
    """

    def cancel(*_args):
        raise asyncio.CancelledError

    frame = _text('{"item": {"modelKey": "linkstation"}}')
    with pytest.raises(asyncio.CancelledError):
        await _client(_WSFakeSession([frame])).async_subscribe_devices(cancel)
    with pytest.raises(asyncio.CancelledError):
        await _client(_WSFakeSession([frame])).async_subscribe_devices(
            lambda _f: None, cancel
        )


# --- ws_close_reason() (pure helper, tier-1) ---


def test_ws_close_reason_is_none_for_a_deliberate_close():
    assert ws_close_reason(aiohttp.WSCloseCode.OK, None) is None
    assert ws_close_reason(aiohttp.WSCloseCode.GOING_AWAY, None) is None
    assert ws_close_reason(None, None) is None
    # A close frame with no payload at all parses as code 0.
    assert ws_close_reason(0, None) is None


def test_ws_close_reason_reports_an_abnormal_closure():
    assert "1006" in ws_close_reason(aiohttp.WSCloseCode.ABNORMAL_CLOSURE, None)
    assert "1011" in ws_close_reason(aiohttp.WSCloseCode.INTERNAL_ERROR, None)


def test_ws_close_reason_prefers_a_recorded_exception():
    exc = aiohttp.ServerTimeoutError("No PONG received after 15.0 seconds")
    reason = ws_close_reason(aiohttp.WSCloseCode.ABNORMAL_CLOSURE, exc)
    assert "No PONG received" in reason
