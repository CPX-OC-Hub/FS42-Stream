import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fs42stream.api_server import create_server
from fs42stream.integrated_runner import DEFAULT_DURATION_LIMIT, DEFAULT_MAX_BLOCKS, IntegratedRunnerConfig, main, run_integrated
from fs42stream.systemd_service import ServiceConfig


class IntegratedRunnerTests(unittest.TestCase):
    def test_default_integrated_config_matches_service_safe_runtime_limits(self):
        config = IntegratedRunnerConfig()

        self.assertEqual(DEFAULT_MAX_BLOCKS, ServiceConfig.max_blocks)
        self.assertEqual(DEFAULT_DURATION_LIMIT, ServiceConfig.duration_limit)
        self.assertEqual(config.max_blocks, ServiceConfig.max_blocks)
        self.assertEqual(config.duration_limit, ServiceConfig.duration_limit)
        self.assertEqual(config.playout_mode, "ts-primary")
        self.assertEqual(config.stream_profiles, ("jellyfin",))
        self.assertEqual(str(config.brb_image_path), "runtime/brb.png")
        self.assertEqual(config.audio_normalization, "off")
        self.assertEqual(config.jellyfin_pre_roll_min_buffer_seconds, 30.0)
        self.assertEqual(config.jellyfin_pre_roll_max_publish_delay_seconds, 60.0)

    def test_cli_reads_brb_image_path_from_environment_and_forwards_it_to_controller(self):
        configs = []

        class FakeServer:
            server_address = ("127.0.0.1", 8088)
            def serve_forever(self):
                pass
            def shutdown(self):
                pass
            def server_close(self):
                pass

        class FakeController:
            def run(self, config):
                configs.append(config)
                return {"status": "complete", "channel": config.channel, "blocks_completed": 1, "events": []}

        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout"), mock.patch.dict(os.environ, {"FS42STREAM_BRB_IMAGE_PATH": "catalog/SkyOne/runtime/brb.png"}, clear=True):
            rc = main(["--channel", "Sky One", "--host", "127.0.0.1", "--port", "8088", "--output-root", tmp, "--max-blocks", "1", "--duration-limit", "1"], server_factory=lambda **kwargs: FakeServer(), controller_factory=lambda: FakeController())

        self.assertEqual(rc, 0)
        self.assertEqual(str(configs[0].brb_image_path), "catalog/SkyOne/runtime/brb.png")

    def test_cli_and_environment_propagate_audio_normalization_and_reject_invalid_values(self):
        configs = []

        with mock.patch("fs42stream.integrated_runner.run_integrated", side_effect=lambda config, **kwargs: configs.append(config) or {"status": "complete"}), mock.patch("sys.stdout"):
            self.assertEqual(main(["--audio-normalization", "loudnorm"]), 0)
            with mock.patch.dict(os.environ, {"FS42STREAM_AUDIO_NORMALIZATION": "loudnorm"}, clear=True):
                self.assertEqual(main([]), 0)
            with self.assertRaises(SystemExit) as invalid:
                main(["--audio-normalization", "invalid"])

        self.assertEqual([config.audio_normalization for config in configs], ["loudnorm", "loudnorm"])
        self.assertEqual(invalid.exception.code, 2)

    def test_cli_and_environment_propagate_jellyfin_pre_roll_lead_seconds(self):
        configs = []

        class FakeServer:
            server_address = ("127.0.0.1", 8088)
            def serve_forever(self):
                pass
            def shutdown(self):
                pass
            def server_close(self):
                pass

        class FakeController:
            def run(self, config):
                configs.append(config)
                return {"status": "complete", "channel": config.channel, "blocks_completed": 1, "events": []}

        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout"):
            self.assertEqual(main(["--output-root", tmp, "--max-blocks", "1", "--duration-limit", "1", "--jellyfin-pre-roll-lead-seconds", "120"], server_factory=lambda **kwargs: FakeServer(), controller_factory=lambda: FakeController()), 0)
            with mock.patch.dict(os.environ, {"FS42STREAM_JELLYFIN_PRE_ROLL_LEAD_SECONDS": "90"}, clear=True):
                self.assertEqual(main(["--output-root", tmp, "--max-blocks", "1", "--duration-limit", "1"], server_factory=lambda **kwargs: FakeServer(), controller_factory=lambda: FakeController()), 0)

        self.assertEqual([config.jellyfin_pre_roll_lead_seconds for config in configs], [120.0, 90.0])

    def test_jellyfin_only_integrated_status_omits_direct_urls(self):
        class FakeServer:
            server_address = ("127.0.0.1", 18088)
            def serve_forever(self):
                pass
            def shutdown(self):
                pass
            def server_close(self):
                pass

        class FakeController:
            def run(self, config):
                return {"status": "complete", "channel": config.channel, "blocks_completed": 1, "stream_profile": config.stream_profile, "events": []}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_integrated(
                IntegratedRunnerConfig(channel="Sky One", host="127.0.0.1", port=0, output_root=root, max_blocks=1, duration_limit=1),
                server_factory=lambda **kwargs: FakeServer(),
                controller_factory=lambda: FakeController(),
            )
            final_status = json.loads((root / "status.json").read_text())

        for payload in (result, final_status):
            self.assertEqual(payload["stream_profiles"], ["jellyfin"])
            self.assertNotIn("hls_playlist_url", payload)
            self.assertNotIn("iptv_url", payload)
            self.assertIn("jellyfin_hls_playlist_url", payload)
            self.assertIn("jellyfin_iptv_url", payload)

    def test_cli_uses_jellyfin_only_by_default_and_accepts_both_or_direct_profiles(self):
        seen_profiles = []

        class FakeServer:
            server_address = ("127.0.0.1", 8088)
            def serve_forever(self):
                pass
            def shutdown(self):
                pass
            def server_close(self):
                pass

        class FakeController:
            def run(self, config):
                seen_profiles.append(config.stream_profile)
                return {"status": "complete", "channel": config.channel, "blocks_completed": 1, "stream_profile": config.stream_profile, "events": []}

        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout"), mock.patch.dict(os.environ, {}, clear=True):
            common = ["--channel", "Sky One", "--host", "127.0.0.1", "--port", "8088", "--output-root", tmp, "--max-blocks", "1", "--duration-limit", "1"]
            self.assertEqual(main(common, server_factory=lambda **kwargs: FakeServer(), controller_factory=lambda: FakeController()), 0)
            self.assertEqual(seen_profiles, ["jellyfin"])
            seen_profiles.clear()
            with mock.patch.dict(os.environ, {"FS42STREAM_STREAM_PROFILES": "direct,jellyfin"}, clear=True):
                self.assertEqual(main(common, server_factory=lambda **kwargs: FakeServer(), controller_factory=lambda: FakeController()), 0)
            self.assertEqual(seen_profiles, ["direct", "jellyfin"])
            seen_profiles.clear()
            self.assertEqual(main([*common, "--stream-profiles", "both"], server_factory=lambda **kwargs: FakeServer(), controller_factory=lambda: FakeController()), 0)
            self.assertEqual(seen_profiles, ["direct", "jellyfin"])
            seen_profiles.clear()
            self.assertEqual(main([*common, "--stream-profiles", "direct"], server_factory=lambda **kwargs: FakeServer(), controller_factory=lambda: FakeController()), 0)
            self.assertEqual(seen_profiles, ["direct"])

    def test_fake_server_and_controller_lifecycle_writes_status_and_closes_server(self):
        events = []
        served = threading.Event()
        case = self

        class FakeServer:
            server_address = ("127.0.0.1", 18088)

            def serve_forever(self):
                events.append("served")
                served.set()

            def shutdown(self):
                events.append("shutdown")

            def server_close(self):
                events.append("closed")

        class FakeController:
            def run(self, config):
                case.assertTrue(served.wait(timeout=2))
                events.append(("controller", config.channel, config.max_blocks, config.duration_limit, config.output_root))
                status = json.loads((config.output_root / "status.json").read_text())
                case.assertEqual(status["status"], "running")
                case.assertEqual(status["api_port"], 18088)
                return {"status": "complete", "channel": config.channel, "blocks_completed": config.max_blocks, "plan_item_count": 4, "plan_item_counts": {"feature": 1, "commercial": 2, "bump": 1}, "commercial_count": 2, "commercial_paths": ["/mnt/fs42/catalog/commercial/ad-a.mp4", "/mnt/fs42/catalog/commercial/ad-b.mp4"], "events": []}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_integrated(
                IntegratedRunnerConfig(channel="Sky One", host="127.0.0.1", port=0, output_root=root, max_blocks=2, duration_limit=10, stream_profiles=("direct", "jellyfin")),
                server_factory=lambda **kwargs: FakeServer(),
                controller_factory=lambda: FakeController(),
            )
            final_status = json.loads((root / "status.json").read_text())

        controller_events = [event for event in events if isinstance(event, tuple) and event[0] == "controller"]
        self.assertEqual(result["status"], "complete")
        self.assertEqual(final_status["status"], "complete")
        self.assertEqual(final_status["blocks_completed"], 2)
        self.assertEqual(final_status["plan_item_count"], 4)
        self.assertEqual(final_status["plan_item_counts"], {"feature": 1, "commercial": 2, "bump": 1})
        self.assertEqual(final_status["commercial_count"], 2)
        self.assertEqual(final_status["commercial_paths"], ["/mnt/fs42/catalog/commercial/ad-a.mp4", "/mnt/fs42/catalog/commercial/ad-b.mp4"])
        self.assertEqual(controller_events, [("controller", "Sky One", 2, 10, root), ("controller", "Sky One", 2, 10, root)])
        self.assertEqual(events[0], "served")
        self.assertEqual(events[-2:], ["shutdown", "closed"])

    def test_real_api_server_exposes_running_status_and_hls_while_controller_runs(self):
        case = self

        class HTTPCheckingController:
            def run(self, config):
                status_path = config.output_root / "status.json"
                running = json.loads(status_path.read_text())
                port = running["api_port"]
                channel_dir = config.output_root / "Sky_One"
                output_dir = channel_dir / "jellyfin" if config.stream_profile == "jellyfin" else channel_dir
                output_dir.mkdir(parents=True, exist_ok=True)
                playlist = output_dir / "Sky_One.m3u8"
                segment = output_dir / "Sky_One_00000.ts"
                playlist.write_text("#EXTM3U\nSky_One_00000.ts\n")
                segment.write_bytes(b"segment")

                if config.stream_profile == "direct":
                    jellyfin_playlist = channel_dir / "jellyfin" / "Sky_One.m3u8"
                    deadline = time.monotonic() + 2
                    while not jellyfin_playlist.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)

                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        conn.request("GET", "/api/channels/Sky_One/status")
                        response = conn.getresponse()
                        case.assertEqual(response.status, 200)
                        case.assertEqual(json.loads(response.read())["status"], "running")

                        conn.request("GET", "/hls/Sky_One/Sky_One.m3u8")
                        response = conn.getresponse()
                        case.assertEqual(response.status, 200)
                        case.assertEqual(response.read(), b"#EXTM3U\nSky_One_00000.ts\n")

                        conn.request("GET", "/hls/Sky_One/jellyfin/Sky_One.m3u8")
                        response = conn.getresponse()
                        case.assertEqual(response.status, 200)
                        case.assertEqual(response.read(), b"#EXTM3U\nSky_One_00000.ts\n")
                    finally:
                        conn.close()
                return {"status": "complete", "channel": config.channel, "blocks_completed": 1, "stream_profile": config.stream_profile, "events": [{"event": "block_complete"}]}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_integrated(
                IntegratedRunnerConfig(channel="Sky One", host="127.0.0.1", port=0, output_root=root, max_blocks=1, duration_limit=1, stream_profiles=("direct", "jellyfin")),
                server_factory=create_server,
                controller_factory=lambda: HTTPCheckingController(),
            )
            final_status = json.loads((root / "status.json").read_text())

        self.assertEqual(result["status"], "complete")
        self.assertEqual(final_status["status"], "complete")
        self.assertEqual(final_status["blocks_completed"], 1)

    def test_default_integrated_run_generates_direct_and_jellyfin_profiles(self):
        events = []

        class FakeServer:
            server_address = ("127.0.0.1", 18088)
            def serve_forever(self):
                events.append("served")
            def shutdown(self):
                events.append("shutdown")
            def server_close(self):
                events.append("closed")

        class FakeController:
            def run(self, config):
                events.append(("controller", config.stream_profile, config.playout_mode, config.output_root))
                return {
                    "status": "complete",
                    "channel": config.channel,
                    "blocks_completed": 1,
                    "stream_profile": config.stream_profile,
                    "playout_mode": config.playout_mode,
                    "channel_output_dir": str(config.output_root / "Sky_One" / ("jellyfin" if config.stream_profile == "jellyfin" else "")),
                    "events": [],
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_integrated(
                IntegratedRunnerConfig(channel="Sky One", host="127.0.0.1", port=0, output_root=root, max_blocks=1, duration_limit=10, stream_profiles=("direct", "jellyfin")),
                server_factory=lambda **kwargs: FakeServer(),
                controller_factory=lambda: FakeController(),
            )

        self.assertEqual([event[1] for event in events if isinstance(event, tuple) and event[0] == "controller"], ["direct", "jellyfin"])
        self.assertEqual([event[2] for event in events if isinstance(event, tuple) and event[0] == "controller"], ["ts-primary", "ts-primary"])
        self.assertEqual(result["profile_statuses"]["direct"]["stream_profile"], "direct")
        self.assertEqual(result["profile_statuses"]["jellyfin"]["stream_profile"], "jellyfin")
        self.assertEqual(result["profile_statuses"]["direct"]["playout_mode"], "ts-primary")
        self.assertEqual(result["profile_statuses"]["jellyfin"]["playout_mode"], "ts-primary")
        self.assertEqual(result["stream_profiles"], ["direct", "jellyfin"])
        self.assertEqual(events[0], "served")
        self.assertEqual(events[-2:], ["shutdown", "closed"])

    def test_integrated_runner_publishes_live_projection_to_real_api_routes(self):
        case = self

        class HTTPProjectionController:
            def run(self, config):
                status_path = config.output_root / "status.json"
                running = json.loads(status_path.read_text())
                port = running["api_port"]
                channel_dir = config.output_root / "Sky_One"
                channel_dir.mkdir(parents=True, exist_ok=True)
                playlist = channel_dir / "Sky_One.m3u8"
                segment = channel_dir / "Sky_One_00000.ts"
                playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:2.0,\nSky_One_00000.ts\n")
                segment.write_bytes(b"segment")

                if config.stream_profile == "direct":
                    config.status_callback(
                        {
                            "status": "running",
                            "channel": config.channel,
                            "channel_output_dir": str(channel_dir),
                            "supervisor": {
                                "selection": {"index": 365, "reason": "current"},
                                "schedule_now": "2026-06-25T22:08:35+01:00",
                                "current_block": {
                                    "index": 365,
                                    "selection_reason": "current",
                                    "title": "Star Trek The Next Generation",
                                    "start_time": "2026-06-25T22:00:00+01:00",
                                    "end_time": "2026-06-25T23:00:00+01:00",
                                    "plan": [{"path": "episode.avi", "duration": 500.0, "skip": 0.0}],
                                },
                                "next_block": {
                                    "index": 366,
                                    "selection_reason": "upcoming",
                                    "title": "The Simpsons",
                                    "start_time": "2026-06-25T23:00:00+01:00",
                                    "end_time": "2026-06-25T23:30:00+01:00",
                                },
                                "upcoming_blocks": [{
                                    "index": 366,
                                    "selection_reason": "upcoming",
                                    "title": "The Simpsons",
                                    "start_time": "2026-06-25T23:00:00+01:00",
                                    "end_time": "2026-06-25T23:30:00+01:00",
                                }],
                                "timeline": [
                                    {"index": 0, "content_type": "feature", "path": "episode.avi", "wallclock_start": "2026-06-25T22:00:10+01:00", "wallclock_end": "2026-06-25T22:08:30+01:00"},
                                    {"index": 1, "content_type": "commercial", "path": "ad-a.mp4", "wallclock_start": "2026-06-25T22:08:30+01:00", "wallclock_end": "2026-06-25T22:09:00+01:00", "media_seek": 25.0},
                                ],
                                "catch_up": {"applied": True, "media_seek": 25.0},
                                "render_plan_item_count": 1,
                                "source_plan_item_count": 2,
                                "current_item": {
                                    "index": 1,
                                    "content_type": "commercial",
                                    "path": "ad-a.mp4",
                                    "wallclock_start": "2026-06-25T22:08:30+01:00",
                                    "wallclock_end": "2026-06-25T22:09:00+01:00",
                                    "media_seek": 25.0,
                                },
                                "next_item": None,
                            },
                            "ffmpeg": {"pid": 18422, "state": "running"},
                            "events": [{"event": "item_advance", "at": "2026-06-25T22:08:30+01:00"}],
                            "hls": {"playlist": str(playlist)},
                        }
                    )

                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        conn.request("GET", "/api/channels/Sky_One/schedule")
                        response = conn.getresponse()
                        case.assertEqual(response.status, 200)
                        schedule_payload = json.loads(response.read())
                        case.assertEqual(schedule_payload["active_block"]["title"], "Star Trek The Next Generation")
                        case.assertEqual(schedule_payload["current_plan_item"]["path"], "ad-a.mp4")
                        case.assertEqual(schedule_payload["upcoming_blocks"][0]["title"], "The Simpsons")

                        conn.request("GET", "/api/channels/Sky_One/runtime")
                        response = conn.getresponse()
                        case.assertEqual(response.status, 200)
                        runtime_payload = json.loads(response.read())
                        case.assertEqual(runtime_payload["block"]["current"]["title"], "Star Trek The Next Generation")
                        case.assertEqual(runtime_payload["item"]["current"]["path"], "ad-a.mp4")
                        case.assertEqual(runtime_payload["playout"]["catch_up"], {"applied": True, "media_seek": 25.0})
                        case.assertEqual(runtime_payload["playout"]["trimmed"], True)
                        case.assertEqual(runtime_payload["hls"]["playlist_url"], "/hls/Sky_One/Sky_One.m3u8")
                    finally:
                        conn.close()

                return {"status": "complete", "channel": config.channel, "blocks_completed": 1, "stream_profile": config.stream_profile, "events": []}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_integrated(
                IntegratedRunnerConfig(channel="Sky One", host="127.0.0.1", port=0, output_root=root, max_blocks=1, duration_limit=1, stream_profiles=("direct", "jellyfin")),
                server_factory=create_server,
                controller_factory=lambda: HTTPProjectionController(),
            )
            final_status = json.loads((root / "status.json").read_text())

        self.assertEqual(result["status"], "complete")
        self.assertEqual(final_status["status_url"].endswith("/api/channels/Sky_One/status"), True)
        self.assertEqual(final_status["schedule_url"].endswith("/api/channels/Sky_One/schedule"), True)
        self.assertEqual(final_status["runtime_url"].endswith("/api/channels/Sky_One/runtime"), True)
        self.assertEqual(final_status["health_url"].endswith("/api/channels/Sky_One/health"), True)

    def test_final_status_json_preserves_last_supervisor_projection_after_controller_completion(self):
        class ProjectionController:
            def run(self, config):
                if config.stream_profile == "direct":
                    config.status_callback(
                        {
                            "status": "running",
                            "channel": config.channel,
                            "supervisor": {
                                "selection": {"index": 365, "reason": "current"},
                                "schedule_now": "2026-06-25T22:08:35+01:00",
                                "current_block": {
                                    "index": 365,
                                    "selection_reason": "current",
                                    "title": "Star Trek The Next Generation",
                                    "start_time": "2026-06-25T22:00:00+01:00",
                                    "end_time": "2026-06-25T23:00:00+01:00",
                                    "plan": [{"path": "episode.avi", "duration": 500.0, "skip": 0.0}],
                                },
                                "upcoming_blocks": [{
                                    "index": 366,
                                    "selection_reason": "upcoming",
                                    "title": "The Simpsons",
                                    "start_time": "2026-06-25T23:00:00+01:00",
                                    "end_time": "2026-06-25T23:30:00+01:00",
                                }],
                                "timeline": [{
                                    "index": 0,
                                    "content_type": "feature",
                                    "path": "episode.avi",
                                    "wallclock_start": "2026-06-25T22:00:10+01:00",
                                    "wallclock_end": "2026-06-25T22:08:30+01:00",
                                }],
                                "catch_up": {"applied": True, "media_seek": 25.0},
                                "render_plan_item_count": 1,
                                "source_plan_item_count": 2,
                                "current_item": {
                                    "index": 0,
                                    "content_type": "feature",
                                    "path": "episode.avi",
                                    "wallclock_start": "2026-06-25T22:00:10+01:00",
                                    "wallclock_end": "2026-06-25T22:08:30+01:00",
                                },
                                "next_item": None,
                                "playout": {
                                    "current_block": {
                                        "index": 365,
                                        "selection_reason": "current",
                                        "title": "Star Trek The Next Generation",
                                        "start_time": "2026-06-25T22:00:00+01:00",
                                        "end_time": "2026-06-25T23:00:00+01:00",
                                        "plan": [{"path": "episode.avi", "duration": 500.0, "skip": 0.0}],
                                    },
                                    "next_block": {
                                        "index": 366,
                                        "selection_reason": "upcoming",
                                        "title": "The Simpsons",
                                        "start_time": "2026-06-25T23:00:00+01:00",
                                        "end_time": "2026-06-25T23:30:00+01:00",
                                    },
                                    "current_item": {
                                        "index": 0,
                                        "content_type": "feature",
                                        "path": "episode.avi",
                                        "wallclock_start": "2026-06-25T22:00:10+01:00",
                                        "wallclock_end": "2026-06-25T22:08:30+01:00",
                                    },
                                    "next_item": None,
                                    "timeline": [{
                                        "index": 0,
                                        "content_type": "feature",
                                        "path": "episode.avi",
                                        "wallclock_start": "2026-06-25T22:00:10+01:00",
                                        "wallclock_end": "2026-06-25T22:08:30+01:00",
                                    }],
                                    "schedule_now": "2026-06-25T22:08:35+01:00",
                                },
                            },
                            "events": [{"event": "block_start"}],
                        }
                    )
                return {"status": "complete", "channel": config.channel, "blocks_completed": 1, "stream_profile": config.stream_profile, "events": []}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_integrated(
                IntegratedRunnerConfig(channel="Sky One", host="127.0.0.1", port=0, output_root=root, max_blocks=1, duration_limit=1, stream_profiles=("direct", "jellyfin")),
                server_factory=lambda **kwargs: type("FakeServer", (), {"server_address": ("127.0.0.1", 18088), "serve_forever": lambda self: None, "shutdown": lambda self: None, "server_close": lambda self: None})(),
                controller_factory=lambda: ProjectionController(),
            )
            final_status = json.loads((root / "status.json").read_text())

        self.assertEqual(result["status"], "complete")
        self.assertEqual(final_status["supervisor"]["selection"], {"index": 365, "reason": "current"})
        self.assertEqual(final_status["active_block"]["title"], "Star Trek The Next Generation")
        self.assertEqual(final_status["current_plan_item"]["path"], "episode.avi")
        self.assertEqual(final_status["playout"]["current_block"]["title"], "Star Trek The Next Generation")

    def test_cli_defaults_use_service_safe_runtime_limits(self):
        seen = []

        class FakeServer:
            server_address = ("127.0.0.1", 8088)
            def serve_forever(self):
                pass
            def shutdown(self):
                seen.append("shutdown")
            def server_close(self):
                seen.append("closed")

        class FakeController:
            def run(self, config):
                seen.append((config.channel, config.output_root, config.max_blocks, config.duration_limit))
                return {"status": "complete", "channel": config.channel, "blocks_completed": config.max_blocks, "events": []}

        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout"):
            rc = main(
                ["--channel", "Sky One", "--host", "127.0.0.1", "--port", "8088", "--output-root", tmp, "--stream-profiles", "both"],
                server_factory=lambda **kwargs: FakeServer(),
                controller_factory=lambda: FakeController(),
            )
            status = json.loads((Path(tmp) / "status.json").read_text())

        controller_events = [entry for entry in seen if isinstance(entry, tuple)]
        self.assertEqual(rc, 0)
        self.assertEqual(status["status"], "complete")
        self.assertEqual(controller_events, [("Sky One", Path(tmp), DEFAULT_MAX_BLOCKS, DEFAULT_DURATION_LIMIT), ("Sky One", Path(tmp), DEFAULT_MAX_BLOCKS, DEFAULT_DURATION_LIMIT)])
        self.assertEqual(seen[-2:], ["shutdown", "closed"])

    def test_cli_parses_arguments_and_prints_final_json(self):
        seen = []

        class FakeServer:
            server_address = ("127.0.0.1", 8088)
            def serve_forever(self):
                pass
            def shutdown(self):
                seen.append("shutdown")
            def server_close(self):
                seen.append("closed")

        class FakeController:
            def run(self, config):
                seen.append((config.channel, config.output_root, config.max_blocks, config.duration_limit))
                return {"status": "complete", "channel": config.channel, "blocks_completed": config.max_blocks, "events": []}

        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout"):
            rc = main(
                ["--channel", "Sky One", "--host", "127.0.0.1", "--port", "8088", "--output-root", tmp, "--max-blocks", "2", "--duration-limit", "10", "--stream-profiles", "both"],
                server_factory=lambda **kwargs: FakeServer(),
                controller_factory=lambda: FakeController(),
            )
            status = json.loads((Path(tmp) / "status.json").read_text())

        controller_events = [entry for entry in seen if isinstance(entry, tuple)]
        self.assertEqual(rc, 0)
        self.assertEqual(status["status"], "complete")
        self.assertEqual(controller_events, [("Sky One", Path(tmp), 2, 10.0), ("Sky One", Path(tmp), 2, 10.0)])
        self.assertEqual(seen[-2:], ["shutdown", "closed"])


if __name__ == "__main__":
    unittest.main()
