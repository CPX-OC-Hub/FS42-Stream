import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fs42stream.jellyfin_preroll import GatedJellyfinPreRoll, staged_run_timing


class GatedJellyfinPreRollTests(unittest.TestCase):
    def _write_stage(self, directory: Path, names: list[str]) -> Path:
        for name in names:
            (directory / name).write_bytes(name.encode("utf-8"))
        playlist = directory / "Sky_One.m3u8"
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:0"]
        for name in names:
            lines.extend(["#EXTINF:2.000000,", name])
        playlist.write_text("\n".join(lines) + "\n")
        return playlist

    def test_two_block_boundary_keeps_staged_next_show_private_then_publishes_ready_segments_without_starvation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / ".staged" / "second"
            stage.mkdir(parents=True)
            staged_playlist = self._write_stage(stage, ["Sky_One_00000.ts", "Sky_One_00001.ts"])
            public = root / "Sky_One.m3u8"
            public.write_text("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:884\n#EXTINF:2.0,\nSky_One_00884.ts\n#EXTINF:2.0,\nSky_One_00885.ts\n#EXTINF:2.0,\nSky_One_00886.ts\n")
            for number in range(884, 887):
                (root / f"Sky_One_{number:05d}.ts").write_bytes(b"old")

            gate = GatedJellyfinPreRoll(public_playlist=public, staged_playlist=staged_playlist, publish_at=datetime(2026, 6, 17, 10, 30, 0), public_next_segment_number=887)

            before = gate.publish_if_due(datetime(2026, 6, 17, 10, 29, 59))
            self.assertEqual(before["pre_roll_state"], "staged-private")
            self.assertNotIn("Sky_One_00887.ts", public.read_text())

            at_boundary = gate.publish_if_due(datetime(2026, 6, 17, 10, 30, 0))
            public_text = public.read_text()
            self.assertEqual(at_boundary["pre_roll_state"], "published")
            self.assertEqual(at_boundary["public_boundary_switch"]["event"], "public_boundary_switch")
            self.assertEqual(at_boundary["staged_segments_ready"], 2)
            self.assertIn("Sky_One_00887.ts", public_text)
            self.assertIn("Sky_One_00888.ts", public_text)
            self.assertNotIn("#EXT-X-ENDLIST", public_text)
            self.assertNotIn("#EXT-X-DISCONTINUITY", public_text)
            self.assertEqual((root / "Sky_One_00887.ts").read_bytes(), b"Sky_One_00000.ts")
            self.assertEqual((root / "Sky_One_00888.ts").read_bytes(), b"Sky_One_00001.ts")

    def test_staged_run_timing_uses_media_zero_when_started_early_and_catches_up_when_late(self):
        block_start = datetime(2026, 6, 17, 10, 30, 0)
        early = staged_run_timing(block_start=block_start, stage_started_at=block_start - timedelta(seconds=20))
        late = staged_run_timing(block_start=block_start, stage_started_at=block_start + timedelta(seconds=13))

        self.assertEqual(early.media_offset, 0.0)
        self.assertEqual(early.run_now, block_start)
        self.assertEqual(late.media_offset, 13.0)
        self.assertEqual(late.run_now, block_start + timedelta(seconds=13))

    def test_staged_run_timing_accepts_aware_clock_values_from_live_service(self):
        block_start = datetime(2026, 6, 17, 10, 30, 0)

        early = staged_run_timing(
            block_start=block_start,
            stage_started_at=datetime(2026, 6, 17, 10, 29, 52, tzinfo=timezone.utc),
        )

        self.assertEqual(early.run_now, block_start)
        self.assertEqual(early.media_offset, 0.0)

    def test_status_diagnostics_expose_fallback_when_boundary_arrives_without_stage_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / ".staged" / "empty"
            stage.mkdir(parents=True)
            staged_playlist = stage / "Sky_One.m3u8"
            staged_playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n")
            public = root / "Sky_One.m3u8"
            public.write_text("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:10\n#EXTINF:2.0,\nSky_One_00010.ts\n")

            gate = GatedJellyfinPreRoll(public_playlist=public, staged_playlist=staged_playlist, publish_at=datetime(2026, 6, 17, 10, 30, 0), public_next_segment_number=11)
            result = gate.publish_if_due(datetime(2026, 6, 17, 10, 30, 0))

        self.assertEqual(result["pre_roll_state"], "fallback")
        self.assertEqual(result["fallback_reason"], "no-staged-segments-ready")
        self.assertEqual(result["staged_block"], "empty")


if __name__ == "__main__":
    unittest.main()
