import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from fs42stream.client import FS42ScheduleClient
from fs42stream.ffmpeg import FFMpegHLSCommandBuilder
from fs42stream.ffprobe import FFProbe, ProbeResult
from fs42stream.paths import PathResolver
from fs42stream.planner import BlockPlanner


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

    def test_validate_video_raises_for_missing_audio_or_video(self):
        completed = subprocess.CompletedProcess(["ffprobe"], 0, stdout=json.dumps({"streams": [], "format": {}}), stderr="")
        with mock.patch("subprocess.run", return_value=completed):
            with self.assertRaises(ValueError):
                FFProbe().validate_video(Path("missing.mp4"))


class BlockPlannerTests(unittest.TestCase):
    def test_preserves_schedule_blocks_plan_entries_while_adding_resolved_path_and_probe(self):
        resolver = PathResolver(fs42_root="/mnt/fs42", sdtv_root="/mnt/media/SDTV")
        probe = mock.Mock()
        probe.validate_video.return_value = ProbeResult(duration=1.0, width=320, height=240, fps=29.97, audio_sample_rate=44100, audio_channels=1)
        blocks = BlockPlanner(resolver, probe).plan(SCHEDULE)
        self.assertEqual(blocks[0].title, "South Park")
        self.assertEqual(blocks[0].items[0].source["path"], SCHEDULE["schedule_blocks"][0]["plan"][0]["path"])
        self.assertEqual(blocks[0].items[0].skip, 12.5)
        self.assertEqual(blocks[0].items[0].duration, 331.25)
        self.assertEqual(blocks[0].items[0].resolved_path, Path("/mnt/fs42/catalog/SkyOne/late/South Park/episode.mp4"))
        self.assertEqual(probe.validate_video.call_count, 2)


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


if __name__ == "__main__":
    unittest.main()
