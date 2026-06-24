# FS42-Stream

Phase 1 Python stdlib prototype skeleton for turning FieldStation42 schedule blocks into normalized block-level concat-filter HLS ffmpeg commands.

This prototype does **not** deploy or run a persistent packager. It provides:

- FS42 schedule client for `/schedules/{channel}`
- path resolver constrained to `/mnt/fs42` and `/mnt/media/SDTV`
- ffprobe JSON validation helper
- block planner preserving `schedule_blocks[*].plan[*]`
- ffmpeg command builder using per-block concat filter and normalization to 640x480, 25fps, SAR 1:1, yuv420p, 48kHz stereo
- fixture-backed HLS integration harness that generates 2-3 synthetic lavfi clips, runs one ffmpeg HLS command, and inspects playlist/segments

Run tests with:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q fs42stream tests
```

Run the harness without deploying any persistent service:

```sh
# Print the generated HLS ffmpeg argv after creating temporary synthetic clips.
python3 scripts/run_hls_harness.py --work-dir /tmp/fs42-hls-harness --count 3 --duration 1 --dry-run

# Generate HLS in /tmp/fs42-hls-harness/hls and inspect the playlist/segments.
python3 scripts/run_hls_harness.py --work-dir /tmp/fs42-hls-harness --count 3 --duration 1
```
