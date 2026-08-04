# FS42-Stream

A headless, schedule-following HLS streaming backend. FS42-Stream reads a FieldStation42-compatible schedule API, follows the active wall-clock block, and publishes Jellyfin HLS by default; direct-player HLS is a startup-configurable debugging profile.

FS42-Stream is channel-agnostic: channel name, URL/filesystem slug, upstream schedule API, published URL, logo filename, media roots, encoder, and service paths are installation configuration.

## Configuration

The persistent-service installer writes `/etc/fs42stream/fs42stream.env`. Prioritize the values you usually set first; keep the more installation-specific defaults lower in the file.

| Priority | Variable | Purpose | Portable default |
| --- | --- | --- | --- |
| Common | `FS42STREAM_CHANNEL` | Schedule network name and display name | `Example Channel` |
| Common | `FS42STREAM_CHANNEL_SLUG` | Safe HLS/API/filesystem identifier | `Example_Channel` |
| Common | `FS42STREAM_SCHEDULE_SCHEME` | FieldStation42 schedule API scheme | `http` |
| Common | `FS42STREAM_SCHEDULE_HOST` | FieldStation42 schedule API hostname/IP | `127.0.0.1` |
| Common | `FS42STREAM_SCHEDULE_PORT` | FieldStation42 schedule API port | `4242` |
| Common | `FS42STREAM_PUBLIC_BASE_URL` | External base URL placed in M3U/XMLTV metadata; blank derives it from the HTTP `Host` header | blank |
| Common | `FS42STREAM_LOGO_FILENAME` | Logo file beneath `<output-root>/<channel-slug>/` | `logo.png` |
| Common | `FS42STREAM_STREAM_PROFILES` | Active output runners: `jellyfin`, `direct`, `both`, or `direct,jellyfin` | `jellyfin` |
| Common | `FS42STREAM_BRB_IMAGE_PATH` | Image used for the BRB/fallback slate | `runtime/brb.png` |
| Safety trial | `FS42STREAM_AUDIO_NORMALIZATION` | `off` (default) or single-pass real-audio `loudnorm` | `off` |
| Usually default | `FS42STREAM_SCHEDULE_BASE_PATH` | Optional schedule API base path | blank |
| Usually default | `FS42STREAM_HOST` / `FS42STREAM_PORT` | API/HLS listen address and port | `0.0.0.0` / `8088` |
| Usually default | `FS42STREAM_OUTPUT_ROOT` | HLS output root | `/var/lib/fs42stream/hls` |
| Usually default | `FS42STREAM_SCHEDULE_TIMEZONE` | Timezone for naive schedule timestamps | `Europe/London` |
| Advanced | `FS42STREAM_API_BASE_URL` | Deprecated full schedule API URL override; leave blank unless migrating old config | blank |

`FS42STREAM_PUBLIC_BASE_URL` is recommended behind a reverse proxy, NAT, TLS terminator, or when IPTV/Jellyfin clients cannot use the service's bind address. It must include scheme and any externally visible port, for example `https://stream.example.net`.

### Stale-schedule BRB slate

`FS42STREAM_BRB_IMAGE_PATH` selects the image emitted when the FS42 schedule summary has expired. The portable default is the FS42-root-relative `runtime/brb.png`, resolving to `/mnt/fs42/runtime/brb.png` on the live host. A channel-branded asset can be selected without code changes:

```bash
FS42STREAM_BRB_IMAGE_PATH="catalog/SkyOne/runtime/brb.png"
```

Relative values resolve beneath the configured FS42 root. Absolute values are accepted only beneath configured FS42 or SDTV media roots; unsafe paths such as `/etc/brb.png` are rejected. The configured BRB image is treated as an image-loop fallback slate, so the existing `--fallback-slate-video` behavior still takes precedence when configured.

## Install or generate service files

Review generated files before installing them. This command does not contact a scheduler or start a service:

```bash
python3 scripts/install_systemd_service.py \
  --dry-run \
  --channel "Retro Movies" \
  --channel-slug Retro_Movies \
  --schedule-scheme https \
  --schedule-host scheduler.example.net \
  --schedule-port 443 \
  --schedule-base-path "" \
  --public-base-url "https://stream.example.net" \
  --logo-filename retro-movies.png
```

The generated systemd service runs the integrated API/HLS server and controller. It defaults to the Jellyfin profile only. For direct debugging without a code change, set `FS42STREAM_STREAM_PROFILES="direct"`; set it to `both` (or `direct,jellyfin`) to run both profiles, then perform an approved service restart.

## Optional loudnorm trial and rollback

Audio normalization is disabled by default. To trial single-pass FFmpeg loudnorm for real media audio only, set `FS42STREAM_AUDIO_NORMALIZATION="loudnorm"` in `/etc/fs42stream/fs42stream.env` (or pass `--audio-normalization loudnorm` to the runner/installer). The filter is appended after `aresample=48000,aformat=channel_layouts=stereo` with conservative `I=-16:LRA=11:TP=-1.5` targets. Generated no-audio silence is not modified.

Review the generated unit, then perform a separately approved restart and verify both enabled profile(s), audio levels, and logs. To roll back, set `FS42STREAM_AUDIO_NORMALIZATION="off"` and perform an approved restart; this restores the prior filter graph. This repository change never deploys or restarts a service.

## Run locally

```bash
python3 -m fs42stream.integrated_runner \
  --channel "Retro Movies" \
  --channel-slug Retro_Movies \
  --schedule-scheme http \
  --schedule-host 127.0.0.1 \
  --schedule-port 4242 \
  --schedule-base-path "" \
  --host 127.0.0.1 \
  --port 8088 \
  --output-root /tmp/fs42stream-hls \
  --max-blocks 1 \
  --duration-limit 120 \
  --playout-mode ts-primary \
  --stream-profiles jellyfin
```

`--channel-slug` is optional: if omitted, the runner converts the channel name to a safe underscore-separated slug. Set it explicitly to preserve a pre-existing HLS URL or directory name.

## Published endpoints

For channel slug `<slug>`, the service publishes:

- `/api/channels/<slug>/status`, `/schedule`, `/runtime`, `/health`, `/events`, and `/epg`
- `/hls/<slug>/jellyfin/<slug>.m3u8` (Jellyfin; enabled by default)
- `/hls/<slug>/<slug>.m3u8` (direct; advertised only when `FS42STREAM_STREAM_PROFILES` includes `direct`)
- `/iptv/jellyfin/channels.m3u` (Jellyfin IPTV; enabled by default)
- `/iptv/channels.m3u` (direct IPTV; served only when `FS42STREAM_STREAM_PROFILES` includes `direct`)
- `/iptv/xmltv.xml`

The M3U and XMLTV logo URLs use `FS42STREAM_PUBLIC_BASE_URL`, or the incoming HTTP `Host` header when it is blank. Place the configured logo asset at `<output-root>/<slug>/<logo-filename>`.

## Existing installation migration

Before replacing a unit file generated by an older release, add these values to its environment file (use your existing channel and endpoints):

```bash
FS42STREAM_CHANNEL="Your Channel"
FS42STREAM_CHANNEL_SLUG="Your_Channel"
FS42STREAM_SCHEDULE_SCHEME="http"
FS42STREAM_SCHEDULE_HOST="fieldstation42.example.net"
FS42STREAM_SCHEDULE_PORT="4242"
FS42STREAM_SCHEDULE_BASE_PATH=""
FS42STREAM_API_BASE_URL=""
FS42STREAM_PUBLIC_BASE_URL="https://stream.example.net"
FS42STREAM_LOGO_FILENAME="logo.png"
FS42STREAM_BRB_IMAGE_PATH="runtime/brb.png"
```

To change the live slate, edit only `FS42STREAM_BRB_IMAGE_PATH` (for example, `catalog/SkyOne/runtime/brb.png`), review the environment file and generated unit, then use the approved deployment process for any restart. To roll back, restore `FS42STREAM_BRB_IMAGE_PATH="runtime/brb.png"` and perform the same approved restart and playback verification. This repository change does not deploy or restart services.

## Retention cleanup

Run cleanup against the configured output root and slug:

```bash
python3 -m fs42stream.hls_retention \
  --output-root /var/lib/fs42stream/hls \
  --channel-slug Your_Channel \
  --max-age-seconds 21600 \
  --max-segments-per-dir 7200 \
  --dry-run
```

It only deletes stale, unreferenced `.ts` segments and preserves playlists and non-HLS assets.

## Tests

```bash
python3 -m unittest discover -s tests
```

### Clearly labelled legacy example

Historical tests and `README-phase1-prototype.md` may use `Sky One`/`Sky_One` as fixture data. They are not runtime defaults or installation instructions.
