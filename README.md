# FS42-Stream

Headless FieldStation42 schedule-following HLS streaming backend.

## Goal

Follow the FieldStation42 schedule API exactly, especially `schedule_blocks[*].plan[*]`, while preserving programmes, adverts, bumps, idents, and channel timing.

## Initial environment

- FS42 API: `http://192.168.10.252:4242`
- First channel: `Sky One`
- Streaming host: `fs42stream` / `192.168.10.139`
- Remote admin user: `hermes-admin`
- Media roots observed on `fs42stream`:
  - `/mnt/media/SDTV`
  - `/mnt/fs42`

## Constraints

Avoid screen capture, `ffconcat`, per-advert FFmpeg restarts, and persistent stale HLS packagers.

Current implementation direction: process one FS42 schedule block at a time, decode and normalize all plan items, use FFmpeg concat filter, then publish HLS.

## Phase 2 bounded block runner

Run the live Sky One schedule block for a bounded duration without installing or starting any persistent service:

```bash
python3 -m fs42stream.run_block --channel "Sky One" --duration-limit 120 --output-dir /tmp/fs42stream-hls
```

The runner fetches `http://192.168.10.252:4242/schedules/Sky%20One`, deterministically selects the current block or the next future block, validates only that selected block's `plan[*]` files with `/usr/bin/ffprobe`, preserves `plan[*]` order, resolves media under `/mnt/fs42` and `/mnt/media/SDTV`, then invokes `/usr/bin/ffmpeg` once to emit bounded HLS. Known runtime/off-air image slate entries such as missing `/mnt/fs42/runtime/brb.png` are explicitly replaced with a generated black slate video, or with a validated configured video when `--fallback-slate-video` is supplied; normal programme/ad media is still probed and fails loudly if missing. It prints JSON diagnostics including selection reason, resolved plan order, any runtime slate replacement, generated argv command, and HLS inspection when available. Use `--dry-run` to fetch, select, resolve, and validate without running FFmpeg.
