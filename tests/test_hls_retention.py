import os
import tempfile
import time
import unittest
from pathlib import Path

from fs42stream.hls_retention import HLSRetentionConfig, cleanup_hls_retention


class HLSRetentionTests(unittest.TestCase):
    def _touch(self, path: Path, *, age_seconds: float, content: bytes = b"x") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_prunes_old_segments_but_preserves_active_playlist_window_and_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            channel = root / "Sky_One"
            channel.mkdir()
            playlist = channel / "Sky_One.m3u8"
            playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nSky_One_00002.ts\n#EXTINF:2,\nSky_One_00003.ts\n")
            self._touch(channel / "Sky_One_00000.ts", age_seconds=7200)
            self._touch(channel / "Sky_One_00001.ts", age_seconds=7200)
            self._touch(channel / "Sky_One_00002.ts", age_seconds=7200)
            self._touch(channel / "Sky_One_00003.ts", age_seconds=7200)
            asset = self._touch(channel / "skyone.png", age_seconds=7200, content=b"png")
            non_hls = self._touch(channel / "notes.txt", age_seconds=7200, content=b"notes")

            result = cleanup_hls_retention(HLSRetentionConfig(output_root=root, channel_slug="Sky_One", max_age_seconds=60, max_segments_per_dir=50))

            self.assertEqual({path.name for path in result.removed}, {"Sky_One_00000.ts", "Sky_One_00001.ts"})
            self.assertTrue(playlist.exists())
            self.assertTrue((channel / "Sky_One_00002.ts").exists())
            self.assertTrue((channel / "Sky_One_00003.ts").exists())
            self.assertTrue(asset.exists())
            self.assertTrue(non_hls.exists())

    def test_applies_count_retention_to_direct_and_jellyfin_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for subdir in (root / "Sky_One", root / "Sky_One" / "jellyfin"):
                subdir.mkdir(parents=True)
                (subdir / "Sky_One.m3u8").write_text("#EXTM3U\n#EXTINF:2,\nSky_One_00004.ts\n")
                for index in range(5):
                    self._touch(subdir / f"Sky_One_0000{index}.ts", age_seconds=100 - index)

            result = cleanup_hls_retention(HLSRetentionConfig(output_root=root, channel_slug="Sky_One", max_age_seconds=9999, max_segments_per_dir=2))

            removed_by_parent = {(path.parent.name, path.name) for path in result.removed}
            self.assertIn(("Sky_One", "Sky_One_00000.ts"), removed_by_parent)
            self.assertIn(("jellyfin", "Sky_One_00000.ts"), removed_by_parent)
            self.assertTrue((root / "Sky_One" / "Sky_One_00004.ts").exists())
            self.assertTrue((root / "Sky_One" / "jellyfin" / "Sky_One_00004.ts").exists())

    def test_rejects_unsafe_channel_slug_and_playlist_traversal_does_not_protect_outside_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.ts"
            outside.write_bytes(b"outside")
            channel = root / "Sky_One"
            channel.mkdir()
            (channel / "Sky_One.m3u8").write_text("#EXTM3U\n#EXTINF:2,\n../outside.ts\n#EXTINF:2,\nSky_One_00000.ts\n")
            self._touch(channel / "Sky_One_00000.ts", age_seconds=7200)

            with self.assertRaises(ValueError):
                cleanup_hls_retention(HLSRetentionConfig(output_root=root, channel_slug="../Sky_One"))

            result = cleanup_hls_retention(HLSRetentionConfig(output_root=root, channel_slug="Sky_One", max_age_seconds=60, max_segments_per_dir=50))
            self.assertEqual(result.removed, [])
            self.assertTrue(outside.exists())
            self.assertTrue((channel / "Sky_One_00000.ts").exists())


if __name__ == "__main__":
    unittest.main()
