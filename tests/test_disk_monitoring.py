import json
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fs42stream.api_server import create_server
from fs42stream.disk_monitor import DiskThresholds, DiskUsage


class DiskMonitoringAPITests(unittest.TestCase):
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

    def test_api_health_includes_output_root_disk_usage_and_threshold_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_usage = DiskUsage(path=root, total_bytes=1000, used_bytes=850, free_bytes=150, used_percent=85.0, thresholds=DiskThresholds(warn_percent=80.0, degraded_percent=90.0, critical_percent=95.0), state="warn")
            with mock.patch("fs42stream.api_server.disk_usage_report", return_value=fake_usage):
                server = self._start_server(root)
                status, headers, body = self._request(server, "/api/health")

            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "application/json")
            payload = json.loads(body)
            self.assertEqual(payload["disk"]["output_root"]["path"], str(root))
            self.assertEqual(payload["disk"]["output_root"]["used_bytes"], 850)
            self.assertEqual(payload["disk"]["output_root"]["free_bytes"], 150)
            self.assertEqual(payload["disk"]["output_root"]["used_percent"], 85.0)
            self.assertEqual(payload["disk"]["output_root"]["state"], "warn")
            self.assertEqual(payload["disk"]["output_root"]["thresholds"], {"warn_percent": 80.0, "degraded_percent": 90.0, "critical_percent": 95.0})

    def test_runtime_status_and_channel_health_include_disk_space_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel_dir = root / "Sky_One"
            channel_dir.mkdir()
            (channel_dir / "Sky_One_00001.ts").write_bytes(b"segment")
            (channel_dir / "Sky_One.m3u8").write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:2,\nSky_One_00001.ts\n")
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "schedule_now": "2026-06-25T22:10:00+01:00",
                "active_block": {"index": 1, "title": "Show", "start_time": "2026-06-25T22:00:00+01:00", "end_time": "2026-06-25T22:30:00+01:00"},
                "ffmpeg": {"pid": 123, "state": "running"},
                "events": [],
            }))
            fake_usage = DiskUsage(path=root, total_bytes=1000, used_bytes=930, free_bytes=70, used_percent=93.0, thresholds=DiskThresholds(), state="degraded")
            with mock.patch("fs42stream.api_server.disk_usage_report", return_value=fake_usage):
                server = self._start_server(root, status_json=status_path)
                status, _headers, body = self._request(server, "/api/channels/Sky_One/runtime")
                runtime = json.loads(body)
                self.assertEqual(status, 200)
                self.assertEqual(runtime["disk"]["output_root"]["state"], "degraded")

                status, _headers, body = self._request(server, "/api/channels/Sky_One/health")
                health = json.loads(body)

            self.assertEqual(status, 200)
            self.assertEqual(health["checks"]["disk_space"], "degraded")
            self.assertEqual(health["details"]["disk"]["output_root"]["used_percent"], 93.0)

    def test_create_server_accepts_configurable_disk_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = create_server(output_root=Path(tmp), port=0, disk_thresholds=DiskThresholds(warn_percent=70.0, degraded_percent=80.0, critical_percent=90.0))
            try:
                self.assertEqual(server.config.disk_thresholds.warn_percent, 70.0)
                self.assertEqual(server.config.disk_thresholds.degraded_percent, 80.0)
                self.assertEqual(server.config.disk_thresholds.critical_percent, 90.0)
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
