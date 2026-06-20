# UniFi Protect Alarm Hub — HACS Custom Component Design

**Date:** 2026-06-19
**Status:** Approved design (pre-implementation)
**Repo:** `unifi-protect-alarm-hub` (GitHub `hughwf/hass-unifi-protect-alarm-hub`)
**Domain:** `unifi_protect_alarm_hub`

---

## 1. Purpose

The official Home Assistant `unifiprotect` integration does not create entities for the
UniFi Protect **Alarm Hub** or its wired zones. The underlying `uiprotect` library *does*
fully model the Alarm Hub. This standalone HACS custom component fills the gap: it exposes
Alarm Hub **input zones** as `binary_sensor` entities and **output channels** as `switch`
entities, plus hub-level diagnostics, using its own connection (no dependency on the
official integration).

It runs **side by side** with the official integration: separate domain, separate config
entry, separate device identifiers.

---

## 2. Ground-truth data model (from `uiprotect` 13.4.0 source)

The Alarm Hub is a **Public Integration API** construct. This is the single most important
architectural fact and it drives the auth model.

- The hub is a `LinkStation` (`uiprotect/data/public_devices.py`), `model = LINK_STATION`
  (`modelKey: "linkstation"`), distinguished by `is_alarm_hub == True`.
- A single wire schema covers both link stations and alarm hubs.
- Accessed via the **public** bootstrap: `client.public_bootstrap.alarm_hubs` →
  `dict[str, LinkStation]` (a derived subset already filtered to `is_alarm_hub`).
- Because it lives on the public API, auth **must** use an **API key**
  (`update_public()`), not username/password (the private API has no alarm-hub endpoints).

### `LinkStation` (alarm hub) typed accessors
| Accessor | Type | Notes |
|---|---|---|
| `is_alarm_hub` | `bool` | filter; only `True` hubs are exposed |
| `name` | `str \| None` | hub display name |
| `mac` | `str` | device identifier |
| `state` | `DeviceState` | connection/online state |
| `alarm_hub_armed` | `OnOffState \| None` | armed flag |
| `alarm_hub_battery` | `AlarmHubBattery \| None` | backup battery (may be absent) |
| `alarm_hub_cover` | `AlarmHubCover \| None` | tamper cover |
| `alarm_hub_inputs` | `dict[int, AlarmHubInput]` | input zones keyed by id |
| `alarm_hub_outputs` | `dict[int, AlarmHubOutput]` | output channels keyed by id |
| `trigger_output(id, *, enable, delay, duration)` | coroutine | triggers an output |

### `AlarmHubInput` (input zone)
| Field | Type / enum values |
|---|---|
| `enable` | `OnOffState` (`on`/`off`) |
| `type` | `AlarmHubInputContactType` (`no`/`nc`) — wiring |
| `status` | `AlarmHubInputStatus` (`normal`/`alarm`/`fault`/`short`/`cut`/`unknown`) |
| `input_type` | `AlarmHubInputType` (`MOTION`/`ENTRY`/`SMOKE`/`GLASS_BREAK`/`EMERGENCY_BUTTON`/`unknown`) or `None` |
| `name` | `str \| None` |
| `last_triggered_at` | `int \| None` (epoch ms) |
| `camera_id` | `str \| None` |

### `AlarmHubOutput` (output channel)
| Field | Type / enum values |
|---|---|
| `active` | `OnOffState` (`on`/`off`) — current on/off; controllable |
| `enable` | `OnOffState` |
| `status` | `AlarmHubOutputStatus` (`wet`/`dry`/`unknown`) — contact form |
| `name` | `str \| None` |
| `delay` | `int \| None` |
| `duration` | `int \| None` |

### Hub sub-objects
- `AlarmHubCover`: `status` (`AlarmHubCoverStatus`: `open`/`close`), `distance: int \| None`.
- `AlarmHubBattery`: `charging` (`OnOffState`), `connection` (`AlarmHubConnectionState`:
  `connected`/`disconnected`), `voltage: float \| None`, `battery_status`
  (`AlarmHubBatteryStatus`: `ok`/`low`/`critical`).

All enums use `UnknownValuesEnumMixin`, so unknown firmware values coerce to `UNKNOWN`
rather than raising — entity code must tolerate `UNKNOWN`.

---

## 3. Architecture

**DataUpdateCoordinator + WebSocket-push hybrid** (`iot_class: local_push`).

```
ConfigEntry ──> AlarmHubCoordinator
                  ├─ ProtectApiClient.public_only(host, port, api_key, verify_ssl)
                  ├─ update_public()                  # initial bootstrap
                  ├─ subscribe_websocket(ws_callback) # real-time push
                  └─ SCAN_INTERVAL update_public()    # 5-min safety resync
                          │
                          ▼
          coordinator.data = { hub_id: LinkStation, ... }  (alarm hubs only)
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
   binary_sensor       switch            sensor
   (zones, faults,   (outputs)        (battery status,
    tamper, armed,                     voltage)
    connectivity,
    battery conn)
```

- **WS callback:** the `uiprotect` socket mutates the stored bootstrap in place; the
  callback calls `coordinator.async_set_updated_data(self._collect_hubs())` to push fresh
  state to all entities. Rapid zone open/close is captured without polling.
- **Safety poll:** `SCAN_INTERVAL = timedelta(minutes=5)` re-runs `update_public()` to
  recover if the socket silently drops. `uiprotect` also resyncs the public bootstrap
  internally on reconnect.
- **`_collect_hubs()`** returns `dict(client.public_bootstrap.alarm_hubs)`.

### Lifecycle
- `async_setup_entry`: build client → `update_public()` → `async_config_entry_first_refresh()`
  → `subscribe_websocket()` → forward to platforms.
- `async_unload_entry`: unsubscribe WS, `close_public_api_session()`,
  `async_disconnect_ws()`.

### Error handling
- `NotAuthorized` → `ConfigEntryAuthFailed` (triggers reauth flow).
- `NvrError` / connection failure during refresh → `UpdateFailed` (coordinator retries).
- No alarm hub adopted → setup succeeds, `_LOGGER.warning`, zero entities (per requirement).
- Entity code guards every enum against `UNKNOWN` and every accessor against `None`.

---

## 4. Entities

One HA **device** per alarm hub: `identifiers={(DOMAIN, hub.mac)}`,
`manufacturer="Ubiquiti"`, `model="Alarm Hub"`, `name=hub.name`, `sw_version` if exposed.
A shared `AlarmHubEntity` base (in `entity.py`) wires `CoordinatorEntity`, device info, and
a `hub_id`/`zone_id` lookup that re-reads from `coordinator.data` on every property access
(so entities stay correct across WS mutations and never hold stale objects).

### 4.1 `binary_sensor` — input zone state (one per zone)
- Created for **every** zone in `alarm_hub_inputs`. Zones with `enable == OFF` are created
  `entity_registry_enabled_default = False` (disabled, not omitted).
- `is_on` = `status == ALARM`.
- `device_class` from `input_type`:
  `MOTION`→`MOTION`, `ENTRY`→`DOOR`, `SMOKE`→`SMOKE`, `GLASS_BREAK`→`SOUND`,
  `EMERGENCY_BUTTON`→`SAFETY`, `unknown`/`None`→`SAFETY`.
- `unique_id` = `f"{hub.mac}_zone_{id}"`; name = zone `name` or `f"Zone {id}"`.
- attributes: `status` (raw), `contact_type` (`no`/`nc`), `input_type`,
  `last_triggered_at`, `camera_id`.

### 4.2 `binary_sensor` — zone fault (one per zone, diagnostic)
- `device_class = PROBLEM`, `entity_category = DIAGNOSTIC`.
- `is_on` = `status in {FAULT, SHORT, CUT}`.
- `unique_id` = `f"{hub.mac}_zone_{id}_fault"`; name = `"{zone} Fault"`.
- Same enabled-default rule as 4.1.
- Rationale: a cut/shorted sensor wire is a first-class security signal, surfaced
  separately so it can drive its own automations/alerts.

### 4.3 `switch` — output channel (one per output)
- `is_on` = `active == ON`.
- `async_turn_on` → `hub.trigger_output(id, enable=True)`;
  `async_turn_off` → `hub.trigger_output(id, enable=False)`.
- Optimistic: set local state immediately, then let the next WS/poll confirm.
- `unique_id` = `f"{hub.mac}_output_{id}"`; name = output `name` or `f"Output {id}"`.
- attributes: `status` (`wet`/`dry`), `delay`, `duration`.
- (During development we will not trigger real outputs against live hardware.)

### 4.4 Hub diagnostics (all `entity_category = DIAGNOSTIC`)
- `binary_sensor` **tamper** (`device_class = TAMPER`): `is_on` = `cover.status == OPEN`.
  Created only if `alarm_hub_cover` present.
- `binary_sensor` **armed**: `is_on` = `alarm_hub_armed == ON`.
- `binary_sensor` **connectivity** (`device_class = CONNECTIVITY`): from `state`.
- Battery backup — only if `alarm_hub_battery` present:
  - `binary_sensor` **battery connection** (`device_class = CONNECTIVITY`):
    `connection == CONNECTED`.
  - `sensor` **battery status**: enum string `ok`/`low`/`critical` (or `UNKNOWN`).
  - `sensor` **battery voltage** (`device_class = VOLTAGE`, `unit = V`): `voltage`.

---

## 5. Config flow

Single-step user flow + reauth step.

Fields:
- `host` (str, required) — IP/hostname of the UniFi OS console running Protect.
- `port` (int, default `443`).
- `api_key` (str, required) — UniFi OS → Settings → Control Plane → Integrations.
- `verify_ssl` (bool, default `False`) — self-signed console certs.

Validation: construct `ProtectApiClient.public_only(...)`, call `update_public()`.
- success → create entry; `unique_id` = console/NVR id (or NVR mac) to prevent duplicates.
- `NotAuthorized` → error `invalid_auth`.
- `NvrError`/timeout → error `cannot_connect`.
- Empty `public_bootstrap.alarm_hubs` → still create the entry; warn (no entities yet).

Reauth: re-prompt for `api_key` only.

---

## 6. Repository layout

```
unifi-protect-alarm-hub/
├── custom_components/unifi_protect_alarm_hub/
│   ├── __init__.py            # setup/unload, coordinator wiring
│   ├── manifest.json          # requirements: ["uiprotect>=13.0,<14"], iot_class local_push
│   ├── const.py               # DOMAIN, PLATFORMS, defaults, enum→device_class maps
│   ├── coordinator.py         # AlarmHubCoordinator (client + WS + poll)
│   ├── config_flow.py         # user + reauth flows
│   ├── entity.py              # AlarmHubEntity base (device info, live lookup)
│   ├── binary_sensor.py       # zone state, zone fault, tamper, armed, connectivity, battery conn
│   ├── switch.py              # output channels
│   ├── sensor.py              # battery status, battery voltage
│   ├── strings.json
│   └── translations/en.json
├── hacs.json                  # { "name": "...", "render_readme": true, "homeassistant": "2025.1.0" }
├── README.md                  # HACS custom-repo install, config, entity list, troubleshooting
├── tests/                     # mocked LinkStation fixtures (no hardware)
│   ├── conftest.py
│   ├── fixtures.py            # fabricated alarm-hub bootstrap dicts
│   ├── test_config_flow.py
│   ├── test_coordinator.py
│   ├── test_binary_sensor.py
│   └── test_switch.py
└── .github/workflows/ci.yml   # ruff + mypy + hassfest
```

`manifest.json`: domain `unifi_protect_alarm_hub`, `config_flow: true`,
`iot_class: "local_push"`, `requirements: ["uiprotect>=13.0,<14"]`, `dependencies: []`,
`codeowners: ["@hughwf"]`, `version: "0.1.0"`, documentation/issue URLs →
`github.com/hughwf/hass-unifi-protect-alarm-hub`.

---

## 7. Testing

No alarm-hub hardware is reachable from the dev machine (LAN isolation), so tests run
entirely on **fabricated `LinkStation` bootstrap dicts** that mirror the 13.4.0 schema:
- coordinator: `update_public` populates hubs; WS callback pushes updates; empty-hub case.
- binary_sensor: status→`is_on`, `input_type`→device_class, fault logic, enable→enabled-default.
- switch: `is_on` from `active`; `turn_on`/`turn_off` call `trigger_output(enable=…)`.
- config_flow: success, `invalid_auth`, `cannot_connect`, duplicate-unique-id abort.

Pure-logic helpers (enum→device_class mapping) are unit-tested directly. Ships as **v0.1,
explicitly "needs real-world validation against hardware"** in the README.

---

## 8. CI

`.github/workflows/ci.yml`: `ruff check`, `ruff format --check`, `mypy`, and Home
Assistant **hassfest** manifest validation. (HACS-action validation optional, added once
the repo is pushed and a release tag exists.)

---

## 9. Open assumptions (to confirm against a live dump)

These do not block implementation; a real `alarm_hub` JSON from the HA box will confirm:
1. **Unwired/unconfigured zones** — the schema has no "unused" status. We create entities
   for all zones in `alarm_hub_inputs` and disable (`enabled_default=False`) those with
   `enable == OFF`. A dump confirms whether unwired zones appear in the dict at all.
2. **Output on/off source** — we use `active`; confirm it tracks the trigger result and
   isn't superseded by `status`/`enable`.
3. **`sw_version`/firmware field** for device info — confirm exact attribute name on the
   live object.

---

## 10. Out of scope (v0.1)

- Arm/disarm control (write) and arm-profile management — read-only `armed` only.
- HDMI/camera linkage beyond exposing `camera_id` as an attribute.
- The plain (non-alarm) Link Station device.
- HACS default-store submission (custom-repo install only for now).
