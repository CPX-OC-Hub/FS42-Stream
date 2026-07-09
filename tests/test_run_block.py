import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from fs42stream.ffmpeg import FFMpegHLSCommandBuilder
from fs42stream.ffprobe import ProbeResult
from fs42stream.hls_harness import create_fixture_clips
from fs42stream.paths import PathResolver
from fs42stream.planner import BlockPlanner
from fs42stream.run_block import BlockRunConfig, BlockRunner, _hls_segment_duration_since, _normalize_jellyfin_live_playlist, _rewrite_live_playlist_boundaries, select_current_or_next_block


SCHEDULE = {
    "network_name": "Sky One",
    "schedule_blocks": [
        {
            "title": "Earlier Block",
            "start_time": "2026-06-17T09:00:00",
            "end_time": "2026-06-17T09:30:00",
            "plan": [
                {"path": "catalog/SkyOne/earlier.mp4", "duration": 1800, "skip": 0, "is_stream": False},
            ],
        },
        {
            "title": "Current Block",
            "start_time": "2026-06-17T10:00:00",
            "end_time": "2026-06-17T10:30:00",
            "plan": [
                {"path": "catalog/SkyOne/current-first.mp4", "duration": 30, "skip": 0, "is_stream": False},
                {"realpath": "/mnt/media/SDTV/Current Second.mp4", "duration": 60, "skip": 5, "is_stream": False},
            ],
        },
        {
            "title": "Next Block",
            "start_time": "2026-06-17T11:00:00",
            "end_time": "2026-06-17T11:30:00",
            "plan": [
                {"path": "catalog/SkyOne/next.mp4", "duration": 1800, "skip": 0, "is_stream": False},
            ],
        },
    ],
}


class BlockSelectionTests(unittest.TestCase):
    def test_selects_current_block_when_now_is_inside_block_window(self):
        selected = select_current_or_next_block(SCHEDULE, now=datetime(2026, 6, 17, 10, 5, 0))
        self.assertEqual(selected.index, 1)
        self.assertEqual(selected.block["title"], "Current Block")
        self.assertEqual(selected.reason, "current")

    def test_selects_next_future_block_when_no_block_is_current(self):
        selected = select_current_or_next_block(SCHEDULE, now=datetime(2026, 6, 17, 10, 45, 0))
        self.assertEqual(selected.index, 2)
        self.assertEqual(selected.block["title"], "Next Block")
        self.assertEqual(selected.reason, "next")


class BlockRunnerTests(unittest.TestCase):
    def test_runner_fetches_schedule_plans_only_selected_block_and_emits_json_diagnostics(self):
        client = mock.Mock()
        client.fetch_schedule.return_value = SCHEDULE
        probe = mock.Mock()
        probe.validate_video.return_value = ProbeResult(duration=60, width=640, height=480, fps=25, audio_sample_rate=48000, audio_channels=2)
        runner = BlockRunner(
            client=client,
            planner=BlockPlanner(PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV"), probe),
            builder=FFMpegHLSCommandBuilder("/usr/bin/ffmpeg"),
        )

        completed = subprocess.CompletedProcess(["ffmpeg"], 0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=completed) as run:
            diagnostics = runner.run(
                BlockRunConfig(channel="Sky One", duration_limit=120, output_dir=Path("/tmp/fs42stream-hls"), now=datetime(2026, 6, 17, 10, 10, 0))
            )

        client.fetch_schedule.assert_called_once_with("Sky One", expected_blocks=None)
        self.assertEqual(probe.validate_video.call_count, 2)
        self.assertEqual([call.args[0] for call in probe.validate_video.call_args_list], [Path("/mnt/fs42/catalog/SkyOne/current-first.mp4"), Path("/mnt/media/SDTV/Current Second.mp4")])
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/ffmpeg")
        self.assertIn(["-t", "30"], [command[index:index + 2] for index in range(len(command) - 1)])
        self.assertIn(["-t", "60"], [command[index:index + 2] for index in range(len(command) - 1)])
        self.assertIn("/mnt/fs42/catalog/SkyOne/current-first.mp4", command)
        self.assertIn("/mnt/media/SDTV/Current Second.mp4", command)
        self.assertIn("concat=n=2:v=1:a=1", " ".join(command))
        self.assertIn("-hls_flags", command)
        self.assertEqual(diagnostics["status"], "ok")
        self.assertEqual(diagnostics["channel"], "Sky One")
        self.assertEqual(diagnostics["selection"]["reason"], "current")
        self.assertEqual(diagnostics["selection"]["index"], 1)
        self.assertEqual([item["resolved_path"] for item in diagnostics["plan"]], ["/mnt/fs42/catalog/SkyOne/current-first.mp4", "/mnt/media/SDTV/Current Second.mp4"])
        json.dumps(diagnostics)

    def test_runner_dry_run_does_not_execute_ffmpeg(self):
        client = mock.Mock(fetch_schedule=mock.Mock(return_value=SCHEDULE))
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        runner = BlockRunner(client=client, planner=BlockPlanner(PathResolver(), probe), builder=FFMpegHLSCommandBuilder())
        with mock.patch("subprocess.run") as run:
            diagnostics = runner.run(BlockRunConfig(channel="Sky One", duration_limit=15, output_dir=Path("/tmp/out"), now=datetime(2026, 6, 17, 11, 5, 0), dry_run=True))
        run.assert_not_called()
        self.assertEqual(diagnostics["status"], "dry-run")
        self.assertEqual(diagnostics["selection"]["reason"], "current")

    def test_runner_preserves_nonzero_ffmpeg_returncode_in_diagnostics(self):
        client = mock.Mock(fetch_schedule=mock.Mock(return_value=SCHEDULE))
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        runner = BlockRunner(client=client, planner=BlockPlanner(PathResolver(), probe), builder=FFMpegHLSCommandBuilder())
        completed = subprocess.CompletedProcess(["ffmpeg"], 42, stdout="", stderr="boom")

        with mock.patch("subprocess.run", return_value=completed):
            diagnostics = runner.run(BlockRunConfig(channel="Sky One", duration_limit=15, output_dir=Path("/tmp/out"), now=datetime(2026, 6, 17, 11, 5, 0)))

        self.assertEqual(diagnostics["status"], "ffmpeg-error")
        self.assertEqual(diagnostics["ffmpeg"]["returncode"], 42)
        self.assertEqual(diagnostics["ffmpeg"]["stderr"], "boom")

    def test_runner_reports_runtime_fallback_replacement_in_json_diagnostics(self):
        schedule = {
            "network_name": "Sky One",
            "schedule_blocks": [
                {
                    "title": "Off Air",
                    "start_time": "2026-06-17T10:00:00",
                    "end_time": "2026-06-17T10:30:00",
                    "plan": [
                        {"path": "runtime/brb.png", "duration": 12, "skip": 0, "is_stream": False, "content_type": "slate", "media_type": "image"},
                    ],
                }
            ],
        }
        client = mock.Mock(fetch_schedule=mock.Mock(return_value=schedule))
        probe = mock.Mock()
        runner = BlockRunner(
            client=client,
            planner=BlockPlanner(PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV"), probe),
            builder=FFMpegHLSCommandBuilder("/usr/bin/ffmpeg"),
        )

        diagnostics = runner.run(BlockRunConfig(channel="Sky One", duration_limit=15, output_dir=Path("/tmp/out"), now=datetime(2026, 6, 17, 10, 5, 0), dry_run=True))

        probe.validate_video.assert_not_called()
        self.assertEqual(diagnostics["plan"][0]["resolved_path"], "/mnt/fs42/runtime/brb.png")
        self.assertEqual(diagnostics["plan"][0]["input_kind"], "lavfi")
        self.assertEqual(diagnostics["plan"][0]["runtime_action"], "generated_fallback_slate")
        self.assertEqual(diagnostics["plan"][0]["diagnostic"], "known runtime/off-air image slate replaced with generated fallback video")
        self.assertIn("lavfi", diagnostics["command"])

    def test_runner_replaces_runtime_png_even_when_schedule_labels_it_as_commercial_video(self):
        schedule = {
            "network_name": "Sky One",
            "schedule_blocks": [
                {
                    "title": "Bad Runtime Metadata",
                    "start_time": "2026-06-17T10:00:00",
                    "end_time": "2026-06-17T10:30:00",
                    "plan": [
                        {"path": "runtime/brb.png", "duration": 8, "skip": 0, "is_stream": False, "content_type": "commercial", "media_type": "video"},
                    ],
                }
            ],
        }
        client = mock.Mock(fetch_schedule=mock.Mock(return_value=schedule))
        probe = mock.Mock()
        runner = BlockRunner(
            client=client,
            planner=BlockPlanner(PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV"), probe),
            builder=FFMpegHLSCommandBuilder("/usr/bin/ffmpeg"),
        )

        diagnostics = runner.run(BlockRunConfig(channel="Sky One", duration_limit=15, output_dir=Path("/tmp/out"), now=datetime(2026, 6, 17, 10, 5, 0), dry_run=True))

        probe.validate_video.assert_not_called()
        self.assertEqual(diagnostics["plan"][0]["resolved_path"], "/mnt/fs42/runtime/brb.png")
        self.assertEqual(diagnostics["plan"][0]["input_kind"], "lavfi")
        self.assertEqual(diagnostics["plan"][0]["runtime_action"], "generated_fallback_slate")

    def test_runner_dry_run_reports_commercial_counts_paths_and_preserves_inputs(self):
        schedule = {
            "network_name": "Sky One",
            "schedule_blocks": [
                {
                    "title": "Current With Commercials",
                    "start_time": "2026-06-17T10:00:00",
                    "end_time": "2026-06-17T10:30:00",
                    "plan": [
                        {"path": "catalog/SkyOne/feature-a.mp4", "duration": 30, "is_stream": False, "type": "feature"},
                        {"path": "catalog/SkyOne/../commercial/ad-a.mp4", "duration": 15, "is_stream": False, "type": "commercial"},
                        {"path": "catalog/SkyOne/bump.mp4", "duration": 1, "is_stream": False, "type": "bump"},
                        {"path": "catalog/SkyOne/../commercial/ad-b.mp4", "duration": 20, "is_stream": False, "type": "commercial"},
                    ],
                }
            ],
        }
        client = mock.Mock(fetch_schedule=mock.Mock(return_value=schedule))
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 640, 480, 25, 48000, 2)))
        runner = BlockRunner(
            client=client,
            planner=BlockPlanner(PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV"), probe),
            builder=FFMpegHLSCommandBuilder("/usr/bin/ffmpeg"),
        )

        diagnostics = runner.run(BlockRunConfig(channel="Sky One", duration_limit=120, output_dir=Path("/tmp/out"), now=datetime(2026, 6, 17, 10, 5, 0), dry_run=True))

        self.assertEqual(diagnostics["plan_item_counts"], {"feature": 1, "commercial": 2, "bump": 1})
        self.assertEqual(diagnostics["plan_item_count"], 4)
        self.assertEqual(diagnostics["commercial_count"], 2)
        self.assertEqual(diagnostics["ad_count"], 2)
        self.assertEqual(diagnostics["commercial_paths"], ["/mnt/fs42/catalog/commercial/ad-a.mp4", "/mnt/fs42/catalog/commercial/ad-b.mp4"])
        self.assertEqual([item["type"] for item in diagnostics["plan"]], ["feature", "commercial", "bump", "commercial"])
        self.assertEqual([item["index"] for item in diagnostics["plan"]], [0, 1, 2, 3])
        flattened_commands = [part for command in diagnostics["commands"] for part in command]
        self.assertIn("/mnt/fs42/catalog/commercial/ad-a.mp4", flattened_commands)
        self.assertIn("/mnt/fs42/catalog/commercial/ad-b.mp4", flattened_commands)
        self.assertNotIn("lavfi", flattened_commands)

    def test_jellyfin_runner_advances_elapsed_timestamp_offset_after_live_window_rollover(self):
        schedule = {
            "network_name": "Sky One",
            "schedule_blocks": [
                {
                    "title": "Off Air",
                    "start_time": "2026-06-17T10:00:00",
                    "end_time": "2026-06-17T10:30:00",
                    "plan": [{"path": "runtime/brb.png", "duration": 40, "is_stream": False, "content_type": "slate", "media_type": "image"}],
                }
            ],
        }
        client = mock.Mock(fetch_schedule=mock.Mock(return_value=schedule))
        runner = BlockRunner(
            client=client,
            planner=BlockPlanner(PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV"), mock.Mock()),
            builder=FFMpegHLSCommandBuilder("/usr/bin/ffmpeg"),
        )

        def write_rolled_playlist(command, check, shell, stdout, stderr, text):
            playlist = Path(command[-1])
            playlist.parent.mkdir(parents=True, exist_ok=True)
            lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:8"]
            for number in range(8, 20):
                lines.extend(["#EXTINF:2.000000,", f"Sky_One_{number:05d}.ts"])
                (playlist.parent / f"Sky_One_{number:05d}.ts").write_bytes(b"")
            playlist.write_text("\n".join(lines) + "\n")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp, mock.patch("subprocess.run", side_effect=write_rolled_playlist):
            diagnostics = runner.run(
                BlockRunConfig(
                    channel="Sky One",
                    duration_limit=40,
                    output_dir=Path(tmp),
                    now=datetime(2026, 6, 17, 10, 0, 0),
                    output_name="Sky_One",
                    hls_start_number=0,
                    hls_start_time_offset=100.0,
                    stream_profile="jellyfin",
                )
            )

        self.assertEqual(diagnostics["hls_next_start_number"], 20)
        self.assertEqual(diagnostics["hls_segment_duration"], 40.0)
        self.assertEqual(diagnostics["hls_next_start_time_offset"], 140.0)

    def test_jellyfin_runner_renders_remaining_block_in_single_concat_command(self):
        schedule = {
            "network_name": "Sky One",
            "schedule_blocks": [
                {
                    "title": "Two Part Block",
                    "start_time": "2026-06-17T10:00:00",
                    "end_time": "2026-06-17T10:30:00",
                    "plan": [
                        {"path": "catalog/SkyOne/part-a.mp4", "duration": 30, "skip": 0, "is_stream": False, "content_type": "feature"},
                        {"path": "catalog/SkyOne/part-b.mp4", "duration": 30, "skip": 0, "is_stream": False, "content_type": "commercial"},
                    ],
                }
            ],
        }
        client = mock.Mock(fetch_schedule=mock.Mock(return_value=schedule))
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(30, 640, 480, 25, 48000, 2)))
        runner = BlockRunner(
            client=client,
            planner=BlockPlanner(PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV"), probe),
            builder=FFMpegHLSCommandBuilder("/usr/bin/ffmpeg"),
        )

        calls: list[list[str]] = []

        def write_single_playlist(command, check, shell, stdout, stderr, text):
            calls.append(command)
            playlist = Path(command[-1])
            playlist.parent.mkdir(parents=True, exist_ok=True)
            lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:12"]
            for number in range(12, 27):
                lines.extend(["#EXTINF:2.000000,", f"Sky_One_{number:05d}.ts"])
                (playlist.parent / f"Sky_One_{number:05d}.ts").write_bytes(b"")
            playlist.write_text("\n".join(lines) + "\n")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp, mock.patch("subprocess.run", side_effect=write_single_playlist):
            diagnostics = runner.run(
                BlockRunConfig(
                    channel="Sky One",
                    duration_limit=60,
                    output_dir=Path(tmp),
                    now=datetime(2026, 6, 17, 10, 0, 0),
                    output_name="Sky_One",
                    hls_start_number=0,
                    hls_start_time_offset=100.0,
                    stream_profile="jellyfin",
                )
            )

        self.assertEqual(len(calls), 1)
        joined = " ".join(calls[0])
        self.assertIn("concat=n=2:v=1:a=1", joined)
        self.assertIn("-start_number 0", joined)
        self.assertIn("-output_ts_offset 100", joined)
        self.assertEqual(diagnostics["hls_next_start_number"], 27)
        self.assertEqual(diagnostics["hls_next_start_time_offset"], 154.0)
        self.assertEqual(diagnostics["render_mode"], "block-concat")

    @unittest.skipUnless(Path("/usr/bin/ffmpeg").exists() and Path("/usr/bin/ffprobe").exists(), "requires system ffmpeg/ffprobe")
    def test_jellyfin_runner_keeps_item_boundaries_inside_single_command_and_dense_numbering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = create_fixture_clips(root / "clips", count=3, duration=2.2)
            schedule = {
                "network_name": "Sky One",
                "schedule_blocks": [
                    {
                        "title": "Transition Test",
                        "start_time": "2026-06-17T10:00:00",
                        "end_time": "2026-06-17T10:00:09",
                        "plan": [
                            {"realpath": str(clips[0]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "feature", "media_type": "video"},
                            {"realpath": str(clips[1]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "commercial", "media_type": "video"},
                            {"realpath": str(clips[2]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "bump", "media_type": "video"},
                        ],
                    }
                ],
            }
            client = mock.Mock(fetch_schedule=mock.Mock(return_value=schedule))
            runner = BlockRunner(
                client=client,
                planner=BlockPlanner(PathResolver(fs42_root=root, sdtv_root=root), mock.Mock()),
                builder=FFMpegHLSCommandBuilder("/usr/bin/ffmpeg"),
            )
            runner.planner.probe.validate_video.return_value = ProbeResult(2.2, 640, 480, 25, 48000, 2)
            diagnostics = runner.run(
                BlockRunConfig(
                    channel="Sky One",
                    duration_limit=6.6,
                    output_dir=root / "out",
                    now=datetime(2026, 6, 17, 10, 0, 0),
                    output_name="Sky_One",
                    hls_start_time_offset=0.0,
                    stream_profile="jellyfin",
                )
            )
            playlist = Path(diagnostics["playlist"]).read_text()

        joined_commands = [" ".join(command) for command in diagnostics["commands"]]
        self.assertEqual(len(joined_commands), 1)
        self.assertIn("concat=n=3:v=1:a=1", joined_commands[0])
        self.assertIn("-start_number 0", joined_commands[0])
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:0", playlist)
        self.assertNotIn("#EXT-X-DISCONTINUITY\n#EXTINF:2.000000,\nSky_One_00001.ts", playlist)
        self.assertNotIn("Sky_One_00010.ts", playlist)
        self.assertEqual(
            [Path(path).name for path in diagnostics["hls"]["segments"]],
            [
                "Sky_One_00000.ts",
                "Sky_One_00001.ts",
                "Sky_One_00002.ts",
                "Sky_One_00003.ts",
            ],
        )
        self.assertEqual(diagnostics["hls_next_start_number"], 4)

    @unittest.skipUnless(Path("/usr/bin/ffmpeg").exists() and Path("/usr/bin/ffprobe").exists(), "requires system ffmpeg/ffprobe")
    def test_jellyfin_playlist_normalizer_strips_discontinuity_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            playlist = Path(tmp) / "Sky_One.m3u8"
            playlist.write_text(
                "\n".join([
                    "#EXTM3U",
                    "#EXT-X-VERSION:3",
                    "#EXT-X-TARGETDURATION:2",
                    "#EXT-X-MEDIA-SEQUENCE:0",
                    "#EXT-X-DISCONTINUITY",
                    "#EXT-X-DISCONTINUITY",
                    "#EXTINF:2.000000,",
                    "Sky_One_00000.ts",
                    "#EXT-X-DISCONTINUITY",
                    "#EXT-X-DISCONTINUITY-SEQUENCE:4",
                    "#EXTINF:2.000000,",
                    "Sky_One_00001.ts",
                ])
                + "\n"
            )

            _normalize_jellyfin_live_playlist(playlist)

            self.assertEqual(
                playlist.read_text(),
                "\n".join([
                    "#EXTM3U",
                    "#EXT-X-VERSION:3",
                    "#EXT-X-TARGETDURATION:2",
                    "#EXT-X-MEDIA-SEQUENCE:0",
                    "#EXTINF:2.000000,",
                    "Sky_One_00000.ts",
                    "#EXTINF:2.000000,",
                    "Sky_One_00001.ts",
                ])
                + "\n",
            )

    def test_playlist_boundary_rewriter_emits_discontinuity_sequence_after_rollover(self):
        with tempfile.TemporaryDirectory() as tmp:
            playlist = Path(tmp) / "Sky_One.m3u8"
            playlist.write_text(
                "\n".join([
                    "#EXTM3U",
                    "#EXT-X-VERSION:3",
                    "#EXT-X-TARGETDURATION:2",
                    "#EXT-X-MEDIA-SEQUENCE:4",
                    "#EXTINF:2.000000,",
                    "Sky_One_00004.ts",
                    "#EXTINF:2.000000,",
                    "Sky_One_00005.ts",
                    "#EXTINF:2.000000,",
                    "Sky_One_00006.ts",
                    "#EXTINF:0.600000,",
                    "Sky_One_00007.ts",
                    "#EXTINF:2.000000,",
                    "Sky_One_00008.ts",
                    "#EXTINF:2.000000,",
                    "Sky_One_00009.ts",
                ])
                + "\n"
            )

            state = _rewrite_live_playlist_boundaries(playlist, boundary_starts=[4, 8])

            self.assertEqual(state["discontinuity_sequence"], 0)
            self.assertEqual(
                playlist.read_text(),
                "\n".join([
                    "#EXTM3U",
                    "#EXT-X-VERSION:3",
                    "#EXT-X-TARGETDURATION:2",
                    "#EXT-X-MEDIA-SEQUENCE:4",
                    "#EXTINF:2.000000,",
                    "Sky_One_00004.ts",
                    "#EXTINF:2.000000,",
                    "Sky_One_00005.ts",
                    "#EXTINF:2.000000,",
                    "Sky_One_00006.ts",
                    "#EXTINF:0.600000,",
                    "Sky_One_00007.ts",
                    "#EXT-X-DISCONTINUITY",
                    "#EXTINF:2.000000,",
                    "Sky_One_00008.ts",
                    "#EXTINF:2.000000,",
                    "Sky_One_00009.ts",
                ])
                + "\n",
            )

            playlist.write_text(
                "\n".join([
                    "#EXTM3U",
                    "#EXT-X-VERSION:3",
                    "#EXT-X-TARGETDURATION:2",
                    "#EXT-X-MEDIA-SEQUENCE:8",
                    "#EXTINF:2.000000,",
                    "Sky_One_00008.ts",
                    "#EXTINF:2.000000,",
                    "Sky_One_00009.ts",
                    "#EXTINF:2.000000,",
                    "Sky_One_00010.ts",
                    "#EXTINF:0.600000,",
                    "Sky_One_00011.ts",
                ])
                + "\n"
            )

            state = _rewrite_live_playlist_boundaries(playlist, boundary_starts=[4, 8])

            self.assertEqual(state["discontinuity_sequence"], 1)
            self.assertEqual(
                playlist.read_text(),
                "\n".join([
                    "#EXTM3U",
                    "#EXT-X-VERSION:3",
                    "#EXT-X-TARGETDURATION:2",
                    "#EXT-X-MEDIA-SEQUENCE:8",
                    "#EXT-X-DISCONTINUITY-SEQUENCE:1",
                    "#EXTINF:2.000000,",
                    "Sky_One_00008.ts",
                    "#EXTINF:2.000000,",
                    "Sky_One_00009.ts",
                    "#EXTINF:2.000000,",
                    "Sky_One_00010.ts",
                    "#EXTINF:0.600000,",
                    "Sky_One_00011.ts",
                ])
                + "\n",
            )


class HLSTimestampOffsetTests(unittest.TestCase):
    def test_segment_duration_since_accounts_for_live_window_rollover(self):
        with tempfile.TemporaryDirectory() as tmp:
            playlist = Path(tmp) / "Sky_One.m3u8"
            lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:8"]
            for number in range(8, 20):
                lines.extend(["#EXTINF:2.000000,", f"Sky_One_{number:05d}.ts"])
            playlist.write_text("\n".join(lines) + "\n")

            duration_from_start_0 = _hls_segment_duration_since(playlist, start_number=0)
            duration_from_start_8 = _hls_segment_duration_since(playlist, start_number=8)

        self.assertEqual(duration_from_start_0, 40.0)
        self.assertEqual(duration_from_start_8, 24.0)


class RunBlockCLITests(unittest.TestCase):
    def test_cli_prints_json_status_and_returns_zero_on_success(self):
        completed = subprocess.CompletedProcess(["ffmpeg"], 0, stdout="", stderr="")
        with mock.patch("fs42stream.client.FS42ScheduleClient.fetch_schedule", return_value=SCHEDULE), \
             mock.patch("fs42stream.ffprobe.FFProbe.validate_video", return_value=ProbeResult(1, 320, 240, 25, 44100, 1)), \
             mock.patch("subprocess.run", return_value=completed), \
             mock.patch("sys.stdout") as stdout:
            from fs42stream.run_block import main
            rc = main(["--channel", "Sky One", "--duration-limit", "120", "--output-dir", "/tmp/fs42stream-hls", "--now", "2026-06-17T10:05:00"])
        self.assertEqual(rc, 0)
        written = "".join(call.args[0] for call in stdout.write.call_args_list)
        payload = json.loads(written)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["selection"]["block_title"], "Current Block")


if __name__ == "__main__":
    unittest.main()
