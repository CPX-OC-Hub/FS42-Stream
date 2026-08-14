import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fs42stream.client import FS42ScheduleClient
from fs42stream.ffmpeg import FFMpegHLSCommandBuilder
from fs42stream.ffprobe import FFProbe, ProbeResult
from fs42stream.paths import PathResolver
from fs42stream.planner import BlockPlanner, plan_item_type_counts, plan_item_type_summary
from fs42stream.run_block import BlockRunConfig, BlockRunner, select_current_or_next_block


SCHEDULE = {
    "network_name": "Sky One",
    "schedule_blocks": [
        {
            "title": "South Park",
            "start_time": "2026-06-17T00:00:00",
            "end_time": "2026-06-17T00:30:00",
            "plan": [
                {
                    "path": "catalog/SkyOne/late/South Park/episode.mp4",
                    "skip": 12.5,
                    "duration": 331.25,
                    "is_stream": False,
                    "content_type": "feature",
                    "media_type": "video",
                },
                {
                    "path": "catalog/SkyOne/bump/one_second_black.mp4",
                    "skip": 0,
                    "duration": 1.0,
                    "is_stream": False,
                    "content_type": "bump",
                    "media_type": "video",
                },
            ],
        }
    ],
}


class FS42ScheduleClientTests(unittest.TestCase):
    def test_fetch_schedule_uses_encoded_channel_endpoint_and_checks_count(self):
        body = json.dumps({"network_name": "Sky One", "schedule_blocks": [1, 2, 3]}).encode()

        def opener(request, timeout):
            self.assertEqual(request.full_url, "http://fs42.example:4242/schedules/Sky%20One")
            self.assertEqual(timeout, 7)
            return mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *a: None, read=lambda: body)

        client = FS42ScheduleClient("http://fs42.example:4242", timeout=7, opener=opener)
        schedule = client.fetch_schedule("Sky One", expected_blocks=3)
        self.assertEqual(schedule["network_name"], "Sky One")

    def test_fetch_schedule_raises_on_unexpected_block_count(self):
        body = json.dumps({"network_name": "Sky One", "schedule_blocks": []}).encode()
        opener = lambda request, timeout: mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *a: None, read=lambda: body)
        client = FS42ScheduleClient("http://fs42.example:4242", opener=opener)
        with self.assertRaisesRegex(ValueError, "expected 338 schedule blocks"):
            client.fetch_schedule("Sky One", expected_blocks=338)


class ScheduleSelectionTests(unittest.TestCase):
    def test_selects_naive_fs42_schedule_using_configured_local_timezone(self):
        schedule = {
            "schedule_blocks": [
                {"title": "Jeopardy", "start_time": "2026-06-25T18:30:00", "end_time": "2026-06-25T19:00:00"},
                {"title": "The Nanny", "start_time": "2026-06-25T19:00:00", "end_time": "2026-06-25T19:30:00"},
                {"title": "Married", "start_time": "2026-06-25T19:30:00", "end_time": "2026-06-25T20:00:00"},
            ]
        }
        now_utc = datetime(2026, 6, 25, 18, 43, tzinfo=timezone.utc)

        selected = select_current_or_next_block(schedule, now=now_utc, schedule_timezone="Europe/London")

        self.assertEqual(selected.index, 2)
        self.assertEqual(selected.block["title"], "Married")


class PathResolverTests(unittest.TestCase):
    def test_resolves_fs42_catalog_path_and_sdtv_realpath_under_allowed_roots(self):
        resolver = PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV")
        self.assertEqual(
            resolver.resolve("catalog/SkyOne/bump/one_second_black.mp4"),
            Path("/mnt/fs42/catalog/SkyOne/bump/one_second_black.mp4"),
        )
        self.assertEqual(
            resolver.resolve("/mnt/media/SDTV/South Park/episode.mp4"),
            Path("/mnt/media/SDTV/South Park/episode.mp4"),
        )

    def test_rejects_uppercase_fs42_and_traversal_outside_allowed_roots(self):
        resolver = PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV")
        for bad in ["/mnt/FS42/catalog/foo.mp4", "../../etc/passwd", "/etc/passwd"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    resolver.resolve(bad)

    def test_recovers_unique_nested_commercial_when_schedule_points_at_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fs42_root = root / "fs42"
            sdtv_root = root / "SDTV"
            commercial_root = fs42_root / "catalog" / "commercial"
            late_dir = commercial_root / "Late"
            late_dir.mkdir(parents=True)
            sdtv_root.mkdir()
            recovered = late_dir / "Miller - 1995 - fixed.mp4"
            recovered.write_text("stub")

            resolver = PathResolver(fs42_root=fs42_root, sdtv_root=sdtv_root)
            self.assertEqual(
                resolver.resolve("catalog/commercial/Miller - 1995 - fixed.mp4"),
                recovered,
            )

    def test_recovers_unique_nested_commercial_when_schedule_uses_catalog_parent_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fs42_root = root / "fs42"
            sdtv_root = root / "SDTV"
            commercial_root = fs42_root / "catalog" / "commercial"
            late_dir = commercial_root / "Late"
            late_dir.mkdir(parents=True)
            sdtv_root.mkdir()
            recovered = late_dir / "guinness - engima 1995.mp4"
            recovered.write_text("stub")

            resolver = PathResolver(fs42_root=fs42_root, sdtv_root=sdtv_root)
            self.assertEqual(
                resolver.resolve("catalog/SkyOne/../commercial/guinness - engima 1995.mp4"),
                recovered,
            )


class FFProbeTests(unittest.TestCase):
    def test_validate_video_invokes_ffprobe_and_parses_json(self):
        payload = {
            "streams": [
                {"codec_type": "video", "width": 640, "height": 480, "r_frame_rate": "25/1"},
                {"codec_type": "audio", "sample_rate": "48000", "channels": 2},
            ],
            "format": {"duration": "31.200"},
        }
        completed = subprocess.CompletedProcess(["ffprobe"], 0, stdout=json.dumps(payload), stderr="")
        with mock.patch("subprocess.run", return_value=completed) as run:
            result = FFProbe("/usr/bin/ffprobe").validate_video(Path("/mnt/fs42/a.mp4"))
        self.assertEqual(result, ProbeResult(duration=31.2, width=640, height=480, fps=25.0, audio_sample_rate=48000, audio_channels=2))
        self.assertIn("/usr/bin/ffprobe", run.call_args.args[0][0])

    def test_validate_video_allows_video_only_inputs_for_silent_bumps(self):
        payload = {
            "streams": [
                {"codec_type": "video", "width": 640, "height": 480, "avg_frame_rate": "25/1"},
            ],
            "format": {"duration": "1.000"},
        }
        completed = subprocess.CompletedProcess(["ffprobe"], 0, stdout=json.dumps(payload), stderr="")
        with mock.patch("subprocess.run", return_value=completed):
            result = FFProbe().validate_video(Path("silent-bump.mp4"))
        self.assertEqual(result, ProbeResult(duration=1.0, width=640, height=480, fps=25.0, audio_sample_rate=0, audio_channels=0))

    def test_validate_video_raises_when_video_stream_is_missing(self):
        completed = subprocess.CompletedProcess(["ffprobe"], 0, stdout=json.dumps({"streams": [], "format": {}}), stderr="")
        with mock.patch("subprocess.run", return_value=completed):
            with self.assertRaises(ValueError):
                FFProbe().validate_video(Path("missing.mp4"))


class BlockPlannerTests(unittest.TestCase):
    def test_preserves_schedule_blocks_plan_entries_while_adding_resolved_path_and_probe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fs42_root = root / "fs42"
            sdtv_root = root / "SDTV"
            fs42_root.mkdir()
            sdtv_root.mkdir()
            resolver = PathResolver(fs42_root=fs42_root, sdtv_root=sdtv_root)
            probe = mock.Mock()
            probe.validate_video.return_value = ProbeResult(duration=1.0, width=320, height=240, fps=29.97, audio_sample_rate=44100, audio_channels=1)
            blocks = BlockPlanner(resolver, probe).plan(SCHEDULE)
        self.assertEqual(blocks[0].title, "South Park")
        self.assertEqual(blocks[0].items[0].source["path"], SCHEDULE["schedule_blocks"][0]["plan"][0]["path"])
        self.assertEqual(blocks[0].items[0].skip, 12.5)
        self.assertEqual(blocks[0].items[0].duration, 331.25)
        self.assertEqual(blocks[0].items[0].resolved_path, fs42_root / "catalog/SkyOne/late/South Park/episode.mp4")
        self.assertEqual(probe.validate_video.call_count, 2)

    def test_clamps_file_backed_item_duration_to_actual_media_remaining_after_skip(self):
        schedule = {
            "schedule_blocks": [
                {
                    "title": "Duration Clamp",
                    "plan": [
                        {
                            "path": "catalog/SkyOne/show.mp4",
                            "duration": 120,
                            "skip": 10,
                            "is_stream": False,
                            "content_type": "feature",
                            "media_type": "video",
                        }
                    ],
                }
            ]
        }
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(40, 640, 480, 25, 48000, 2)))
        block = BlockPlanner(PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV"), probe).plan(schedule)[0]

        self.assertEqual(block.items[0].skip, 10.0)
        self.assertEqual(block.items[0].duration, 30.0)

    def test_replaces_known_runtime_image_slate_with_generated_fallback_without_probing_missing_png(self):
        schedule = {
            "schedule_blocks": [
                {
                    "title": "Off Air",
                    "plan": [
                        {
                            "path": "runtime/brb.png",
                            "duration": 10,
                            "skip": 0,
                            "is_stream": False,
                            "content_type": "slate",
                            "media_type": "image",
                        }
                    ],
                }
            ]
        }
        probe = mock.Mock()
        block = BlockPlanner(PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV"), probe).plan(schedule)[0]

        self.assertEqual(probe.validate_video.call_count, 0)
        self.assertEqual(block.items[0].resolved_path, Path("/mnt/fs42/runtime/brb.png"))
        self.assertEqual(block.items[0].input_kind, "image_loop")
        self.assertEqual(block.items[0].ffmpeg_input, str(Path("/mnt/fs42/runtime/brb.png")))
        self.assertEqual(block.items[0].runtime_action, "generated_fallback_slate")

    def test_can_use_configured_fallback_video_for_known_runtime_slate(self):
        schedule = {
            "schedule_blocks": [
                {
                    "title": "Off Air",
                    "plan": [
                        {"path": "runtime/brb.png", "duration": 10, "is_stream": False, "content_type": "slate", "media_type": "image"}
                    ],
                }
            ]
        }
        fallback = Path("/tmp/fallback-slate.mp4")
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(10, 640, 480, 25, 48000, 2)))
        block = BlockPlanner(PathResolver(), probe, fallback_slate_video=fallback).plan(schedule)[0]

        probe.validate_video.assert_called_once_with(fallback)
        self.assertEqual(block.items[0].ffmpeg_input, str(fallback))
        self.assertEqual(block.items[0].runtime_action, "configured_fallback_slate")

    def test_does_not_replace_or_skip_missing_programme_media(self):
        probe = mock.Mock()
        probe.validate_video.side_effect = FileNotFoundError("missing programme")

        with self.assertRaisesRegex(FileNotFoundError, "missing programme"):
            BlockPlanner(PathResolver(), probe).plan(SCHEDULE)

    def test_preserves_type_commercial_entries_and_counts_fs42_item_types(self):
        schedule = {
            "schedule_blocks": [
                {
                    "title": "Advert Fidelity",
                    "plan": [
                        {"path": "catalog/SkyOne/part1.mp4", "duration": 30, "is_stream": False, "type": "feature"},
                        {"path": "catalog/SkyOne/../commercial/ad-a.mp4", "duration": 15, "is_stream": False, "type": "commercial"},
                        {"path": "catalog/SkyOne/bump.mp4", "duration": 1, "is_stream": False, "type": "bump"},
                        {"path": "catalog/SkyOne/../commercial/ad-b.mp4", "duration": 20, "is_stream": False, "type": "commercial"},
                    ],
                }
            ]
        }
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 640, 480, 25, 48000, 2)))
        block = BlockPlanner(PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV"), probe).plan(schedule)[0]

        self.assertEqual([item.source["type"] for item in block.items], ["feature", "commercial", "bump", "commercial"])
        self.assertEqual([item.fs42_type for item in block.items], ["feature", "commercial", "bump", "commercial"])
        self.assertEqual(plan_item_type_counts(block.items), {"feature": 1, "commercial": 2, "bump": 1})
        self.assertEqual(plan_item_type_summary(block.items)["commercial_count"], 2)
        self.assertEqual(
            plan_item_type_summary(block.items)["commercial_paths"],
            ["/mnt/fs42/catalog/commercial/ad-a.mp4", "/mnt/fs42/catalog/commercial/ad-b.mp4"],
        )

    def test_runtime_png_with_commercial_type_is_replaced_with_off_air_fallback(self):
        schedule = {
            "schedule_blocks": [
                {
                    "title": "Commercial Missing",
                    "plan": [
                        {"path": "runtime/brb.png", "duration": 10, "is_stream": False, "type": "commercial", "content_type": "slate", "media_type": "image"},
                    ],
                }
            ]
        }
        probe = mock.Mock()
        block = BlockPlanner(PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV"), probe).plan(schedule)[0]

        probe.validate_video.assert_not_called()
        self.assertEqual(block.items[0].resolved_path, Path("/mnt/fs42/runtime/brb.png"))
        self.assertEqual(block.items[0].input_kind, "image_loop")
        self.assertEqual(block.items[0].runtime_action, "generated_fallback_slate")

    def test_runtime_png_with_bad_commercial_metadata_is_still_replaced_with_fallback(self):
        schedule = {
            "schedule_blocks": [
                {
                    "title": "Bad Runtime Metadata",
                    "plan": [
                        {
                            "path": "runtime/brb.png",
                            "duration": 8,
                            "skip": 0,
                            "is_stream": False,
                            "content_type": "commercial",
                            "media_type": "video",
                        }
                    ],
                }
            ]
        }
        probe = mock.Mock()
        block = BlockPlanner(PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV"), probe).plan(schedule)[0]

        probe.validate_video.assert_not_called()
        self.assertEqual(block.items[0].resolved_path, Path("/mnt/fs42/runtime/brb.png"))
        self.assertEqual(block.items[0].input_kind, "image_loop")
        self.assertEqual(block.items[0].runtime_action, "generated_fallback_slate")


class CatchUpBlockRunnerTests(unittest.TestCase):
    def _runner_for(self, schedule):
        class FakeClient:
            def fetch_schedule(self, channel, expected_blocks=None):
                return schedule

        class CapturingBuilder:
            def __init__(self):
                self.block = None
                self.blocks = []
                self.kwargs = None
                self.kwargs_by_call = []

            def build(self, block, **kwargs):
                if self.block is None:
                    self.block = block
                self.blocks.append(block)
                self.kwargs = kwargs
                self.kwargs_by_call.append(kwargs)
                return ["/usr/bin/ffmpeg", "-version", str(len(self.blocks))]

        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1000, 640, 480, 25, 48000, 2)))
        planner = BlockPlanner(PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV"), probe)
        builder = CapturingBuilder()
        return BlockRunner(client=FakeClient(), planner=planner, builder=builder), builder

    def test_starting_mid_resumed_feature_seeks_to_plan_skip_plus_wallclock_offset(self):
        schedule = {
            "network_name": "Sky One",
            "schedule_blocks": [
                {
                    "title": "Show With Breaks",
                    "start_time": "2026-06-25T22:00:00",
                    "end_time": "2026-06-25T23:00:00",
                    "plan": [
                        {"path": "catalog/SkyOne/show.mp4", "duration": 500, "skip": 0, "is_stream": False, "content_type": "feature"},
                        {"path": "catalog/SkyOne/../commercial/ad.mp4", "duration": 30, "skip": 0, "is_stream": False, "content_type": "commercial"},
                        {"path": "catalog/SkyOne/show.mp4", "duration": 500, "skip": 500, "is_stream": False, "content_type": "feature"},
                    ],
                }
            ],
        }
        runner, builder = self._runner_for(schedule)

        diagnostics = runner.run(BlockRunConfig(now=datetime(2026, 6, 25, 22, 9, 0), dry_run=True))

        self.assertEqual(diagnostics["catch_up"]["applied"], True)
        self.assertEqual(diagnostics["catch_up"]["start_plan_index"], 2)
        self.assertEqual(diagnostics["playout"]["current_item"]["path"], "catalog/SkyOne/show.mp4")
        self.assertEqual(diagnostics["playout"]["current_item"]["media_seek"], 510.0)
        self.assertEqual(builder.block.items[0].source["content_type"], "feature")
        self.assertEqual(builder.block.items[0].skip, 510.0)
        self.assertEqual(builder.block.items[0].duration, 490.0)
        self.assertEqual(diagnostics["plan"][0]["skip"], 510.0)
        self.assertEqual(diagnostics["plan"][0]["duration"], 490.0)

    def test_starting_inside_commercial_keeps_commercial_as_first_remaining_item(self):
        schedule = {
            "network_name": "Sky One",
            "schedule_blocks": [
                {
                    "title": "Show With Ad",
                    "start_time": "2026-06-25T22:00:00",
                    "end_time": "2026-06-25T22:30:00",
                    "plan": [
                        {"path": "catalog/SkyOne/show.mp4", "duration": 500, "skip": 0, "is_stream": False, "content_type": "feature"},
                        {"path": "catalog/SkyOne/../commercial/ad.mp4", "duration": 30, "skip": 0, "is_stream": False, "content_type": "commercial"},
                        {"path": "catalog/SkyOne/show.mp4", "duration": 500, "skip": 500, "is_stream": False, "content_type": "feature"},
                    ],
                }
            ],
        }
        runner, builder = self._runner_for(schedule)

        diagnostics = runner.run(BlockRunConfig(now=datetime(2026, 6, 25, 22, 8, 45), dry_run=True))

        self.assertEqual(diagnostics["catch_up"]["start_plan_index"], 1)
        self.assertEqual(builder.block.items[0].source["content_type"], "commercial")
        self.assertEqual(builder.block.items[0].skip, 25.0)
        self.assertEqual(builder.block.items[0].duration, 5.0)
        self.assertEqual(builder.block.items[1].source["content_type"], "feature")
        self.assertEqual(builder.block.items[1].skip, 500.0)

    def test_direct_profile_builds_single_block_concat_command_for_remaining_plan_items(self):
        schedule = {
            "network_name": "Sky One",
            "schedule_blocks": [
                {
                    "title": "Show With Ad Break",
                    "start_time": "2026-06-25T22:00:00",
                    "end_time": "2026-06-25T22:30:00",
                    "plan": [
                        {"path": "catalog/SkyOne/show.mp4", "duration": 60, "skip": 0, "is_stream": False, "content_type": "feature"},
                        {"path": "catalog/SkyOne/../commercial/ad.mp4", "duration": 30, "skip": 0, "is_stream": False, "content_type": "commercial"},
                        {"path": "catalog/SkyOne/show.mp4", "duration": 60, "skip": 60, "is_stream": False, "content_type": "feature"},
                    ],
                }
            ],
        }
        runner, builder = self._runner_for(schedule)

        diagnostics = runner.run(BlockRunConfig(now=datetime(2026, 6, 25, 22, 0, 0), dry_run=True, hls_start_number=7, output_name="Sky_One"))

        self.assertEqual(len(builder.blocks), 1)
        self.assertEqual([item.source["content_type"] for item in builder.blocks[0].items], ["feature", "commercial", "feature"])
        self.assertEqual([kwargs["hls_start_number"] for kwargs in builder.kwargs_by_call], [7])
        self.assertEqual([kwargs["hls_append"] for kwargs in builder.kwargs_by_call], [False])
        self.assertEqual(len(diagnostics["commands"]), 1)
        self.assertEqual(len(builder.block.items), 3)
        self.assertEqual(diagnostics["render_mode"], "block-concat")

    def test_jellyfin_profile_builds_single_block_concat_command_for_remaining_block(self):
        schedule = {
            "network_name": "Sky One",
            "schedule_blocks": [
                {
                    "title": "Show With Ad Break",
                    "start_time": "2026-06-25T22:00:00",
                    "end_time": "2026-06-25T22:30:00",
                    "plan": [
                        {"path": "catalog/SkyOne/show.mp4", "duration": 60, "skip": 0, "is_stream": False, "content_type": "feature"},
                        {"path": "catalog/SkyOne/../commercial/ad.mp4", "duration": 30, "skip": 0, "is_stream": False, "content_type": "commercial"},
                        {"path": "catalog/SkyOne/show.mp4", "duration": 60, "skip": 60, "is_stream": False, "content_type": "feature"},
                    ],
                }
            ],
        }
        runner, builder = self._runner_for(schedule)

        diagnostics = runner.run(
            BlockRunConfig(
                now=datetime(2026, 6, 25, 22, 0, 0),
                dry_run=True,
                hls_start_number=42,
                hls_start_time_offset=100.0,
                output_name="Sky_One",
                stream_profile="jellyfin",
            )
        )

        self.assertEqual(len(builder.blocks), 1)
        self.assertEqual([item.source["content_type"] for item in builder.blocks[0].items], ["feature", "commercial", "feature"])
        self.assertEqual([kwargs["stream_profile"] for kwargs in builder.kwargs_by_call], ["jellyfin"])
        self.assertEqual([kwargs["hls_start_number"] for kwargs in builder.kwargs_by_call], [42])
        self.assertEqual([kwargs["hls_append"] for kwargs in builder.kwargs_by_call], [False])
        self.assertEqual([kwargs["hls_start_time_offset"] for kwargs in builder.kwargs_by_call], [100.0])
        self.assertEqual(len(diagnostics["commands"]), 1)
        self.assertEqual(diagnostics["render_mode"], "block-concat")
        self.assertEqual(diagnostics["stream_profile"], "jellyfin")


class FFMpegCommandBuilderTests(unittest.TestCase):
    def test_audio_normalization_defaults_off_without_loudnorm(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]

        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg").build(block, output_dir=Path("/tmp/hls"))

        self.assertNotIn("loudnorm", " ".join(cmd))

    def test_loudnorm_normalizes_real_audio_after_resample_and_stereo_formatting(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]

        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg", audio_normalization="loudnorm").build(block, output_dir=Path("/tmp/hls"))

        self.assertIn("[0:a]aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-16:LRA=11:TP=-1.5[a0]", " ".join(cmd))

    def test_loudnorm_does_not_modify_generated_silence_for_video_only_inputs(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 0, 0)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]

        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg", audio_normalization="loudnorm").build(block, output_dir=Path("/tmp/hls"))

        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=48000", " ".join(cmd))
        self.assertNotIn("loudnorm", " ".join(cmd))

    def test_builds_block_level_concat_filter_hls_command_with_normalisation(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]
        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg").build(block, output_dir=Path("/tmp/hls"))
        joined = " ".join(cmd)
        self.assertIn("/usr/bin/ffmpeg", cmd[0])
        self.assertIn("-ss", cmd)
        self.assertIn("-t", cmd)
        self.assertIn("concat=n=2:v=1:a=1", joined)
        self.assertIn("scale=640:480:force_original_aspect_ratio=decrease", joined)
        self.assertIn("fps=25", joined)
        self.assertIn("setsar=1", joined)
        self.assertIn("format=yuv420p", joined)
        self.assertIn("aresample=48000", joined)
        self.assertIn("aformat=channel_layouts=stereo", joined)
        self.assertIn("-f hls", joined)
        self.assertEqual(cmd[-1], "/tmp/hls/South_Park.m3u8")

    def test_paces_every_input_for_live_hls_generation(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]

        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg").build(block, output_dir=Path("/tmp/hls"))

        input_indexes = [index for index, arg in enumerate(cmd) if arg == "-i"]
        self.assertGreater(input_indexes, [])
        for index in input_indexes:
            self.assertIn("-re", cmd[max(0, index - 4):index])

    def test_builds_transition_safe_hls_profile_for_vlc_live_edge(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]

        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg").build(block, output_dir=Path("/tmp/hls"))

        joined = " ".join(cmd)
        self.assertIn("-hls_time 2", joined)
        self.assertIn("-hls_list_size 60", joined)
        self.assertIn("-g 50", joined)
        self.assertIn("-keyint_min 50", joined)
        self.assertIn("-sc_threshold 0", joined)

    def test_jellyfin_profile_uses_longer_live_playlist_window(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]

        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg").build(
            block,
            output_dir=Path("/tmp/hls"),
            output_name="Sky_One",
            stream_profile="jellyfin",
        )

        joined = " ".join(cmd)
        self.assertIn("-hls_list_size 60", joined)
        self.assertNotIn("-hls_list_size 12", joined)

    def test_builds_vaapi_transition_safe_gop_without_unsupported_x264_scene_cut_flags(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]

        cmd = FFMpegHLSCommandBuilder(
            ffmpeg="/usr/bin/ffmpeg",
            video_encoder="h264_vaapi",
            vaapi_device="/dev/dri/renderD128",
        ).build(block, output_dir=Path("/tmp/hls"), output_name="Sky_One")

        joined = " ".join(cmd)
        self.assertIn("-hls_time 2", joined)
        self.assertIn("-hls_list_size 60", joined)
        self.assertIn("-g 50", joined)
        self.assertNotIn("-keyint_min", cmd)
        self.assertNotIn("-sc_threshold", cmd)

    def test_builds_live_playlist_without_event_endlist_mode(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]

        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg").build(block, output_dir=Path("/tmp/hls"))

        self.assertNotIn("-hls_playlist_type", cmd)
        self.assertNotIn("event", cmd)
        joined = " ".join(cmd)
        self.assertIn("omit_endlist", joined)

    def test_append_commands_omit_endlist_between_internal_plan_items(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]

        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg").build(block, output_dir=Path("/tmp/hls"), hls_append=True, hls_start_number=10)

        joined = " ".join(cmd)
        self.assertIn("append_list", joined)
        self.assertNotIn("discont_start", joined)
        self.assertIn("omit_endlist", joined)

    def test_builds_vaapi_h264_command_when_requested(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]

        cmd = FFMpegHLSCommandBuilder(
            ffmpeg="/usr/bin/ffmpeg",
            video_encoder="h264_vaapi",
            vaapi_device="/dev/dri/renderD128",
        ).build(block, output_dir=Path("/tmp/hls"), output_name="Sky_One")

        joined = " ".join(cmd)
        self.assertIn("-vaapi_device", cmd)
        self.assertIn("/dev/dri/renderD128", cmd)
        self.assertIn("format=nv12,hwupload[vout]", joined)
        self.assertIn("-c:v h264_vaapi", joined)
        self.assertIn("-qp 23", joined)
        self.assertNotIn("libx264", cmd)
        self.assertEqual(cmd[-1], "/tmp/hls/Sky_One.m3u8")

    def test_rejects_vaapi_encoder_without_device(self):
        with self.assertRaisesRegex(ValueError, "vaapi_device"):
            FFMpegHLSCommandBuilder(video_encoder="h264_vaapi")

    def test_builds_stable_channel_playlist_when_output_name_is_provided(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]

        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg").build(block, output_dir=Path("/tmp/hls"), output_name="Sky_One")

        joined = " ".join(cmd)
        self.assertIn("/tmp/hls/Sky_One_%05d.ts", joined)
        self.assertEqual(cmd[-1], "/tmp/hls/Sky_One.m3u8")

    def test_appends_later_blocks_without_resetting_hls_sequence_and_keeps_direct_timestamps_continuous(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]

        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg").build(
            block,
            output_dir=Path("/tmp/hls"),
            output_name="Sky_One",
            hls_start_number=42,
            hls_start_time_offset=83.25,
            hls_append=True,
        )

        joined = " ".join(cmd)
        self.assertIn("-output_ts_offset 83.25", joined)
        self.assertIn("-hls_flags omit_endlist+append_list", joined)
        self.assertNotIn("discont_start", joined)
        self.assertNotIn("-start_number", joined)
        self.assertIn("/tmp/hls/Sky_One_%05d.ts", joined)
        self.assertEqual(cmd[-1], "/tmp/hls/Sky_One.m3u8")

    def test_jellyfin_profile_uses_explicit_elapsed_timestamp_offset_and_marks_append_boundaries(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]

        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg").build(
            block,
            output_dir=Path("/tmp/hls"),
            output_name="Sky_One",
            hls_start_number=42,
            hls_start_time_offset=83.25,
            hls_append=True,
            stream_profile="jellyfin",
        )

        joined = " ".join(cmd)
        self.assertIn("-fflags +genpts", joined)
        self.assertIn("-output_ts_offset 83.25", joined)
        self.assertNotIn("-output_ts_offset 84", joined)
        self.assertIn("-hls_flags omit_endlist+append_list", joined)
        self.assertNotIn("-start_number", joined)
        self.assertNotIn("discont_start", joined)
        self.assertNotIn("-avoid_negative_ts", joined)

    def test_builds_silent_audio_chain_for_video_only_inputs(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 0, 0)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]
        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg").build(block, output_dir=Path("/tmp/hls"))
        joined = " ".join(cmd)
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=48000", joined)
        self.assertIn("atrim=duration=331.25", joined)
        self.assertNotIn("[0:a]aresample", joined)

    def test_builds_lavfi_input_for_generated_runtime_slate_without_shell(self):
        schedule = {
            "schedule_blocks": [
                {
                    "title": "Off Air",
                    "plan": [
                        {"path": "runtime/brb.png", "duration": 7, "content_type": "slate", "media_type": "image", "is_stream": False}
                    ],
                }
            ]
        }
        block = BlockPlanner(PathResolver(), mock.Mock()).plan(schedule)[0]
        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg").build(block, output_dir=Path("/tmp/hls"))

        self.assertIsInstance(cmd, list)
        self.assertIn("-f", cmd)
        self.assertIn("-loop", cmd)
        self.assertIn("/mnt/fs42/runtime/brb.png", cmd)
        self.assertNotIn("shell=True", " ".join(cmd))

    def test_includes_commercial_plan_entries_as_ffmpeg_inputs_in_order(self):
        schedule = {
            "schedule_blocks": [
                {
                    "title": "South Park With Adverts",
                    "plan": [
                        {"path": "catalog/SkyOne/feature-a.mp4", "duration": 30, "is_stream": False, "type": "feature"},
                        {"path": "catalog/SkyOne/../commercial/ad-a.mp4", "duration": 15, "is_stream": False, "type": "commercial"},
                        {"path": "catalog/SkyOne/feature-b.mp4", "duration": 30, "is_stream": False, "type": "feature"},
                    ],
                }
            ]
        }
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV"), probe).plan(schedule)[0]
        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg").build(block, output_dir=Path("/tmp/hls"))

        inputs = [cmd[index + 1] for index, arg in enumerate(cmd[:-1]) if arg == "-i"]
        self.assertEqual(
            inputs,
            [
                "/mnt/fs42/catalog/SkyOne/feature-a.mp4",
                "/mnt/fs42/catalog/commercial/ad-a.mp4",
                "/mnt/fs42/catalog/SkyOne/feature-b.mp4",
            ],
        )
        self.assertIn("concat=n=3:v=1:a=1", " ".join(cmd))


if __name__ == "__main__":
    unittest.main()
