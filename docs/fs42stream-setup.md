# fs42stream Setup and Operations Notes

These notes describe the current `fs42stream` deployment on `192.168.10.139`.

## Host and service

```text
Host:        fs42stream / 192.168.10.139
SSH user:    hermes-admin
Install dir: /opt/fs42stream
Output root: /var/lib/fs42stream/hls
Service:     fs42stream.service
Channel:     Sky One
Mode:        ts-primary
Timezone:    Europe/London
```

The service runs the integrated API/HLS server and live controller:

```bash
/usr/bin/python3 -m fs42stream.integrated_runner \
  --channel "Sky One" \
  --host 0.0.0.0 \
  --port 8088 \
  --output-root /var/lib/fs42stream/hls \
  --max-blocks 1000000 \
  --duration-limit 7200 \
  --video-encoder h264_vaapi \
  --vaapi-device /dev/dri/renderD128 \
  --schedule-timezone Europe/London \
  --playout-mode ts-primary
```

## FFmpeg / FFprobe

Passwordless sudo is enabled for `hermes-admin`. The system packages are installed and should be preferred:

```text
/usr/bin/ffmpeg
/usr/bin/ffprobe
```

The older user-local static build may still exist under `/home/hermes-admin/.local/bin`, but it is not the normal production path.

## Media paths

Canonical roots on `fs42stream`:

```text
FS42 catalog root: /mnt/fs42
Media/show root:   /mnt/media/SDTV
```

The streamer must not depend on uppercase `/mnt/FS42`.

## Source schedule API

The upstream FieldStation42 schedule API is currently:

```text
http://192.168.10.252:4242
```

The local stream service is separate and publishes its own playback/API/IPTV/XMLTV endpoints from:

```text
http://192.168.10.139:8088
```

Do not use the `.252` source host for local stream/icon/XMLTV links. Future issue `#57` tracks replacing hardcoded IPs with configurable public base URLs.

## Local logo asset

The Sky One logo is served locally from the HLS tree:

```text
/var/lib/fs42stream/hls/Sky_One/skyone.png
http://192.168.10.139:8088/hls/Sky_One/skyone.png
```

The file was copied from the upstream source host and should be present on the stream server. IPTV M3U and XMLTV should reference the `.139:8088` URL.

## Systemd checks

Check service state:

```bash
systemctl is-active fs42stream.service
systemctl show -p MainPID --value fs42stream.service
systemctl show -p ExecMainStartTimestamp --value fs42stream.service
```

Restart after tests pass:

```bash
sudo systemctl restart fs42stream.service
```

If approval-gated tooling blocks `systemctl restart`, stop and request approval instead of using a workaround unless explicitly directed.

## Health checks

Core endpoints:

```text
http://192.168.10.139:8088/api/health
http://192.168.10.139:8088/api/channels/Sky_One/status
http://192.168.10.139:8088/api/channels/Sky_One/runtime
http://192.168.10.139:8088/api/channels/Sky_One/events
```

Direct playback:

```text
http://192.168.10.139:8088/hls/Sky_One/Sky_One.m3u8
```

Jellyfin playback:

```text
http://192.168.10.139:8088/hls/Sky_One/jellyfin/Sky_One.m3u8
```

IPTV/XMLTV:

```text
http://192.168.10.139:8088/iptv/channels.m3u
http://192.168.10.139:8088/iptv/jellyfin/channels.m3u
http://192.168.10.139:8088/iptv/xmltv.xml
```

## Direct/Jellyfin verification

When checking playback, verify both profiles separately. API health alone is not enough.

A healthy live state should show:

- direct and Jellyfin ffmpeg processes on the same active block/media;
- fresh segment mtimes for both profile directories;
- playlist media sequence/tail advancing for both profiles;
- no `#EXT-X-ENDLIST` in either playlist;
- no persistent `#EXT-X-DISCONTINUITY` in Jellyfin;
- no active `color=c=black` or `runtime/brb.png` process unless expected filler/slate is intentionally active.

Quick playlist freshness check:

```bash
python3 - <<'PY'
import re, time, urllib.request
urls = [
    ('direct', 'http://192.168.10.139:8088/hls/Sky_One/Sky_One.m3u8'),
    ('jellyfin', 'http://192.168.10.139:8088/hls/Sky_One/jellyfin/Sky_One.m3u8'),
]
last = {}
for i in range(3):
    print('SAMPLE', i)
    for label, url in urls:
        text = urllib.request.urlopen(url, timeout=15).read().decode('utf-8', 'replace')
        seq = re.search(r'#EXT-X-MEDIA-SEQUENCE:(\d+)', text)
        segs = re.findall(r'([^\n]+\.ts)', text)
        tail = int(re.search(r'_(\d+)\.ts$', segs[-1]).group(1)) if segs else None
        print(label, 'seq', seq.group(1) if seq else None, 'tail', tail, 'delta', None if label not in last else tail - last[label], 'count', len(segs), 'endlist', '#EXT-X-ENDLIST' in text, 'discont', text.count('#EXT-X-DISCONTINUITY'))
        if tail is not None:
            last[label] = tail
    time.sleep(4)
PY
```

## Known failure modes and checks

### Profile drift

Symptom: direct continues while Jellyfin shows old frames or stops.

Check process inputs and segment mtimes for both profile directories. The integrated runner now uses a shared schedule/block lifecycle to prevent silent direct/Jellyfin drift.

### Pipe deadlock

Symptom: Jellyfin ffmpeg is still running but playlist tail stops advancing.

Check `/proc/<pid>/wchan`; if it is `pipe_write`, ffmpeg output is blocked. The live normalization path should write ffmpeg stdout/stderr to temp files rather than undrained pipes.

### Black filler

Symptom: both direct and Jellyfin decode but show black.

Check whether ffmpeg is using `-f lavfi -i color=c=black`. If yes, determine whether this is expected boundary filler or a premature block completion/filler regression.

### Stale schedule

The service should prefer explicit placeholder/BRB behavior with degraded health when the upstream schedule is stale. It should not silently render the wrong old block indefinitely.

## Test commands

Targeted suites used for recent service work:

```bash
python3 -m unittest \
  tests.test_api_server \
  tests.test_run_block \
  tests.test_live_controller \
  tests.test_integrated_runner \
  tests.test_systemd_service
```

Full suite:

```bash
python3 -m unittest discover -s tests
```
