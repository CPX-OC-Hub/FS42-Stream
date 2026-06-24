# Phase 1 QA validation checklist (PR #6 / issue #4)

Use this immediately after HLS harness changes. It does not deploy a persistent service.

## Automated runner

```sh
python3 scripts/phase1_validate.py --work-dir /tmp/fs42-phase1-validation --evidence-json /tmp/fs42-phase1-validation-evidence.json
```

To include the live FieldStation42 schedule API gate:

```sh
python3 scripts/phase1_validate.py --live-schedule --api-timeout 10 --work-dir /tmp/fs42-phase1-validation --evidence-json /tmp/fs42-phase1-validation-evidence.json
```

Canonical defaults baked into the runner:

- FS42 API: `http://192.168.10.252:4242`
- channel: `Sky One`
- catalog root: `/mnt/fs42`
- media root: `/mnt/media/SDTV`
- no uppercase `/mnt/FS42` dependency
- ffmpeg: `/usr/bin/ffmpeg`
- ffprobe: `/usr/bin/ffprobe`

## Gates and required evidence

1. **Unit output**
   - `python3 -m unittest discover -s tests -v` return code is `0`.
   - Runner embeds stdout/stderr in the evidence JSON.

2. **Path resolution**
   - HLS argv input paths resolve under the synthetic work root for harness runs.
   - Live schedule mode verifies every `path`/`realpath` resolves under `/mnt/fs42` or `/mnt/media/SDTV`.
   - Live schedule mode fails on any uppercase `/mnt/FS42` path.

3. **Schedule fidelity**
   - Fixture schedule uses network `Sky One`.
   - Fixture plan entry count equals generated clip count.
   - Live schedule mode checks the API returns the expected 338 blocks and records total plan entries checked.

4. **ffmpeg argv safety**
   - Command is an argv list executed with `shell=False` by the harness.
   - First argv element must be `/usr/bin/ffmpeg`.
   - Command must contain HLS muxer flags plus the expected normalisation/concat filter tokens.
   - Evidence JSON includes both `argv_json` and shell-escaped display form.

5. **HLS playlist continuity**
   - Playlist exists and contains `#EXT-X-ENDLIST`.
   - Media sequence is non-negative.
   - Every playlist media entry has an `#EXTINF` and points to an existing non-empty segment.
   - Evidence JSON records playlist path, media sequence, EXTINF values, segment paths and byte sizes.

6. **Readback and timing sanity**
   - `/usr/bin/ffprobe` can read the generated playlist.
   - `/usr/bin/ffmpeg -v error -i <playlist> -t 1 -f null -` can read back the playlist.
   - Playlist `EXTINF` sum is compared to requested fixture duration; drift is recorded and bounded by the runner.

## Manual review notes

- The synthetic run is a harness integrity gate; it does not prove production media availability.
- Use `--live-schedule` on the FS42 network to validate canonical API/channel/block count and real schedule paths.
- Do not claim a live/prod gate passed unless the command was actually run and the output/evidence JSON is available.
