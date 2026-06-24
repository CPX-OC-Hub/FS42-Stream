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
