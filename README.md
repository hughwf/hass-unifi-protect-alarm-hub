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
