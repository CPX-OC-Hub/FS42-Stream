# FS42-Stream validation checklist

Use this checklist after changes to the live HLS renderer, controller, API server, IPTV/XMLTV generation, or systemd service wiring.

For the original phase-1 prototype-only harness notes, see `README-phase1-prototype.md`.

## Automated tests

Run the targeted suites used for current service work:

```bash
python3 -m unittest \
  tests.test_api_server \
  tests.test_run_block \
  tests.test_live_controller \
  tests.test_integrated_runner \
  tests.test_systemd_service
```

Run the full suite before opening/merging broader changes:

```bash
python3 -m unittest discover -s tests
```

Optional historical phase-1 harness:

```bash
python3 scripts/phase1_validate.py \
  --work-dir /tmp/fs42-phase1-validation \
  --evidence-json /tmp/fs42-phase1-validation-evidence.json
```

To include the live FieldStation42 schedule API gate:

```bash
python3 scripts/phase1_validate.py \
  --live-schedule \
  --api-timeout 10 \
  --work-dir /tmp/fs42-phase1-validation \
  --evidence-json /tmp/fs42-phase1-validation-evidence.json
```

## Production defaults to verify

```text
Source FS42 API: http://192.168.10.252:4242
Stream host:     http://192.168.10.139:8088
Channel:         Sky One
Channel slug:    Sky_One
Catalog root:    /mnt/fs42
Media root:      /mnt/media/SDTV
ffmpeg:          /usr/bin/ffmpeg
ffprobe:         /usr/bin/ffprobe
Service:         fs42stream.service
Playout mode:    ts-primary
Timezone:        Europe/London
```

## API and metadata checks

Verify these endpoints return HTTP 200:

```text
http://192.168.10.139:8088/api/health
http://192.168.10.139:8088/api/channels
http://192.168.10.139:8088/api/channels/Sky_One/status
http://192.168.10.139:8088/api/channels/Sky_One/schedule
http://192.168.10.139:8088/api/channels/Sky_One/runtime
http://192.168.10.139:8088/api/channels/Sky_One/events
http://192.168.10.139:8088/iptv/channels.m3u
http://192.168.10.139:8088/iptv/jellyfin/channels.m3u
http://192.168.10.139:8088/iptv/xmltv.xml
http://192.168.10.139:8088/hls/Sky_One/skyone.png
```

Metadata expectations:

- channel ID is stable: `fs42.sky_one`;
- channel name is `Sky One`;
- logo URL is local to the stream server: `http://192.168.10.139:8088/hls/Sky_One/skyone.png`;
- IPTV M3U and XMLTV do not reference `192.168.10.252:8080/hls/skyone.png`;
- XMLTV contains programme rows from the full upstream schedule when the schedule fetch succeeds.

## Live HLS checks

Direct playlist:

```text
http://192.168.10.139:8088/hls/Sky_One/Sky_One.m3u8
```

Jellyfin playlist:

```text
http://192.168.10.139:8088/hls/Sky_One/jellyfin/Sky_One.m3u8
```

Required live properties:

- both playlists return HTTP 200;
- neither playlist contains `#EXT-X-ENDLIST`;
- Jellyfin has no persistent `#EXT-X-DISCONTINUITY`;
- segment numbers are dense and increasing;
- both direct and Jellyfin tails advance over repeated samples;
- both profiles are on the same active block/media;
- segment files exist and have fresh mtimes;
- frames decode successfully from both endpoints.

Quick segment-tail sampler:

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

Quick frame decode:

```bash
mkdir -p /tmp/fs42-verify
ffmpeg -y -loglevel error \
  -i 'http://192.168.10.139:8088/hls/Sky_One/Sky_One.m3u8' \
  -frames:v 1 /tmp/fs42-verify/direct.jpg
ffmpeg -y -loglevel error \
  -i 'http://192.168.10.139:8088/hls/Sky_One/jellyfin/Sky_One.m3u8' \
  -frames:v 1 /tmp/fs42-verify/jelly.jpg
```

## Process checks

On the stream host:

```bash
systemctl is-active fs42stream.service
systemctl show -p MainPID --value fs42stream.service
ps -eo pid,ppid,stat,lstart,wchan,args | grep -E 'fs42stream|ffmpeg' | grep -v grep
```

Healthy process expectations:

- one integrated runner process under `fs42stream.service`;
- direct and Jellyfin ffmpeg child processes when active;
- direct and Jellyfin child commands should reference the same current programme/item around the same wall-clock time;
- no long-lived ffmpeg process stuck in `pipe_write`;
- no active `color=c=black` or `runtime/brb.png` process unless boundary filler/slate is expected.

## Schedule and path checks

- `/api/channels/Sky_One/status` exposes the active block, current plan item, timeline, catch-up information, and recent events.
- The active block should match the upstream FS42 schedule for `Europe/London` local time.
- Plan item paths should resolve under `/mnt/fs42` or `/mnt/media/SDTV`.
- The service must not depend on uppercase `/mnt/FS42`.
- Known runtime/off-air image slate entries should be replaced with generated or configured video; normal media should still fail loudly if missing.

## Boundary checks

For renderer/controller changes, do not stop after immediate startup validation. Hold through at least one relevant boundary and confirm:

- direct and Jellyfin stay on the same block lifecycle;
- neither profile silently drifts to stale content;
- both playlists keep advancing;
- no `#EXT-X-ENDLIST` appears;
- Jellyfin does not freeze at show-to-show transitions;
- filler is only used intentionally and clears after the boundary.

## Evidence to record

For any claimed production fix, capture:

- commit SHA and branch;
- local and remote test output;
- service PID/start timestamp after restart;
- active block/status payload summary;
- direct/Jellyfin playlist samples before and after the fix;
- process table snippets showing current ffmpeg inputs;
- frame decode success from both endpoints;
- any GitHub issue/PR links.
