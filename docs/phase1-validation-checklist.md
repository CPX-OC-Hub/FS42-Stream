# FS42-Stream validation checklist

Use this checklist after changes to the live renderer, controller, API server, IPTV/XMLTV generation, or service wiring. It is installation-neutral: substitute values from the active environment file; do not use another installation's host, channel, media roots, or credentials.

## Automated verification

```bash
python3 -m unittest \
  tests.test_api_server \
  tests.test_run_block \
  tests.test_live_controller \
  tests.test_integrated_runner \
  tests.test_systemd_service

python3 -m unittest discover -s tests
```

## Record the active configuration

Before live verification, record the reviewed values of:

```text
FS42STREAM_CHANNEL
FS42STREAM_CHANNEL_SLUG
FS42STREAM_SCHEDULE_SCHEME
FS42STREAM_SCHEDULE_HOST
FS42STREAM_SCHEDULE_PORT
FS42STREAM_SCHEDULE_BASE_PATH
FS42STREAM_API_BASE_URL
FS42STREAM_PUBLIC_BASE_URL
FS42STREAM_LOGO_FILENAME
FS42STREAM_OUTPUT_ROOT
FS42STREAM_SCHEDULE_TIMEZONE
FS42STREAM_PLAYOUT_MODE
```

For public base `<public-base>` and slug `<slug>`, check API and generated metadata:

```text
<public-base>/api/health
<public-base>/api/channels
<public-base>/api/channels/<slug>/status
<public-base>/api/channels/<slug>/runtime
<public-base>/api/channels/<slug>/health
<public-base>/iptv/channels.m3u
<public-base>/iptv/jellyfin/channels.m3u
<public-base>/iptv/xmltv.xml
```

Confirm that M3U/XMLTV use the configured public base and logo location, never the schedule-source endpoint.

## Direct and Jellyfin playback checks

Verify both profiles independently:

```text
<public-base>/hls/<slug>/<slug>.m3u8
<public-base>/hls/<slug>/jellyfin/<slug>.m3u8
```

Required properties:

- both playlists return HTTP 200 and advance over repeated samples;
- neither live playlist contains `#EXT-X-ENDLIST`;
- Jellyfin has no persistent `#EXT-X-DISCONTINUITY`;
- segment files exist and have fresh mtimes;
- both profiles represent the same active schedule block;
- a frame decodes from both sources.

Use this portable sampler by setting `PUBLIC_BASE_URL` and `CHANNEL_SLUG`:

```bash
PUBLIC_BASE_URL="https://stream.example.net"
CHANNEL_SLUG="Your_Channel"
python3 - <<'PY'
import os, re, time, urllib.request
base = os.environ['PUBLIC_BASE_URL'].rstrip('/')
slug = os.environ['CHANNEL_SLUG']
urls = [('direct', f'{base}/hls/{slug}/{slug}.m3u8'), ('jellyfin', f'{base}/hls/{slug}/jellyfin/{slug}.m3u8')]
last = {}
for i in range(3):
    print('SAMPLE', i)
    for label, url in urls:
        text = urllib.request.urlopen(url, timeout=15).read().decode('utf-8', 'replace')
        segments = re.findall(r'([^\n]+\.ts)', text)
        tail = int(re.search(r'_(\d+)\.ts$', segments[-1]).group(1)) if segments else None
        print(label, 'tail', tail, 'endlist', '#EXT-X-ENDLIST' in text, 'discont', text.count('#EXT-X-DISCONTINUITY'))
        last[label] = tail
    time.sleep(4)
PY
```

## Evidence and release gate

For any later deployment, retain test output, reviewed configuration, status payloads, direct/Jellyfin playlist samples, and frame-decode evidence. Do not treat API health as a replacement for direct-source playback verification.

### Historical fixture note

The phase-1 prototype documentation may contain Sky One fixture data. It is historical test evidence, not a runtime default or deployment instruction.
