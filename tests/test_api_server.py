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
            self.assertEqual(payload["channels"][0]["schedule_url"], "/api/channels/Sky_One/schedule")
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

    def test_channel_schedule_exposes_streamer_derived_now_and_next_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "channel_output_dir": str(root / "Sky_One"),
                "playlist": str(root / "Sky_One" / "Sky_One.m3u8"),
                "events": [
                    {
                        "event": "block_start",
                        "block_number": 1,
                        "block": {"index": 10, "title": "Show A", "start_time": "2026-06-25T16:00:00", "end_time": "2026-06-25T16:30:00", "selection_reason": "current"},
                    },
                    {
                        "event": "block_complete",
                        "block_number": 1,
                        "block": {"index": 10, "title": "Show A", "start_time": "2026-06-25T16:00:00", "end_time": "2026-06-25T16:30:00", "selection_reason": "current"},
                        "plan_item_count": 12,
                        "playlist": str(root / "Sky_One" / "Sky_One.m3u8"),
                    },
                    {
                        "event": "block_start",
                        "block_number": 2,
                        "block": {"index": 11, "title": "Show B", "start_time": "2026-06-25T16:30:00", "end_time": "2026-06-25T17:00:00", "selection_reason": "next"},
                    },
                ],
            }))
            server = self._start_server(root, status_json=status_path)

            status, headers, body = self._request(server, "/api/channels/Sky_One/schedule")

            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "application/json")
            payload = json.loads(body)
            self.assertEqual(payload["source"], "fs42stream-status")
            self.assertEqual(payload["channel"], "Sky One")
            self.assertEqual(payload["slug"], "Sky_One")
            self.assertEqual(payload["active_block"]["title"], "Show B")
            self.assertEqual(payload["previous_blocks"][0]["title"], "Show A")
            self.assertEqual(payload["upcoming_blocks"], [])
            self.assertEqual(payload["recent_events"][-1]["event"], "block_start")
            self.assertEqual(payload["hls"]["playlist"], str(root / "Sky_One" / "Sky_One.m3u8"))

    def test_channel_schedule_uses_live_top_level_schedule_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "active_block": {"index": 20, "title": "Live Current", "start_time": "2026-06-25T18:00:00", "end_time": "2026-06-25T18:30:00"},
                "upcoming_blocks": [{"index": 21, "title": "Live Next", "start_time": "2026-06-25T18:30:00", "end_time": "2026-06-25T19:00:00"}],
                "events": [],
            }))
            server = self._start_server(root, status_json=status_path)

            status, headers, body = self._request(server, "/api/channels/Sky_One/schedule")

            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["active_block"]["title"], "Live Current")
            self.assertEqual(payload["upcoming_blocks"][0]["title"], "Live Next")

    def test_channel_schedule_exposes_derived_plan_timeline_and_current_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "schedule_now": "2026-06-25T22:09:05",
                "active_block": {
                    "index": 365,
                    "title": "Star Trek The Next Generation",
                    "start_time": "2026-06-25T22:00:00",
                    "end_time": "2026-06-25T23:00:00",
                    "plan": [
                        {"content_type": "bump", "media_type": "video", "path": "ident.mp4", "duration": 10.0, "skip": 0, "is_stream": False},
                        {"content_type": "feature", "media_type": "video", "path": "episode.avi", "duration": 500.0, "skip": 0, "is_stream": False},
                        {"content_type": "bump", "media_type": "video", "path": "black.mp4", "duration": 1.0, "skip": 0, "is_stream": False},
                        {"content_type": "commercial", "media_type": "video", "path": "ad-a.mp4", "duration": 30.0, "skip": 0, "is_stream": False},
                        {"content_type": "feature", "media_type": "video", "path": "episode.avi", "duration": 500.0, "skip": 500.0, "is_stream": False},
                    ],
                },
                "events": [],
            }))
            server = self._start_server(root, status_json=status_path)

            status, headers, body = self._request(server, "/api/channels/Sky_One/schedule")

            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["schedule_now"], "2026-06-25T22:09:05")
            self.assertEqual(len(payload["timeline"]), 5)
            self.assertEqual(payload["timeline"][0]["wallclock_start"], "2026-06-25T22:00:00")
            self.assertEqual(payload["timeline"][0]["wallclock_end"], "2026-06-25T22:00:10")
            self.assertEqual(payload["timeline"][3]["content_type"], "commercial")
            self.assertEqual(payload["timeline"][3]["wallclock_start"], "2026-06-25T22:08:31")
            self.assertEqual(payload["timeline"][3]["wallclock_end"], "2026-06-25T22:09:01")
            current = payload["current_plan_item"]
            self.assertEqual(current["index"], 4)
            self.assertEqual(current["content_type"], "feature")
            self.assertEqual(current["path"], "episode.avi")
            self.assertEqual(current["current_offset_in_item"], 4.0)
            self.assertEqual(current["media_seek"], 504.0)
            self.assertEqual(current["wallclock_start"], "2026-06-25T22:09:01")
            self.assertEqual(payload["timeline"][4]["media_seek_start"], 500.0)

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
