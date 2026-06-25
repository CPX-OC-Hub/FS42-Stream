import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from fs42stream.api_server import create_server, main


class APIServerTests(unittest.TestCase):
    def _start_server(self, output_root, status_json=None):
        server = create_server(host="127.0.0.1", port=0, output_root=output_root, status_json=status_json)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        return server

    def _request(self, server, path):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, headers, body

    def test_health_and_channels_are_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = self._start_server(Path(tmp))

            status, headers, body = self._request(server, "/api/health")
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "application/json")
            health = json.loads(body)
            self.assertEqual(health["status"], "ok")
            self.assertEqual(health["service"], "fs42stream-api")

            status, headers, body = self._request(server, "/api/channels")
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["channels"][0]["name"], "Sky One")
            self.assertEqual(payload["channels"][0]["slug"], "Sky_One")
            self.assertEqual(payload["channels"][0]["status_url"], "/api/channels/Sky_One/status")
            self.assertEqual(payload["channels"][0]["hls_url"], "/hls/Sky_One/")

    def test_channel_status_exposes_latest_status_json_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.json"
            status_path.write_text(json.dumps({"status": "complete", "channel": "Sky One", "blocks_completed": 2}))
            server = self._start_server(root, status_json=status_path)

            status, headers, body = self._request(server, "/api/channels/Sky_One/status")

            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "application/json")
            self.assertEqual(json.loads(body), {"status": "complete", "channel": "Sky One", "blocks_completed": 2})

    def test_channel_status_returns_json_error_when_missing_or_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = self._start_server(Path(tmp), status_json=Path(tmp) / "missing.json")
            status, headers, body = self._request(server, "/api/channels/Sky_One/status")
            self.assertEqual(status, 404)
            payload = json.loads(body)
            self.assertEqual(payload["error"], "status_not_found")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.json"
            status_path.write_text("not json")
            server = self._start_server(root, status_json=status_path)
            status, headers, body = self._request(server, "/api/channels/Sky_One/status")
            self.assertEqual(status, 502)
            payload = json.loads(body)
            self.assertEqual(payload["error"], "invalid_status_json")

    def test_serves_hls_files_with_expected_content_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel_dir = root / "Sky_One"
            channel_dir.mkdir()
            (channel_dir / "live.m3u8").write_text("#EXTM3U\nsegment0.ts\n")
            (channel_dir / "segment0.ts").write_bytes(b"segment bytes")
            server = self._start_server(root)

            status, headers, body = self._request(server, "/hls/Sky_One/live.m3u8")
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "application/vnd.apple.mpegurl")
            self.assertEqual(body, b"#EXTM3U\nsegment0.ts\n")

            status, headers, body = self._request(server, "/hls/Sky_One/segment0.ts")
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "video/mp2t")
            self.assertEqual(body, b"segment bytes")

    def test_hls_rejects_traversal_outside_paths_and_unknown_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Sky_One").mkdir()
            outside = root.parent / "secret.ts"
            outside.write_text("secret")
            server = self._start_server(root)

            for path in ("/hls/Sky_One/../secret.ts", "/hls/Sky_One/%2e%2e/secret.ts", "/hls/Sky_One//tmp/secret.ts"):
                status, headers, body = self._request(server, path)
                self.assertEqual(status, 400, path)
                payload = json.loads(body)
                self.assertEqual(payload["error"], "unsafe_hls_path")

            status, headers, body = self._request(server, "/hls/Other/live.m3u8")
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body)["error"], "channel_not_found")

    def test_unknown_routes_and_methods_return_json_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = self._start_server(Path(tmp))
            status, headers, body = self._request(server, "/nope")
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body)["error"], "not_found")

            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            self.addCleanup(conn.close)
            conn.request("POST", "/api/health")
            response = conn.getresponse()
            body = response.read()
            self.assertEqual(response.status, 405)
            self.assertEqual(json.loads(body)["error"], "method_not_allowed")

    def test_cli_argument_parsing_starts_foreground_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            created = []

            class FakeServer:
                def serve_forever(self):
                    created.append("served")

                def server_close(self):
                    created.append("closed")

            def fake_create_server(**kwargs):
                created.append(kwargs)
                return FakeServer()

            rc = main([
                "--host", "127.0.0.1",
                "--port", "8088",
                "--output-root", tmp,
                "--status-json", str(Path(tmp) / "status.json"),
            ], server_factory=fake_create_server)

        self.assertEqual(rc, 0)
        self.assertEqual(created[0]["host"], "127.0.0.1")
        self.assertEqual(created[0]["port"], 8088)
        self.assertEqual(created[0]["output_root"], Path(tmp))
        self.assertEqual(created[0]["status_json"], Path(tmp) / "status.json")
        self.assertEqual(created[1:], ["served", "closed"])


if __name__ == "__main__":
    unittest.main()
