import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from fs42stream.live_controller import LiveController, LiveControllerConfig, main


SCHEDULE = {
    "network_name": "Sky One",
    "schedule_blocks": [
        {
            "title": "First Live Block",
            "start_time": "2026-06-17T10:00:00",
            "end_time": "2026-06-17T10:30:00",
            "plan": [{"path": "catalog/first.mp4", "duration": 1800, "skip": 0, "is_stream": False}],
        },
        {
            "title": "Second Live Block",
            "start_time": "2026-06-17T10:30:00",
            "end_time": "2026-06-17T11:00:00",
            "plan": [{"path": "catalog/second.mp4", "duration": 1800, "skip": 0, "is_stream": False}],
        },
    ],
}


class FakeScheduleClient:
    def __init__(self):
        self.calls = []

    def fetch_schedule(self, channel, expected_blocks=None):
        self.calls.append((channel, expected_blocks))
        return SCHEDULE


class FakeBlockRunner:
    def __init__(self):
        self.calls = []

    def run(self, config):
        self.calls.append(config)
        index = len(self.calls) - 1
        playlist = config.output_dir / f"fake-{index}.m3u8"
        segment = config.output_dir / f"fake-{index}_00000.ts"
        playlist.write_text(f"#EXTM3U\n{segment.name}\n#EXT-X-ENDLIST\n")
        segment.write_text("segment")
        return {
            "status": "ok",
            "playlist": str(playlist),
            "hls": {"playlist": str(playlist), "segments": [str(segment)], "segment_count": 1, "has_endlist": True},
            "hls_next_start_number": index + 1,
            "ffmpeg": {"returncode": 0, "stdout": "", "stderr": ""},
            "plan": [
                {"runtime_action": "generated_fallback_slate" if index == 1 else None, "diagnostic": "fallback used" if index == 1 else None}
            ],
        }


class FailingThenRecoveringBlockRunner:
    def __init__(self):
        self.calls = []

    def run(self, config):
        self.calls.append(config)
        index = len(self.calls) - 1
        playlist = config.output_dir / f"recover-{index}.m3u8"
        segment = config.output_dir / f"recover-{index}_00000.ts"
        playlist.write_text(f"#EXTM3U\n{segment.name}\n#EXT-X-ENDLIST\n")
        segment.write_text("segment")
        if index == 0:
            return {
                "status": "ffmpeg-error",
                "playlist": str(playlist),
                "hls": {"playlist": str(playlist), "segments": [str(segment)], "segment_count": 1, "has_endlist": True},
                "hls_next_start_number": 1,
                "ffmpeg": {"returncode": 1, "stdout": "", "stderr": "boom"},
                "catch_up": {"applied": True, "media_seek": 300.0},
                "plan": [],
            }
        return {
            "status": "ok",
            "playlist": str(playlist),
            "hls": {"playlist": str(playlist), "segments": [str(segment)], "segment_count": 1, "has_endlist": True},
            "hls_next_start_number": 2,
            "ffmpeg": {"returncode": 0, "stdout": "", "stderr": ""},
            "catch_up": {"applied": True, "media_seek": 360.0},
            "plan": [],
        }


class FakeFillerRunner:
    def __init__(self, on_run=None):
        self.calls = []
        self.on_run = on_run

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_run is not None:
            self.on_run(kwargs)
        return {
            "status": "ok",
            "playlist": str(kwargs["output_dir"] / f"{kwargs['output_name']}.m3u8"),
            "hls_next_start_number": kwargs["hls_start_number"] + 3,
            "hls": {"segment_count": 3, "segments": []},
            "ffmpeg": {"returncode": 0, "stdout": "", "stderr": ""},
        }


class LiveControllerTests(unittest.TestCase):
    def test_runs_two_blocks_in_order_cleans_stale_hls_files_and_emits_json_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel_dir = root / "Sky_One"
            channel_dir.mkdir()
            stale_segment = channel_dir / "old_00000.ts"
            stale_playlist = channel_dir / "old.m3u8"
            keep_file = channel_dir / "notes.txt"
            stale_segment.write_text("old")
            stale_playlist.write_text("old")
            keep_file.write_text("keep")

            client = FakeScheduleClient()
            runner = FakeBlockRunner()
            controller = LiveController(schedule_client=client, block_runner=runner)

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=root,
                    max_blocks=2,
                    duration_limit=10,
                    now=datetime(2026, 6, 17, 10, 5, 0),
                )
            )

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["channel_output_dir"], str(channel_dir))
            self.assertEqual([event["event"] for event in result["events"]], ["block_start", "block_complete", "block_start", "block_complete"])
            self.assertEqual([event["block"]["index"] for event in result["events"] if event["event"] == "block_start"], [0, 1])
            self.assertEqual([event["block"]["title"] for event in result["events"] if event["event"] == "block_start"], ["First Live Block", "Second Live Block"])
            self.assertEqual([call.output_dir for call in runner.calls], [channel_dir, channel_dir])
            self.assertEqual([call.output_name for call in runner.calls], ["Sky_One", "Sky_One"])
            self.assertEqual([call.duration_limit for call in runner.calls], [10, 10])
            self.assertEqual([call.hls_start_number for call in runner.calls], [0, 1])
            self.assertEqual([call.hls_append for call in runner.calls], [False, True])
            self.assertFalse(stale_segment.exists())
            self.assertFalse(stale_playlist.exists())
            self.assertTrue(keep_file.exists())
            complete_events = [event for event in result["events"] if event["event"] == "block_complete"]
            self.assertEqual([event["ffmpeg_returncode"] for event in complete_events], [0, 0])
            self.assertEqual(complete_events[0]["playlist"], str(channel_dir / "fake-0.m3u8"))
            self.assertEqual(complete_events[1]["runtime_fallback_diagnostics"], ["fallback used"])
            json.dumps(result)

    def test_emits_live_status_updates_with_current_and_next_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            updates = []
            controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=FakeBlockRunner())

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=Path(tmp),
                    max_blocks=1,
                    duration_limit=10,
                    now=datetime(2026, 6, 17, 10, 5, 0),
                    status_callback=updates.append,
                )
            )

        self.assertGreaterEqual(len(updates), 2)
        first = updates[0]
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["active_block"]["title"], "First Live Block")
        self.assertEqual(first["upcoming_blocks"][0]["title"], "Second Live Block")
        self.assertEqual(first["hls"]["playlist"], str(Path(tmp) / "Sky_One" / "Sky_One.m3u8"))
        self.assertEqual(first["events"][-1]["event"], "block_start")
        self.assertEqual(first["schedule_now"], "2026-06-17T10:05:00")
        self.assertEqual(first["active_block"]["plan"][0]["path"], "catalog/first.mp4")
        self.assertEqual(updates[-1]["status"], "complete")
        self.assertEqual(result["status"], "complete")


    def test_does_not_start_future_block_when_current_block_finishes_before_wallclock_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            sleeps = []
            updates = []
            current = [datetime(2026, 6, 17, 10, 5, 0)]

            def sleep_until_boundary(seconds):
                sleeps.append(seconds)
                current[0] = datetime(2026, 6, 17, 10, 30, 0)

            runner = FakeBlockRunner()
            filler = FakeFillerRunner(on_run=lambda kwargs: current.__setitem__(0, datetime(2026, 6, 17, 10, 30, 0)))
            controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=runner, filler_runner=filler)

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=Path(tmp),
                    max_blocks=2,
                    duration_limit=1800,
                    clock=lambda: current[0],
                    sleep=sleep_until_boundary,
                    status_callback=updates.append,
                )
            )

        event_names = [event["event"] for event in result["events"]]
        started = [event["block"]["title"] for event in result["events"] if event["event"] == "block_start"]
        self.assertEqual(started, ["First Live Block", "Second Live Block"])
        self.assertLess(event_names.index("block_wait_until_boundary"), event_names.index("block_start", 3))
        self.assertIn("block_filler", event_names)
        self.assertEqual(sleeps, [])
        self.assertEqual([call.duration_limit for call in runner.calls], [1500.0, 1800])
        self.assertEqual(updates[-1]["status"], "complete")

    def test_runs_filler_hls_during_wait_when_block_finishes_before_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            sleeps = []
            updates = []
            current = [datetime(2026, 6, 17, 10, 5, 0)]

            def sleep_until_boundary(seconds):
                sleeps.append(seconds)
                current[0] = datetime(2026, 6, 17, 10, 30, 0)

            runner = FakeBlockRunner()
            filler = FakeFillerRunner(on_run=lambda kwargs: current.__setitem__(0, datetime(2026, 6, 17, 10, 30, 0)))
            controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=runner, filler_runner=filler)

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=Path(tmp),
                    max_blocks=2,
                    duration_limit=1800,
                    clock=lambda: current[0],
                    sleep=sleep_until_boundary,
                    status_callback=updates.append,
                )
            )

        self.assertEqual(len(filler.calls), 1)
        self.assertEqual(filler.calls[0]["duration"], 1500.0)
        self.assertEqual(filler.calls[0]["hls_append"], True)
        self.assertEqual(filler.calls[0]["hls_start_number"], 1)
        self.assertEqual(filler.calls[0]["output_name"], "Sky_One")
        self.assertEqual(sleeps, [])
        self.assertIn("block_filler", [event["event"] for event in result["events"]])
        filler_event = [event for event in result["events"] if event["event"] == "block_filler"][0]
        self.assertEqual(filler_event["duration"], 1500.0)
        self.assertEqual(filler_event["hls_start_number"], 1)
        self.assertEqual(filler_event["hls_next_start_number"], 4)
        self.assertEqual([call.hls_start_number for call in runner.calls], [0, 4])

    def test_recovers_failed_ffmpeg_run_at_current_wallclock_without_advancing_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ticks = [
                datetime(2026, 6, 17, 10, 5, 0),
                datetime(2026, 6, 17, 10, 6, 0),
                datetime(2026, 6, 17, 10, 30, 0),
            ]

            def clock():
                return ticks.pop(0) if ticks else datetime(2026, 6, 17, 10, 30, 0)

            runner = FailingThenRecoveringBlockRunner()
            controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=runner)

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=root,
                    max_blocks=1,
                    duration_limit=1800,
                    clock=clock,
                    sleep=lambda seconds: None,
                    status_callback=lambda update: None,
                )
            )

        self.assertEqual(len(runner.calls), 2)
        self.assertEqual([call.now for call in runner.calls], [datetime(2026, 6, 17, 10, 5, 0), datetime(2026, 6, 17, 10, 6, 0)])
        self.assertEqual([call.hls_append for call in runner.calls], [False, True])
        self.assertEqual([call.hls_start_number for call in runner.calls], [0, 1])
        self.assertEqual([event["event"] for event in result["events"]], ["block_start", "block_complete", "block_recovery", "block_complete"])
        self.assertEqual([event["block"]["title"] for event in result["events"] if event["event"] == "block_complete"], ["First Live Block", "First Live Block"])
        recovery = [event for event in result["events"] if event["event"] == "block_recovery"][0]
        self.assertEqual(recovery["attempt"], 1)
        self.assertEqual(recovery["reason"], "ffmpeg-error")
        self.assertEqual(recovery["recover_at"], "2026-06-17T10:06:00")
        self.assertEqual(result["status"], "complete")

    def test_complete_event_exposes_plan_item_and_commercial_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = mock.Mock()
            runner.run.return_value = {
                "status": "dry-run",
                "playlist": str(root / "Sky_One" / "out.m3u8"),
                "ffmpeg": {"returncode": None, "stdout": "", "stderr": ""},
                "plan_item_count": 4,
                "plan_item_counts": {"feature": 1, "commercial": 2, "bump": 1},
                "commercial_count": 2,
                "ad_count": 2,
                "commercial_paths": ["/mnt/fs42/catalog/commercial/ad-a.mp4", "/mnt/fs42/catalog/commercial/ad-b.mp4"],
                "catch_up": {"applied": True, "start_plan_index": 2, "media_seek": 510.0},
                "plan": [],
            }
            controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=runner)

            result = controller.run(LiveControllerConfig(channel="Sky One", output_root=root, max_blocks=1, duration_limit=10, now=datetime(2026, 6, 17, 10, 5, 0), dry_run=True))

        complete = [event for event in result["events"] if event["event"] == "block_complete"][0]
        self.assertEqual(complete["plan_item_count"], 4)
        self.assertEqual(complete["plan_item_counts"], {"feature": 1, "commercial": 2, "bump": 1})
        self.assertEqual(complete["commercial_count"], 2)
        self.assertEqual(complete["ad_count"], 2)
        self.assertEqual(complete["commercial_paths"], ["/mnt/fs42/catalog/commercial/ad-a.mp4", "/mnt/fs42/catalog/commercial/ad-b.mp4"])
        self.assertEqual(complete["catch_up"], {"applied": True, "start_plan_index": 2, "media_seek": 510.0})
        self.assertEqual(result["plan_item_count"], 4)
        self.assertEqual(result["plan_item_counts"], {"feature": 1, "commercial": 2, "bump": 1})
        self.assertEqual(result["commercial_count"], 2)
        self.assertEqual(result["commercial_paths"], ["/mnt/fs42/catalog/commercial/ad-a.mp4", "/mnt/fs42/catalog/commercial/ad-b.mp4"])

    def test_cli_is_bounded_and_prints_json(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("fs42stream.live_controller.FS42ScheduleClient.fetch_schedule", return_value=SCHEDULE), \
             mock.patch("fs42stream.live_controller.BlockRunner.run", return_value={
                 "status": "ok",
                 "playlist": str(Path(tmp) / "Sky_One" / "out.m3u8"),
                 "ffmpeg": {"returncode": 0, "stdout": "", "stderr": ""},
                 "hls": {"playlist": str(Path(tmp) / "Sky_One" / "out.m3u8"), "segments": [], "segment_count": 0, "has_endlist": True},
                 "plan": [],
             }), \
             mock.patch("sys.stdout") as stdout:
            rc = main(["--channel", "Sky One", "--output-root", tmp, "--max-blocks", "2", "--duration-limit", "10", "--now", "2026-06-17T10:05:00"])

        self.assertEqual(rc, 0)
        payload = json.loads("".join(call.args[0] for call in stdout.write.call_args_list))
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["blocks_completed"], 2)
        self.assertEqual(payload["events"][0]["block"]["title"], "First Live Block")


if __name__ == "__main__":
    unittest.main()
