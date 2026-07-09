import http.client
import json
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
                IntegratedRunnerConfig(channel="Sky One", host="127.0.0.1", port=0, output_root=root, max_blocks=2, duration_limit=10),
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
                IntegratedRunnerConfig(channel="Sky One", host="127.0.0.1", port=0, output_root=root, max_blocks=1, duration_limit=1),
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
                events.append(("controller", config.stream_profile, config.output_root))
                return {
                    "status": "complete",
                    "channel": config.channel,
                    "blocks_completed": 1,
                    "stream_profile": config.stream_profile,
                    "channel_output_dir": str(config.output_root / "Sky_One" / ("jellyfin" if config.stream_profile == "jellyfin" else "")),
                    "events": [],
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_integrated(
                IntegratedRunnerConfig(channel="Sky One", host="127.0.0.1", port=0, output_root=root, max_blocks=1, duration_limit=10),
                server_factory=lambda **kwargs: FakeServer(),
                controller_factory=lambda: FakeController(),
            )

        self.assertEqual([event[1] for event in events if isinstance(event, tuple) and event[0] == "controller"], ["direct", "jellyfin"])
        self.assertEqual(result["profile_statuses"]["direct"]["stream_profile"], "direct")
        self.assertEqual(result["profile_statuses"]["jellyfin"]["stream_profile"], "jellyfin")
        self.assertEqual(result["stream_profiles"], ["direct", "jellyfin"])
        self.assertEqual(events[0], "served")
        self.assertEqual(events[-2:], ["shutdown", "closed"])

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
                ["--channel", "Sky One", "--host", "127.0.0.1", "--port", "8088", "--output-root", tmp],
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
                ["--channel", "Sky One", "--host", "127.0.0.1", "--port", "8088", "--output-root", tmp, "--max-blocks", "2", "--duration-limit", "10"],
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
