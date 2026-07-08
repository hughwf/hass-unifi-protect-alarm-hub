# UniFi Protect Alarm Hub (Home Assistant)

Exposes a UniFi Protect **Alarm Hub** in Home Assistant — wired input zones as
`binary_sensor` entities and output channels as `switch` entities — which the
official `unifiprotect` integration does not create.

**Self-contained:** this component talks to the UniFi Protect **public
integration API** directly over its own small HTTP client, with no `uiprotect`
dependency. It therefore runs side by side with the official UniFi Protect
integration without any shared-library version conflict, on any recent Home
Assistant.

> **v0.1 — needs real-world validation against hardware.** Built against the
> documented public alarm-hub API. Please file issues with debug logs.

## Requirements
- Home Assistant 2025.1 or newer.
- A UniFi OS console running Protect with an adopted **Alarm Hub**.
- A Protect **API key** (UniFi OS → Settings → Control Plane → Integrations).

## Install (HACS custom repository)
1. HACS → ⋮ → Custom repositories → add `https://github.com/hughwf/hass-unifi-protect-alarm-hub`, category **Integration**.
2. Install "UniFi Protect Alarm Hub", restart Home Assistant.
3. Settings → Devices & Services → Add Integration → "UniFi Protect Alarm Hub".

## Configuration
- **Host** — IP/hostname of the UniFi OS console running Protect
- **Port** — default 443
- **API key** — UniFi OS → Settings → Control Plane → Integrations
- **Verify SSL** — default off (self-signed console certs)

## Entities created
Per adopted alarm hub (grouped under one device):
- **Binary sensors** — one per input zone (device_class from zone type:
  motion/door/smoke/sound/safety); a *Fault* diagnostic per zone; *Tamper*,
  *Armed*, *Connectivity*, and *Backup battery connection*.
- **Switches** — one per output channel (on = active; toggling calls the hub's
  trigger-output endpoint).
- **Sensors** — *Backup battery status* (ok/low/critical) and *Backup battery
  voltage*, when a backup battery is present.

Zones reported as disabled by the hub are created **disabled by default** —
enable them in the entity settings if wired.

## How updates work
Updates are **real-time**. The integration subscribes to the Protect devices
WebSocket (`/subscribe/devices`); when the alarm hub reports a change (zone
opened, output triggered, tamper, armed state, etc.) it triggers an immediate
refresh of full hub state, so entities update within about a second.

REST polling still runs every **5 minutes** as a fallback safety net (and for
the initial load), so state stays correct even if the WebSocket drops. The
WebSocket reconnects automatically with exponential backoff, and a connection
failure never blocks setup — the integration loads on the first REST refresh and
adds the WebSocket on top.

## Troubleshooting
Enable debug logging:
```yaml
logger:
  logs:
    custom_components.unifi_protect_alarm_hub: debug
```
If no entities appear, confirm an Alarm Hub is adopted in UniFi Protect and that
the API key has Protect read access.

## Status
v0.1 — built against the public alarm-hub API and validated with mocked data;
needs real-world testing. Please report issues with debug logs.
