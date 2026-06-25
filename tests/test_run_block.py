import json
import subprocess
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from fs42stream.ffmpeg import FFMpegHLSCommandBuilder
from fs42stream.ffprobe import ProbeResult
from fs42stream.paths import PathResolver
from fs42stream.planner import BlockPlanner
from fs42stream.run_block import BlockRunConfig, BlockRunner, select_current_or_next_block


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
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/ffmpeg")
        self.assertIn(["-t", "120"], [command[index:index + 2] for index in range(len(command) - 1)])
        self.assertLess(command.index("/mnt/fs42/catalog/SkyOne/current-first.mp4"), command.index("/mnt/media/SDTV/Current Second.mp4"))
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
