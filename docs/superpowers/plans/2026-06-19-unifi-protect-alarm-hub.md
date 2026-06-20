# UniFi Protect Alarm Hub Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone HACS custom component (`unifi_protect_alarm_hub`) that exposes UniFi Protect Alarm Hub input zones as `binary_sensor` entities, output channels as `switch` entities, and hub status as diagnostic entities.

**Architecture:** A `DataUpdateCoordinator` wraps one `ProtectApiClient.public_only(...)`. Initial state comes from `update_public()`; real-time updates arrive via `subscribe_devices_websocket()` (the library mutates `public_bootstrap` in place, then our callback pushes a fresh snapshot). All derivation logic lives in a pure, HA-free `logic.py` (unit-tested with plain pytest); HA entity classes are thin wrappers.

**Tech Stack:** Python 3.13, Home Assistant 2025.1+, `uiprotect>=13.0,<14`, pytest. The alarm hub is a public-API `LinkStation` with `is_alarm_hub=True`; auth is **API-key only** (`update_public()`).

**Reference spec:** `docs/superpowers/specs/2026-06-19-unifi-protect-alarm-hub-design.md`

---

## Testing strategy (two tiers)

- **Tier 1 — pure logic (`tests/test_logic.py`):** runs with only `pytest` + `uiprotect`. Constructs real `uiprotect` model objects from string kwargs and asserts on `logic.py`. This is where correctness lives and **must pass on the dev machine**.
- **Tier 2 — HA harness (`tests/test_config_flow.py`, `tests/test_init.py`):** uses `pytest-homeassistant-custom-component`. Validates config flow and setup/unload. Included and runnable once that harness installs.

Dev environment (run once at start of Task 1):
```bash
cd "/Users/hughfindlay/Library/Mobile Documents/com~apple~CloudDocs/Coding/MacMini/unifi-protect-alarm-hub"
python3.13 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install "uiprotect>=13.0,<14" pytest ruff
# Tier-2 (may take a few minutes; HA is large):
./.venv/bin/pip install pytest-homeassistant-custom-component
```
All `pytest`/`ruff` commands below assume `./.venv/bin/` is the venv (`.venv/` is gitignored).

---

## File structure

```
custom_components/unifi_protect_alarm_hub/
├── __init__.py          # async_setup_entry / async_unload_entry
├── manifest.json        # HACS/HA metadata
├── const.py             # DOMAIN, PLATFORMS, defaults, MANUFACTURER, MODEL, SCAN_INTERVAL
├── logic.py             # PURE derivation logic (no HA imports)
├── coordinator.py       # AlarmHubCoordinator + WS subscription
├── config_flow.py       # user + reauth flows
├── entity.py            # AlarmHubBaseEntity (device info, live hub lookup)
├── binary_sensor.py     # zone state, zone fault, tamper, armed, connectivity, battery-connection
├── switch.py            # output channels
├── sensor.py            # battery status, battery voltage
├── strings.json
└── translations/en.json
tests/
├── test_logic.py        # Tier 1
├── test_config_flow.py  # Tier 2
└── test_init.py         # Tier 2
hacs.json
README.md
.github/workflows/ci.yml
```

---

## Task 1: Repo scaffolding & metadata

**Files:**
- Create: `custom_components/unifi_protect_alarm_hub/manifest.json`
- Create: `custom_components/unifi_protect_alarm_hub/const.py`
- Create: `custom_components/unifi_protect_alarm_hub/__init__.py` (stub)
- Create: `hacs.json`, `README.md`, `.github/workflows/ci.yml`, `requirements_test.txt`

- [ ] **Step 1: Create the dev venv** (commands in "Dev environment" above). Verify: `./.venv/bin/python -c "import uiprotect, pytest"` exits 0.

- [ ] **Step 2: Write `manifest.json`**

```json
{
  "domain": "unifi_protect_alarm_hub",
  "name": "UniFi Protect Alarm Hub",
  "codeowners": ["@hughwf"],
  "config_flow": true,
  "dependencies": [],
  "documentation": "https://github.com/hughwf/hass-unifi-protect-alarm-hub",
  "integration_type": "hub",
  "iot_class": "local_push",
  "issue_tracker": "https://github.com/hughwf/hass-unifi-protect-alarm-hub/issues",
  "requirements": ["uiprotect>=13.0,<14"],
  "version": "0.1.0"
}
```

- [ ] **Step 3: Write `const.py`**

```python
"""Constants for the UniFi Protect Alarm Hub integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "unifi_protect_alarm_hub"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SWITCH,
]

DEFAULT_PORT = 443
DEFAULT_VERIFY_SSL = False

# Safety-net resync; real-time updates arrive via the devices WebSocket.
SCAN_INTERVAL = timedelta(minutes=5)

MANUFACTURER = "Ubiquiti"
MODEL = "Alarm Hub"
```

- [ ] **Step 4: Write `__init__.py` stub** (replaced in Task 11; lets HA import the component)

```python
"""The UniFi Protect Alarm Hub integration."""

from __future__ import annotations
```

- [ ] **Step 5: Write `hacs.json`**

```json
{
  "name": "UniFi Protect Alarm Hub",
  "content_in_root": false,
  "render_readme": true,
  "homeassistant": "2025.1.0"
}
```

- [ ] **Step 6: Write `requirements_test.txt`**

```
uiprotect>=13.0,<14
pytest
ruff
```

- [ ] **Step 7: Write `.github/workflows/ci.yml`**

```yaml
name: CI
on:
  push:
  pull_request:
jobs:
  lint-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: pip install -r requirements_test.txt
      - run: ruff check custom_components tests
      - run: ruff format --check custom_components tests
      - run: pytest tests/test_logic.py -v
```

- [ ] **Step 8: Write `README.md`** (minimal; finalized in Task 12)

```markdown
# UniFi Protect Alarm Hub (Home Assistant)

Exposes a UniFi Protect **Alarm Hub** — wired input zones as `binary_sensor`
entities and output channels as `switch` entities — which the official
`unifiprotect` integration does not create. Runs side by side with the official
integration.

> **v0.1 — needs real-world validation against hardware.** Built against the
> `uiprotect` 13.x data model. Please file issues with debug logs.

## Install (HACS custom repository)
1. HACS → ⋮ → Custom repositories → add `https://github.com/hughwf/hass-unifi-protect-alarm-hub`, category **Integration**.
2. Install "UniFi Protect Alarm Hub", restart Home Assistant.
3. Settings → Devices & Services → Add Integration → "UniFi Protect Alarm Hub".

## Configuration
- **Host** — IP/hostname of the UniFi OS console running Protect
- **Port** — default 443
- **API key** — UniFi OS → Settings → Control Plane → Integrations
- **Verify SSL** — default off (self-signed console certs)
```

- [ ] **Step 9: Verify metadata is valid JSON**

Run:
```bash
./.venv/bin/python -c "import json,pathlib; [json.loads(pathlib.Path(p).read_text()) for p in ['custom_components/unifi_protect_alarm_hub/manifest.json','hacs.json']]; print('json ok')"
```
Expected: `json ok`

- [ ] **Step 10: Commit**

```bash
git add -A && git commit -m "feat: scaffold unifi_protect_alarm_hub component metadata"
```

---

## Task 2: `logic.py` — zone state & fault predicates (TDD)

**Files:**
- Create: `custom_components/unifi_protect_alarm_hub/logic.py`
- Test: `tests/test_logic.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_logic.py
"""Tier-1 pure-logic tests: only pytest + uiprotect required."""

from __future__ import annotations

from uiprotect.data.public_devices import AlarmHubInput, AlarmHubOutput, LinkStation
from custom_components.unifi_protect_alarm_hub import logic


def _zone(**kw) -> AlarmHubInput:
    base = {"enable": "on", "type": "nc", "status": "normal", "input_type": "ENTRY"}
    base.update(kw)
    return AlarmHubInput(**base)


def test_zone_is_on_only_when_alarm():
    assert logic.zone_is_on(_zone(status="alarm")) is True
    assert logic.zone_is_on(_zone(status="normal")) is False
    assert logic.zone_is_on(_zone(status="fault")) is False


def test_zone_fault_is_on_for_wire_faults():
    assert logic.zone_fault_is_on(_zone(status="fault")) is True
    assert logic.zone_fault_is_on(_zone(status="short")) is True
    assert logic.zone_fault_is_on(_zone(status="cut")) is True
    assert logic.zone_fault_is_on(_zone(status="normal")) is False
    assert logic.zone_fault_is_on(_zone(status="alarm")) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_logic.py -v`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError: module 'logic' has no attribute 'zone_is_on'`

- [ ] **Step 3: Write minimal implementation**

```python
# custom_components/unifi_protect_alarm_hub/logic.py
"""Pure derivation logic for the alarm hub.

NO Home Assistant imports — only ``uiprotect`` + stdlib — so this module is
unit-testable with plain pytest. device_class values are returned as plain
strings matching Home Assistant's BinarySensorDeviceClass values; the entity
layer wraps them.
"""

from __future__ import annotations

from uiprotect.data.public_devices import AlarmHubInput, AlarmHubOutput
from uiprotect.data.types import AlarmHubInputStatus, OnOffState

ZONE_FAULT_STATUSES = {
    AlarmHubInputStatus.FAULT,
    AlarmHubInputStatus.SHORT,
    AlarmHubInputStatus.CUT,
}


def zone_is_on(zone: AlarmHubInput) -> bool:
    """True when the zone is triggered (status == alarm)."""
    return zone.status == AlarmHubInputStatus.ALARM


def zone_fault_is_on(zone: AlarmHubInput) -> bool:
    """True when the zone wiring is faulted (fault/short/cut)."""
    return zone.status in ZONE_FAULT_STATUSES
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/pytest tests/test_logic.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add custom_components/unifi_protect_alarm_hub/logic.py tests/test_logic.py
git commit -m "feat: zone state and fault predicates"
```

---

## Task 3: `logic.py` — device-class, enabled-default, names, unique-ids (TDD)

**Files:**
- Modify: `custom_components/unifi_protect_alarm_hub/logic.py`
- Test: `tests/test_logic.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_logic.py`)

```python
def test_zone_device_class_mapping():
    assert logic.zone_device_class(_zone(input_type="MOTION")) == "motion"
    assert logic.zone_device_class(_zone(input_type="ENTRY")) == "door"
    assert logic.zone_device_class(_zone(input_type="SMOKE")) == "smoke"
    assert logic.zone_device_class(_zone(input_type="GLASS_BREAK")) == "sound"
    assert logic.zone_device_class(_zone(input_type="EMERGENCY_BUTTON")) == "safety"


def test_zone_device_class_defaults_to_safety():
    # input_type omitted -> None on the model
    z = AlarmHubInput(enable="on", type="nc", status="normal")
    assert logic.zone_device_class(z) == "safety"


def test_zone_enabled_default_follows_enable_flag():
    assert logic.zone_enabled_default(_zone(enable="on")) is True
    assert logic.zone_enabled_default(_zone(enable="off")) is False


def test_zone_name_falls_back_to_zone_id():
    assert logic.zone_name(_zone(name="Front Door"), 3) == "Front Door"
    assert logic.zone_name(_zone(name=None), 3) == "Zone 3"


def test_unique_ids():
    assert logic.zone_unique_id("AABBCC", 2) == "AABBCC_zone_2"
    assert logic.zone_fault_unique_id("AABBCC", 2) == "AABBCC_zone_2_fault"
    assert logic.output_unique_id("AABBCC", 5) == "AABBCC_output_5"
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/pytest tests/test_logic.py -v`
Expected: FAIL — missing `zone_device_class` etc.

- [ ] **Step 3: Implement** (append to `logic.py`; add the import line at top)

Add to the imports at the top of `logic.py`:
```python
from uiprotect.data.types import AlarmHubInputStatus, AlarmHubInputType, OnOffState
```

Append:
```python
ZONE_DEVICE_CLASS: dict[AlarmHubInputType, str] = {
    AlarmHubInputType.MOTION: "motion",
    AlarmHubInputType.ENTRY: "door",
    AlarmHubInputType.SMOKE: "smoke",
    AlarmHubInputType.GLASS_BREAK: "sound",
    AlarmHubInputType.EMERGENCY_BUTTON: "safety",
}
DEFAULT_ZONE_DEVICE_CLASS = "safety"


def zone_device_class(zone: AlarmHubInput) -> str:
    """Map a zone's input_type to a Home Assistant binary_sensor device_class."""
    if zone.input_type is None:
        return DEFAULT_ZONE_DEVICE_CLASS
    return ZONE_DEVICE_CLASS.get(zone.input_type, DEFAULT_ZONE_DEVICE_CLASS)


def zone_enabled_default(zone: AlarmHubInput) -> bool:
    """Whether this zone's entities should be enabled by default."""
    return zone.enable == OnOffState.ON


def zone_name(zone: AlarmHubInput, zone_id: int) -> str:
    return zone.name or f"Zone {zone_id}"


def zone_unique_id(mac: str, zone_id: int) -> str:
    return f"{mac}_zone_{zone_id}"


def zone_fault_unique_id(mac: str, zone_id: int) -> str:
    return f"{mac}_zone_{zone_id}_fault"


def output_unique_id(mac: str, output_id: int) -> str:
    return f"{mac}_output_{output_id}"
```

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/bin/pytest tests/test_logic.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add custom_components/unifi_protect_alarm_hub/logic.py tests/test_logic.py
git commit -m "feat: zone device-class, naming and unique-id helpers"
```

---

## Task 4: `logic.py` — output & hub-diagnostic predicates (TDD)

**Files:**
- Modify: `custom_components/unifi_protect_alarm_hub/logic.py`
- Test: `tests/test_logic.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
def _output(**kw) -> AlarmHubOutput:
    base = {"active": "off", "enable": "on", "status": "dry"}
    base.update(kw)
    return AlarmHubOutput(**base)


def test_output_is_on_from_active():
    assert logic.output_is_on(_output(active="on")) is True
    assert logic.output_is_on(_output(active="off")) is False


def test_output_name_fallback():
    assert logic.output_name(_output(name="Siren"), 1) == "Siren"
    assert logic.output_name(_output(name=None), 1) == "Output 1"


def _hub() -> LinkStation:
    raw = {
        "id": "ah1", "modelKey": "linkstation", "state": "CONNECTED",
        "name": "Alarm Hub", "mac": "AABBCCDDEEFF", "isAlarmHub": True,
        "ledSettings": {"isEnabled": True},
        "alarmHub": {
            "armed": "on",
            "battery": {"connection": "connected", "charging": "on",
                        "voltage": 13.2, "batteryStatus": "ok"},
            "cover": {"status": "open", "distance": 5},
            "input": {"1": {"enable": "on", "type": "nc", "status": "normal",
                            "inputType": "ENTRY", "name": "Front Door"}},
            "output": {"1": {"active": "off", "enable": "on", "status": "dry",
                             "name": "Siren"}},
        },
    }
    return LinkStation.from_unifi_dict(**raw)


def test_armed_is_on():
    assert logic.armed_is_on(_hub().alarm_hub_armed) is True


def test_cover_is_on_when_open():
    assert logic.cover_is_on(_hub().alarm_hub_cover) is True


def test_battery_connected_is_on():
    assert logic.battery_connected_is_on(_hub().alarm_hub_battery) is True


def test_snapshot_extracts_only_alarm_hubs():
    # a fake bootstrap exposing .alarm_hubs
    class FakeBootstrap:
        alarm_hubs = {"ah1": _hub()}
    snap = logic.snapshot(FakeBootstrap())
    assert set(snap) == {"ah1"}
    assert snap["ah1"].is_alarm_hub is True
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/pytest tests/test_logic.py -v`
Expected: FAIL — missing `output_is_on` etc.

- [ ] **Step 3: Implement** (append to `logic.py`; extend the types import)

Replace the types import line with:
```python
from uiprotect.data.types import (
    AlarmHubConnectionState,
    AlarmHubCoverStatus,
    AlarmHubInputStatus,
    AlarmHubInputType,
    OnOffState,
)
```

Append:
```python
def output_is_on(output: AlarmHubOutput) -> bool:
    return output.active == OnOffState.ON


def output_name(output: AlarmHubOutput, output_id: int) -> str:
    return output.name or f"Output {output_id}"


def armed_is_on(armed: OnOffState | None) -> bool:
    return armed == OnOffState.ON


def cover_is_on(cover) -> bool:
    """True when the tamper cover is open."""
    return cover is not None and cover.status == AlarmHubCoverStatus.OPEN


def battery_connected_is_on(battery) -> bool:
    """True when the backup battery is connected."""
    return battery is not None and battery.connection == AlarmHubConnectionState.CONNECTED


def snapshot(public_bootstrap) -> dict:
    """Return a plain ``{hub_id: LinkStation}`` dict of alarm hubs only."""
    return dict(public_bootstrap.alarm_hubs)
```

Also add `LinkStation` and `AlarmHubOutput` to the `public_devices` import at the top:
```python
from uiprotect.data.public_devices import AlarmHubInput, AlarmHubOutput, LinkStation
```

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/bin/pytest tests/test_logic.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add custom_components/unifi_protect_alarm_hub/logic.py tests/test_logic.py
git commit -m "feat: output and hub-diagnostic predicates + snapshot helper"
```

---

## Task 5: `coordinator.py`

**Files:**
- Create: `custom_components/unifi_protect_alarm_hub/coordinator.py`

- [ ] **Step 1: Write `coordinator.py`**

```python
"""Data update coordinator for the UniFi Protect Alarm Hub."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from uiprotect import ProtectApiClient
from uiprotect.data.public_devices import LinkStation
from uiprotect.exceptions import NotAuthorized, NvrError

from . import logic
from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class AlarmHubCoordinator(DataUpdateCoordinator[dict[str, LinkStation]]):
    """Polls the Protect public API and pushes WebSocket device updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ProtectApiClient,
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.entry = entry
        self.client = client
        self._unsub_ws: callable | None = None

    async def _async_update_data(self) -> dict[str, LinkStation]:
        try:
            await self.client.update_public()
        except NotAuthorized as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except NvrError as err:
            raise UpdateFailed(f"Error talking to UniFi Protect: {err}") from err
        return logic.snapshot(self.client.public_bootstrap)

    @callback
    def async_subscribe_ws(self) -> None:
        """Subscribe to the public devices WebSocket for real-time updates."""
        self._unsub_ws = self.client.subscribe_devices_websocket(self._ws_callback)

    @callback
    def _ws_callback(self, _message) -> None:
        # The library has already applied the change to public_bootstrap.
        self.async_set_updated_data(logic.snapshot(self.client.public_bootstrap))

    async def async_shutdown(self) -> None:
        if self._unsub_ws is not None:
            self._unsub_ws()
            self._unsub_ws = None
        await self.client.async_disconnect_ws()
        await self.client.close_public_api_session()
        await super().async_shutdown()
```

- [ ] **Step 2: Verify it imports and lints**

Run: `./.venv/bin/ruff check custom_components/unifi_protect_alarm_hub/coordinator.py`
Expected: `All checks passed!` (HA imports resolve only with the Tier-2 env installed; ruff checks syntax/style without importing.)

- [ ] **Step 3: Commit**

```bash
git add custom_components/unifi_protect_alarm_hub/coordinator.py
git commit -m "feat: alarm hub data update coordinator with WS subscription"
```

---

## Task 6: `entity.py` base class

**Files:**
- Create: `custom_components/unifi_protect_alarm_hub/entity.py`

- [ ] **Step 1: Write `entity.py`**

```python
"""Shared entity base for the UniFi Protect Alarm Hub."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from uiprotect.data.public_devices import LinkStation

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import AlarmHubCoordinator


class AlarmHubBaseEntity(CoordinatorEntity[AlarmHubCoordinator]):
    """Base entity bound to one alarm hub device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AlarmHubCoordinator, hub_id: str) -> None:
        super().__init__(coordinator)
        self._hub_id = hub_id
        hub = self.hub
        mac = hub.mac if hub else hub_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=hub.name if hub else "Alarm Hub",
        )

    @property
    def hub(self) -> LinkStation | None:
        """Return the live hub object from the latest coordinator snapshot."""
        return self.coordinator.data.get(self._hub_id)

    @property
    def available(self) -> bool:
        return super().available and self.hub is not None
```

- [ ] **Step 2: Verify lint**

Run: `./.venv/bin/ruff check custom_components/unifi_protect_alarm_hub/entity.py`
Expected: `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add custom_components/unifi_protect_alarm_hub/entity.py
git commit -m "feat: shared alarm hub entity base"
```

---

## Task 7: `binary_sensor.py`

**Files:**
- Create: `custom_components/unifi_protect_alarm_hub/binary_sensor.py`

- [ ] **Step 1: Write `binary_sensor.py`**

```python
"""Binary sensors for the UniFi Protect Alarm Hub."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import logic
from .coordinator import AlarmHubCoordinator
from .entity import AlarmHubBaseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry,  # AlarmHubConfigEntry; entry.runtime_data is the coordinator
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AlarmHubCoordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for hub_id, hub in coordinator.data.items():
        for zone_id in hub.alarm_hub_inputs:
            entities.append(ZoneBinarySensor(coordinator, hub_id, zone_id))
            entities.append(ZoneFaultBinarySensor(coordinator, hub_id, zone_id))
        if hub.alarm_hub_cover is not None:
            entities.append(TamperBinarySensor(coordinator, hub_id))
        entities.append(ArmedBinarySensor(coordinator, hub_id))
        entities.append(ConnectivityBinarySensor(coordinator, hub_id))
        if hub.alarm_hub_battery is not None:
            entities.append(BatteryConnectionBinarySensor(coordinator, hub_id))
    async_add_entities(entities)


class _ZoneBase(AlarmHubBaseEntity, BinarySensorEntity):
    def __init__(self, coordinator: AlarmHubCoordinator, hub_id: str, zone_id: int) -> None:
        super().__init__(coordinator, hub_id)
        self._zone_id = zone_id
        zone = self._zone
        self._attr_entity_registry_enabled_default = (
            logic.zone_enabled_default(zone) if zone else True
        )

    @property
    def _zone(self):
        hub = self.hub
        return hub.alarm_hub_inputs.get(self._zone_id) if hub else None

    @property
    def available(self) -> bool:
        return super().available and self._zone is not None


class ZoneBinarySensor(_ZoneBase):
    def __init__(self, coordinator, hub_id, zone_id):
        super().__init__(coordinator, hub_id, zone_id)
        self._attr_unique_id = logic.zone_unique_id(self.hub.mac, zone_id)
        zone = self._zone
        self._attr_name = logic.zone_name(zone, zone_id) if zone else f"Zone {zone_id}"
        if zone:
            self._attr_device_class = BinarySensorDeviceClass(logic.zone_device_class(zone))

    @property
    def is_on(self) -> bool | None:
        zone = self._zone
        return logic.zone_is_on(zone) if zone else None

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        zone = self._zone
        if zone is None:
            return {}
        return {
            "status": zone.status.value,
            "contact_type": zone.type.value,
            "input_type": zone.input_type.value if zone.input_type else None,
            "last_triggered_at": zone.last_triggered_at,
            "camera_id": zone.camera_id,
        }


class ZoneFaultBinarySensor(_ZoneBase):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, hub_id, zone_id):
        super().__init__(coordinator, hub_id, zone_id)
        self._attr_unique_id = logic.zone_fault_unique_id(self.hub.mac, zone_id)
        zone = self._zone
        base = logic.zone_name(zone, zone_id) if zone else f"Zone {zone_id}"
        self._attr_name = f"{base} Fault"

    @property
    def is_on(self) -> bool | None:
        zone = self._zone
        return logic.zone_fault_is_on(zone) if zone else None


class TamperBinarySensor(AlarmHubBaseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.TAMPER
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Tamper"

    def __init__(self, coordinator, hub_id):
        super().__init__(coordinator, hub_id)
        self._attr_unique_id = f"{self.hub.mac}_tamper"

    @property
    def is_on(self) -> bool | None:
        hub = self.hub
        return logic.cover_is_on(hub.alarm_hub_cover) if hub else None


class ArmedBinarySensor(AlarmHubBaseEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Armed"

    def __init__(self, coordinator, hub_id):
        super().__init__(coordinator, hub_id)
        self._attr_unique_id = f"{self.hub.mac}_armed"

    @property
    def is_on(self) -> bool | None:
        hub = self.hub
        return logic.armed_is_on(hub.alarm_hub_armed) if hub else None


class ConnectivityBinarySensor(AlarmHubBaseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Connectivity"

    def __init__(self, coordinator, hub_id):
        super().__init__(coordinator, hub_id)
        self._attr_unique_id = f"{self.hub.mac}_connectivity"

    @property
    def is_on(self) -> bool | None:
        hub = self.hub
        return hub.state.value.upper() == "CONNECTED" if hub else None


class BatteryConnectionBinarySensor(AlarmHubBaseEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Backup battery connection"

    def __init__(self, coordinator, hub_id):
        super().__init__(coordinator, hub_id)
        self._attr_unique_id = f"{self.hub.mac}_battery_connection"

    @property
    def is_on(self) -> bool | None:
        hub = self.hub
        return logic.battery_connected_is_on(hub.alarm_hub_battery) if hub else None
```

- [ ] **Step 2: Verify lint**

Run: `./.venv/bin/ruff check custom_components/unifi_protect_alarm_hub/binary_sensor.py`
Expected: `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add custom_components/unifi_protect_alarm_hub/binary_sensor.py
git commit -m "feat: binary_sensor platform (zones, faults, hub diagnostics)"
```

---

## Task 8: `switch.py`

**Files:**
- Create: `custom_components/unifi_protect_alarm_hub/switch.py`

- [ ] **Step 1: Write `switch.py`**

```python
"""Switches for UniFi Protect Alarm Hub output channels."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import logic
from .coordinator import AlarmHubCoordinator
from .entity import AlarmHubBaseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AlarmHubCoordinator = entry.runtime_data
    entities: list[OutputSwitch] = []
    for hub_id, hub in coordinator.data.items():
        for output_id in hub.alarm_hub_outputs:
            entities.append(OutputSwitch(coordinator, hub_id, output_id))
    async_add_entities(entities)


class OutputSwitch(AlarmHubBaseEntity, SwitchEntity):
    def __init__(self, coordinator: AlarmHubCoordinator, hub_id: str, output_id: int) -> None:
        super().__init__(coordinator, hub_id)
        self._output_id = output_id
        self._attr_unique_id = logic.output_unique_id(self.hub.mac, output_id)
        output = self._output
        self._attr_name = (
            logic.output_name(output, output_id) if output else f"Output {output_id}"
        )

    @property
    def _output(self):
        hub = self.hub
        return hub.alarm_hub_outputs.get(self._output_id) if hub else None

    @property
    def available(self) -> bool:
        return super().available and self._output is not None

    @property
    def is_on(self) -> bool | None:
        output = self._output
        return logic.output_is_on(output) if output else None

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        output = self._output
        if output is None:
            return {}
        return {
            "status": output.status.value,
            "delay": output.delay,
            "duration": output.duration,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hub.trigger_output(self._output_id, enable=True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hub.trigger_output(self._output_id, enable=False)
        await self.coordinator.async_request_refresh()
```

- [ ] **Step 2: Verify lint**

Run: `./.venv/bin/ruff check custom_components/unifi_protect_alarm_hub/switch.py`
Expected: `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add custom_components/unifi_protect_alarm_hub/switch.py
git commit -m "feat: switch platform for output channels"
```

---

## Task 9: `sensor.py`

**Files:**
- Create: `custom_components/unifi_protect_alarm_hub/sensor.py`

- [ ] **Step 1: Write `sensor.py`**

```python
"""Diagnostic sensors for the UniFi Protect Alarm Hub backup battery."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfElectricPotential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AlarmHubCoordinator
from .entity import AlarmHubBaseEntity

BATTERY_STATUS_OPTIONS = ["ok", "low", "critical", "unknown"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AlarmHubCoordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for hub_id, hub in coordinator.data.items():
        if hub.alarm_hub_battery is not None:
            entities.append(BatteryStatusSensor(coordinator, hub_id))
            entities.append(BatteryVoltageSensor(coordinator, hub_id))
    async_add_entities(entities)


class BatteryStatusSensor(AlarmHubBaseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = BATTERY_STATUS_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Backup battery status"

    def __init__(self, coordinator, hub_id):
        super().__init__(coordinator, hub_id)
        self._attr_unique_id = f"{self.hub.mac}_battery_status"

    @property
    def native_value(self) -> str | None:
        hub = self.hub
        if hub is None or hub.alarm_hub_battery is None:
            return None
        status = hub.alarm_hub_battery.battery_status
        return status.value if status else None


class BatteryVoltageSensor(AlarmHubBaseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Backup battery voltage"

    def __init__(self, coordinator, hub_id):
        super().__init__(coordinator, hub_id)
        self._attr_unique_id = f"{self.hub.mac}_battery_voltage"

    @property
    def native_value(self) -> float | None:
        hub = self.hub
        if hub is None or hub.alarm_hub_battery is None:
            return None
        return hub.alarm_hub_battery.voltage
```

- [ ] **Step 2: Verify lint**

Run: `./.venv/bin/ruff check custom_components/unifi_protect_alarm_hub/sensor.py`
Expected: `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add custom_components/unifi_protect_alarm_hub/sensor.py
git commit -m "feat: sensor platform for backup battery"
```

---

## Task 10: `config_flow.py` + strings (Tier-2 TDD)

**Files:**
- Create: `custom_components/unifi_protect_alarm_hub/config_flow.py`
- Create: `custom_components/unifi_protect_alarm_hub/strings.json`
- Create: `custom_components/unifi_protect_alarm_hub/translations/en.json`
- Test: `tests/test_config_flow.py`, `tests/conftest.py`

- [ ] **Step 1: Write `tests/conftest.py`**

```python
"""Shared Tier-2 fixtures (require pytest-homeassistant-custom-component)."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow the custom component to load in tests."""
    yield
```

- [ ] **Step 2: Write the failing test** `tests/test_config_flow.py`

```python
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.data_entry_flow import FlowResultType

from custom_components.unifi_protect_alarm_hub.const import DOMAIN


async def test_user_flow_success(hass):
    with patch(
        "custom_components.unifi_protect_alarm_hub.config_flow.ProtectApiClient"
    ) as mock_cls:
        client = mock_cls.public_only.return_value
        client.update_public = AsyncMock()
        client.close_public_api_session = AsyncMock()
        client.async_disconnect_ws = AsyncMock()
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] == FlowResultType.FORM
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: "192.168.0.103", "port": 443,
             CONF_API_KEY: "k", "verify_ssl": False},
        )
        assert result2["type"] == FlowResultType.CREATE_ENTRY
        assert result2["data"][CONF_HOST] == "192.168.0.103"


async def test_user_flow_invalid_auth(hass):
    from uiprotect.exceptions import NotAuthorized

    with patch(
        "custom_components.unifi_protect_alarm_hub.config_flow.ProtectApiClient"
    ) as mock_cls:
        client = mock_cls.public_only.return_value
        client.update_public = AsyncMock(side_effect=NotAuthorized("bad key"))
        client.close_public_api_session = AsyncMock()
        client.async_disconnect_ws = AsyncMock()
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: "192.168.0.103", "port": 443,
             CONF_API_KEY: "bad", "verify_ssl": False},
        )
        assert result2["type"] == FlowResultType.FORM
        assert result2["errors"] == {"base": "invalid_auth"}
```

- [ ] **Step 3: Run to verify failure**

Run: `./.venv/bin/pytest tests/test_config_flow.py -v`
Expected: FAIL — config flow not implemented / import error.

- [ ] **Step 4: Write `config_flow.py`**

```python
"""Config flow for the UniFi Protect Alarm Hub."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from uiprotect import ProtectApiClient
from uiprotect.exceptions import NotAuthorized, NvrError

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
            client = ProtectApiClient.public_only(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
                api_key=user_input[CONF_API_KEY],
                verify_ssl=user_input[CONF_VERIFY_SSL],
            )
            try:
                await client.update_public()
            except NotAuthorized:
                errors["base"] = "invalid_auth"
            except NvrError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating UniFi Protect")
                errors["base"] = "unknown"
            else:
                if not client.public_bootstrap.alarm_hubs:
                    _LOGGER.warning(
                        "Connected to UniFi Protect but no alarm hub is adopted; "
                        "no entities will be created"
                    )
                await self.async_set_unique_id(
                    f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="UniFi Protect Alarm Hub", data=user_input
                )
            finally:
                await client.async_disconnect_ws()
                await client.close_public_api_session()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )
```

- [ ] **Step 5: Write `strings.json`**

```json
{
  "config": {
    "step": {
      "user": {
        "title": "UniFi Protect Alarm Hub",
        "data": {
          "host": "Host",
          "port": "Port",
          "api_key": "API key",
          "verify_ssl": "Verify SSL certificate"
        }
      }
    },
    "error": {
      "invalid_auth": "Invalid API key.",
      "cannot_connect": "Failed to connect to the UniFi Protect console.",
      "unknown": "Unexpected error."
    },
    "abort": {
      "already_configured": "This UniFi Protect console is already configured."
    }
  }
}
```

- [ ] **Step 6: Write `translations/en.json`** (identical content to `strings.json`)

Copy `strings.json` verbatim to `custom_components/unifi_protect_alarm_hub/translations/en.json`.

- [ ] **Step 7: Run to verify pass**

Run: `./.venv/bin/pytest tests/test_config_flow.py -v`
Expected: PASS (2 tests)

- [ ] **Step 8: Commit**

```bash
git add custom_components/unifi_protect_alarm_hub/config_flow.py \
        custom_components/unifi_protect_alarm_hub/strings.json \
        custom_components/unifi_protect_alarm_hub/translations/en.json \
        tests/test_config_flow.py tests/conftest.py
git commit -m "feat: config flow with connection validation"
```

---

## Task 11: `__init__.py` setup/unload wiring (Tier-2 TDD)

**Files:**
- Modify: `custom_components/unifi_protect_alarm_hub/__init__.py`
- Test: `tests/test_init.py`

- [ ] **Step 1: Write the failing test** `tests/test_init.py`

```python
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_protect_alarm_hub.const import DOMAIN


def _make_client():
    client = MagicMock()
    client.update_public = AsyncMock()
    client.subscribe_devices_websocket = MagicMock(return_value=lambda: None)
    client.async_disconnect_ws = AsyncMock()
    client.close_public_api_session = AsyncMock()
    bootstrap = MagicMock()
    bootstrap.alarm_hubs = {}
    client.public_bootstrap = bootstrap
    return client


async def test_setup_and_unload(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "h", CONF_PORT: 443, CONF_API_KEY: "k", CONF_VERIFY_SSL: False},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.unifi_protect_alarm_hub.ProtectApiClient"
    ) as mock_cls:
        mock_cls.public_only.return_value = _make_client()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data is not None
        assert await hass.config_entries.async_unload(entry.entry_id)
```

- [ ] **Step 2: Run to verify failure**

Run: `./.venv/bin/pytest tests/test_init.py -v`
Expected: FAIL — `async_setup_entry` not defined.

- [ ] **Step 3: Write `__init__.py`**

```python
"""The UniFi Protect Alarm Hub integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from uiprotect import ProtectApiClient

from .const import PLATFORMS
from .coordinator import AlarmHubCoordinator

type AlarmHubConfigEntry = ConfigEntry[AlarmHubCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AlarmHubConfigEntry) -> bool:
    """Set up UniFi Protect Alarm Hub from a config entry."""
    client = ProtectApiClient.public_only(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        api_key=entry.data[CONF_API_KEY],
        verify_ssl=entry.data[CONF_VERIFY_SSL],
    )
    coordinator = AlarmHubCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_subscribe_ws()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AlarmHubConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok
```

- [ ] **Step 4: Run to verify pass**

Run: `./.venv/bin/pytest tests/test_init.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/unifi_protect_alarm_hub/__init__.py tests/test_init.py
git commit -m "feat: integration setup and unload wiring"
```

---

## Task 12: Finalize README, validation & full test run

**Files:**
- Modify: `README.md` (add entity list + troubleshooting)

- [ ] **Step 1: Expand `README.md`** — append:

```markdown
## Entities created
Per adopted alarm hub (grouped under one device):
- **Binary sensors** — one per input zone (device_class from zone type: motion/door/smoke/sound/safety); one *Fault* diagnostic per zone (problem); *Tamper*, *Armed*, *Connectivity*, *Backup battery connection*.
- **Switches** — one per output channel (on = active; toggling calls the hub's trigger-output API).
- **Sensors** — *Backup battery status* (ok/low/critical) and *Backup battery voltage*, when a backup battery is present.

Zones reported as disabled by the hub are created **disabled by default** — enable them in the entity settings if wired.

## Troubleshooting
Enable debug logging:
```yaml
logger:
  logs:
    custom_components.unifi_protect_alarm_hub: debug
    uiprotect: debug
```
If no entities appear, confirm an Alarm Hub is adopted in UniFi Protect and that the API key has Protect read access.

## Status
v0.1 — built against the `uiprotect` 13.x model and validated with mocked data; needs real-world testing. Please report issues with debug logs.
```

- [ ] **Step 2: Lint everything**

Run: `./.venv/bin/ruff check custom_components tests && ./.venv/bin/ruff format --check custom_components tests`
Expected: `All checks passed!` (run `ruff format custom_components tests` first if the format check fails, then re-commit)

- [ ] **Step 3: Run the full test suite**

Run: `./.venv/bin/pytest tests/ -v`
Expected: all Tier-1 and Tier-2 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "docs: finalize README; lint clean; full test pass"
```

- [ ] **Step 5 (optional): hassfest validation** — once pushed to GitHub, the standard `home-assistant/actions/hassfest` workflow can validate the manifest. Not required for local custom-repo install.

---

## Self-review notes (for the implementer)
- **Spec coverage:** §3 coordinator → Task 5; §4.1 zone sensors → Task 7; §4.2 fault → Task 7; §4.3 switch → Task 8; §4.4 hub diagnostics → Tasks 7 & 9; §5 config flow → Task 10; §6 layout → all; §7 testing → Tier-1/2 throughout; §8 CI → Task 1.
- **Open assumptions (spec §9)** are encoded so they are easy to revisit when a live dump arrives: `zone_enabled_default` (enable→enabled_default) in `logic.py`; `output_is_on` uses `active`. Changing either is a one-line edit + test update.
- **Type consistency:** `hub` property, `_zone`/`_output` helpers, and `logic.*` names are used identically across `entity.py`, `binary_sensor.py`, `switch.py`, `sensor.py`.
```
