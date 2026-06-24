#!/usr/bin/env python3
"""Phase 1 QA validation runner for PR #6 / issue #4 gates.

This script is intentionally non-daemonizing: it runs unit tests, exercises the
fixture-backed HLS harness once, and records concrete evidence for path
resolution, schedule fidelity, ffmpeg argv safety, playlist continuity, and
basic timing/readback sanity. It is safe to run immediately after harness changes
because it uses synthetic media under --work-dir by default.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fs42stream.client import FS42ScheduleClient
from fs42stream.ffmpeg import FFMpegHLSCommandBuilder
from fs42stream.ffprobe import FFProbe
from fs42stream.hls_harness import HLSHarness, create_fixture_clips, fixture_schedule, inspect_hls_output
from fs42stream.paths import PathResolver
from fs42stream.planner import BlockPlanner

CANONICAL_API = "http://192.168.10.252:4242"
CANONICAL_CHANNEL = "Sky One"
CANONICAL_CATALOG_ROOT = Path("/mnt/fs42")
CANONICAL_MEDIA_ROOT = Path("/mnt/media/SDTV")
CANONICAL_FFMPEG = "/usr/bin/ffmpeg"
CANONICAL_FFPROBE = "/usr/bin/ffprobe"


@dataclass
class PlaylistInfo:
    target_duration: float | None
    media_sequence: int
    extinf: list[float]
    segments: list[Path]
    has_endlist: bool

    @property
    def duration(self) -> float:
        return sum(self.extinf)


def run_cmd(cmd: Sequence[str], *, cwd: Path | None = None, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), cwd=cwd, timeout=timeout, check=False, shell=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def assert_ok(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def parse_playlist(path: Path) -> PlaylistInfo:
    lines = path.read_text(encoding="utf-8").splitlines()
    extinf: list[float] = []
    segments: list[Path] = []
    target_duration: float | None = None
    media_sequence = 0
    for line in lines:
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            extinf.append(float(line.split(":", 1)[1].split(",", 1)[0]))
        elif line and not line.startswith("#"):
            segments.append((path.parent / line).resolve(strict=False))
    return PlaylistInfo(
        target_duration=target_duration,
        media_sequence=media_sequence,
        extinf=extinf,
        segments=segments,
        has_endlist="#EXT-X-ENDLIST" in lines,
    )


def validate_argv(command: Sequence[str], *, allowed_roots: Sequence[Path], output_dir: Path) -> dict[str, Any]:
    cmd = list(command)
    assert_ok(bool(cmd) and cmd[0] == CANONICAL_FFMPEG, f"ffmpeg executable must be {CANONICAL_FFMPEG}, got {cmd[0] if cmd else '<empty>'}")
    assert_ok("-f" in cmd and "hls" in cmd, "ffmpeg argv must select HLS muxer")
    assert_ok("-filter_complex" in cmd, "ffmpeg argv must use a concat/normalisation filter graph")
    joined = " ".join(cmd)
    for token in ["concat=n=", "scale=640:480", "fps=25", "setsar=1", "format=yuv420p", "aresample=48000", "aformat=channel_layouts=stereo"]:
        assert_ok(token in joined, f"ffmpeg argv missing expected token: {token}")
    assert_ok("/mnt/FS42" not in joined, "ffmpeg argv must not depend on uppercase /mnt/FS42")

    input_paths: list[str] = []
    for idx, token in enumerate(cmd[:-1]):
        if token == "-i":
            input_paths.append(cmd[idx + 1])
    resolved_inputs = [Path(p).resolve(strict=False) for p in input_paths if not p.startswith(("lavfi", "testsrc=", "sine="))]
    resolved_roots = [root.resolve(strict=False) for root in allowed_roots]
    for path in resolved_inputs:
        assert_ok(any(path.is_relative_to(root) for root in resolved_roots), f"input path outside allowed roots: {path}")
    playlist = Path(cmd[-1]).resolve(strict=False)
    assert_ok(playlist.is_relative_to(output_dir.resolve(strict=False)), f"playlist output outside work dir: {playlist}")
    return {"input_paths": input_paths, "playlist": str(playlist), "argv_json": cmd, "argv_shell_escaped": shlex.join(cmd)}


def validate_fixture_hls(args: argparse.Namespace, evidence: dict[str, Any]) -> None:
    work_dir = args.work_dir.resolve(strict=False)
    fixtures_dir = work_dir / "fixtures"
    output_dir = work_dir / "hls"
    clips = create_fixture_clips(fixtures_dir, count=args.count, duration=args.duration, ffmpeg=args.ffmpeg)
    schedule = fixture_schedule(clips, duration=args.duration)
    assert_ok(schedule["network_name"] == CANONICAL_CHANNEL, "fixture schedule must use Sky One")
    assert_ok(len(schedule["schedule_blocks"]) == 1, "fixture schedule should contain exactly one block")
    assert_ok(len(schedule["schedule_blocks"][0]["plan"]) == len(clips), "fixture schedule must preserve one plan entry per clip")

    harness = HLSHarness(
        planner=BlockPlanner(PathResolver(fs42_root=work_dir, sdtv_root=work_dir), FFProbe(args.ffprobe)),
        builder=FFMpegHLSCommandBuilder(args.ffmpeg),
    )
    start = time.monotonic()
    command = harness.run(schedule, output_dir=output_dir, dry_run=False)
    elapsed = time.monotonic() - start
    argv_evidence = validate_argv(command, allowed_roots=[work_dir], output_dir=output_dir)

    playlist = Path(argv_evidence["playlist"])
    inspection = inspect_hls_output(playlist)
    info = parse_playlist(playlist)
    assert_ok(inspection.has_endlist and info.has_endlist, "completed event playlist must include #EXT-X-ENDLIST")
    assert_ok(info.media_sequence >= 0, "media-sequence must be non-negative")
    assert_ok(len(info.segments) == len(inspection.segments) >= 1, "playlist must reference at least one existing segment")
    assert_ok(len(info.extinf) == len(info.segments), "each playlist segment must have an EXTINF duration")
    for segment in info.segments:
        assert_ok(segment.exists(), f"segment missing: {segment}")
        assert_ok(segment.stat().st_size > 0, f"segment is empty: {segment}")

    ffprobe_playlist = run_cmd([args.ffprobe, "-v", "error", "-show_format", "-print_format", "json", str(playlist)], timeout=60)
    assert_ok(ffprobe_playlist.returncode == 0, f"ffprobe playlist readback failed: {ffprobe_playlist.stderr.strip()}")
    ffmpeg_readback = run_cmd([args.ffmpeg, "-v", "error", "-i", str(playlist), "-t", "1", "-f", "null", "-"], timeout=60)
    assert_ok(ffmpeg_readback.returncode == 0, f"ffmpeg playlist readback failed: {ffmpeg_readback.stderr.strip()}")

    expected_duration = args.count * args.duration
    drift = info.duration - expected_duration
    # Synthetic clips can quantize at frame/HLS boundaries; one segment duration should still be close.
    assert_ok(abs(drift) <= max(1.0, args.duration), f"playlist duration drift too large: {drift:.3f}s")

    evidence["fixture_hls"] = {
        "schedule_network": schedule["network_name"],
        "plan_entries": len(schedule["schedule_blocks"][0]["plan"]),
        "created_clips": [str(p) for p in clips],
        "argv": argv_evidence,
        "playlist": str(playlist),
        "target_duration": info.target_duration,
        "media_sequence": info.media_sequence,
        "extinf": info.extinf,
        "playlist_duration_seconds": round(info.duration, 6),
        "expected_duration_seconds": round(expected_duration, 6),
        "duration_drift_seconds": round(drift, 6),
        "segments": [{"path": str(p), "bytes": p.stat().st_size} for p in info.segments],
        "has_endlist": info.has_endlist,
        "ffprobe_playlist_returncode": ffprobe_playlist.returncode,
        "ffmpeg_readback_returncode": ffmpeg_readback.returncode,
        "generation_elapsed_seconds": round(elapsed, 3),
    }


def validate_live_schedule(args: argparse.Namespace, evidence: dict[str, Any]) -> None:
    client = FS42ScheduleClient(args.api_url, timeout=args.api_timeout)
    schedule = client.fetch_schedule(args.channel, expected_blocks=args.expected_blocks)
    blocks = schedule["schedule_blocks"]
    assert_ok(schedule.get("network_name") in (args.channel, CANONICAL_CHANNEL), f"unexpected network_name: {schedule.get('network_name')!r}")
    uppercase_hits: list[str] = []
    outside_hits: list[str] = []
    plan_entries = 0
    resolver = PathResolver(fs42_root=args.catalog_root, sdtv_root=args.media_root)
    roots = [args.catalog_root.resolve(strict=False), args.media_root.resolve(strict=False)]
    for block in blocks:
        for item in block.get("plan", []):
            plan_entries += 1
            raw = item.get("realpath") or item.get("path")
            if raw and "/mnt/FS42" in str(raw):
                uppercase_hits.append(str(raw))
            if raw:
                try:
                    resolved = resolver.resolve(raw)
                    if not any(resolved.is_relative_to(root) for root in roots):
                        outside_hits.append(str(raw))
                except Exception as exc:
                    outside_hits.append(f"{raw} ({exc})")
    assert_ok(not uppercase_hits, f"schedule contains uppercase /mnt/FS42 paths: {uppercase_hits[:5]}")
    assert_ok(not outside_hits, f"schedule contains paths outside canonical roots: {outside_hits[:5]}")
    evidence["live_schedule"] = {
        "api_url": args.api_url,
        "channel": args.channel,
        "blocks": len(blocks),
        "expected_blocks": args.expected_blocks,
        "plan_entries_checked": plan_entries,
        "catalog_root": str(args.catalog_root),
        "media_root": str(args.media_root),
        "uppercase_fs42_hits": len(uppercase_hits),
        "outside_root_hits": len(outside_hits),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Phase 1 validation gates for FS42-Stream PR #6 / issue #4.")
    parser.add_argument("--work-dir", type=Path, default=Path("/tmp/fs42-phase1-validation"), help="scratch directory for synthetic HLS output")
    parser.add_argument("--count", type=int, default=3, help="synthetic clip count")
    parser.add_argument("--duration", type=float, default=1.0, help="synthetic clip duration seconds")
    parser.add_argument("--ffmpeg", default=CANONICAL_FFMPEG)
    parser.add_argument("--ffprobe", default=CANONICAL_FFPROBE)
    parser.add_argument("--api-url", default=CANONICAL_API)
    parser.add_argument("--channel", default=CANONICAL_CHANNEL)
    parser.add_argument("--expected-blocks", type=int, default=338)
    parser.add_argument("--api-timeout", type=float, default=10.0)
    parser.add_argument("--catalog-root", type=Path, default=CANONICAL_CATALOG_ROOT)
    parser.add_argument("--media-root", type=Path, default=CANONICAL_MEDIA_ROOT)
    parser.add_argument("--live-schedule", action="store_true", help="also call the live FS42 API and validate schedule paths/count")
    parser.add_argument("--skip-unit-tests", action="store_true")
    parser.add_argument("--evidence-json", type=Path, default=None, help="optional path to write JSON evidence")
    args = parser.parse_args(argv)

    evidence: dict[str, Any] = {"repo": str(REPO_ROOT), "gates": {}}
    failures: list[str] = []

    for executable in [args.ffmpeg, args.ffprobe]:
        if not Path(executable).exists():
            failures.append(f"missing executable: {executable}")
    if args.ffmpeg != CANONICAL_FFMPEG:
        failures.append(f"ffmpeg must be canonical {CANONICAL_FFMPEG}; got {args.ffmpeg}")
    if args.ffprobe != CANONICAL_FFPROBE:
        failures.append(f"ffprobe must be canonical {CANONICAL_FFPROBE}; got {args.ffprobe}")

    if not args.skip_unit_tests:
        completed = run_cmd([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=REPO_ROOT, timeout=180)
        evidence["unit_tests"] = {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
        if completed.returncode != 0:
            failures.append("unit tests failed")

    try:
        validate_fixture_hls(args, evidence)
        evidence["gates"].update({
            "path_resolution": "passed for fixture roots and argv inputs",
            "schedule_fidelity": "passed for fixture schedule plan preservation",
            "ffmpeg_argv_safety": "passed: argv list, canonical executable, no shell, HLS muxer",
            "hls_playlist_continuity": "passed: playlist entries have existing non-empty segments and non-negative media-sequence",
            "timing_sanity": "passed: playlist EXTINF duration within tolerance of requested fixture duration",
        })
    except Exception as exc:
        failures.append(f"fixture HLS validation failed: {exc}")

    if args.live_schedule:
        try:
            validate_live_schedule(args, evidence)
            evidence["gates"]["live_schedule"] = "passed canonical API/channel/count/path checks"
        except Exception as exc:
            failures.append(f"live schedule validation failed: {exc}")
    else:
        evidence["live_schedule"] = "not run; pass --live-schedule to call FS42 API"

    if args.evidence_json:
        args.evidence_json.parent.mkdir(parents=True, exist_ok=True)
        args.evidence_json.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")

    print(json.dumps(evidence, indent=2, sort_keys=True))
    if failures:
        print("FAILURES:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
