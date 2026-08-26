# UniFi Protect Alarm Hub (Home Assistant)

Exposes a UniFi Protect **Alarm Hub** in Home Assistant — wired input zones as
`binary_sensor` entities and output channels as `switch` entities — which the
official `unifiprotect` integration does not create.

**Self-contained:** this component talks to the UniFi Protect **public
integration API** directly over its own small HTTP client, with no `uiprotect`
dependency. It therefore runs side by side with the official UniFi Protect
integration without any shared-library version conflict.

> **v0.3 — still being validated against real hardware.** Built against the
> documented public alarm-hub API. Please file issues with debug logs.

## Requirements
- Home Assistant **2025.11 or newer**. Earlier versions cannot run this: the
  entity plumbing it uses arrived in 2025.3 and the coordinator locking it
  relies on in 2025.11. HACS will not offer the update below that floor.
- A UniFi OS console running Protect with an adopted **Alarm Hub**.
- A Protect **API key** (UniFi OS → Settings → Control Plane → Integrations).

## Install (HACS custom repository)
1. HACS → ⋮ → Custom repositories → add `https://github.com/hughwf/hass-unifi-protect-alarm-hub`, category **Integration**.
2. Install "UniFi Protect Alarm Hub", restart Home Assistant.
3. Settings → Devices & Services → Add Integration → "UniFi Protect Alarm Hub".

## Configuration
- **Host** — IP/hostname of the UniFi OS console running Protect. Pasting a
  browser URL works; a port in the address wins over the port box. Anything that
  would resolve somewhere other than what you typed is refused on the field
  rather than quietly reaching a different machine with your API key.
- **Port** — default 443
- **API key** — UniFi OS → Settings → Control Plane → Integrations
- **Verify SSL** — default off (self-signed console certs)

If the API key is rotated or revoked, Home Assistant offers **Reauthenticate**;
if the console moves, **Reconfigure** changes its address. Reconfigure refuses a
console that shares no hardware with the entry, so it cannot silently repoint an
entry at different hardware and orphan the entities your automations name.

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
enable them in the entity settings if wired. Zones, outputs and the battery
sensors appear on their own when you wire them, without reloading.

**Zone state is deliberately three-valued.** A zone reads *on* or *off* only
when the hub reports a status that means something (`alarm` / `normal`). A cut,
shorted or faulted loop reads **unknown**, not "Closed" — an alarm integration
must not affirm a secure door on a severed circuit. If you have automations
conditioned on `state == 'off'`, they will no longer fire for a broken loop; use
the per-zone *Fault* diagnostic to act on that case explicitly.

Entities go **unavailable** when their hub is not connected, rather than
continuing to publish the last thing it said.

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
If no entities appear, confirm the API key has Protect read access. A console
with no adopted alarm hub is now refused while adding the integration, and a hub
adopted afterwards appears on its own.

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

**Leftover devices or entities.** Nothing here ever deletes a device row or an
entity: a hub or zone that goes away goes *unavailable* instead, because an
entity that vanishes takes every automation naming it with it. An install that
went through a rough adoption can therefore accumulate rows you no longer want,
and those have to be deleted by hand.

## Known limitations
- A hub with **no usable mac** (none reported, or the `000000000000` /
  `ffffffffffff` placeholders) is identified by its Protect device id. If such a
  hub is later re-adopted under a new id, it reads as new hardware and its old
  entities are left behind.
- A console reporting a **malformed mac** that is neither empty nor a known
  placeholder is taken at face value; if it later reports a real one, that hub
  is treated as new hardware.
- **Two hubs reporting the same mac** keep separate devices and never report or
  command each other, but which one keeps the mac-derived identity is decided by
  the order the console lists them the first time they are seen.

## Status
v0.3 — built against the public alarm-hub API and validated against mocked
consoles, including upgrades replayed from the registries earlier releases
actually wrote. Still wants real-world testing. Please report issues with debug
logs.
