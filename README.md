# FS42-Stream

Headless FieldStation42 schedule-following HLS streaming backend for Sky One.

FS42-Stream reads the FieldStation42 schedule API, follows the current wall-clock block, preserves the upstream `schedule_blocks[*].plan[*]` order, and publishes live HLS outputs for direct players and Jellyfin.

## Current production shape

- Source schedule API: `http://192.168.10.252:4242`
- Streaming host: `fs42stream` / `192.168.10.139`
- Service port: `8088`
- Channel: `Sky One`
- Channel slug: `Sky_One`
- Output root: `/var/lib/fs42stream/hls`
- Service user: `hermes-admin`
- Service unit: `fs42stream.service`
- Current playout mode: `ts-primary`
- Schedule timezone: `Europe/London`

## Public endpoints

Use the full URLs below when testing from clients on the LAN.

| Purpose | URL |
| --- | --- |
| Health | `http://192.168.10.139:8088/api/health` |
| Channel list | `http://192.168.10.139:8088/api/channels` |
| Live status | `http://192.168.10.139:8088/api/channels/Sky_One/status` |
| Derived schedule | `http://192.168.10.139:8088/api/channels/Sky_One/schedule` |
| Runtime diagnostics | `http://192.168.10.139:8088/api/channels/Sky_One/runtime` |
| Recent events | `http://192.168.10.139:8088/api/channels/Sky_One/events` |
| Direct HLS playlist | `http://192.168.10.139:8088/hls/Sky_One/Sky_One.m3u8` |
| Jellyfin HLS playlist | `http://192.168.10.139:8088/hls/Sky_One/jellyfin/Sky_One.m3u8` |
| Direct IPTV M3U | `http://192.168.10.139:8088/iptv/channels.m3u` |
| Jellyfin IPTV M3U | `http://192.168.10.139:8088/iptv/jellyfin/channels.m3u` |
| XMLTV | `http://192.168.10.139:8088/iptv/xmltv.xml` |
| Sky One logo | `http://192.168.10.139:8088/hls/Sky_One/skyone.png` |

The IPTV and XMLTV outputs should reference the local FS42-Stream logo URL above, not the upstream `.252` schedule/source host.

## Architecture notes

### Wall-clock schedule following

The service treats the FS42 schedule as authoritative. It selects the active block using schedule-local wall-clock time, then derives the current plan item and media offset from cumulative plan durations inside that block.

This is important for restarts and mid-block joins:

- elapsed plan items are dropped;
- the current item is seeked to the correct offset;
- resumed feature items preserve their existing `skip` values;
- commercials, bumps, and idents remain in the original upstream order.

### Direct and Jellyfin outputs

The integrated runner publishes two live profiles:

- `direct` → `/hls/Sky_One/Sky_One.m3u8`
- `jellyfin` → `/hls/Sky_One/jellyfin/Sky_One.m3u8`

Both profiles share one schedule/block lifecycle clock so they cannot silently drift onto different blocks. If one profile stalls, that is a service fault to investigate; do not trust the direct/primary status alone.

### `ts-primary` playout

`ts-primary` is the current production path. It renders planned items into MPEG-TS/HLS with per-item tee commands, appending to the same public playlist while preserving dense segment numbering.

Important live HLS rules:

- public playlists must not contain `#EXT-X-ENDLIST` while live;
- Jellyfin playlists should not expose persistent `#EXT-X-DISCONTINUITY` tags;
- segment numbers must advance monotonically;
- HLS commands must be rebuilt from emitted playlist state before appended item runs;
- ffmpeg output pipes must not be left undrained, or ffmpeg can block in `pipe_write` and freeze playback.

## Running locally

Run the bounded block runner without installing a persistent service:

```bash
python3 -m fs42stream.run_block \
  --channel "Sky One" \
  --duration-limit 120 \
  --output-dir /tmp/fs42stream-hls \
  --playout-mode ts-primary
```

Run the foreground API/status/HLS server:

```bash
python3 -m fs42stream.api_server \
  --host 127.0.0.1 \
  --port 8088 \
  --output-root /tmp/fs42stream-live \
  --status-json /tmp/fs42stream-live/status.json
```

Run the integrated foreground server + live controller:

```bash
python3 -m fs42stream.integrated_runner \
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

## Tests

Run the targeted service suites:

```bash
python3 -m unittest \
  tests.test_api_server \
  tests.test_run_block \
  tests.test_live_controller \
  tests.test_integrated_runner \
  tests.test_systemd_service
```

Run the full test suite:

```bash
python3 -m unittest discover -s tests
```

## Deployment notes

Production service files live under `/opt/fs42stream` on `192.168.10.139`.

Typical service checks:

```bash
systemctl is-active fs42stream.service
systemctl show -p MainPID --value fs42stream.service
systemctl show -p ExecMainStartTimestamp --value fs42stream.service
```

Restart only after tests pass:

```bash
sudo systemctl restart fs42stream.service
```

After restart, verify both direct and Jellyfin playlists advance and remain live-clean:

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
        segs = re.findall(r'([^\n]+\.ts)', text)
        tail = int(re.search(r'_(\d+)\.ts$', segs[-1]).group(1)) if segs else None
        print(label, 'tail', tail, 'delta', None if label not in last else tail - last[label], 'endlist', '#EXT-X-ENDLIST' in text, 'discont', text.count('#EXT-X-DISCONTINUITY'))
        if tail is not None:
            last[label] = tail
    time.sleep(4)
PY
```

## Current known future work

- GitHub issue `#56`: optimise FS42-Stream service performance and reliability.
- GitHub issue `#57`: remove hardcoded IP addresses and configure public base URLs.

## Historical notes

`README-phase1-prototype.md` documents the original phase-1 prototype and is retained for historical context only. The active service behavior is described in this README and `docs/fs42stream-setup.md`.
