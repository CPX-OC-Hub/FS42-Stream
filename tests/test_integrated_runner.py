import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fs42stream.api_server import create_server
from fs42stream.integrated_runner import IntegratedRunnerConfig, main, run_integrated


class IntegratedRunnerTests(unittest.TestCase):
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

        self.assertEqual(result["status"], "complete")
        self.assertEqual(final_status["status"], "complete")
        self.assertEqual(final_status["blocks_completed"], 2)
        self.assertEqual(final_status["plan_item_count"], 4)
        self.assertEqual(final_status["plan_item_counts"], {"feature": 1, "commercial": 2, "bump": 1})
        self.assertEqual(final_status["commercial_count"], 2)
        self.assertEqual(final_status["commercial_paths"], ["/mnt/fs42/catalog/commercial/ad-a.mp4", "/mnt/fs42/catalog/commercial/ad-b.mp4"])
        self.assertEqual(events, ["served", ("controller", "Sky One", 2, 10, root), "shutdown", "closed"])

    def test_real_api_server_exposes_running_status_and_hls_while_controller_runs(self):
        case = self

        class HTTPCheckingController:
            def run(self, config):
                status_path = config.output_root / "status.json"
                running = json.loads(status_path.read_text())
                port = running["api_port"]
                channel_dir = config.output_root / "Sky_One"
                channel_dir.mkdir(parents=True, exist_ok=True)
                (channel_dir / "live.m3u8").write_text("#EXTM3U\nsegment0.ts\n")
                (channel_dir / "segment0.ts").write_bytes(b"segment")

                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    conn.request("GET", "/api/channels/Sky_One/status")
                    response = conn.getresponse()
                    case.assertEqual(response.status, 200)
                    case.assertEqual(json.loads(response.read())["status"], "running")

                    conn.request("GET", "/hls/Sky_One/live.m3u8")
                    response = conn.getresponse()
                    case.assertEqual(response.status, 200)
                    case.assertEqual(response.read(), b"#EXTM3U\nsegment0.ts\n")
                finally:
                    conn.close()
                return {"status": "complete", "channel": config.channel, "blocks_completed": 1, "events": [{"event": "block_complete"}]}

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

        self.assertEqual(rc, 0)
        self.assertEqual(status["status"], "complete")
        self.assertEqual(seen[0], ("Sky One", Path(tmp), 2, 10.0))
        self.assertEqual(seen[1:], ["shutdown", "closed"])


if __name__ == "__main__":
    unittest.main()
