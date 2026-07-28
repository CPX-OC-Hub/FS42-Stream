# FS42-Stream Setup and Operations

This guide is installation-neutral. Do not copy private IP addresses, hostnames, usernames, channel names, or media paths from another deployment.

## Preflight configuration

Set these values in the environment file used by the systemd unit:

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
FS42STREAM_HOST="0.0.0.0"
FS42STREAM_PORT="8088"
FS42STREAM_OUTPUT_ROOT="/var/lib/fs42stream/hls"
FS42STREAM_SCHEDULE_TIMEZONE="Europe/London"
```

Configure the other existing variables (`FS42STREAM_VIDEO_ENCODER`, `FS42STREAM_VAAPI_DEVICE`, block limits, and media roots supplied as CLI options) for the host's available hardware and media layout.

`FS42STREAM_SCHEDULE_*` identifies the FieldStation42 schedule-source endpoint. `FS42STREAM_API_BASE_URL` remains as a deprecated full-URL override for older deployments; leave it blank for new installs. `FS42STREAM_PUBLIC_BASE_URL` is the URL that IPTV/XMLTV clients receive. They intentionally need not be the same address. Leave the public value blank only when clients can use the request `Host` header directly.

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

## Endpoints and playback verification

Given public base `<public-base>` and channel slug `<slug>`, inspect both profiles independently:

```text
<public-base>/hls/<slug>/<slug>.m3u8
<public-base>/hls/<slug>/jellyfin/<slug>.m3u8
```

A healthy live service has fresh segments for both, advancing playlist tails, no `#EXT-X-ENDLIST`, and no persistent Jellyfin discontinuities. API health is not a substitute for direct-source playback verification.

The API/IPTV/XMLTV endpoints are:

```text
/api/health
/api/channels
/api/channels/<slug>/status
/api/channels/<slug>/runtime
/api/channels/<slug>/health
/iptv/channels.m3u
/iptv/jellyfin/channels.m3u
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
