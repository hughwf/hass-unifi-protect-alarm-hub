# WebSocket Push (v0.2) — Design

## Goal
Replace 15-second polling with near-real-time updates by subscribing to the UniFi Protect public **devices WebSocket**, while keeping REST polling as a fallback safety net. Sub-second latency for zone/output/tamper/armed changes.

## Background: real WS frame format (captured live, 2026-06-20)
Endpoint: `wss://{host}:{port}/proxy/protect/integration/v1/subscribe/devices`, header `X-API-KEY`. Frames are **text JSON partial deltas**:

```json
{ "type": "update",
  "item": { "id": "<device-id>", "modelKey": "linkstation|bridge|chime|camera|...",
            ...only the changed fields... } }
```

- `type`: `"update"` (also `"add"`/`"remove"` for device add/remove).
- `item`: a partial device object — `id`, `modelKey`, and only the changed fields.
- The stream carries **all** device models and is **chatty** (bridge/chime updates every few seconds). We must filter to `modelKey == "linkstation"`.
- The server pushes data frames frequently (no client keepalive ping required), but the client must still handle ping/pong and disconnects.

## Approach (chosen): WS as a "refresh-now" trigger
On a `linkstation` frame, trigger the coordinator to **re-fetch full hub state** via the existing, tested `GET /v1/alarm-hubs`. The WS signals *when* to refresh; parsing is unchanged. Rejected alternative: in-place partial-delta merge (fragile nested-merge for little gain — alarm-hub changes are infrequent and a full GET is cheap).

## Components & changes

### `api.py` — add WS subscription
```
async def async_subscribe_devices(self, on_alarm_hub_change: Callable[[], None]) -> None
```
- Open an aiohttp WS (`session.ws_connect(url, headers={X-API-KEY}, ssl=...)`; aiohttp is provided by HA, session via `async_get_clientsession`).
- Loop over messages:
  - TEXT → `json.loads`; if `data.get("item", {}).get("modelKey") == "linkstation"`, call `on_alarm_hub_change()`.
  - Ignore all other modelKeys.
  - CLOSED/ERROR → return (so the caller can reconnect).
- Pure helper for testability: `is_alarm_hub_frame(raw: str) -> bool` (parse + modelKey check; returns False on bad JSON). Unit-tested tier-1.

### `coordinator.py` — maintain WS, fall back to polling
- Keep `DataUpdateCoordinator` REST polling, but lengthen `SCAN_INTERVAL` from 15 s to **5 min** (fallback/initial-load; WS drives real-time).
- Add a background task started in `__init__.py` after first refresh: a reconnect loop that calls `client.async_subscribe_devices(self._on_ws_change)`; on return/exception, wait with **exponential backoff** (e.g. 1 s → cap 60 s, reset on a clean run) and reconnect.
- `_on_ws_change` (a `@callback`) → `self.async_request_refresh()` (debounced; coalesces bursts).
- Expose `async_shutdown()` again: cancel the WS task and let it close the WS; call on unload.
- Backoff decision is a pure helper `next_backoff(prev: float) -> float` — unit-tested tier-1.

### `__init__.py`
- After `async_config_entry_first_refresh()`, start the coordinator's WS task (`coordinator.start_ws()` which creates the background task via `entry.async_create_background_task`).
- `async_unload_entry`: unload platforms, then `await coordinator.async_shutdown()`.

### `const.py`
- `SCAN_INTERVAL = timedelta(minutes=5)`.

### `manifest.json`
- `iot_class` → `local_push` (WS is now the primary update path).

### `README.md`
- Update "How updates work": real-time via WebSocket, 5-min REST poll as fallback.

## Data flow
zone changes on hub → Protect pushes `linkstation` WS frame → `async_subscribe_devices` filters & calls `_on_ws_change` → `async_request_refresh()` → `_async_update_data` does `GET /v1/alarm-hubs` → fresh `{id: AlarmHub}` → entities update. Polling still fires every 5 min as a safety net.

## Error handling
- **WS auth failure (401/403) or repeated connect failure:** log a warning; the reconnect loop keeps retrying with backoff; 5-min polling keeps state fresh meanwhile. The integration never fails setup because of WS — setup succeeds on the first REST refresh; WS is best-effort on top.
- **WS drop / network blip:** loop reconnects with exponential backoff; backoff resets after a connection that stayed up.
- **Unload/shutdown:** cancel the background task; close the WS session cleanly.

## Testing
- **Tier-1 (pytest):** `is_alarm_hub_frame()` — linkstation→True, other modelKeys→False, malformed JSON→False, add/remove types; `next_backoff()` growth + cap.
- **Tier-2 (HA harness):** setup still succeeds with a mocked client whose `async_subscribe_devices` is an AsyncMock (no real WS); unload cancels the task without error. The WS listen loop itself is covered by the pure helpers + a fake-WS message-dispatch test.

## Unverified assumption
That the alarm hub emits `modelKey == "linkstation"` frames on `/subscribe/devices` (no door moved during the capture window). The polling fallback makes the integration correct regardless. **Confirm at deploy:** watch the live WS and have the user open a door once to observe a `linkstation` frame trigger an update.

## Out of scope (v0.2)
- Partial-delta merge / per-hub targeted refetch (full GET is fine).
- The events WS (`/subscribe/events`) — not needed for device state.
