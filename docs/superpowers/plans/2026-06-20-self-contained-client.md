# Self-Contained Client Rewrite — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Remove the `uiprotect` runtime dependency. The component talks to the UniFi Protect **public integration API** through its own small `aiohttp` client, so it never conflicts with the HA-bundled uiprotect used by the official `unifiprotect` integration. Deployable on any HA today.

**Why:** HA (current stable, 2026.2.3) pins `uiprotect==10.1.0`, which has **no** alarm-hub support. The alarm-hub public API only exists in uiprotect ≥12. A custom component shares one Python env with the official integration, so forcing a newer uiprotect would break the user's cameras. A self-contained client sidesteps this entirely.

**Architecture:** `api.py` (aiohttp client, REST) → `models.py` (lightweight dataclasses parsed from the public-API JSON, mirroring the attribute names the entities use) → `logic.py` (pure predicates, now string-based) → `coordinator.py` (REST polling) → entity platforms. No WebSocket in v0.1 (REST poll every 15 s; full state per poll). API-key auth.

**Tech stack:** Python 3.13, Home Assistant 2025.1+, `aiohttp` (provided by HA), pytest. NO `uiprotect`.

**API contract (authoritative, extracted from uiprotect 13.4.0 source):**
- Base: `https://{host}:{port}/proxy/protect/integration`; header `X-API-KEY: <key>`.
- `GET /v1/alarm-hubs` → JSON list of alarm-hub objects (full state, incl. nested `alarmHub`).
- `GET /v1/meta/info` → succeeds on valid key (used for config-flow validation; we actually validate via the alarm-hubs call).
- `POST /v1/alarm-hubs/{hub_id}/outputs/{output_id}/trigger`, JSON body `{"enable": true|false}` (optional `"delay"`, `"duration"` ints ≥ 0). 401 ⇒ auth error.
- Wire JSON per hub (camelCase): `{"id","modelKey":"linkstation","name","mac","state":"CONNECTED","isAlarmHub":true,"alarmHub":{"armed":"on","battery":{"connection","charging","voltage","batteryStatus"},"cover":{"status","distance"},"input":{"1":{"enable","type","status","inputType","name","lastTriggeredAt","cameraId"}},"output":{"1":{"active","enable","status","name","delay","duration"}}}}`.

**Status of existing branch `feat/alarm-hub-v0.1`:** Tasks 1–9 of the prior plan are committed (scaffold, logic, coordinator, entity, platforms — all uiprotect-based). This plan REWRITES the data layer on the same branch.

---

## Wire-value reference (string constants — preserve casing)
- `state`: `"CONNECTED"` / `"DISCONNECTED"` / … (uppercase)
- zone `status`: `"normal"` / `"alarm"` / `"fault"` / `"short"` / `"cut"` / `"unknown"`
- zone `inputType`: `"MOTION"` / `"ENTRY"` / `"SMOKE"` / `"GLASS_BREAK"` / `"EMERGENCY_BUTTON"` / `"unknown"` (uppercase) or absent
- zone/output `enable`, output `active`, `armed`, battery `charging`: `"on"` / `"off"`
- output `status`: `"wet"` / `"dry"`
- cover `status`: `"open"` / `"close"`
- battery `connection`: `"connected"` / `"disconnected"`; `batteryStatus`: `"ok"` / `"low"` / `"critical"`

---

## Task A: `models.py` — lightweight dataclasses (TDD)

**Files:** Create `custom_components/unifi_protect_alarm_hub/models.py`; Test `tests/test_models.py`.

- [ ] **Step 1: Failing test** `tests/test_models.py`

```python
"""Tier-1 tests for JSON parsing into lightweight models (pytest only)."""

from __future__ import annotations

from custom_components.unifi_protect_alarm_hub.models import AlarmHub

RAW = {
    "id": "ah1", "modelKey": "linkstation", "name": "Alarm Hub",
    "mac": "AABBCCDDEEFF", "state": "CONNECTED", "isAlarmHub": True,
    "alarmHub": {
        "armed": "on",
        "battery": {"connection": "connected", "charging": "on",
                     "voltage": 13.2, "batteryStatus": "ok"},
        "cover": {"status": "open", "distance": 5},
        "input": {
            "1": {"enable": "on", "type": "nc", "status": "normal",
                   "inputType": "ENTRY", "name": "Front Door",
                   "lastTriggeredAt": 1700, "cameraId": "cam1"},
            "2": {"enable": "off", "type": "no", "status": "alarm"},
        },
        "output": {
            "1": {"active": "off", "enable": "on", "status": "dry",
                   "name": "Siren", "delay": 0, "duration": 30},
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
    hub = AlarmHub.from_json({
        "id": "ah2", "name": None, "mac": "X", "state": "DISCONNECTED",
        "isAlarmHub": True, "alarmHub": {},
    })
    assert hub.alarm_hub_armed is None
    assert hub.alarm_hub_battery is None
    assert hub.alarm_hub_cover is None
    assert hub.alarm_hub_inputs == {}
    assert hub.alarm_hub_outputs == {}


def test_missing_alarmhub_key():
    hub = AlarmHub.from_json({"id": "x", "mac": "Y", "state": "CONNECTED",
                               "isAlarmHub": True})
    assert hub.alarm_hub_inputs == {}
    assert hub.alarm_hub_battery is None
```

- [ ] **Step 2: Run — fails** `./.venv/bin/pytest tests/test_models.py -v` → ImportError.

- [ ] **Step 3: Implement** `custom_components/unifi_protect_alarm_hub/models.py`

```python
"""Lightweight dataclasses parsed from the UniFi Protect public-API JSON.

Pure: only stdlib. Attribute names deliberately mirror the shape the entity
layer consumes (``alarm_hub_inputs`` etc.) so platforms stay thin. All status /
type values are kept as their raw wire strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _int_keyed(raw: Any, parse) -> dict[int, Any]:
    """Map a ``{"1": {...}}`` wire dict to ``{1: parse(1, {...})}``; skip junk."""
    if not isinstance(raw, dict):
        return {}
    out: dict[int, Any] = {}
    for key, value in raw.items():
        if isinstance(key, str) and key.isdigit() and isinstance(value, dict):
            out[int(key)] = parse(int(key), value)
    return out


@dataclass(frozen=True)
class InputZone:
    zone_id: int
    enable: str | None
    type: str | None
    status: str | None
    input_type: str | None
    name: str | None
    last_triggered_at: int | None
    camera_id: str | None

    @classmethod
    def from_json(cls, zone_id: int, data: dict[str, Any]) -> InputZone:
        return cls(
            zone_id=zone_id,
            enable=data.get("enable"),
            type=data.get("type"),
            status=data.get("status"),
            input_type=data.get("inputType"),
            name=data.get("name"),
            last_triggered_at=data.get("lastTriggeredAt"),
            camera_id=data.get("cameraId"),
        )


@dataclass(frozen=True)
class OutputChannel:
    output_id: int
    active: str | None
    enable: str | None
    status: str | None
    name: str | None
    delay: int | None
    duration: int | None

    @classmethod
    def from_json(cls, output_id: int, data: dict[str, Any]) -> OutputChannel:
        return cls(
            output_id=output_id,
            active=data.get("active"),
            enable=data.get("enable"),
            status=data.get("status"),
            name=data.get("name"),
            delay=data.get("delay"),
            duration=data.get("duration"),
        )


@dataclass(frozen=True)
class Battery:
    connection: str | None
    charging: str | None
    voltage: float | None
    battery_status: str | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Battery:
        return cls(
            connection=data.get("connection"),
            charging=data.get("charging"),
            voltage=data.get("voltage"),
            battery_status=data.get("batteryStatus"),
        )


@dataclass(frozen=True)
class Cover:
    status: str | None
    distance: int | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Cover:
        return cls(status=data.get("status"), distance=data.get("distance"))


@dataclass(frozen=True)
class AlarmHub:
    id: str
    name: str | None
    mac: str
    state: str | None
    is_alarm_hub: bool
    alarm_hub_armed: str | None
    alarm_hub_battery: Battery | None
    alarm_hub_cover: Cover | None
    alarm_hub_inputs: dict[int, InputZone] = field(default_factory=dict)
    alarm_hub_outputs: dict[int, OutputChannel] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AlarmHub:
        hub = data.get("alarmHub") or {}
        battery = hub.get("battery")
        cover = hub.get("cover")
        return cls(
            id=data.get("id", ""),
            name=data.get("name"),
            mac=data.get("mac", ""),
            state=data.get("state"),
            is_alarm_hub=bool(data.get("isAlarmHub", False)),
            alarm_hub_armed=hub.get("armed"),
            alarm_hub_battery=Battery.from_json(battery) if isinstance(battery, dict) else None,
            alarm_hub_cover=Cover.from_json(cover) if isinstance(cover, dict) else None,
            alarm_hub_inputs=_int_keyed(hub.get("input"), InputZone.from_json),
            alarm_hub_outputs=_int_keyed(hub.get("output"), OutputChannel.from_json),
        )
```

- [ ] **Step 4: Run — passes.** `./.venv/bin/pytest tests/test_models.py -v` → all pass.
- [ ] **Step 5: Commit** `git add custom_components/unifi_protect_alarm_hub/models.py tests/test_models.py && git commit -m "feat: lightweight public-API models (no uiprotect)"`

---

## Task B: `logic.py` rewrite — string-based predicates on models (TDD)

**Files:** Rewrite `custom_components/unifi_protect_alarm_hub/logic.py`; rewrite `tests/test_logic.py`.

- [ ] **Step 1: Replace `tests/test_logic.py`** (build models directly; no uiprotect)

```python
"""Tier-1 pure-logic tests (pytest only)."""

from __future__ import annotations

from custom_components.unifi_protect_alarm_hub import logic
from custom_components.unifi_protect_alarm_hub.models import (
    AlarmHub, Battery, Cover, InputZone, OutputChannel,
)


def _zone(**kw) -> InputZone:
    base = dict(zone_id=1, enable="on", type="nc", status="normal",
                input_type="ENTRY", name=None, last_triggered_at=None, camera_id=None)
    base.update(kw)
    return InputZone(**base)


def _output(**kw) -> OutputChannel:
    base = dict(output_id=1, active="off", enable="on", status="dry",
                name=None, delay=None, duration=None)
    base.update(kw)
    return OutputChannel(**base)


def _hub(state="CONNECTED") -> AlarmHub:
    return AlarmHub(id="ah1", name="Hub", mac="AABBCC", state=state, is_alarm_hub=True,
                    alarm_hub_armed="on",
                    alarm_hub_battery=Battery("connected", "on", 13.2, "ok"),
                    alarm_hub_cover=Cover("open", 5),
                    alarm_hub_inputs={1: _zone()}, alarm_hub_outputs={1: _output()})


def test_zone_is_on_only_when_alarm():
    assert logic.zone_is_on(_zone(status="alarm")) is True
    assert logic.zone_is_on(_zone(status="normal")) is False
    assert logic.zone_is_on(_zone(status="fault")) is False


def test_zone_fault_is_on():
    for s in ("fault", "short", "cut"):
        assert logic.zone_fault_is_on(_zone(status=s)) is True
    for s in ("normal", "alarm"):
        assert logic.zone_fault_is_on(_zone(status=s)) is False


def test_zone_device_class_mapping():
    assert logic.zone_device_class(_zone(input_type="MOTION")) == "motion"
    assert logic.zone_device_class(_zone(input_type="ENTRY")) == "door"
    assert logic.zone_device_class(_zone(input_type="SMOKE")) == "smoke"
    assert logic.zone_device_class(_zone(input_type="GLASS_BREAK")) == "sound"
    assert logic.zone_device_class(_zone(input_type="EMERGENCY_BUTTON")) == "safety"
    assert logic.zone_device_class(_zone(input_type=None)) == "safety"
    assert logic.zone_device_class(_zone(input_type="unknown")) == "safety"


def test_zone_enabled_default():
    assert logic.zone_enabled_default(_zone(enable="on")) is True
    assert logic.zone_enabled_default(_zone(enable="off")) is False


def test_zone_name_fallback():
    assert logic.zone_name(_zone(name="Front Door"), 3) == "Front Door"
    assert logic.zone_name(_zone(name=None), 3) == "Zone 3"


def test_unique_ids():
    assert logic.zone_unique_id("M", 2) == "M_zone_2"
    assert logic.zone_fault_unique_id("M", 2) == "M_zone_2_fault"
    assert logic.output_unique_id("M", 5) == "M_output_5"


def test_output_is_on_and_name():
    assert logic.output_is_on(_output(active="on")) is True
    assert logic.output_is_on(_output(active="off")) is False
    assert logic.output_name(_output(name="Siren"), 1) == "Siren"
    assert logic.output_name(_output(name=None), 1) == "Output 1"


def test_hub_predicates():
    assert logic.hub_is_connected(_hub("CONNECTED")) is True
    assert logic.hub_is_connected(_hub("DISCONNECTED")) is False
    assert logic.armed_is_on("on") is True
    assert logic.armed_is_on("off") is False
    assert logic.armed_is_on(None) is False
    assert logic.cover_is_on(Cover("open", 0)) is True
    assert logic.cover_is_on(Cover("close", 0)) is False
    assert logic.cover_is_on(None) is False
    assert logic.battery_connected_is_on(Battery("connected", None, None, "ok")) is True
    assert logic.battery_connected_is_on(Battery("disconnected", None, None, None)) is False
    assert logic.battery_connected_is_on(None) is False
```

- [ ] **Step 2: Run — fails** (logic still imports uiprotect). `./.venv/bin/pytest tests/test_logic.py -v`

- [ ] **Step 3: Replace `logic.py`**

```python
"""Pure derivation logic. No Home Assistant, no uiprotect — stdlib + local
models only — so it is unit-testable with plain pytest. device_class values are
plain strings matching HA's BinarySensorDeviceClass values; the entity layer
wraps them.
"""

from __future__ import annotations

from .models import AlarmHub, Battery, Cover, InputZone, OutputChannel

ZONE_FAULT_STATUSES = {"fault", "short", "cut"}

ZONE_DEVICE_CLASS: dict[str, str] = {
    "MOTION": "motion",
    "ENTRY": "door",
    "SMOKE": "smoke",
    "GLASS_BREAK": "sound",
    "EMERGENCY_BUTTON": "safety",
}
DEFAULT_ZONE_DEVICE_CLASS = "safety"


def zone_is_on(zone: InputZone) -> bool:
    """True when the zone is triggered (status == 'alarm')."""
    return zone.status == "alarm"


def zone_fault_is_on(zone: InputZone) -> bool:
    """True when the zone wiring is faulted (fault/short/cut)."""
    return zone.status in ZONE_FAULT_STATUSES


def zone_device_class(zone: InputZone) -> str:
    """Map a zone's input_type to an HA binary_sensor device_class string."""
    if zone.input_type is None:
        return DEFAULT_ZONE_DEVICE_CLASS
    return ZONE_DEVICE_CLASS.get(zone.input_type, DEFAULT_ZONE_DEVICE_CLASS)


def zone_enabled_default(zone: InputZone) -> bool:
    """Whether this zone's entities are enabled by default (enable == 'on')."""
    return zone.enable == "on"


def zone_name(zone: InputZone, zone_id: int) -> str:
    """Zone name, or 'Zone {id}' when unnamed."""
    return zone.name or f"Zone {zone_id}"


def zone_unique_id(mac: str, zone_id: int) -> str:
    return f"{mac}_zone_{zone_id}"


def zone_fault_unique_id(mac: str, zone_id: int) -> str:
    return f"{mac}_zone_{zone_id}_fault"


def output_unique_id(mac: str, output_id: int) -> str:
    return f"{mac}_output_{output_id}"


def output_is_on(output: OutputChannel) -> bool:
    """True when the output relay is energised (active == 'on')."""
    return output.active == "on"


def output_name(output: OutputChannel, output_id: int) -> str:
    """Output name, or 'Output {id}' when unnamed."""
    return output.name or f"Output {output_id}"


def hub_is_connected(hub: AlarmHub) -> bool:
    """True when the hub's connection state is CONNECTED."""
    return hub.state == "CONNECTED"


def armed_is_on(armed: str | None) -> bool:
    return armed == "on"


def cover_is_on(cover: Cover | None) -> bool:
    """True when the tamper cover is open."""
    return cover is not None and cover.status == "open"


def battery_connected_is_on(battery: Battery | None) -> bool:
    """True when the backup battery is connected."""
    return battery is not None and battery.connection == "connected"
```

- [ ] **Step 4: Run — passes.** `./.venv/bin/pytest tests/test_logic.py tests/test_models.py -v`
- [ ] **Step 5: Commit** `git add custom_components/unifi_protect_alarm_hub/logic.py tests/test_logic.py && git commit -m "refactor: logic predicates operate on local models, drop uiprotect"`

---

## Task C: `api.py` — self-contained aiohttp client (TDD)

**Files:** Create `custom_components/unifi_protect_alarm_hub/api.py`; Test `tests/test_api.py`.

- [ ] **Step 1: Failing test** `tests/test_api.py` (mock aiohttp session)

```python
"""Tier-1 tests for the API client using a fake aiohttp session."""

from __future__ import annotations

import pytest

from custom_components.unifi_protect_alarm_hub.api import (
    AlarmHubApiClient, AlarmHubAuthError, AlarmHubConnectionError,
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
    payload = [{"id": "ah1", "mac": "M", "state": "CONNECTED", "isAlarmHub": True,
                "alarmHub": {"input": {"1": {"status": "alarm"}}}}]
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
```

Add an asyncio marker config so `async def` tests run. In `tests/conftest.py` (created in Task F) we enable `asyncio_mode`. For now, also acceptable: these run under pytest-homeassistant-custom-component's asyncio support once Task F lands. To run them standalone before then, install `pytest-asyncio` and add `pytestmark = pytest.mark.asyncio` — but DEFER running test_api.py until Task F sets `asyncio_mode = auto`. Implement the client now; run its tests in Task F's full-suite step.

- [ ] **Step 2: Implement** `custom_components/unifi_protect_alarm_hub/api.py`

```python
"""Minimal async client for the UniFi Protect public integration API.

Talks to ``/proxy/protect/integration/v1`` with an ``X-API-KEY`` header. No
uiprotect dependency, so it never conflicts with the HA-bundled uiprotect used
by the official integration. The caller supplies an ``aiohttp.ClientSession``
already configured for SSL verification (HA's ``async_get_clientsession``).
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .models import AlarmHub

_LOGGER = logging.getLogger(__name__)


class AlarmHubAuthError(Exception):
    """Invalid or revoked API key (HTTP 401/403)."""


class AlarmHubConnectionError(Exception):
    """Network failure or non-auth error talking to the console."""


class AlarmHubApiClient:
    """Tiny REST client for the Protect public alarm-hub endpoints."""

    def __init__(
        self,
        host: str,
        port: int,
        api_key: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._base = f"https://{host}:{port}/proxy/protect/integration"
        self._headers = {"X-API-KEY": api_key}
        self._session = session

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._base}{path}"
        try:
            async with self._session.request(
                method, url, headers=self._headers, **kwargs
            ) as resp:
                if resp.status in (401, 403):
                    raise AlarmHubAuthError(f"Auth failed ({resp.status})")
                if resp.status >= 400:
                    raise AlarmHubConnectionError(f"HTTP {resp.status} from {path}")
                if resp.status == 204:
                    return None
                return await resp.json()
        except aiohttp.ClientError as err:
            raise AlarmHubConnectionError(str(err)) from err

    async def async_get_alarm_hubs(self) -> list[AlarmHub]:
        """Return all adopted alarm hubs with full current state."""
        data = await self._request("GET", "/v1/alarm-hubs")
        if not isinstance(data, list):
            return []
        return [AlarmHub.from_json(item) for item in data if isinstance(item, dict)]

    async def async_trigger_output(
        self, hub_id: str, output_id: int, enable: bool
    ) -> None:
        """Trigger (enable=True) or clear (enable=False) an output channel."""
        await self._request(
            "POST",
            f"/v1/alarm-hubs/{hub_id}/outputs/{output_id}/trigger",
            json={"enable": enable},
        )
```

- [ ] **Step 3: Commit** `git add custom_components/unifi_protect_alarm_hub/api.py tests/test_api.py && git commit -m "feat: self-contained aiohttp client for Protect public API"`

---

## Task D: rewrite `coordinator.py` + adapt `entity.py`

**Files:** Rewrite `coordinator.py`; edit `entity.py`.

- [ ] **Step 1: Replace `coordinator.py`**

```python
"""Polling data update coordinator for the UniFi Protect Alarm Hub."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AlarmHubApiClient, AlarmHubAuthError, AlarmHubConnectionError
from .const import DOMAIN, SCAN_INTERVAL
from .models import AlarmHub

_LOGGER = logging.getLogger(__name__)


class AlarmHubCoordinator(DataUpdateCoordinator[dict[str, AlarmHub]]):
    """Polls the Protect public API for alarm-hub state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AlarmHubApiClient,
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.entry = entry
        self.client = client

    async def _async_update_data(self) -> dict[str, AlarmHub]:
        try:
            hubs = await self.client.async_get_alarm_hubs()
        except AlarmHubAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AlarmHubConnectionError as err:
            raise UpdateFailed(f"Error talking to UniFi Protect: {err}") from err
        return {hub.id: hub for hub in hubs if hub.is_alarm_hub}
```

- [ ] **Step 2: Edit `entity.py`** — change the model import/type only. Replace the line `from uiprotect.data.public_devices import LinkStation` with `from .models import AlarmHub`, and change the `hub` property return annotation from `LinkStation | None` to `AlarmHub | None`. Everything else (DeviceInfo, `_attr_has_entity_name`, `available`) is unchanged.

- [ ] **Step 3: Lint** `./.venv/bin/ruff check custom_components/unifi_protect_alarm_hub/coordinator.py custom_components/unifi_protect_alarm_hub/entity.py` and `ruff format` them.
- [ ] **Step 4: Commit** `git add custom_components/unifi_protect_alarm_hub/coordinator.py custom_components/unifi_protect_alarm_hub/entity.py && git commit -m "refactor: coordinator polls self-contained client; entity uses local model"`

---

## Task E: adapt platforms (`switch.py`, `sensor.py`; verify `binary_sensor.py`)

**Files:** Edit `switch.py`, `sensor.py`; verify `binary_sensor.py`.

The platforms already use the mirrored attribute names (`hub.alarm_hub_inputs`, `zone.status`, `output.active`, `hub.alarm_hub_battery`, `logic.*`), so most needs no change. Apply these specific edits:

- [ ] **Step 1: `switch.py`** — outputs now trigger via the coordinator's client, not the model. Replace the two service methods:

```python
    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.client.async_trigger_output(self._hub_id, self._output_id, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.client.async_trigger_output(self._hub_id, self._output_id, False)
        await self.coordinator.async_request_refresh()
```

(`self._hub_id` exists on `AlarmHubBaseEntity`. The hub-None guard is no longer needed since we no longer dereference `self.hub`.) Remove the now-unused `Any` import only if it becomes unused — it is still used in the signatures, so keep it.

- [ ] **Step 2: `sensor.py`** — `BatteryStatusSensor.native_value` previously returned `status.value`; battery_status is now a plain string. Change:

```python
    @property
    def native_value(self) -> str | None:
        hub = self.hub
        if hub is None or hub.alarm_hub_battery is None:
            return None
        return hub.alarm_hub_battery.battery_status
```

(`BatteryVoltageSensor.native_value` already returns `hub.alarm_hub_battery.voltage` — unchanged.)

- [ ] **Step 3: Verify `binary_sensor.py`** needs NO change — it already reads `hub.alarm_hub_inputs`, `zone.status`/`zone.type`/`zone.input_type`/`zone.last_triggered_at`/`zone.camera_id`, `hub.alarm_hub_cover`, `logic.hub_is_connected(hub)`, `hub.alarm_hub_battery`. Confirm there are no remaining `.value` calls or uiprotect references in the three platform files: `grep -rn "uiprotect\|\.value\b" custom_components/unifi_protect_alarm_hub/{binary_sensor,switch,sensor}.py` should return nothing.

- [ ] **Step 4: Lint** `./.venv/bin/ruff check custom_components/unifi_protect_alarm_hub/{binary_sensor,switch,sensor}.py` + `ruff format`.
- [ ] **Step 5: Commit** `git add custom_components/unifi_protect_alarm_hub/{switch,sensor,binary_sensor}.py && git commit -m "refactor: platforms use local models and client trigger"`

---

## Task F: rewrite `config_flow.py` + `__init__.py`, metadata, Tier-2 tests

**Files:** Rewrite `config_flow.py`, `__init__.py`; edit `const.py`, `manifest.json`, `requirements_test.txt`; create `tests/conftest.py`; rewrite `tests/test_config_flow.py`, `tests/test_init.py`.

- [ ] **Step 1: `const.py`** — change `SCAN_INTERVAL = timedelta(minutes=5)` to `SCAN_INTERVAL = timedelta(seconds=15)` (polling is now primary). Keep everything else.

- [ ] **Step 2: `manifest.json`** — set `"requirements": []` and `"iot_class": "local_polling"`. (No uiprotect dependency; HA provides aiohttp.)

- [ ] **Step 3: `requirements_test.txt`** — replace `uiprotect>=13.0,<14` line; file becomes:
```
pytest
pytest-homeassistant-custom-component
ruff
```

- [ ] **Step 4: `tests/conftest.py`** — enable asyncio + custom integrations:

```python
"""Shared Tier-2 fixtures."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
```

Also add, in `tests/` (a `pytest.ini` or `pyproject` is overkill) — set asyncio mode via the existing root `conftest.py` is not enough; instead add a `tests/pytest.ini`-free approach: create `pyproject.toml` at repo root with:
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```
(pytest-homeassistant-custom-component depends on pytest-asyncio; `asyncio_mode = "auto"` makes the `async def` api/config/init tests run without per-test markers.)

- [ ] **Step 5: Rewrite `config_flow.py`**

```python
"""Config flow for the UniFi Protect Alarm Hub."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AlarmHubApiClient, AlarmHubAuthError, AlarmHubConnectionError
from .const import DEFAULT_PORT, DEFAULT_VERIFY_SSL, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_API_KEY): str,
        vol.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
    }
)


class AlarmHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for UniFi Protect Alarm Hub."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(
                self.hass, verify_ssl=user_input[CONF_VERIFY_SSL]
            )
            client = AlarmHubApiClient(
                user_input[CONF_HOST], user_input[CONF_PORT],
                user_input[CONF_API_KEY], session,
            )
            try:
                hubs = await client.async_get_alarm_hubs()
            except AlarmHubAuthError:
                errors["base"] = "invalid_auth"
            except AlarmHubConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating UniFi Protect")
                errors["base"] = "unknown"
            else:
                if not hubs:
                    _LOGGER.warning(
                        "Connected but no alarm hub is adopted; no entities created"
                    )
                await self.async_set_unique_id(
                    f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="UniFi Protect Alarm Hub", data=user_input
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )
```

- [ ] **Step 6: Rewrite `__init__.py`**

```python
"""The UniFi Protect Alarm Hub integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AlarmHubApiClient
from .const import PLATFORMS
from .coordinator import AlarmHubCoordinator

type AlarmHubConfigEntry = ConfigEntry[AlarmHubCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AlarmHubConfigEntry) -> bool:
    """Set up UniFi Protect Alarm Hub from a config entry."""
    session = async_get_clientsession(hass, verify_ssl=entry.data[CONF_VERIFY_SSL])
    client = AlarmHubApiClient(
        entry.data[CONF_HOST], entry.data[CONF_PORT],
        entry.data[CONF_API_KEY], session,
    )
    coordinator = AlarmHubCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AlarmHubConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
```

(HA owns the shared aiohttp session, so there is nothing to close on unload.)

- [ ] **Step 7: Rewrite `tests/test_config_flow.py`** (mock our client)

```python
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.data_entry_flow import FlowResultType

from custom_components.unifi_protect_alarm_hub.api import (
    AlarmHubAuthError, AlarmHubConnectionError,
)
from custom_components.unifi_protect_alarm_hub.const import DOMAIN

USER_INPUT = {CONF_HOST: "192.168.0.103", CONF_PORT: 443,
              CONF_API_KEY: "k", CONF_VERIFY_SSL: False}


async def _run(hass, side_effect=None, return_value=None):
    with patch(
        "custom_components.unifi_protect_alarm_hub.config_flow.AlarmHubApiClient"
    ) as cls:
        cls.return_value.async_get_alarm_hubs = AsyncMock(
            side_effect=side_effect, return_value=return_value or []
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )


async def test_user_flow_success(hass):
    result = await _run(hass, return_value=[object()])
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "192.168.0.103"


async def test_user_flow_invalid_auth(hass):
    result = await _run(hass, side_effect=AlarmHubAuthError("bad"))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass):
    result = await _run(hass, side_effect=AlarmHubConnectionError("down"))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
```

- [ ] **Step 8: Rewrite `tests/test_init.py`**

```python
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_protect_alarm_hub.const import DOMAIN


async def test_setup_and_unload(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "h", CONF_PORT: 443, CONF_API_KEY: "k", CONF_VERIFY_SSL: False},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.unifi_protect_alarm_hub.AlarmHubApiClient"
    ) as cls:
        cls.return_value.async_get_alarm_hubs = AsyncMock(return_value=[])
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data is not None
        assert await hass.config_entries.async_unload(entry.entry_id)
```

- [ ] **Step 9: Install harness if absent, then run the WHOLE suite from repo root**
`./.venv/bin/pip install pytest-homeassistant-custom-component` (if not present), then `./.venv/bin/pytest -v`. Expected: test_models, test_logic, test_api, test_config_flow (3), test_init (1) all pass.
- [ ] **Step 10: Lint** `./.venv/bin/ruff check custom_components tests && ./.venv/bin/ruff format --check custom_components tests`.
- [ ] **Step 11: Commit** `git add -A && git commit -m "feat: self-contained config flow + setup; drop uiprotect dependency"`

---

## Task G: cleanup + README + final validation

- [ ] **Step 1:** Confirm no `uiprotect` references remain anywhere: `grep -rn "uiprotect" custom_components tests` returns nothing. Confirm `requirements` in manifest is `[]`.
- [ ] **Step 2:** Update `README.md` — note REST polling (15 s) with WebSocket as a planned enhancement; entities list unchanged; under "Status" note it is self-contained (no uiprotect dependency, runs alongside the official UniFi Protect integration).
- [ ] **Step 3:** `./.venv/bin/ruff check custom_components tests && ./.venv/bin/ruff format --check custom_components tests` clean; `./.venv/bin/pytest -v` all green.
- [ ] **Step 4:** `git add -A && git commit -m "docs: README for self-contained polling design; final cleanup"`

---

## Self-review notes
- **Removed dependency:** manifest `requirements: []`; no `uiprotect`/`homeassistant` import in `models.py`/`logic.py`/`api.py` (api.py imports aiohttp only, which is fine — it's HA-provided and the tests fake the session).
- **Coverage:** models parsing (Task A), all predicates (Task B), client REST + error mapping (Task C), config-flow auth/connect/success + setup/unload (Task F).
- **§9 assumptions still isolated:** zone enabled-default (`enable=='on'`), output on (`active=='on'`) — one-line edits in `logic.py`.
- **Deferred:** WebSocket push (documented); REST poll at 15 s is the v0.1 baseline.
```
