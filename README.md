# Tablo Tuner — Dispatcharr plugin

Connects Dispatcharr to a [tablo-bridge](https://github.com/jkoehler11/tablo-legacy-m3u)
playlist, which is what makes a legacy Nuvyyo Tablo usable as an IPTV source.
The bridge does the hard part (tuning, session sharing, HLS); this plugin does
the tedious part:

- creates or fixes the **M3U account** pointing at `<bridge>/playlist.m3u`,
- creates or fixes the **XMLTV source** pointing at `<bridge>/guide.xml`,
- gives every stream a **channel** (number from `tvg-chno`, name from `tvg-name`)
  using the locked **Proxy** profile — Streamlink/VLC/ffmpeg all fail on a raw TS,
- raises `stream_startup_timeout` / `connection_timeout` so the proxy waits out
  the Tablo's 6-11 s tuner allocation,
- maps each channel to its guide entry by `tvg-id` then display name,
- optionally adds the channels to a channel profile.

Every action is idempotent and reports what it changed.

## Requirements

A reachable [tablo-bridge](https://github.com/jkoehler11/tablo-legacy-m3u)
instance (Docker, port 8050 by default) and Dispatcharr 0.33.0 or newer.

### Which hardware this works with

The bridge needs the legacy local REST API on port 8885. `curl -s
http://<tablo-ip>:8885/server/info` returning JSON with a `model` field means the
unit is supported.

| Hardware | Works | Notes |
| --- | --- | --- |
| Legacy network Tablo — 2-Tuner, 4-Tuner, DUAL LITE, QUAD (Nuvyyo era, firmware 2.2.x) | **Yes** | Written against a Tablo 2-Tuner on 2.2.60. No authentication on any endpoint. Set *Concurrent streams (tuners)* to the unit's real tuner count. |
| 4th-generation Tablo (2023+, current models) | **No** | No local API; cloud-managed and cloud-authenticated only, so there is nothing for the bridge to connect to. |
| Legacy HDMI-only models (DUAL HDMI, QUAD HDMI) | **No** (expected) | HDMI output to a TV rather than network streaming; different firmware branch. |
| HDHomeRun and other network tuners | Not applicable | Already speak M3U/HDHomeRun or are natively supported by Dispatcharr — no bridge needed. |

## Install

Copy this directory to `/data/plugins/tablo-tuner` in the Dispatcharr container
(host: `<dispatcharr_data>/plugins/tablo-tuner`), then **Settings → Plugins →
Discover** and enable *Tablo Tuner*.

## Actions

| Action | What it does |
| --- | --- |
| Set up Tablo live TV | Full idempotent bootstrap. Safe to re-run. |
| Re-sync channels | Refresh the playlist, add new channels, retire ones whose stream disappeared. Only channels this plugin created are ever deleted. |
| Show status | Read-only: bridge reachability, account/source presence, proxy timeouts, channels without guide data. |

## Notes

- The plugin never touches the bridge and never spawns a process; Dispatcharr
  runs under gevent, where fork-based spawn deadlocks.
- It does not enable Dispatcharr's own `auto_channel_sync`; channels it creates
  are flagged `auto_created_by` this account, so switching that on later adopts
  them instead of duplicating.
