import json
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from fs42stream.ffmpeg import FFMpegHLSCommandBuilder
from fs42stream.ffprobe import FFProbe, ProbeResult
from fs42stream.hls_harness import create_fixture_clips, inspect_hls_output
from fs42stream.live_controller import HLSBlackSlateFillerRunner, LiveController, LiveControllerConfig, SharedScheduleBlockClock, main
from fs42stream.paths import PathResolver
from fs42stream.planner import BlockPlanner
from fs42stream.run_block import BlockRunner
from fs42stream.systemd_service import ServiceConfig


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
    def __init__(self, summary=None):
        self.calls = []
        self.summary = summary

    def fetch_schedule(self, channel, expected_blocks=None):
        self.calls.append((channel, expected_blocks))
        return SCHEDULE

    def fetch_schedule_summary(self, channel):
        return self.summary or {"schedule_summary": {"network_id": channel, "start": "2026-06-17T10:00:00", "end": "2026-06-17T11:00:00"}}


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
        callback = getattr(config, "ffmpeg_status_callback", None)
        if callback is not None:
            callback({"state": "running", "pid": 4321, "started_at": "2026-06-17T10:00:05+00:00"})
            callback({"state": "exited", "pid": None, "started_at": "2026-06-17T10:00:05+00:00", "last_exit_code": 0})
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


class DurationConsumingBlockRunner:
    def __init__(self, current):
        self.calls = []
        self.current = current

    def run(self, config):
        self.calls.append(config)
        self.current[0] = self.current[0] + timedelta(seconds=config.duration_limit)
        playlist = config.output_dir / "consume.m3u8"
        segment = config.output_dir / "consume_00000.ts"
        playlist.write_text(f"#EXTM3U\n{segment.name}\n")
        segment.write_text("segment")
        return {
            "status": "ok",
            "playlist": str(playlist),
            "hls": {"playlist": str(playlist), "segments": [str(segment)], "segment_count": 1, "has_endlist": False},
            "hls_next_start_number": len(self.calls),
            "ffmpeg": {"returncode": 0, "stdout": "", "stderr": ""},
            "plan": [],
        }


class PrematureSuccessBlockRunner:
    def __init__(self, current, *, first_advance_seconds=120, second_advance_seconds=1500):
        self.calls = []
        self.current = current
        self.first_advance_seconds = first_advance_seconds
        self.second_advance_seconds = second_advance_seconds

    def run(self, config):
        self.calls.append(config)
        advance = self.first_advance_seconds if len(self.calls) == 1 else self.second_advance_seconds
        self.current[0] = self.current[0] + timedelta(seconds=advance)
        playlist = config.output_dir / f"premature-{len(self.calls)-1}.m3u8"
        segment = config.output_dir / f"premature-{len(self.calls)-1}_00000.ts"
        playlist.write_text(f"#EXTM3U\n{segment.name}\n")
        segment.write_text("segment")
        return {
            "status": "ok",
            "playlist": str(playlist),
            "hls": {"playlist": str(playlist), "segments": [str(segment)], "segment_count": 1, "has_endlist": False},
            "hls_next_start_number": len(self.calls),
            "ffmpeg": {"returncode": 0, "stdout": "", "stderr": ""},
            "plan": [],
        }


class SequencedPrematureSuccessBlockRunner:
    def __init__(self, current, advances):
        self.calls = []
        self.current = current
        self.advances = list(advances)

    def run(self, config):
        self.calls.append(config)
        advance = self.advances[min(len(self.calls) - 1, len(self.advances) - 1)]
        self.current[0] = self.current[0] + timedelta(seconds=advance)
        playlist = config.output_dir / f"sequenced-premature-{len(self.calls)-1}.m3u8"
        segment = config.output_dir / f"sequenced-premature-{len(self.calls)-1}_00000.ts"
        playlist.write_text(f"#EXTM3U\n{segment.name}\n")
        segment.write_text("segment")
        return {
            "status": "ok",
            "playlist": str(playlist),
            "hls": {"playlist": str(playlist), "segments": [str(segment)], "segment_count": 1, "has_endlist": False},
            "hls_next_start_number": len(self.calls),
            "ffmpeg": {"returncode": 0, "stdout": "", "stderr": ""},
            "plan": [],
        }


class EmittedDurationBlockRunner:
    def __init__(self, emitted_media_duration):
        self.calls = []
        self.emitted_media_duration = emitted_media_duration

    def run(self, config):
        self.calls.append(config)
        playlist = config.output_dir / "emitted-duration.m3u8"
        segment = config.output_dir / "emitted-duration_00000.ts"
        playlist.write_text(f"#EXTM3U\n{segment.name}\n")
        segment.write_text("segment")
        return {
            "status": "ok",
            "playlist": str(playlist),
            "hls": {"playlist": str(playlist), "segments": [str(segment)], "segment_count": 1, "has_endlist": False},
            "hls_next_start_number": 1,
            "emitted_media_duration": self.emitted_media_duration,
            "ffmpeg": {"returncode": 0, "stdout": "", "stderr": ""},
            "plan": [],
        }


class LongBlockScheduleClient:
    def fetch_schedule(self, channel, expected_blocks=None):
        return {
            "network_name": "Sky One",
            "schedule_blocks": [
                {
                    "title": "Wwf Raw Is War",
                    "start_time": "2026-06-27T11:00:00",
                    "end_time": "2026-06-27T12:30:00",
                    "plan": [{"path": "catalog/raw.mp4", "duration": 5400, "skip": 0, "is_stream": False}],
                },
                {
                    "title": "Next Block",
                    "start_time": "2026-06-27T12:30:00",
                    "end_time": "2026-06-27T13:00:00",
                    "plan": [{"path": "catalog/next.mp4", "duration": 1800, "skip": 0, "is_stream": False}],
                },
            ],
        }


class LiveControllerTests(unittest.TestCase):
    def test_default_live_config_uses_ts_primary_playout_mode_and_passes_it_to_block_runner(self):
        updates = []
        runner = FakeBlockRunner()
        controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=runner)

        result = controller.run(
            LiveControllerConfig(
                channel="Sky One",
                output_root=Path(datetime.now().strftime("/tmp/fs42-live-%Y%m%d%H%M%S")),
                max_blocks=1,
                duration_limit=10,
                now=datetime(2026, 6, 17, 10, 5, 0),
                dry_run=True,
                status_callback=updates.append,
            )
        )

        self.assertEqual(runner.calls[0].playout_mode, "ts-primary")
        self.assertEqual(updates[0]["playout_mode"], "ts-primary")
        self.assertEqual(result["playout_mode"], "ts-primary")

    def test_live_status_publishes_current_ffmpeg_process_while_rendering(self):
        updates = []
        runner = FakeBlockRunner()
        controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=runner)

        controller.run(
            LiveControllerConfig(
                channel="Example Channel",
                output_root=Path(datetime.now().strftime("/tmp/fs42-live-ffmpeg-%Y%m%d%H%M%S")),
                max_blocks=1,
                duration_limit=10,
                now=datetime(2026, 6, 17, 10, 5, 0),
                dry_run=True,
                status_callback=updates.append,
                stream_profile="direct",
            )
        )

        running_updates = [update for update in updates if update.get("ffmpeg", {}).get("state") == "running"]
        self.assertTrue(running_updates)
        self.assertEqual(running_updates[-1]["ffmpeg"]["pid"], 4321)
        self.assertEqual(running_updates[-1]["ffmpeg"]["profile"], "direct")
        self.assertEqual(running_updates[-1]["ffmpeg"]["started_at"], "2026-06-17T10:00:05+00:00")
        exited_updates = [update for update in updates if update.get("ffmpeg", {}).get("state") == "exited"]
        self.assertTrue(exited_updates)
        self.assertEqual(exited_updates[-1]["ffmpeg"]["last_exit_code"], 0)
        self.assertEqual(updates[-1]["status"], "complete")
        self.assertEqual(updates[-1]["ffmpeg"]["state"], "exited")
        self.assertEqual(updates[-1]["ffmpeg"]["last_exit_code"], 0)

    def test_shared_schedule_clock_forces_profiles_to_use_same_schedule_snapshot(self):
        def schedule(title):
            return {
                "network_name": "Sky One",
                "schedule_blocks": [
                    {
                        "title": title,
                        "start_time": "2026-06-17T10:00:00",
                        "end_time": "2026-06-17T10:30:00",
                        "plan": [{"path": f"catalog/{title}.mp4", "duration": 1800, "skip": 0, "is_stream": False}],
                    }
                ],
            }

        class OneScheduleClient:
            def __init__(self, payload):
                self.payload = payload
                self.calls = 0

            def fetch_schedule(self, channel, expected_blocks=None):
                self.calls += 1
                return self.payload

            def fetch_schedule_summary(self, channel):
                return {"schedule_summary": {"network_id": channel, "start": "2026-06-17T10:00:00", "end": "2026-06-17T10:30:00"}}

        class RecordingRunner:
            def __init__(self):
                self.calls = []

            def run(self, config):
                self.calls.append(config)
                title = config.schedule["schedule_blocks"][config.selected_block_index]["title"]
                playlist = config.output_dir / f"{config.stream_profile}.m3u8"
                segment = config.output_dir / f"{config.stream_profile}_00000.ts"
                playlist.write_text(f"#EXTM3U\n{segment.name}\n")
                segment.write_text("segment")
                return {
                    "status": "ok",
                    "playlist": str(playlist),
                    "hls": {"playlist": str(playlist), "segments": [str(segment)], "segment_count": 1, "has_endlist": False},
                    "hls_next_start_number": 1,
                    "ffmpeg": {"returncode": 0, "stdout": "", "stderr": ""},
                    "plan": [{"title": title}],
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            direct_client = OneScheduleClient(schedule("Direct-only block"))
            jellyfin_client = OneScheduleClient(schedule("Jellyfin-only block"))
            direct_runner = RecordingRunner()
            jellyfin_runner = RecordingRunner()
            clock = SharedScheduleBlockClock(profile_count=2, timeout=2.0)
            results = {}
            errors = []

            def run_profile(profile, client, runner):
                try:
                    controller = LiveController(schedule_client=client, block_runner=runner)
                    results[profile] = controller.run(
                        LiveControllerConfig(
                            channel="Sky One",
                            output_root=root,
                            max_blocks=1,
                            duration_limit=10,
                            now=datetime(2026, 6, 17, 10, 5, 0),
                            stream_profile=profile,
                            shared_schedule_clock=clock,
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=run_profile, args=("direct", direct_client, direct_runner)),
                threading.Thread(target=run_profile, args=("jellyfin", jellyfin_client, jellyfin_runner)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(errors, [])
        direct_title = direct_runner.calls[0].schedule["schedule_blocks"][direct_runner.calls[0].selected_block_index]["title"]
        jellyfin_title = jellyfin_runner.calls[0].schedule["schedule_blocks"][jellyfin_runner.calls[0].selected_block_index]["title"]
        self.assertEqual(direct_title, jellyfin_title)
        self.assertEqual(results["direct"]["active_block"]["title"], results["jellyfin"]["active_block"]["title"])

    @unittest.skipUnless(Path("/usr/bin/ffmpeg").exists() and Path("/usr/bin/ffprobe").exists(), "requires system ffmpeg/ffprobe")
    def test_direct_profile_keeps_dense_segment_numbering_across_block_boundary(self):
        class FixtureScheduleClient:
            def __init__(self, schedule):
                self.schedule = schedule

            def fetch_schedule(self, channel, expected_blocks=None):
                return self.schedule

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = create_fixture_clips(root / "clips", count=4, duration=2.2)
            schedule = {
                "network_name": "Sky One",
                "schedule_blocks": [
                    {
                        "title": "Show A",
                        "start_time": "2026-06-17T10:00:00",
                        "end_time": "2026-06-17T10:00:05",
                        "plan": [
                            {"realpath": str(clips[0]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "feature", "media_type": "video"},
                            {"realpath": str(clips[1]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "commercial", "media_type": "video"},
                        ],
                    },
                    {
                        "title": "Show B",
                        "start_time": "2026-06-17T10:00:05",
                        "end_time": "2026-06-17T10:00:10",
                        "plan": [
                            {"realpath": str(clips[2]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "bump", "media_type": "video"},
                            {"realpath": str(clips[3]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "feature", "media_type": "video"},
                        ],
                    },
                ],
            }
            client = FixtureScheduleClient(schedule)
            runner = BlockRunner(
                client=client,
                planner=BlockPlanner(PathResolver(fs42_root=root, sdtv_root=root), FFProbe("/usr/bin/ffprobe")),
                builder=FFMpegHLSCommandBuilder("/usr/bin/ffmpeg"),
            )
            controller = LiveController(schedule_client=client, block_runner=runner)

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=root / "out",
                    max_blocks=2,
                    duration_limit=10,
                    now=datetime(2026, 6, 17, 10, 0, 0),
                )
            )
            playlist = Path(result["channel_output_dir"]) / "Sky_One.m3u8"
            inspection = inspect_hls_output(playlist)
            playlist_text = playlist.read_text()

        segment_names = [Path(path).name for path in inspection.segments]
        self.assertEqual(segment_names, [f"Sky_One_{index:05d}.ts" for index in range(len(segment_names))])
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:0", playlist_text)

    @unittest.skipUnless(Path("/usr/bin/ffmpeg").exists() and Path("/usr/bin/ffprobe").exists(), "requires system ffmpeg/ffprobe")
    def test_jellyfin_profile_marks_real_block_boundaries_when_segment_timestamps_reset(self):
        class FixtureScheduleClient:
            def __init__(self, schedule):
                self.schedule = schedule

            def fetch_schedule(self, channel, expected_blocks=None):
                return self.schedule

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = create_fixture_clips(root / "clips", count=4, duration=2.2)
            schedule = {
                "network_name": "Sky One",
                "schedule_blocks": [
                    {
                        "title": "Show A",
                        "start_time": "2026-06-17T10:00:00",
                        "end_time": "2026-06-17T10:00:05",
                        "plan": [
                            {"realpath": str(clips[0]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "feature", "media_type": "video"},
                            {"realpath": str(clips[1]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "commercial", "media_type": "video"},
                        ],
                    },
                    {
                        "title": "Show B",
                        "start_time": "2026-06-17T10:00:05",
                        "end_time": "2026-06-17T10:00:10",
                        "plan": [
                            {"realpath": str(clips[2]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "bump", "media_type": "video"},
                            {"realpath": str(clips[3]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "feature", "media_type": "video"},
                        ],
                    },
                ],
            }
            client = FixtureScheduleClient(schedule)
            runner = BlockRunner(
                client=client,
                planner=BlockPlanner(PathResolver(fs42_root=root, sdtv_root=root), FFProbe("/usr/bin/ffprobe")),
                builder=FFMpegHLSCommandBuilder("/usr/bin/ffmpeg"),
            )
            controller = LiveController(schedule_client=client, block_runner=runner)

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=root / "out",
                    max_blocks=2,
                    duration_limit=10,
                    now=datetime(2026, 6, 17, 10, 0, 0),
                    stream_profile="jellyfin",
                )
            )
            playlist = Path(result["channel_output_dir"]) / "Sky_One.m3u8"
            inspection = inspect_hls_output(playlist)
            playlist_text = playlist.read_text()

        segment_names = [Path(path).name for path in inspection.segments]
        self.assertGreaterEqual(len(segment_names), 6)
        self.assertEqual(segment_names, [f"Sky_One_{index:05d}.ts" for index in range(len(segment_names))])
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:0", playlist_text)
        self.assertEqual(playlist_text.count("#EXT-X-DISCONTINUITY"), 0)

    def test_boundary_filler_uses_transition_safe_hls_cadence(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(["ffmpeg"], 0, stdout="", stderr=""),
        ) as run:
            HLSBlackSlateFillerRunner("/usr/bin/ffmpeg").run(
                output_dir=Path(tmp),
                output_name="Sky_One",
                duration=3596,
                hls_start_number=12,
                hls_append=True,
            )

        command = run.call_args.args[0]
        self.assertIn(["-hls_time", "2"], [command[index:index + 2] for index in range(len(command) - 1)])
        self.assertIn(["-g", "50"], [command[index:index + 2] for index in range(len(command) - 1)])
        self.assertIn(["-keyint_min", "50"], [command[index:index + 2] for index in range(len(command) - 1)])
        self.assertIn(["-sc_threshold", "0"], [command[index:index + 2] for index in range(len(command) - 1)])
        self.assertIn(["-hls_list_size", "60"], [command[index:index + 2] for index in range(len(command) - 1)])
        self.assertIn("-hls_flags", command)
        self.assertIn("omit_endlist+append_list+discont_start", command)

    def test_jellyfin_boundary_filler_uses_explicit_elapsed_timestamp_offset(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(["ffmpeg"], 0, stdout="", stderr=""),
        ) as run:
            HLSBlackSlateFillerRunner("/usr/bin/ffmpeg").run(
                output_dir=Path(tmp),
                output_name="Sky_One",
                duration=7.3,
                hls_start_number=12,
                hls_start_time_offset=31.5,
                hls_append=True,
                stream_profile="jellyfin",
            )

        command = run.call_args.args[0]
        joined = " ".join(command)
        self.assertIn("-output_ts_offset 31.5", joined)
        self.assertIn("-t 8", joined)
        self.assertNotIn("-output_ts_offset 24", joined)
        self.assertIn("-hls_list_size 60", joined)
        self.assertIn("-hls_flags omit_endlist+append_list", joined)
        self.assertNotIn("-start_number", joined)
        self.assertNotIn("discont_start", joined)
        self.assertNotIn("-avoid_negative_ts", joined)

    def test_jellyfin_boundary_filler_reports_elapsed_offset_after_live_window_rollover(self):
        def write_rolled_playlist(command, check, shell, stdout, stderr, text):
            playlist = Path(command[-1])
            lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:8"]
            for number in range(8, 20):
                lines.extend(["#EXTINF:2.000000,", f"Sky_One_{number:05d}.ts"])
            playlist.write_text("\n".join(lines) + "\n")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp, mock.patch("subprocess.run", side_effect=write_rolled_playlist):
            diagnostics = HLSBlackSlateFillerRunner("/usr/bin/ffmpeg").run(
                output_dir=Path(tmp),
                output_name="Sky_One",
                duration=40.0,
                hls_start_number=0,
                hls_start_time_offset=10.0,
                hls_append=True,
                stream_profile="jellyfin",
            )
            playlist_text = (Path(tmp) / "Sky_One.m3u8").read_text()

        self.assertEqual(diagnostics["hls_next_start_number"], 20)
        self.assertEqual(diagnostics["hls_segment_duration"], 40.0)
        self.assertEqual(diagnostics["hls_next_start_time_offset"], 50.0)
        self.assertNotIn("#EXT-X-DISCONTINUITY\n#EXT-X-DISCONTINUITY", playlist_text)

    def test_status_and_block_start_event_share_supervisor_projection_state(self):
        updates = []
        controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=FakeBlockRunner())

        controller.run(
            LiveControllerConfig(
                channel="Sky One",
                output_root=Path(datetime.now().strftime("/tmp/fs42-live-status-%Y%m%d%H%M%S")),
                max_blocks=1,
                duration_limit=10,
                now=datetime(2026, 6, 17, 10, 5, 0),
                dry_run=True,
                status_callback=updates.append,
            )
        )

        self.assertGreaterEqual(len(updates), 1)
        first = updates[0]
        start_event = first["events"][0]
        self.assertEqual(first["active_block"], first["playout"]["current_block"])
        self.assertEqual(first["upcoming_blocks"][0], first["playout"]["next_block"])
        self.assertEqual(start_event["block"], first["playout"]["current_block"])
        self.assertEqual(start_event["current_item"], first["playout"]["current_item"])
        self.assertEqual(start_event["next_item"], first["playout"]["next_item"])
        self.assertEqual(start_event["playout"]["current_block"]["plan"][0]["skip"], 300.0)
        self.assertEqual(start_event["current_item"]["media_seek"], 300.0)

    @unittest.skipUnless(Path("/usr/bin/ffmpeg").exists() and Path("/usr/bin/ffprobe").exists(), "requires system ffmpeg/ffprobe")
    def test_jellyfin_live_repro_keeps_monotonic_segment_starts_without_discontinuities(self):
        class FixtureScheduleClient:
            def __init__(self, schedule):
                self.schedule = schedule

            def fetch_schedule(self, channel, expected_blocks=None):
                return self.schedule

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = create_fixture_clips(root / "clips", count=4, duration=2.2)
            schedule = {
                "network_name": "Sky One",
                "schedule_blocks": [
                    {
                        "title": "Show A",
                        "start_time": "2026-06-17T10:00:00",
                        "end_time": "2026-06-17T10:00:08",
                        "plan": [
                            {"realpath": str(clips[0]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "feature", "media_type": "video"},
                            {"realpath": str(clips[1]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "commercial", "media_type": "video"},
                        ],
                    },
                    {
                        "title": "Show B",
                        "start_time": "2026-06-17T10:00:08",
                        "end_time": "2026-06-17T10:00:14",
                        "plan": [
                            {"realpath": str(clips[2]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "bump", "media_type": "video"},
                            {"realpath": str(clips[3]), "duration": 2.2, "skip": 0, "is_stream": False, "content_type": "feature", "media_type": "video"},
                        ],
                    },
                ],
            }
            current = [
                datetime(2026, 6, 17, 10, 0, 0),
                datetime(2026, 6, 17, 10, 0, 5),
                datetime(2026, 6, 17, 10, 0, 8),
                datetime(2026, 6, 17, 10, 0, 8),
            ]

            def clock():
                return current.pop(0) if current else datetime(2026, 6, 17, 10, 0, 8)

            client = FixtureScheduleClient(schedule)
            runner = BlockRunner(
                client=client,
                planner=BlockPlanner(PathResolver(fs42_root=root, sdtv_root=root), FFProbe("/usr/bin/ffprobe")),
                builder=FFMpegHLSCommandBuilder("/usr/bin/ffmpeg"),
            )
            controller = LiveController(
                schedule_client=client,
                block_runner=runner,
                filler_runner=HLSBlackSlateFillerRunner("/usr/bin/ffmpeg"),
            )

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=root / "out",
                    max_blocks=2,
                    duration_limit=7200,
                    clock=clock,
                    stream_profile="jellyfin",
                )
            )
            playlist = Path(result["channel_output_dir"]) / "Sky_One.m3u8"
            playlist_text = playlist.read_text()
            first = json.loads(subprocess.check_output(["/usr/bin/ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of", "json", str(playlist.parent / "Sky_One_00000.ts")], text=True))
            filler = json.loads(subprocess.check_output(["/usr/bin/ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of", "json", str(playlist.parent / "Sky_One_00003.ts")], text=True))
            next_block = json.loads(subprocess.check_output(["/usr/bin/ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of", "json", str(playlist.parent / "Sky_One_00005.ts")], text=True))

        self.assertEqual(playlist_text.count("#EXT-X-DISCONTINUITY"), 0)
        self.assertLess(float(first["format"]["start_time"]), float(filler["format"]["start_time"]))
        self.assertLess(float(filler["format"]["start_time"]), float(next_block["format"]["start_time"]))

    def test_service_duration_default_spans_ninety_minute_block_without_boundary_filler(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = [datetime(2026, 6, 27, 11, 0, 0)]
            runner = DurationConsumingBlockRunner(current)
            filler = FakeFillerRunner()
            controller = LiveController(schedule_client=LongBlockScheduleClient(), block_runner=runner, filler_runner=filler)

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=Path(tmp),
                    max_blocks=2,
                    duration_limit=ServiceConfig().duration_limit,
                    clock=lambda: current[0],
                )
            )

        self.assertEqual(runner.calls[0].duration_limit, 5400.0)
        self.assertEqual(filler.calls, [])
        self.assertNotIn("block_filler", [event["event"] for event in result["events"]])

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
        self.assertEqual(first["active_block"]["selection_reason"], "current")
        self.assertEqual(first["active_block"]["plan"][0]["path"], "catalog/first.mp4")
        self.assertEqual(first["active_block"]["plan"][0]["skip"], 300.0)
        self.assertEqual(first["active_block"]["plan"][0]["duration"], 1500.0)
        self.assertEqual(updates[-1]["status"], "complete")
        self.assertEqual(result["status"], "complete")

    def test_block_events_use_engine_owned_render_state_when_block_starts_mid_playout(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=FakeBlockRunner())

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=Path(tmp),
                    max_blocks=1,
                    duration_limit=10,
                    now=datetime(2026, 6, 17, 10, 5, 0),
                    dry_run=True,
                )
            )

        start_event = [event for event in result["events"] if event["event"] == "block_start"][0]
        complete_event = [event for event in result["events"] if event["event"] == "block_complete"][0]

        self.assertEqual(start_event["block"]["selection_reason"], "current")
        self.assertEqual(start_event["block"]["plan"][0]["skip"], 300.0)
        self.assertEqual(start_event["block"]["plan"][0]["duration"], 1500.0)
        self.assertEqual(complete_event["block"]["plan"][0]["skip"], 300.0)
        self.assertEqual(complete_event["block"]["plan"][0]["duration"], 1500.0)

    def test_status_callback_reports_stale_schedule_and_placeholder_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            updates = []
            summary = {"schedule_summary": {"network_id": "Sky One", "start": "2026-06-17T08:00:00", "end": "2026-06-17T09:00:00"}}
            controller = LiveController(schedule_client=FakeScheduleClient(summary=summary), block_runner=FakeBlockRunner())

            controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=Path(tmp),
                    max_blocks=1,
                    duration_limit=600,
                    now=datetime(2026, 6, 17, 10, 5, 0),
                    status_callback=updates.append,
                    dry_run=True,
                )
            )

        first = updates[0]
        self.assertTrue(first["stale_schedule"]["active"])
        self.assertEqual(first["active_block"]["title"], "Schedule stale - BRB")
        self.assertEqual(first["active_block"]["plan"][0]["path"], "runtime/brb.png")

    def test_status_callback_exposes_engine_owned_current_and_next_plan_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            updates = []
            controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=FakeBlockRunner())

            controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=Path(tmp),
                    max_blocks=1,
                    duration_limit=10,
                    now=datetime(2026, 6, 17, 10, 5, 0),
                    status_callback=updates.append,
                    dry_run=True,
                )
            )

        self.assertGreaterEqual(len(updates), 1)
        first = updates[0]
        self.assertEqual(first["supervisor"]["selection"], {"index": 0, "reason": "current"})
        self.assertEqual(first["supervisor"]["current_block"], first["active_block"])
        self.assertEqual(first["supervisor"]["upcoming_blocks"], first["upcoming_blocks"])
        self.assertEqual(first["supervisor"]["current_item"], first["current_plan_item"])
        self.assertEqual(first["supervisor"]["next_item"], first["next_plan_item"])
        self.assertEqual(first["supervisor"]["timeline"], first["playout"]["timeline"])
        self.assertEqual(first["supervisor"]["catch_up"], {"applied": True, "schedule_now": "2026-06-17T10:05:00", "block_elapsed": 300.0, "start_plan_index": 0, "offset_in_item": 300.0, "original_skip": 0.0, "media_seek": 300.0, "remaining_item_duration": 1500.0, "dropped_plan_items": 0, "current_content_type": None, "current_path": "catalog/first.mp4"})
        self.assertEqual(first["supervisor"]["render_plan_item_count"], 1)
        self.assertEqual(first["supervisor"]["source_plan_item_count"], 1)
        self.assertEqual(first["catch_up"], first["supervisor"]["catch_up"])
        self.assertEqual(first["playout"]["current_block"]["title"], "First Live Block")
        self.assertEqual(first["playout"]["next_block"]["title"], "Second Live Block")
        self.assertEqual(first["active_block"], first["playout"]["current_block"])
        self.assertEqual(first["upcoming_blocks"][0], first["playout"]["next_block"])
        self.assertEqual(first["current_plan_item"]["path"], "catalog/first.mp4")
        self.assertEqual(first["current_plan_item"]["media_seek"], 300.0)
        self.assertEqual(first["current_plan_item"], first["playout"]["current_item"])
        self.assertIsNone(first["next_plan_item"])
        self.assertIsNone(first["playout"]["next_item"])

    def test_final_result_preserves_supervisor_contract_for_completed_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=FakeBlockRunner())

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=Path(tmp),
                    max_blocks=1,
                    duration_limit=10,
                    now=datetime(2026, 6, 17, 10, 5, 0),
                    dry_run=True,
                )
            )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["schedule_now"], "2026-06-17T10:05:00")
        self.assertEqual(result["supervisor"]["selection"], {"index": 0, "reason": "current"})
        self.assertEqual(result["supervisor"]["current_block"], result["active_block"])
        self.assertEqual(result["supervisor"]["current_item"], result["current_plan_item"])
        self.assertEqual(result["playout"]["current_block"], result["active_block"])
        self.assertEqual(result["playout"]["current_item"], result["current_plan_item"])
        self.assertEqual(result["hls"]["playlist"], str(Path(tmp) / "Sky_One" / "Sky_One.m3u8"))

    def test_jellyfin_profile_writes_isolated_hls_directory_and_passes_profile_to_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeBlockRunner()
            updates = []
            controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=runner)

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=Path(tmp),
                    max_blocks=1,
                    duration_limit=10,
                    now=datetime(2026, 6, 17, 10, 5, 0),
                    status_callback=updates.append,
                    stream_profile="jellyfin",
                )
            )

        jellyfin_dir = Path(tmp) / "Sky_One" / "jellyfin"
        self.assertEqual(runner.calls[0].output_dir, jellyfin_dir)
        self.assertEqual(runner.calls[0].output_name, "Sky_One")
        self.assertEqual(runner.calls[0].stream_profile, "jellyfin")
        self.assertEqual(result["stream_profile"], "jellyfin")
        self.assertEqual(result["channel_output_dir"], str(jellyfin_dir))
        self.assertEqual(updates[0]["hls"]["playlist"], str(jellyfin_dir / "Sky_One.m3u8"))

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

    def test_boundary_filler_overruns_wallclock_boundary_instead_of_jellyfin_startup_bridge(self):
        class FeatureScheduleClient:
            def fetch_schedule(self, channel, expected_blocks=None):
                return {
                    "network_name": "Sky One",
                    "schedule_blocks": [
                        {
                            "title": "Show A",
                            "start_time": "2026-06-17T10:00:00",
                            "end_time": "2026-06-17T10:00:05",
                            "plan": [{"path": "a.mp4", "duration": 5, "skip": 0, "content_type": "feature"}],
                        },
                        {
                            "title": "Show B",
                            "start_time": "2026-06-17T10:00:05",
                            "end_time": "2026-06-17T10:00:35",
                            "plan": [{"path": "b.mp4", "duration": 30, "skip": 0, "content_type": "feature"}],
                        },
                    ],
                }

            def fetch_schedule_summary(self, channel):
                return {"schedule_summary": {"network_id": channel, "start": "2026-06-17T10:00:00", "end": "2026-06-17T10:00:35"}}

        with tempfile.TemporaryDirectory() as tmp:
            current = [datetime(2026, 6, 17, 10, 0, 0)]

            def advance_filler(kwargs):
                current[0] = current[0] + timedelta(seconds=kwargs["duration"])

            runner = FakeBlockRunner()
            filler = FakeFillerRunner(on_run=advance_filler)
            controller = LiveController(schedule_client=FeatureScheduleClient(), block_runner=runner, filler_runner=filler)

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=Path(tmp),
                    max_blocks=2,
                    duration_limit=30,
                    clock=lambda: current[0],
                    stream_profile="jellyfin",
                    playout_mode="ts-primary",
                )
            )

        self.assertNotIn("block_startup_bridge", [event["event"] for event in result["events"]])
        block_filler = [event for event in result["events"] if event["event"] == "block_filler"]
        self.assertEqual(len(block_filler), 1)
        self.assertEqual(block_filler[0]["wait_seconds"], 5.0)
        self.assertEqual(block_filler[0]["duration"], 25.0)
        self.assertEqual(block_filler[0]["startup_guard_seconds"], 20.0)
        self.assertEqual([call["duration"] for call in filler.calls], [25.0])
        self.assertEqual([call.hls_start_number for call in runner.calls], [0, 4])
        self.assertEqual(runner.calls[1].now, datetime(2026, 6, 17, 10, 0, 25))
        self.assertEqual(runner.calls[1].stream_profile, "jellyfin")
        self.assertEqual(runner.calls[1].playout_mode, "ts-primary")

    def test_boundary_filler_overrun_applies_to_show_to_commercial_boundary_too(self):
        class CommercialScheduleClient:
            def fetch_schedule(self, channel, expected_blocks=None):
                return {
                    "network_name": "Sky One",
                    "schedule_blocks": [
                        {
                            "title": "Show A",
                            "start_time": "2026-06-17T10:00:00",
                            "end_time": "2026-06-17T10:00:05",
                            "plan": [{"path": "a.mp4", "duration": 5, "skip": 0, "content_type": "feature"}],
                        },
                        {
                            "title": "Ads",
                            "start_time": "2026-06-17T10:00:05",
                            "end_time": "2026-06-17T10:00:35",
                            "plan": [{"path": "ad.mp4", "duration": 30, "skip": 0, "content_type": "commercial"}],
                        },
                    ],
                }

            def fetch_schedule_summary(self, channel):
                return {"schedule_summary": {"network_id": channel, "start": "2026-06-17T10:00:00", "end": "2026-06-17T10:00:35"}}

        with tempfile.TemporaryDirectory() as tmp:
            current = [datetime(2026, 6, 17, 10, 0, 0)]
            runner = FakeBlockRunner()
            filler = FakeFillerRunner(on_run=lambda kwargs: current.__setitem__(0, current[0] + timedelta(seconds=kwargs["duration"])))
            controller = LiveController(schedule_client=CommercialScheduleClient(), block_runner=runner, filler_runner=filler)

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=Path(tmp),
                    max_blocks=2,
                    duration_limit=30,
                    clock=lambda: current[0],
                    stream_profile="jellyfin",
                    playout_mode="ts-primary",
                )
            )

        self.assertNotIn("block_startup_bridge", [event["event"] for event in result["events"]])
        self.assertEqual([call["duration"] for call in filler.calls], [25.0])
        self.assertEqual([call.hls_start_number for call in runner.calls], [0, 4])
        self.assertEqual(runner.calls[1].now, datetime(2026, 6, 17, 10, 0, 25))

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
        self.assertEqual(filler.calls[0]["duration"], 1520.0)
        self.assertEqual(filler.calls[0]["hls_append"], True)
        self.assertEqual(filler.calls[0]["hls_start_number"], 1)
        self.assertEqual(filler.calls[0]["output_name"], "Sky_One")
        self.assertEqual(sleeps, [])
        self.assertIn("block_filler", [event["event"] for event in result["events"]])
        filler_event = [event for event in result["events"] if event["event"] == "block_filler"][0]
        self.assertEqual(filler_event["duration"], 1520.0)
        self.assertEqual(filler_event["wait_seconds"], 1500.0)
        self.assertEqual(filler_event["hls_start_number"], 1)
        self.assertEqual(filler_event["hls_next_start_number"], 4)
        self.assertEqual([call.hls_start_number for call in runner.calls], [0, 4])

    def test_jellyfin_controller_carries_elapsed_timestamp_offset_across_boundary_filler(self):
        class OffsetBlockRunner:
            def __init__(self):
                self.calls = []

            def run(self, config):
                self.calls.append(config)
                index = len(self.calls) - 1
                return {
                    "status": "ok",
                    "playlist": str(config.output_dir / "Sky_One.m3u8"),
                    "hls_next_start_number": 1 if index == 0 else 5,
                    "hls_next_start_time_offset": 1.6 if index == 0 else 9.1,
                    "hls": {"segment_count": 1, "segments": []},
                    "ffmpeg": {"returncode": 0, "stdout": "", "stderr": ""},
                    "plan": [],
                }

        with tempfile.TemporaryDirectory() as tmp:
            current = [datetime(2026, 6, 17, 10, 5, 0)]
            runner = OffsetBlockRunner()
            filler = FakeFillerRunner(on_run=lambda kwargs: current.__setitem__(0, datetime(2026, 6, 17, 10, 30, 0)))

            def filler_run(**kwargs):
                filler.calls.append(kwargs)
                filler.on_run(kwargs)
                return {
                    "status": "ok",
                    "playlist": str(kwargs["output_dir"] / f"{kwargs['output_name']}.m3u8"),
                    "hls_next_start_number": 4,
                    "hls_next_start_time_offset": 7.1,
                    "hls": {"segment_count": 3, "segments": []},
                    "ffmpeg": {"returncode": 0, "stdout": "", "stderr": ""},
                }

            filler.run = filler_run
            controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=runner, filler_runner=filler)

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=Path(tmp),
                    max_blocks=2,
                    duration_limit=1800,
                    clock=lambda: current[0],
                    stream_profile="jellyfin",
                )
            )

        self.assertEqual([call.hls_start_time_offset for call in runner.calls], [0.0, 7.1])
        self.assertEqual(filler.calls[0]["hls_start_time_offset"], 1.6)
        filler_event = [event for event in result["events"] if event["event"] == "block_filler"][0]
        self.assertEqual(filler_event["hls_start_time_offset"], 1.6)
        self.assertEqual(filler_event["hls_next_start_time_offset"], 7.1)

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


    def test_recovers_premature_clean_completion_at_current_wallclock_without_filler(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = [datetime(2026, 6, 17, 10, 5, 0)]

            def clock():
                return current[0]

            runner = PrematureSuccessBlockRunner(current)
            filler = FakeFillerRunner()
            controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=runner, filler_runner=filler)

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
        self.assertEqual([call.hls_append for call in runner.calls], [False, True])
        self.assertEqual([event["event"] for event in result["events"]], ["block_start", "block_complete", "block_recovery", "block_complete"])
        recovery = [event for event in result["events"] if event["event"] == "block_recovery"][0]
        self.assertEqual(recovery["reason"], "premature-complete")
        self.assertEqual(filler.calls, [])
        self.assertEqual(result["status"], "complete")

    def test_recovers_repeated_premature_clean_completions_before_falling_back_to_filler(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = [datetime(2026, 6, 17, 10, 5, 0)]

            def clock():
                return current[0]

            runner = SequencedPrematureSuccessBlockRunner(current, advances=[120, 180, 1500])
            filler = FakeFillerRunner()
            controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=runner, filler_runner=filler)

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

        self.assertEqual(len(runner.calls), 3)
        self.assertEqual([call.hls_append for call in runner.calls], [False, True, True])
        self.assertEqual([event["event"] for event in result["events"]], [
            "block_start",
            "block_complete",
            "block_recovery",
            "block_complete",
            "block_recovery",
            "block_complete",
        ])
        recoveries = [event for event in result["events"] if event["event"] == "block_recovery"]
        self.assertEqual([event["attempt"] for event in recoveries], [1, 2])
        self.assertEqual([event["reason"] for event in recoveries], ["premature-complete", "premature-complete"])
        self.assertEqual(filler.calls, [])
        self.assertEqual(result["status"], "complete")

    def test_ts_primary_does_not_recover_when_emitted_media_duration_matches_block_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = EmittedDurationBlockRunner(1795.0)
            filler = FakeFillerRunner()
            controller = LiveController(schedule_client=FakeScheduleClient(), block_runner=runner, filler_runner=filler)

            result = controller.run(
                LiveControllerConfig(
                    channel="Sky One",
                    output_root=root,
                    max_blocks=1,
                    duration_limit=1800,
                    clock=lambda: datetime(2026, 6, 17, 10, 5, 0),
                    sleep=lambda seconds: None,
                    status_callback=lambda update: None,
                    playout_mode="ts-primary",
                )
            )

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual([event["event"] for event in result["events"]], ["block_start", "block_complete"])
        complete = [event for event in result["events"] if event["event"] == "block_complete"][0]
        self.assertEqual(complete["status"], "ok")
        self.assertEqual(filler.calls, [])
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

    def test_jellyfin_preroll_keeps_next_block_private_until_boundary_then_publishes_and_resumes_after_staged_media(self):
        class PreRollRunner:
            def __init__(self):
                self.calls = []

            def run(self, config):
                self.calls.append(config)
                staged = ".jellyfin-staging" in config.output_dir.parts
                playlist = config.output_dir / f"{config.output_name}.m3u8"
                config.output_dir.mkdir(parents=True, exist_ok=True)
                if staged:
                    names = [f"{config.output_name}_00000.ts", f"{config.output_name}_00001.ts"]
                    for name in names:
                        (config.output_dir / name).write_bytes(b"staged")
                    playlist.write_text("#EXTM3U\n#EXTINF:2.0,\n" + "\n#EXTINF:2.0,\n".join(names) + "\n")
                    return {"status": "ok", "playlist": str(playlist), "hls_next_start_number": 2, "hls_next_start_time_offset": 4.0, "hls": {"segments": [str(config.output_dir / name) for name in names]}, "ffmpeg": {"returncode": 0}, "plan": []}
                if len([call for call in self.calls if ".jellyfin-staging" not in call.output_dir.parts]) == 1:
                    names = [f"{config.output_name}_{number:05d}.ts" for number in range(3)]
                    for name in names:
                        (config.output_dir / name).write_bytes(b"current")
                    playlist.write_text("#EXTM3U\n#EXTINF:2.0,\n" + "\n#EXTINF:2.0,\n".join(names) + "\n")
                    return {"status": "ok", "playlist": str(playlist), "hls_next_start_number": 3, "hls_next_start_time_offset": 6.0, "hls": {"segments": [str(config.output_dir / name) for name in names]}, "ffmpeg": {"returncode": 0}, "plan": []}
                return {"status": "ok", "playlist": str(playlist), "hls_next_start_number": 6, "hls_next_start_time_offset": 12.0, "hls": {"segments": []}, "ffmpeg": {"returncode": 0}, "plan": []}

        with tempfile.TemporaryDirectory() as tmp:
            updates = []
            runner = PreRollRunner()
            result = LiveController(schedule_client=FakeScheduleClient(), block_runner=runner).run(
                LiveControllerConfig(channel="Example Channel", output_root=Path(tmp), max_blocks=2, duration_limit=1800, now=datetime(2026, 6, 17, 10, 0, 0), stream_profile="jellyfin", jellyfin_pre_roll_lead_seconds=20, jellyfin_pre_roll_min_buffer_seconds=4, status_callback=updates.append)
            )
            public = Path(result["channel_output_dir"]) / "Example_Channel.m3u8"
            public_text = public.read_text()

        stage_call = next(call for call in runner.calls if ".jellyfin-staging" in call.output_dir.parts)
        public_calls = [call for call in runner.calls if ".jellyfin-staging" not in call.output_dir.parts]
        self.assertEqual(stage_call.now, datetime(2026, 6, 17, 10, 30, 0))
        self.assertEqual(stage_call.hls_start_time_offset, 0.0)
        self.assertEqual(public_calls[1].now, datetime(2026, 6, 17, 10, 30, 4))
        self.assertEqual(public_calls[1].hls_start_number, 5)
        self.assertEqual(public_calls[1].hls_start_time_offset, 10.0)
        self.assertIn("Example_Channel_00003.ts", public_text)
        self.assertIn("Example_Channel_00004.ts", public_text)
        self.assertNotIn("#EXT-X-ENDLIST", public_text)
        self.assertNotIn("#EXT-X-DISCONTINUITY", public_text)
        diagnostics = result["jellyfin_pre_roll"]
        self.assertEqual(diagnostics["pre_roll_state"], "published")
        self.assertEqual(diagnostics["staged_segments_ready"], 2)
        self.assertEqual(diagnostics["public_boundary_switch"]["event"], "public_boundary_switch")
        self.assertTrue(any(update.get("jellyfin_pre_roll", {}).get("pre_roll_state") == "staged-private" for update in updates))


if __name__ == "__main__":
    unittest.main()
