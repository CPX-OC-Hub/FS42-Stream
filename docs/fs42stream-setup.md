# FS42-Stream Setup and Operations

This guide is installation-neutral. Do not copy private IP addresses, hostnames, usernames, channel names, or media paths from another deployment.

## Preflight configuration

Set these values in the environment file used by the systemd unit. The common deployment values are listed first so a fresh install can be configured quickly:

```bash
FS42STREAM_CHANNEL="Your Channel"
FS42STREAM_CHANNEL_SLUG="Your_Channel"
FS42STREAM_SCHEDULE_SCHEME="http"
FS42STREAM_SCHEDULE_HOST="fieldstation42.example.net"
FS42STREAM_SCHEDULE_PORT="4242"
FS42STREAM_SCHEDULE_BASE_PATH=""
FS42STREAM_PUBLIC_BASE_URL="https://stream.example.net"
FS42STREAM_LOGO_FILENAME="logo.png"
FS42STREAM_BRB_IMAGE_PATH="runtime/brb.png"
FS42STREAM_HOST="0.0.0.0"
FS42STREAM_PORT="8088"
FS42STREAM_OUTPUT_ROOT="/var/lib/fs42stream/hls"
FS42STREAM_SCHEDULE_TIMEZONE="Europe/London"
FS42STREAM_API_BASE_URL=""
FS42STREAM_STREAM_PROFILES="jellyfin"
FS42STREAM_AUDIO_NORMALIZATION="off"
```

Configure the other existing variables (`FS42STREAM_VIDEO_ENCODER`, `FS42STREAM_VAAPI_DEVICE`, block limits, and media roots supplied as CLI options) for the host's available hardware and media layout.

`FS42STREAM_STREAM_PROFILES` controls which output runners start. It defaults to `jellyfin` for production. Set it to `direct` for isolated direct-stream debugging, or `both` (equivalent to `direct,jellyfin`) to run both profiles. After changing this systemd environment value, review the generated unit and restart only as an approved operational action.

### Rollback-safe loudnorm trial

`FS42STREAM_AUDIO_NORMALIZATION` defaults to `off`. To enable the trial, set it to `loudnorm`; the runner appends single-pass FFmpeg `loudnorm=I=-16:LRA=11:TP=-1.5` after the existing 48 kHz/stereo formatting for real media audio. Generated silence for no-audio items remains unchanged. The same setting is available as `--audio-normalization off|loudnorm` on the integrated runner and service installer.

Before enabling, generate or inspect the unit and arrange an approved restart plus playback/audio-level verification for every enabled profile. To roll back, set `FS42STREAM_AUDIO_NORMALIZATION="off"` and restart only through the approved operational process. No deployment or restart is performed by this change.

`FS42STREAM_SCHEDULE_*` identifies the FieldStation42 schedule-source endpoint. `FS42STREAM_API_BASE_URL` remains as a deprecated full-URL override for older deployments; leave it blank for new installs. `FS42STREAM_PUBLIC_BASE_URL` is the URL that IPTV/XMLTV clients receive. They intentionally need not be the same address. Leave the public value blank only when clients can use the request `Host` header directly.

`FS42STREAM_BRB_IMAGE_PATH` controls the image shown when a schedule summary has expired and FS42-Stream generates a stale-schedule BRB placeholder. Its default is `runtime/brb.png`, resolved relative to the FS42 root (for example `/mnt/fs42/runtime/brb.png`). To use a channel-specific slate, set a safe relative path such as:

```bash
FS42STREAM_BRB_IMAGE_PATH="catalog/SkyOne/runtime/brb.png"
```

Absolute paths are permitted only below configured FS42 or SDTV media roots; values outside those roots are rejected. The configured image remains an image-loop fallback slate. A configured `--fallback-slate-video` still overrides image-loop rendering as before.

The logo must exist at:

```text
<FS42STREAM_OUTPUT_ROOT>/<FS42STREAM_CHANNEL_SLUG>/<FS42STREAM_LOGO_FILENAME>
```

## Generate service files safely

Use a dry run first and inspect the output:

```bash
python3 scripts/install_systemd_service.py \
  --dry-run \
  --channel "$FS42STREAM_CHANNEL" \
  --channel-slug "$FS42STREAM_CHANNEL_SLUG" \
  --schedule-scheme "$FS42STREAM_SCHEDULE_SCHEME" \
  --schedule-host "$FS42STREAM_SCHEDULE_HOST" \
  --schedule-port "$FS42STREAM_SCHEDULE_PORT" \
  --schedule-base-path "$FS42STREAM_SCHEDULE_BASE_PATH" \
  --public-base-url "$FS42STREAM_PUBLIC_BASE_URL" \
  --logo-filename "$FS42STREAM_LOGO_FILENAME"
```

The installer can write files and can call `systemctl` unless `--skip-systemctl` is specified. Treat deployment/restart as an approval-gated operational action.

## Changing or rolling back the BRB slate

1. Edit `FS42STREAM_BRB_IMAGE_PATH` in the service environment file; keep `runtime/brb.png` to restore the platform default.
2. Confirm the referenced asset is beneath an allowed media root and review the unit/environment rendering with `scripts/install_systemd_service.py --dry-run --brb-image-path "$FS42STREAM_BRB_IMAGE_PATH"`.
3. Obtain operational approval before reloading/restarting the service, then verify all enabled direct/Jellyfin profiles and the stale-schedule diagnostic path.

This source change does not deploy, reload, or restart a live service.

## Endpoints and playback verification

Given public base `<public-base>` and channel slug `<slug>`, Jellyfin is enabled by default:

```text
<public-base>/hls/<slug>/jellyfin/<slug>.m3u8
```

When `FS42STREAM_STREAM_PROFILES` includes `direct`, also inspect:

```text
<public-base>/hls/<slug>/<slug>.m3u8
```

A healthy live service has fresh segments for each enabled profile, advancing playlist tails, no `#EXT-X-ENDLIST`, and no persistent Jellyfin discontinuities. API health is not a substitute for direct-source playback verification.

The API/IPTV/XMLTV endpoints are:

```text
/api/health
/api/channels
/api/channels/<slug>/status
/api/channels/<slug>/runtime
/api/channels/<slug>/health
/iptv/jellyfin/channels.m3u  # Jellyfin IPTV; enabled by default
/iptv/channels.m3u           # direct IPTV; only when FS42STREAM_STREAM_PROFILES includes direct
/iptv/xmltv.xml
```

## HLS retention

Run the cleanup only with the configured output root and slug:

```bash
python3 -m fs42stream.hls_retention \
  --output-root "$FS42STREAM_OUTPUT_ROOT" \
  --channel-slug "$FS42STREAM_CHANNEL_SLUG" \
  --dry-run
```

Cleanup preserves playlists, referenced live-window segments, the logo, and all non-HLS assets.

## Tests

```bash
python3 -m unittest discover -s tests
```

### Legacy fixture note

Sky One naming may appear in explicitly named tests and historical prototype documentation only. It is not a required channel or runtime default.
