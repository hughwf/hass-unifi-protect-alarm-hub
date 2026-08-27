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
Updates follow the hub rather than a poll clock. The integration subscribes to
the Protect devices WebSocket (`/subscribe/devices`) and acts on every alarm-hub
message it sends.

**What those messages contain depends on the console.** The UP-AlarmHub-Kit
firmware this has been captured against sends a *notification*: the hub's id and
a timestamp, and nothing at all about zones, outputs or armed state. There is no
update in it to apply, so the integration reads that hub back over REST
immediately — the request goes out in the same instant the message arrives, and
a burst of messages is coalesced into a couple of requests rather than one each.
Where a console does put the changed fields on the message, they are applied
directly and no request is made at all.

**The limit, honestly.** Because the read reports what the hub says *now* rather
than what it said when the message was sent, a zone that opens and closes before
the read reaches the console is missed. How long that is depends on what else
the hub is doing.

On a quiet hub it is one LAN round trip: the request goes out in the same
instant the message arrives, so only a contact that pulses for a fraction of a
second slips through, and no amount of polling would have caught that either.

When messages are arriving back to back it is longer, because REST traffic is
capped. A message that lands while a read is already out waits for that read to
finish and then half a second more before its own read goes out — so against a
0.3-second read, a 0.82-second pulse survives and a 0.79-second one does not.
That is the price of the cap, and the cap is the point: without it a chatty
console can drive this integration's REST traffic as fast as it cares to talk,
which measured at over three requests a second with the console's API busy 94%
of the time. As it stands the notification path cannot exceed one read per
read-plus-half-second — 1.25 requests a second against a 0.3-second read, and 2
a second in the limit — however many messages arrive. (A console that sent
changed fields fast enough to overflow the replay buffer could reach about
2.5; the hardware this was measured against sends none.) (The 5-minute poll and the
reconnect resync are on top of that, and have bounds of their own.)

What is fixed is the case in [#3](https://github.com/hughwf/hass-unifi-protect-alarm-hub/issues/3):
a three-second door pulse used to wait on a ten-second cooldown and be read back
after the door had already shut, so Home Assistant recorded nothing at all.

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
zone pulse can easily pass unseen. It should reconnect on its own — please file
an issue with debug logs if it does not. With the socket up, the same thing can
still happen to a short enough pulse — under a second on a busy hub; see *The
limit, honestly* above for the numbers.

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
