# FS42-Stream phase-1 prototype notes

> Historical document. This file describes the original phase-1 Python stdlib prototype and does **not** describe the current production service. For current setup, endpoints, operations, and validation, use:
>
> - `README.md`
> - `docs/fs42stream-setup.md`
> - `docs/phase1-validation-checklist.md`

The phase-1 prototype explored turning FieldStation42 schedule blocks into normalized HLS ffmpeg commands.

It provided:

- FS42 schedule client for `/schedules/{channel}`;
- path resolver constrained to `/mnt/fs42` and `/mnt/media/SDTV`;
- ffprobe JSON validation helper;
- block planner preserving `schedule_blocks[*].plan[*]`;
- ffmpeg command builder using concat-filter normalization to 640x480, 25fps, SAR 1:1, yuv420p, 48kHz stereo;
- fixture-backed HLS integration harness that generated synthetic lavfi clips, ran ffmpeg, and inspected playlist/segments.

Historical test commands:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q fs42stream tests
```

Historical harness commands:

```bash
# Print generated HLS ffmpeg argv after creating temporary synthetic clips.
python3 scripts/run_hls_harness.py \
  --work-dir /tmp/fs42-hls-harness \
  --count 3 \
  --duration 1 \
  --dry-run

# Generate HLS in /tmp/fs42-hls-harness/hls and inspect playlist/segments.
python3 scripts/run_hls_harness.py \
  --work-dir /tmp/fs42-hls-harness \
  --count 3 \
  --duration 1
```

The current service has since evolved into a systemd-managed live streamer with API, direct HLS, Jellyfin HLS, IPTV M3U, XMLTV, local logo hosting, shared direct/Jellyfin block lifecycle, and `ts-primary` live playout.
