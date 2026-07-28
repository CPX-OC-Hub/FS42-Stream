import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fs42stream.hls_harness import (
    HLSHarness,
    HLSInspection,
    create_fixture_clips,
    fixture_schedule,
    inspect_hls_output,
)
from fs42stream.ffmpeg import FFMpegHLSCommandBuilder
from fs42stream.ffprobe import FFProbe
from fs42stream.paths import PathResolver
from fs42stream.planner import BlockPlanner


class HLSHarnessUnitTests(unittest.TestCase):
    def test_fixture_schedule_points_at_generated_clips_without_production_media(self):
        clips = [Path("/tmp/fixtures/clip_00.mp4"), Path("/tmp/fixtures/clip_01.mp4")]
        schedule = fixture_schedule(clips, duration=0.75)

        self.assertEqual(schedule["network_name"], "Example Channel")
        block = schedule["schedule_blocks"][0]
        self.assertEqual(block["title"], "Example Channel HLS Harness")
        self.assertEqual([entry["realpath"] for entry in block["plan"]], [str(path) for path in clips])
        self.assertTrue(all(entry["is_stream"] is False for entry in block["plan"]))

    def test_fixture_schedule_accepts_non_sky_channel_name(self):
        clips = [Path("/tmp/fixtures/clip_00.mp4")]
        schedule = fixture_schedule(clips, duration=0.75, channel_name="Retro Movies")

        self.assertEqual(schedule["network_name"], "Retro Movies")
        self.assertEqual(schedule["schedule_blocks"][0]["title"], "Retro Movies HLS Harness")

    def test_dry_run_returns_argv_and_does_not_execute_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "clip.mp4"
            clip.write_bytes(b"not probed in this unit test")
            schedule = fixture_schedule([clip], duration=0.5)
            probe = mock.Mock()
            probe.validate_video.return_value.duration = 0.5
            probe.validate_video.return_value.width = 320
            probe.validate_video.return_value.height = 240
            probe.validate_video.return_value.fps = 15.0
            probe.validate_video.return_value.audio_sample_rate = 44100
            probe.validate_video.return_value.audio_channels = 1
            planner = BlockPlanner(PathResolver(fs42_root=root, sdtv_root=root), probe)
            harness = HLSHarness(planner=planner, builder=FFMpegHLSCommandBuilder("/usr/bin/ffmpeg"))

            with mock.patch("subprocess.run") as run:
                command = harness.run(schedule, output_dir=root / "hls", dry_run=True)

            run.assert_not_called()
            self.assertEqual(command[0], "/usr/bin/ffmpeg")
            self.assertIn("-f", command)
            self.assertIn("hls", command)

    def test_inspect_hls_output_reports_playlist_segments_and_endlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "demo.m3u8").write_text(
                "#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:1.0,\ndemo_00000.ts\n#EXT-X-ENDLIST\n",
                encoding="utf-8",
            )
            (out / "demo_00000.ts").write_bytes(b"segment")

            inspection = inspect_hls_output(out / "demo.m3u8")

            self.assertEqual(inspection, HLSInspection(playlist=out / "demo.m3u8", segments=[out / "demo_00000.ts"], has_endlist=True))


class HLSHarnessIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(Path("/usr/bin/ffmpeg").exists() and Path("/usr/bin/ffprobe").exists(), "requires system ffmpeg/ffprobe")
    def test_generates_hls_from_synthetic_fixture_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_dir = root / "fixtures"
            output_dir = root / "hls"

            clips = create_fixture_clips(fixture_dir, count=2, duration=0.6)
            schedule = fixture_schedule(clips, duration=0.6)
            harness = HLSHarness(
                planner=BlockPlanner(PathResolver(fs42_root=root, sdtv_root=root), FFProbe("/usr/bin/ffprobe")),
                builder=FFMpegHLSCommandBuilder("/usr/bin/ffmpeg"),
            )
            command = harness.run(schedule, output_dir=output_dir, dry_run=False)
            inspection = inspect_hls_output(output_dir / "Example_Channel_HLS_Harness.m3u8")

            self.assertEqual(command[0], "/usr/bin/ffmpeg")
            self.assertFalse(inspection.has_endlist)
            self.assertGreaterEqual(len(inspection.segments), 1)
            for segment in inspection.segments:
                self.assertGreater(segment.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
