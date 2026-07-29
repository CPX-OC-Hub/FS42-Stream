import http.client
import json
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from fs42stream.api_server import create_server, main

SKY_ONE_TEST_CHANNEL = "Sky One"
SKY_ONE_TEST_SLUG = "Sky_One"
SKY_ONE_TEST_LOGO = "skyone.png"


class APIServerTests(unittest.TestCase):
    def _start_server(self, output_root, status_json=None, schedule_fetcher=None, **kwargs):
        options = {
            "channel_name": SKY_ONE_TEST_CHANNEL,
            "channel_slug": SKY_ONE_TEST_SLUG,
            "logo_filename": SKY_ONE_TEST_LOGO,
            **kwargs,
        }
        server = create_server(
            host="127.0.0.1",
            port=0,
            output_root=output_root,
            status_json=status_json,
            schedule_fetcher=schedule_fetcher,
            **options,
        )
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
            self.assertEqual(payload["channels"][0]["jellyfin_hls_playlist_url"], "/hls/Sky_One/jellyfin/Sky_One.m3u8")
            self.assertEqual(payload["channels"][0]["jellyfin_iptv_url"], "/iptv/jellyfin/channels.m3u")
            self.assertEqual(payload["channels"][0]["logo_url"], f"http://127.0.0.1:{server.server_address[1]}/hls/Sky_One/skyone.png")

    def test_jellyfin_only_status_does_not_advertise_or_serve_direct_stream_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.json"
            status_path.write_text(json.dumps({"status": "running", "channel": "Sky One", "stream_profiles": ["jellyfin"]}))
            server = self._start_server(root, status_json=status_path)

            status, _headers, body = self._request(server, "/api/channels")
            self.assertEqual(status, 200)
            channel = json.loads(body)["channels"][0]
            self.assertEqual(channel["stream_profiles"], ["jellyfin"])
            self.assertNotIn("hls_playlist_url", channel)
            self.assertEqual(channel["jellyfin_hls_playlist_url"], "/hls/Sky_One/jellyfin/Sky_One.m3u8")

            channel_dir = root / "Sky_One"
            channel_dir.mkdir()
            (channel_dir / "Sky_One.m3u8").write_text("#EXTM3U\nSky_One_00000.ts\n")
            (channel_dir / "Sky_One_00000.ts").write_bytes(b"segment")
            jellyfin_dir = channel_dir / "jellyfin"
            jellyfin_dir.mkdir()
            (jellyfin_dir / "Sky_One.m3u8").write_text("#EXTM3U\nSky_One_00000.ts\n")

            status, _headers, _body = self._request(server, "/iptv/channels.m3u")
            self.assertEqual(status, 404)
            status, _headers, body = self._request(server, "/iptv/jellyfin/channels.m3u")
            self.assertEqual(status, 200)
            self.assertIn("/hls/Sky_One/jellyfin/Sky_One.m3u8", body.decode("utf-8"))
            status, _headers, _body = self._request(server, "/hls/Sky_One/Sky_One.m3u8")
            self.assertEqual(status, 404)
            status, _headers, _body = self._request(server, "/hls/Sky_One/Sky_One_00000.ts")
            self.assertEqual(status, 404)
            status, _headers, body = self._request(server, "/hls/Sky_One/jellyfin/Sky_One.m3u8")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"#EXTM3U\nSky_One_00000.ts\n")

    def test_channel_metadata_and_iptv_urls_use_configured_non_sky_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = self._start_server(
                root,
                channel_name="Retro Movies",
                channel_slug="Retro_Movies",
                public_base_url="https://stream.example.test",
                logo_filename="retro-movies.png",
            )

            status, _headers, body = self._request(server, "/api/channels")
            self.assertEqual(status, 200)
            channel = json.loads(body)["channels"][0]
            self.assertEqual(channel["name"], "Retro Movies")
            self.assertEqual(channel["slug"], "Retro_Movies")
            self.assertEqual(channel["logo_url"], "https://stream.example.test/hls/Retro_Movies/retro-movies.png")
            self.assertEqual(channel["hls_playlist_url"], "/hls/Retro_Movies/Retro_Movies.m3u8")

            status, _headers, body = self._request(server, "/iptv/channels.m3u")
            self.assertEqual(status, 200)
            m3u = body.decode("utf-8")
            self.assertIn('tvg-name="Retro Movies"', m3u)
            self.assertIn('tvg-logo="https://stream.example.test/hls/Retro_Movies/retro-movies.png"', m3u)
            self.assertIn("https://stream.example.test/hls/Retro_Movies/Retro_Movies.m3u8", m3u)

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

    def test_channel_schedule_exposes_next_plan_item_from_live_playout_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "schedule_now": "2026-06-25T22:08:35+01:00",
                "active_block": {
                    "index": 365,
                    "title": "Star Trek The Next Generation",
                    "start_time": "2026-06-25T22:00:00+01:00",
                    "end_time": "2026-06-25T23:00:00+01:00",
                    "plan": [
                        {"content_type": "bump", "media_type": "video", "path": "ident.mp4", "duration": 10.0, "skip": 0},
                        {"content_type": "feature", "media_type": "video", "path": "episode.avi", "duration": 500.0, "skip": 0},
                        {"content_type": "commercial", "media_type": "video", "path": "ad-a.mp4", "duration": 30.0, "skip": 0},
                    ],
                },
                "playout": {
                    "timeline": [
                        {"index": 0, "content_type": "bump", "path": "ident.mp4", "wallclock_start": "2026-06-25T22:00:00+01:00", "wallclock_end": "2026-06-25T22:00:10+01:00"},
                        {"index": 1, "content_type": "feature", "path": "episode.avi", "wallclock_start": "2026-06-25T22:00:10+01:00", "wallclock_end": "2026-06-25T22:08:30+01:00"},
                        {"index": 2, "content_type": "commercial", "path": "ad-a.mp4", "wallclock_start": "2026-06-25T22:08:30+01:00", "wallclock_end": "2026-06-25T22:09:00+01:00"},
                    ],
                    "current_item": {"index": 2, "content_type": "commercial", "path": "ad-a.mp4", "wallclock_start": "2026-06-25T22:08:30+01:00", "wallclock_end": "2026-06-25T22:09:00+01:00"},
                    "next_item": None,
                },
                "events": [],
            }))
            server = self._start_server(root, status_json=status_path)

            status, headers, body = self._request(server, "/api/channels/Sky_One/schedule")

            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["current_plan_item"]["path"], "ad-a.mp4")
            self.assertIsNone(payload["next_plan_item"])

    def test_channel_schedule_prefers_engine_owned_playout_projection_for_blocks_and_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "schedule_now": "2026-06-25T22:08:35+01:00",
                "playout": {
                    "schedule_now": "2026-06-25T22:08:35+01:00",
                    "current_block": {
                        "index": 365,
                        "selection_reason": "current",
                        "title": "Star Trek The Next Generation",
                        "start_time": "2026-06-25T22:00:00+01:00",
                        "end_time": "2026-06-25T23:00:00+01:00",
                        "plan": [{"path": "episode.avi", "duration": 500.0, "skip": 0}],
                    },
                    "next_block": {
                        "index": 366,
                        "selection_reason": "upcoming",
                        "title": "The Simpsons",
                        "start_time": "2026-06-25T23:00:00+01:00",
                        "end_time": "2026-06-25T23:30:00+01:00",
                    },
                    "timeline": [
                        {"index": 0, "content_type": "feature", "path": "episode.avi", "wallclock_start": "2026-06-25T22:00:10+01:00", "wallclock_end": "2026-06-25T22:08:30+01:00"},
                        {"index": 1, "content_type": "commercial", "path": "ad-a.mp4", "wallclock_start": "2026-06-25T22:08:30+01:00", "wallclock_end": "2026-06-25T22:09:00+01:00"},
                    ],
                    "current_item": {"index": 1, "content_type": "commercial", "path": "ad-a.mp4", "wallclock_start": "2026-06-25T22:08:30+01:00", "wallclock_end": "2026-06-25T22:09:00+01:00"},
                    "next_item": None,
                },
                "events": [],
            }))
            server = self._start_server(root, status_json=status_path)

            status, headers, body = self._request(server, "/api/channels/Sky_One/schedule")

            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["active_block"]["title"], "Star Trek The Next Generation")
            self.assertEqual(payload["upcoming_blocks"][0]["title"], "The Simpsons")
            self.assertEqual(payload["current_plan_item"]["path"], "ad-a.mp4")
            self.assertIsNone(payload["next_plan_item"])

    def test_channel_schedule_prefers_supervisor_contract_over_legacy_top_level_reconstruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "active_block": {"index": 1, "title": "Legacy Block", "start_time": "2026-06-25T21:00:00+01:00", "end_time": "2026-06-25T21:30:00+01:00"},
                "upcoming_blocks": [{"index": 2, "title": "Legacy Next", "start_time": "2026-06-25T21:30:00+01:00", "end_time": "2026-06-25T22:00:00+01:00"}],
                "supervisor": {
                    "selection": {"index": 365, "reason": "current"},
                    "schedule_now": "2026-06-25T22:08:35+01:00",
                    "current_block": {
                        "index": 365,
                        "selection_reason": "current",
                        "title": "Supervisor Block",
                        "start_time": "2026-06-25T22:00:00+01:00",
                        "end_time": "2026-06-25T23:00:00+01:00",
                        "plan": [{"path": "episode.avi", "duration": 500.0, "skip": 0.0}],
                    },
                    "next_block": {
                        "index": 366,
                        "selection_reason": "upcoming",
                        "title": "Supervisor Next",
                        "start_time": "2026-06-25T23:00:00+01:00",
                        "end_time": "2026-06-25T23:30:00+01:00",
                    },
                    "upcoming_blocks": [{
                        "index": 366,
                        "selection_reason": "upcoming",
                        "title": "Supervisor Next",
                        "start_time": "2026-06-25T23:00:00+01:00",
                        "end_time": "2026-06-25T23:30:00+01:00",
                    }],
                    "timeline": [
                        {"index": 0, "content_type": "commercial", "path": "ad-a.mp4", "wallclock_start": "2026-06-25T22:08:30+01:00", "wallclock_end": "2026-06-25T22:09:00+01:00"}
                    ],
                    "catch_up": {"applied": True, "media_seek": 25.0},
                    "render_plan_item_count": 1,
                    "source_plan_item_count": 3,
                    "current_item": {"index": 0, "content_type": "commercial", "path": "ad-a.mp4", "wallclock_start": "2026-06-25T22:08:30+01:00", "wallclock_end": "2026-06-25T22:09:00+01:00"},
                    "next_item": None,
                },
                "events": [],
            }))
            server = self._start_server(root, status_json=status_path)

            status, headers, body = self._request(server, "/api/channels/Sky_One/schedule")

            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["schedule_now"], "2026-06-25T22:08:35+01:00")
            self.assertEqual(payload["active_block"]["title"], "Supervisor Block")
            self.assertEqual(payload["upcoming_blocks"][0]["title"], "Supervisor Next")
            self.assertEqual(payload["current_plan_item"]["path"], "ad-a.mp4")
            self.assertEqual(payload["timeline"][0]["path"], "ad-a.mp4")
            self.assertEqual(payload["catch_up"], {"applied": True, "media_seek": 25.0})

    def test_runtime_route_derives_current_next_item_transition_ffmpeg_and_hls_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel_dir = root / "Sky_One"
            channel_dir.mkdir()
            playlist = channel_dir / "Sky_One.m3u8"
            segment = channel_dir / "Sky_One_00012.ts"
            segment.write_bytes(b"segment")
            playlist.write_text("\n".join([
                "#EXTM3U",
                "#EXT-X-TARGETDURATION:2",
                "#EXT-X-MEDIA-SEQUENCE:10",
                "#EXTINF:2.0,",
                "Sky_One_00011.ts",
                "#EXTINF:2.0,",
                "Sky_One_00012.ts",
                "",
            ]))
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "updated_at": "2026-06-25T21:09:10+00:00",
                "schedule_now": "2026-06-25T22:08:35+01:00",
                "channel_output_dir": str(channel_dir),
                "active_block": {
                    "index": 365,
                    "title": "Star Trek The Next Generation",
                    "start_time": "2026-06-25T22:00:00+01:00",
                    "end_time": "2026-06-25T23:00:00+01:00",
                    "selection_reason": "current",
                    "plan": [
                        {"content_type": "bump", "media_type": "video", "path": "ident.mp4", "duration": 10.0, "skip": 0},
                        {"content_type": "feature", "media_type": "video", "path": "episode.avi", "duration": 500.0, "skip": 0},
                        {"content_type": "commercial", "media_type": "video", "path": "ad-a.mp4", "duration": 30.0, "skip": 0},
                    ],
                },
                "upcoming_blocks": [{
                    "index": 366,
                    "title": "The Simpsons",
                    "start_time": "2026-06-25T23:00:00+01:00",
                    "end_time": "2026-06-25T23:30:00+01:00",
                }],
                "catch_up": {"applied": True, "media_seek": 25.0},
                "ffmpeg": {"pid": 18422, "state": "running", "started_at": "2026-06-25T22:00:01+01:00", "last_exit_code": None, "last_error": None},
                "events": [
                    {"event": "block_start", "at": "2026-06-25T22:00:00+01:00", "block_number": 1, "block": {"index": 365, "title": "Star Trek The Next Generation"}},
                    {"event": "item_advance", "at": "2026-06-25T22:08:30+01:00", "reason": "item_advance", "block_number": 1},
                ],
            }))
            server = self._start_server(root, status_json=status_path)

            status, headers, body = self._request(server, "/api/channels/Sky_One/runtime")

            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "application/json")
            payload = json.loads(body)
            self.assertEqual(payload["channel"], {"id": "fs42.sky_one", "slug": "Sky_One", "name": "Sky One", "logo_url": f"http://127.0.0.1:{server.server_address[1]}/hls/Sky_One/skyone.png"})
            self.assertEqual(payload["block"]["current"]["title"], "Star Trek The Next Generation")
            self.assertEqual(payload["block"]["current"]["seconds_remaining"], 3085.0)
            self.assertEqual(payload["block"]["next"]["title"], "The Simpsons")
            self.assertEqual(payload["item"]["current"]["index"], 2)
            self.assertEqual(payload["item"]["current"]["content_type"], "commercial")
            self.assertEqual(payload["item"]["current"]["seconds_remaining"], 25.0)
            self.assertIsNone(payload["item"]["next"])
            self.assertEqual(payload["transition"]["last_reason"], "item_advance")
            self.assertEqual(payload["ffmpeg"]["pid"], 18422)
            self.assertEqual(payload["ffmpeg"]["state"], "running")
            self.assertEqual(payload["playout"]["catch_up"], {"applied": True, "media_seek": 25.0})
            self.assertEqual(payload["playout"]["render_plan_item_count"], 1)
            self.assertEqual(payload["playout"]["source_plan_item_count"], 3)
            self.assertEqual(payload["playout"]["trimmed"], True)
            self.assertEqual(payload["hls"]["playlist_url"], "/hls/Sky_One/Sky_One.m3u8")
            self.assertEqual(payload["hls"]["playlist_path"], str(playlist))
            self.assertEqual(payload["hls"]["media_sequence"], 10)
            self.assertEqual(payload["hls"]["target_duration"], 2)
            self.assertEqual(payload["hls"]["segment_count"], 2)
            self.assertEqual(payload["hls"]["last_segment_name"], "Sky_One_00012.ts")
            self.assertIn(payload["hls"]["freshness"], {"fresh", "degraded"})

    def test_runtime_playlist_url_uses_resolved_canonical_playlist_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel_dir = root / "Sky_One"
            channel_dir.mkdir()
            playlist = channel_dir / "live.m3u8"
            segment = channel_dir / "segment0.ts"
            segment.write_bytes(b"segment")
            playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:2,\nsegment0.ts\n")
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "schedule_now": "2026-06-25T22:10:00+01:00",
                "hls": {"playlist": str(playlist)},
                "active_block": {"index": 1, "title": "Show", "start_time": "2026-06-25T22:00:00+01:00", "end_time": "2026-06-25T22:30:00+01:00"},
                "ffmpeg": {"pid": 123, "state": "running"},
                "events": [],
            }))
            server = self._start_server(root, status_json=status_path)

            status, headers, body = self._request(server, "/api/channels/Sky_One/runtime")

            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["hls"]["playlist_path"], str(playlist))
            self.assertEqual(payload["hls"]["playlist_url"], "/hls/Sky_One/live.m3u8")

            status, headers, body = self._request(server, payload["hls"]["playlist_url"])
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "application/vnd.apple.mpegurl")
            self.assertEqual(body, playlist.read_bytes())

    def test_runtime_and_health_prefer_supervisor_contract_over_conflicting_legacy_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel_dir = root / "Sky_One"
            channel_dir.mkdir()
            playlist = channel_dir / "Sky_One.m3u8"
            segment = channel_dir / "Sky_One_00001.ts"
            segment.write_bytes(b"segment")
            playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:2,\nSky_One_00001.ts\n")
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "updated_at": "2026-06-25T21:09:10+00:00",
                "schedule_now": "2026-06-25T21:40:00+01:00",
                "active_block": {"index": 1, "title": "Legacy Block", "start_time": "2026-06-25T21:00:00+01:00", "end_time": "2026-06-25T21:30:00+01:00"},
                "upcoming_blocks": [{"index": 2, "title": "Legacy Next", "start_time": "2026-06-25T21:30:00+01:00", "end_time": "2026-06-25T22:00:00+01:00"}],
                "current_plan_item": {"index": 0, "path": "legacy.mp4", "wallclock_start": "2026-06-25T21:00:00+01:00", "wallclock_end": "2026-06-25T21:10:00+01:00"},
                "supervisor": {
                    "selection": {"index": 365, "reason": "current"},
                    "schedule_now": "2026-06-25T22:08:35+01:00",
                    "current_block": {
                        "index": 365,
                        "selection_reason": "current",
                        "title": "Supervisor Block",
                        "start_time": "2026-06-25T22:00:00+01:00",
                        "end_time": "2026-06-25T23:00:00+01:00",
                        "plan": [{"path": "episode.avi", "duration": 500.0, "skip": 0.0}],
                    },
                    "next_block": {
                        "index": 366,
                        "selection_reason": "upcoming",
                        "title": "Supervisor Next",
                        "start_time": "2026-06-25T23:00:00+01:00",
                        "end_time": "2026-06-25T23:30:00+01:00",
                    },
                    "upcoming_blocks": [{
                        "index": 366,
                        "selection_reason": "upcoming",
                        "title": "Supervisor Next",
                        "start_time": "2026-06-25T23:00:00+01:00",
                        "end_time": "2026-06-25T23:30:00+01:00",
                    }],
                    "timeline": [
                        {"index": 0, "content_type": "commercial", "path": "ad-a.mp4", "wallclock_start": "2026-06-25T22:08:30+01:00", "wallclock_end": "2026-06-25T22:09:00+01:00"}
                    ],
                    "catch_up": {"applied": True, "media_seek": 25.0},
                    "render_plan_item_count": 1,
                    "source_plan_item_count": 3,
                    "current_item": {"index": 0, "content_type": "commercial", "path": "ad-a.mp4", "wallclock_start": "2026-06-25T22:08:30+01:00", "wallclock_end": "2026-06-25T22:09:00+01:00"},
                    "next_item": None,
                },
                "ffmpeg": {"pid": 18422, "state": "running", "started_at": "2026-06-25T22:00:01+01:00"},
                "events": [{"event": "item_advance", "at": "2026-06-25T22:08:30+01:00", "reason": "item_advance"}],
            }))
            server = self._start_server(root, status_json=status_path)

            status, headers, body = self._request(server, "/api/channels/Sky_One/runtime")

            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["service"]["schedule_now"], "2026-06-25T22:08:35+01:00")
            self.assertEqual(payload["block"]["current"]["title"], "Supervisor Block")
            self.assertEqual(payload["block"]["next"]["title"], "Supervisor Next")
            self.assertEqual(payload["item"]["current"]["path"], "ad-a.mp4")
            self.assertIsNone(payload["item"]["next"])

            status, headers, body = self._request(server, "/api/channels/Sky_One/health")

            self.assertEqual(status, 200)
            health = json.loads(body)
            self.assertEqual(health["status"], "ok")
            self.assertEqual(health["checks"]["active_block_present"], "ok")

    def test_epg_and_xmltv_prefer_supervisor_contract_programmes_over_legacy_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "active_block": {"index": 1, "title": "Legacy Block", "start_time": "2026-06-25T21:00:00+01:00", "end_time": "2026-06-25T21:30:00+01:00"},
                "upcoming_blocks": [{"index": 2, "title": "Legacy Next", "start_time": "2026-06-25T21:30:00+01:00", "end_time": "2026-06-25T22:00:00+01:00"}],
                "supervisor": {
                    "selection": {"index": 365, "reason": "current"},
                    "schedule_now": "2026-06-25T22:08:35+01:00",
                    "current_block": {
                        "index": 365,
                        "selection_reason": "current",
                        "title": "Supervisor Block",
                        "start_time": "2026-06-25T22:00:00+01:00",
                        "end_time": "2026-06-25T23:00:00+01:00",
                    },
                    "upcoming_blocks": [{
                        "index": 366,
                        "selection_reason": "upcoming",
                        "title": "Supervisor Next",
                        "start_time": "2026-06-25T23:00:00+01:00",
                        "end_time": "2026-06-25T23:30:00+01:00",
                    }],
                },
                "events": [],
            }))
            server = self._start_server(root, status_json=status_path)

            status, headers, body = self._request(server, "/api/channels/Sky_One/epg")

            self.assertEqual(status, 200)
            epg = json.loads(body)
            self.assertEqual([programme["title"] for programme in epg["programmes"]], ["Supervisor Block", "Supervisor Next"])

            status, headers, body = self._request(server, "/iptv/xmltv.xml")

            self.assertEqual(status, 200)
            xmltv = ET.fromstring(body)
            self.assertEqual([programme.findtext("title") for programme in xmltv.findall("programme")], ["Supervisor Block", "Supervisor Next"])

    def test_health_reports_stale_schedule_as_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel_dir = root / "Sky_One"
            channel_dir.mkdir(parents=True)
            playlist = channel_dir / "Sky_One.m3u8"
            segment = channel_dir / "Sky_One_00000.ts"
            playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nSky_One_00000.ts\n")
            segment.write_text("segment")
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "updated_at": "2026-06-25T22:10:00+01:00",
                "schedule_now": "2026-06-25T22:10:00+01:00",
                "stale_schedule": {"active": True, "reason": "summary-expired", "summary_end": "2026-06-25T06:00:00"},
                "active_block": {"index": 1, "title": "Schedule stale - BRB", "start_time": "2026-06-25T22:10:00+01:00", "end_time": "2026-06-25T22:20:00+01:00"},
                "playlist": str(playlist)
            }))
            server = self._start_server(root, status_json=status_path)
            status, headers, body = self._request(server, "/api/channels/Sky_One/health")
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["status"], "degraded")
            self.assertEqual(payload["checks"]["schedule_freshness"], "degraded")
            self.assertTrue(payload["details"]["stale_schedule"]["active"])

    def test_channel_health_reports_ok_degraded_or_error_from_runtime_checks(self):
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
            server = self._start_server(root, status_json=status_path)

            status, headers, body = self._request(server, "/api/channels/Sky_One/health")

            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["channel_id"], "fs42.sky_one")
            self.assertIn(payload["status"], {"ok", "degraded"})
            self.assertEqual(payload["checks"]["service_state"], "ok")
            self.assertEqual(payload["checks"]["active_block_present"], "ok")
            self.assertEqual(payload["checks"]["ffmpeg_running"], "ok")
            self.assertEqual(payload["checks"]["playlist_present"], "ok")
            self.assertIn("media_sequence", payload["details"])

    def test_events_route_returns_bounded_recent_transition_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "events": [{"event": f"event_{index}", "reason": f"reason_{index}"} for index in range(15)],
            }))
            server = self._start_server(root, status_json=status_path)

            status, headers, body = self._request(server, "/api/channels/Sky_One/events")

            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["channel"]["id"], "fs42.sky_one")
            self.assertEqual(len(payload["events"]), 10)
            self.assertEqual(payload["events"][0]["event"], "event_5")
            self.assertEqual(payload["events"][-1]["event"], "event_14")

    def test_iptv_m3u_xmltv_and_epg_share_stable_channel_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "schedule_now": "2026-06-25T22:05:00+01:00",
                "active_block": {"index": 1, "title": "Show A", "start_time": "2026-06-25T22:00:00+01:00", "end_time": "2026-06-25T22:30:00+01:00"},
                "upcoming_blocks": [
                    {"index": 2, "title": "Show B", "start_time": "2026-06-25T22:30:00+01:00", "end_time": "2026-06-25T23:00:00+01:00"},
                ],
                "events": [],
            }))
            server = self._start_server(root, status_json=status_path)
            port = server.server_address[1]

            status, headers, body = self._request(server, "/iptv/channels.m3u")
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "application/vnd.apple.mpegurl")
            m3u = body.decode("utf-8")
            self.assertIn(f'#EXTINF:-1 tvg-id="fs42.sky_one" tvg-name="Sky One" tvg-logo="http://127.0.0.1:{port}/hls/Sky_One/skyone.png" group-title="FS42",Sky One', m3u)
            self.assertIn(f"http://127.0.0.1:{port}/hls/Sky_One/Sky_One.m3u8", m3u)

            status, headers, body = self._request(server, "/iptv/xmltv.xml")
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "application/xml; charset=utf-8")
            root_xml = ET.fromstring(body)
            channel = root_xml.find("channel")
            self.assertIsNotNone(channel)
            self.assertEqual(channel.attrib["id"], "fs42.sky_one")
            self.assertEqual(channel.find("icon").attrib["src"], f"http://127.0.0.1:{port}/hls/Sky_One/skyone.png")
            programmes = root_xml.findall("programme")
            self.assertEqual([programme.attrib["channel"] for programme in programmes], ["fs42.sky_one", "fs42.sky_one"])
            self.assertEqual(programmes[0].findtext("title"), "Show A")
            self.assertEqual(programmes[0].attrib["start"], "20260625220000 +0100")

            status, headers, body = self._request(server, "/iptv/jellyfin/channels.m3u")
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "application/vnd.apple.mpegurl")
            jellyfin_m3u = body.decode("utf-8")
            self.assertIn(f'#EXTINF:-1 tvg-id="fs42.sky_one" tvg-name="Sky One (Jellyfin)" tvg-logo="http://127.0.0.1:{port}/hls/Sky_One/skyone.png" group-title="FS42",Sky One (Jellyfin)', jellyfin_m3u)
            self.assertIn(f"http://127.0.0.1:{port}/hls/Sky_One/jellyfin/Sky_One.m3u8", jellyfin_m3u)

            status, headers, body = self._request(server, "/api/channels/Sky_One/epg")
            self.assertEqual(status, 200)
            epg = json.loads(body)
            self.assertEqual(epg["channel"]["id"], "fs42.sky_one")
            self.assertEqual(epg["programmes"][0]["channel_id"], "fs42.sky_one")

    def test_xmltv_prefers_full_fs42_schedule_when_fetcher_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.json"
            status_path.write_text(json.dumps({
                "status": "running",
                "channel": "Sky One",
                "active_block": {"index": 1, "title": "Status Current", "start_time": "2026-06-25T22:00:00+01:00", "end_time": "2026-06-25T22:30:00+01:00"},
                "upcoming_blocks": [],
            }))
            calls = []

            def schedule_fetcher(channel):
                calls.append(channel)
                return {
                    "network_name": "Sky One",
                    "schedule_blocks": [
                        {"index": 10, "title": "FS42 Show A", "start_time": "2026-06-25T22:00:00+01:00", "end_time": "2026-06-25T22:30:00+01:00"},
                        {"index": 11, "title": "FS42 Show B", "start_time": "2026-06-25T22:30:00+01:00", "end_time": "2026-06-25T23:00:00+01:00"},
                        {"index": 12, "title": "FS42 Show C", "start_time": "2026-06-25T23:00:00+01:00", "end_time": "2026-06-25T23:30:00+01:00"},
                    ],
                }

            server = self._start_server(root, status_json=status_path, schedule_fetcher=schedule_fetcher)

            status, _headers, body = self._request(server, "/iptv/xmltv.xml")

            self.assertEqual(status, 200)
            self.assertEqual(calls, ["Sky One"])
            root_xml = ET.fromstring(body)
            self.assertEqual([programme.findtext("title") for programme in root_xml.findall("programme")], ["FS42 Show A", "FS42 Show B", "FS42 Show C"])

    def test_unknown_iptv_channel_playlist_returns_channel_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = self._start_server(Path(tmp))

            status, headers, body = self._request(server, "/iptv/channels/Other.m3u")

            self.assertEqual(status, 404)
            payload = json.loads(body)
            self.assertEqual(payload["error"], "channel_not_found")
            self.assertIn("Other", payload["message"])

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
