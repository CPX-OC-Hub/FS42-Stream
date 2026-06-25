import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fs42stream.client import FS42ScheduleClient
from fs42stream.ffmpeg import FFMpegHLSCommandBuilder
from fs42stream.ffprobe import FFProbe, ProbeResult
from fs42stream.paths import PathResolver
from fs42stream.planner import BlockPlanner, plan_item_type_counts, plan_item_type_summary


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
        self.assertEqual(block.items[0].input_kind, "lavfi")
        self.assertIn("color=black", block.items[0].ffmpeg_input)
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

    def test_commercial_runtime_path_is_not_replaced_with_off_air_fallback(self):
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
        probe.validate_video.side_effect = FileNotFoundError("missing commercial")

        with self.assertRaisesRegex(FileNotFoundError, "missing commercial"):
            BlockPlanner(PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV"), probe).plan(schedule)
        probe.validate_video.assert_called_once_with(Path("/mnt/fs42/runtime/brb.png"))


class FFMpegCommandBuilderTests(unittest.TestCase):
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

    def test_builds_stable_channel_playlist_when_output_name_is_provided(self):
        resolver = PathResolver()
        probe = mock.Mock(validate_video=mock.Mock(return_value=ProbeResult(1, 320, 240, 25, 44100, 1)))
        block = BlockPlanner(resolver, probe).plan(SCHEDULE)[0]

        cmd = FFMpegHLSCommandBuilder(ffmpeg="/usr/bin/ffmpeg").build(block, output_dir=Path("/tmp/hls"), output_name="Sky_One")

        joined = " ".join(cmd)
        self.assertIn("/tmp/hls/Sky_One_%05d.ts", joined)
        self.assertEqual(cmd[-1], "/tmp/hls/Sky_One.m3u8")

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
        self.assertIn("lavfi", cmd)
        self.assertIn("color=black", cmd)
        self.assertNotIn("/mnt/fs42/runtime/brb.png", cmd)
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
