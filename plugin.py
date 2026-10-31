"""Dispatcharr plugin: install and maintain the Dispatcharr side of a legacy
Tablo, which is served by the tablo-bridge container.

The bridge turns the Tablo's private watch/HLS API into an M3U playlist plus an
XMLTV guide. Everything after that -- the M3U account, the EPG source, one
channel per stream with the right profile, and the EPG mapping -- is ordinary
Dispatcharr bookkeeping that has to be redone by hand whenever the Tablo
rescans. That is what this plugin automates. It never touches the bridge and
never spawns a process.
"""
import json
import re
import urllib.error
import urllib.request

DEFAULT_BRIDGE = "http://192.168.86.30:8050"


def _log(context, msg):
    logger = (context or {}).get("logger")
    if logger:
        logger.info("[tablo-tuner] %s", msg)


def _parse_m3u(text):
    """Minimal M3U parser: attributes of #EXTINF plus the following URL."""
    entries = []
    pending = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs, _, display = line.partition(",")
            meta = {"name": display.strip()}
            for key, value in re.findall(r'([\w-]+)="([^"]*)"', attrs):
                meta[key] = value
            pending = meta
        elif line.startswith("#"):
            continue
        elif pending is not None:
            pending["url"] = line
            entries.append(pending)
            pending = None
    return entries


class Plugin:
    name = "Tablo Tuner"
    version = "0.1.0"
    description = (
        "Point Dispatcharr at a tablo-bridge playlist and keep the M3U account, "
        "EPG source, channels and EPG mapping in step with the Tablo's lineup."
    )
    author = ""
    help_url = ""

    fields = [
        {"id": "bridge_url", "label": "Bridge URL", "type": "string",
         "default": DEFAULT_BRIDGE,
         "help_text": "Base URL of the tablo-bridge container as Dispatcharr sees it."},
        {"id": "account_name", "label": "M3U account name", "type": "string",
         "default": "Tablo OTA"},
        {"id": "epg_source_name", "label": "EPG source name", "type": "string",
         "default": "Tablo EPG"},
        {"id": "max_streams", "label": "Concurrent streams (tuners)", "type": "number",
         "default": 2, "min": 1, "max": 8},
        {"id": "stream_profile", "label": "Channel stream profile", "type": "string",
         "default": "Proxy",
         "help_text": "Locked profile for these channels. Anything but Proxy runs "
                      "the stream through streamlink/VLC/ffmpeg, which fails on a raw TS."},
        {"id": "channel_profile", "label": "Channel profile (optional)", "type": "string",
         "default": "",
         "help_text": "Also add the channels to this Dispatcharr channel profile."},
        {"id": "startup_timeout", "label": "Proxy startup timeout (s)", "type": "number",
         "default": 30, "min": 5, "max": 120,
         "help_text": "A Tablo takes 6-11 s to allocate a tuner; the 8 s default "
                      "declares the channel dead before the first byte."},
        {"id": "manage_channels", "label": "Create and maintain channels",
         "type": "boolean", "default": True},
    ]

    actions = [
        {"id": "setup", "label": "Set up Tablo live TV",
         "description": "Idempotent: account, guide, channels, profiles, EPG mapping.",
         "button_label": "Set up", "button_variant": "primary"},
        {"id": "resync", "label": "Re-sync channels",
         "description": "Refresh the playlist and add, rename or retire channels.",
         "button_label": "Re-sync"},
        {"id": "status", "label": "Show status",
         "description": "Read-only summary of what is configured and what is missing.",
         "button_label": "Status"},
    ]

    # ------------------------------------------------------------------ run
    def run(self, action_id, params, context):
        settings = dict((context or {}).get("settings") or {})
        bridge = str(settings.get("bridge_url") or DEFAULT_BRIDGE).rstrip("/")
        try:
            if action_id == "status":
                return self._status(settings, bridge)
            if action_id in ("setup", "resync"):
                return self._apply(settings, bridge, context, resync=action_id == "resync")
            return {"status": "error", "error": "unknown action %r" % action_id}
        except Exception as exc:  # noqa: BLE001 - surface, never crash the worker
            _log(context, "action %s failed: %s" % (action_id, exc))
            return {"status": "error", "error": "%s: %s" % (type(exc).__name__, exc)}

    # -------------------------------------------------------------- helpers
    def _http(self, url, timeout=10):
        req = urllib.request.Request(url, headers={"User-Agent": "dispatcharr-tablo-tuner"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def _profile(self, name):
        from apps.channels.models import StreamProfile
        from core.models import FFMPEG_PROFILE_NAME
        return (StreamProfile.objects.filter(name=name).first()
                or StreamProfile.objects.filter(name="Proxy").first()
                or StreamProfile.objects.filter(locked=True, name=FFMPEG_PROFILE_NAME).first())

    def _streams(self, account):
        from apps.channels.models import Stream
        return list(Stream.objects.filter(m3u_account=account, is_stale=False).order_by("stream_chno"))

    def _channel_for(self, stream, number):
        from apps.channels.models import Channel, ChannelStream
        link = ChannelStream.objects.filter(stream=stream).select_related("channel").first()
        if link:
            return link.channel, True
        if number is not None:
            chan = Channel.objects.filter(channel_number=number).first()
            if chan:
                return chan, True
        return None, False

    # -------------------------------------------------------------- actions
    def _status(self, settings, bridge):
        from apps.channels.models import Channel, ChannelStream, Stream
        from apps.epg.models import EPGData, EPGSource
        from apps.m3u.models import M3UAccount
        from core.models import CoreSettings

        acc_name = settings.get("account_name") or "Tablo OTA"
        epg_name = settings.get("epg_source_name") or "Tablo EPG"
        checks = []

        try:
            health = json.loads(self._http(bridge + "/healthz", 8))
            checks.append({"ok": True, "item": "bridge",
                           "detail": "%s channels, sessions %s" % (
                               health.get("channels"), health.get("sessions"))})
        except Exception as exc:  # noqa: BLE001
            checks.append({"ok": False, "item": "bridge",
                           "detail": "%s unreachable (%s)" % (bridge, exc)})

        account = M3UAccount.objects.filter(name=acc_name).first()
        source = EPGSource.objects.filter(name=epg_name).first()
        proxy = CoreSettings.get_proxy_settings()

        streams, channels, unmapped = 0, 0, []
        if account:
            streams = Stream.objects.filter(m3u_account=account, is_stale=False).count()
            linked = ChannelStream.objects.filter(
                stream__m3u_account=account, stream__is_stale=False)
            channels = linked.values("channel_id").distinct().count()
            for chan in Channel.objects.filter(id__in=linked.values("channel_id")).distinct():
                if chan.epg_data_id is None:
                    unmapped.append(chan.name)

        checks.append({"ok": bool(account), "item": "m3u account",
                       "detail": acc_name if account else "missing"})
        checks.append({"ok": bool(source), "item": "epg source",
                       "detail": epg_name if source else "missing"})
        checks.append({"ok": int(proxy.get("stream_startup_timeout") or 8) >= 20,
                       "item": "startup timeout",
                       "detail": "%ss (connection %ss)" % (
                           proxy.get("stream_startup_timeout"), proxy.get("connection_timeout"))})
        checks.append({"ok": not unmapped, "item": "epg mapping",
                       "detail": ("%d channels without guide data: %s" % (
                           len(unmapped), ", ".join(unmapped))) if unmapped else "all mapped"})

        return {"status": "ok", "bridge": bridge, "streams": streams,
                "channels": channels, "checks": checks}

    def _apply(self, settings, bridge, context, resync=False):
        from apps.channels.models import (Channel, ChannelProfile,
                                          ChannelProfileMembership, ChannelStream,
                                          Stream)
        from apps.epg.models import EPGData, EPGSource
        from apps.m3u.models import M3UAccount
        from core.models import CoreSettings

        steps = []
        acc_name = settings.get("account_name") or "Tablo OTA"
        epg_name = settings.get("epg_source_name") or "Tablo EPG"
        want_timeout = int(settings.get("startup_timeout") or 30)

        # 1. proxy timeouts: the device needs longer than the 8 s default
        proxy = CoreSettings.get_proxy_settings()
        touched = []
        if int(proxy.get("stream_startup_timeout") or 8) < want_timeout:
            proxy["stream_startup_timeout"] = want_timeout
            touched.append("stream_startup_timeout=%s" % want_timeout)
        if int(proxy.get("connection_timeout") or 10) < max(30, want_timeout):
            proxy["connection_timeout"] = max(30, want_timeout)
            touched.append("connection_timeout=%s" % max(30, want_timeout))
        if touched:
            CoreSettings.objects.update_or_create(
                key="proxy_settings",
                defaults={"name": "Proxy Settings", "value": proxy})
            steps.append("proxy settings: " + ", ".join(touched))
        else:
            steps.append("proxy settings: already adequate")

        # 2. M3U account pointing at the bridge playlist
        playlist = bridge + "/playlist.m3u"
        account, created = M3UAccount.objects.get_or_create(
            name=acc_name,
            defaults={"server_url": playlist, "max_streams": int(settings.get("max_streams") or 2),
                      "account_type": M3UAccount.Types.STADNARD})
        changed = created
        if not created:
            if account.server_url != playlist:
                account.server_url = playlist
                changed = True
            want = int(settings.get("max_streams") or 2)
            if account.max_streams != want:
                account.max_streams = want
                changed = True
            if not account.is_active:
                account.is_active = True
                changed = True
        if changed:
            account.save()
        steps.append("m3u account %r %s" % (acc_name, "created" if created else
                                            ("updated" if changed else "unchanged")))

        if created or changed or resync:
            try:
                from apps.m3u.tasks import refresh_single_m3u_account
                refresh_single_m3u_account(account.id)
                steps.append("playlist refreshed")
            except Exception as exc:  # noqa: BLE001
                steps.append("playlist refresh FAILED: %s" % exc)
        else:
            steps.append("playlist refresh: skipped (account unchanged; use Re-sync)")

        # 3. XMLTV source for the bridge guide
        source, created = EPGSource.objects.get_or_create(
            name=epg_name,
            defaults={"source_type": "xmltv", "url": bridge + "/guide.xml",
                      "is_active": True})
        if not created and (source.url != bridge + "/guide.xml" or not source.is_active):
            source.url = bridge + "/guide.xml"
            source.is_active = True
            source.save()
            steps.append("epg source %r updated" % epg_name)
        elif created:
            steps.append("epg source %r created" % epg_name)
        else:
            steps.append("epg source %r unchanged" % epg_name)

        if created or resync or not EPGData.objects.filter(epg_source=source).exists():
            try:
                from apps.epg.tasks import refresh_epg_data
                refresh_epg_data(source.id)
                steps.append("guide refreshed (%d channel entries)" %
                             EPGData.objects.filter(epg_source=source).count())
            except Exception as exc:  # noqa: BLE001
                steps.append("guide refresh FAILED: %s" % exc)

        # 4. one channel per stream
        streams = self._streams(account)
        steps.append("streams in playlist: %d" % len(streams))
        if not (settings.get("manage_channels", True) in (True, "true", "True", 1)):
            return {"status": "ok", "steps": steps, "channels": 0,
                    "note": "channel management disabled in settings"}

        profile = self._profile(settings.get("stream_profile") or "Proxy")
        made, renamed, linked, retired = 0, 0, 0, 0
        channel_ids = []
        for stream in streams:
            props = stream.custom_properties or {}
            try:
                number = float(stream.stream_chno or props.get("tvg-chno") or 0) or None
            except (TypeError, ValueError):
                number = None
            tvg_name = (props.get("tvg-name") or stream.name or "").strip()
            label = ("%s %s" % (number, tvg_name)).strip() if number else tvg_name

            channel, found = self._channel_for(stream, number)
            if channel is None:
                channel = Channel.objects.create(
                    channel_number=number or 0, name=label or stream.name,
                    tvg_id=stream.tvg_id or props.get("tvg-id") or None,
                    channel_group=stream.channel_group,
                    stream_profile=profile, auto_created=True,
                    auto_created_by=account)
                made += 1
            else:
                fields = []
                if not channel.tvg_id and (stream.tvg_id or props.get("tvg-id")):
                    channel.tvg_id = stream.tvg_id or props.get("tvg-id")
                    fields.append("tvg_id")
                if profile and channel.stream_profile_id != profile.id:
                    channel.stream_profile = profile
                    fields.append("stream_profile")
                if fields:
                    channel.save(update_fields=fields)
                    renamed += 1
            channel_ids.append(channel.id)
            if not ChannelStream.objects.filter(channel=channel, stream=stream).exists():
                ChannelStream.objects.create(channel=channel, stream=stream, order=0)
                linked += 1

        # drop links to streams the playlist no longer carries, so failover never
        # lands on a dead URL; the stream rows themselves age out on their own
        pruned = 0
        if resync:
            dead = ChannelStream.objects.filter(
                stream__m3u_account=account, stream__is_stale=True)
            pruned = dead.count()
            if pruned:
                dead.delete()
            steps.append("stale stream links pruned: %d" % pruned)

        # retire channels this account created whose stream is gone
        if resync:
            live_ids = set(channel_ids)
            orphans = Channel.objects.filter(auto_created_by=account).exclude(id__in=live_ids)
            for chan in orphans:
                if not ChannelStream.objects.filter(channel=chan).exists():
                    chan.delete()
                    retired += 1
        steps.append("channels: %d created, %d updated, %d newly linked, %d retired" %
                     (made, renamed, linked, retired))

        # 5. EPG mapping for channels that have none yet
        rows = list(EPGData.objects.filter(epg_source=source))
        by_tvg, by_name = {}, {}
        for row in rows:
            if row.tvg_id:
                by_tvg.setdefault(row.tvg_id.strip().lower(), row)
            if row.name:
                by_name.setdefault(row.name.strip().lower(), row)

        mapped, unmapped = 0, []
        for channel in Channel.objects.filter(id__in=channel_ids):
            if channel.epg_data_id:
                mapped += 1
                continue
            stream = ChannelStream.objects.filter(channel=channel).select_related("stream").first()
            props = (stream.stream.custom_properties or {}) if stream else {}
            name_tail = channel.name.split(" ", 1)[-1] if " " in (channel.name or "") else channel.name
            candidates = [channel.tvg_id, props.get("tvg-id"), props.get("tvg-name"),
                          name_tail, channel.name]
            hit = None
            for cand in candidates:
                if cand and cand.strip().lower() in by_tvg:
                    hit = by_tvg[cand.strip().lower()]
                    break
            if hit is None:
                for cand in candidates:
                    if cand and cand.strip().lower() in by_name:
                        hit = by_name[cand.strip().lower()]
                        break
            if hit is not None:
                channel.epg_data = hit
                channel.save(update_fields=["epg_data"])
                mapped += 1
            else:
                unmapped.append(channel.name)
        steps.append("epg: %d mapped, %d without guide data%s" %
                     (mapped, len(unmapped),
                      (" (" + ", ".join(unmapped) + ")") if unmapped else ""))

        # 6. optional channel profile membership
        wanted = (settings.get("channel_profile") or "").strip()
        if wanted:
            profile = ChannelProfile.objects.filter(name=wanted).first()
            if profile is None:
                steps.append("channel profile %r not found; skipped" % wanted)
            else:
                added = 0
                for channel in Channel.objects.filter(id__in=channel_ids):
                    _, created = ChannelProfileMembership.objects.get_or_create(
                        channel_profile=profile, channel=channel,
                        defaults={"enabled": True})
                    added += 1 if created else 0
                steps.append("channel profile %r: %d added" % (wanted, added))

        _log(context, "setup finished: %s" % "; ".join(steps))
        return {"status": "ok", "channels": len(channel_ids), "mapped": mapped,
                "unmapped": unmapped, "steps": steps}
