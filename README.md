# UniFi Protect Alarm Hub (Home Assistant)

Exposes a UniFi Protect **Alarm Hub** in Home Assistant — wired input zones as
`binary_sensor` entities and output channels as `switch` entities — which the
official `unifiprotect` integration does not create.

**Self-contained:** this component talks to the UniFi Protect **public
integration API** directly over its own small HTTP client, with no `uiprotect`
dependency. It therefore runs side by side with the official UniFi Protect
integration without any shared-library version conflict, on any recent Home
Assistant.

> **v0.3 — still being validated against real hardware.** Built against the
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
WebSocket (`/subscribe/devices`); each frame carries the fields that changed
(zone opened, output triggered, tamper, armed state, etc.) and is applied
straight to the hub's state, so entities change at the moment the hub reports
it.

Applying the frame — rather than treating it as a hint to re-read the whole hub
over REST — is what makes brief events survive. A door that opens and closes in
three seconds produces two state changes at the right times, which a re-read
would miss: by the time it ran, the zone would read closed again, as if nothing
had happened.

REST polling runs every **5 minutes** as a reconciling fallback (and for the
initial load), and a reconnect resyncs in full, so nothing that happened while
the socket was down is left stale. The WebSocket is kept alive with a 30-second
heartbeat and reconnects with exponential backoff; a connection failure never
blocks setup — the integration loads on the first REST refresh and adds the
WebSocket on top. A drop is logged at **warning** level so a silent fall back
to 5-minute polling is visible in the log (the retries that follow stay at
debug).

## Troubleshooting
Enable debug logging:
```yaml
logger:
  logs:
    custom_components.unifi_protect_alarm_hub: debug
```
If no entities appear, confirm an Alarm Hub is adopted in UniFi Protect and that
the API key has Protect read access.

**A zone reads backwards (open when the door is shut).** Check the zone's
contact type in UniFi Protect — a **normally closed** (NC) contact configured as
normally open, or the reverse, inverts the status the hub reports, and this
integration shows exactly what the hub reports. This turned out to be the cause
in [#2](https://github.com/hughwf/hass-unifi-protect-alarm-hub/issues/2), where
the wording of the Protect setting was easy to read the wrong way round: fix it
on the hub and the entity follows. The raw value is on the entity's `status`
attribute (`normal` / `alarm`) if you want to confirm what is arriving.

**A state change never reached Home Assistant.** Check the log for a WebSocket
warning: while the socket is down, state only refreshes every 5 minutes, so a
short zone pulse can pass unseen. It should reconnect on its own — please file
an issue with debug logs if it does not.

## Status
v0.3 — built against the public alarm-hub API and validated with mocked data;
still wants real-world testing. Please report issues with debug logs.
