# FS42-Stream

Phase 1 Python stdlib prototype skeleton for turning FieldStation42 schedule blocks into normalized block-level concat-filter HLS ffmpeg commands.

This prototype does **not** deploy or run a persistent packager. It provides:

- FS42 schedule client for `/schedules/{channel}`
- path resolver constrained to `/mnt/fs42` and `/mnt/media/SDTV`
- ffprobe JSON validation helper
- block planner preserving `schedule_blocks[*].plan[*]`
- ffmpeg command builder using per-block concat filter and normalization to 640x480, 25fps, SAR 1:1, yuv420p, 48kHz stereo

Run tests with:

```sh
python3 -m unittest discover -s tests -v
```
