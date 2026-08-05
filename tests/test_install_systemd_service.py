import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.install_systemd_service import main


class InstallSystemdServiceTests(unittest.TestCase):
    def test_dry_run_prints_unit_and_env_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout") as stdout:
            rc = main([
                "--dry-run",
                "--install-dir", str(Path(tmp) / "opt"),
                "--env-file", str(Path(tmp) / "etc" / "fs42stream.env"),
                "--unit-file", str(Path(tmp) / "systemd" / "fs42stream.service"),
            ])

        self.assertEqual(rc, 0)
        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn("[Unit]", output)
        self.assertIn("FS42STREAM_CHANNEL", output)
        self.assertIn("DRY_RUN", output)

    def test_install_writes_env_and_unit_when_not_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_file = root / "etc" / "fs42stream.env"
            unit_file = root / "systemd" / "fs42stream.service"
            output_root = root / "hls"
            rc = main([
                "--install-dir", str(root / "opt"),
                "--env-file", str(env_file),
                "--unit-file", str(unit_file),
                "--output-root", str(output_root),
                "--skip-systemctl",
            ])

            self.assertEqual(rc, 0)
            env_text = env_file.read_text()
            unit_text = unit_file.read_text()
            self.assertIn("FS42STREAM_OUTPUT_ROOT", env_text)
            self.assertIn('FS42STREAM_DURATION_LIMIT="7200"', env_text)
            self.assertIn("FS42STREAM_SCHEDULE_TIMEZONE", env_text)
            self.assertIn('FS42STREAM_STREAM_PROFILES="jellyfin"', env_text)
            self.assertIn("--schedule-timezone ${FS42STREAM_SCHEDULE_TIMEZONE}", unit_text)
            self.assertIn("ExecStart=/usr/bin/python3 -m fs42stream.integrated_runner", unit_text)
            self.assertTrue(output_root.exists())

    def test_dry_run_rejects_invalid_stream_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                main(["--env-file", str(Path(tmp) / "env"), "--unit-file", str(Path(tmp) / "unit"), "--output-root", str(Path(tmp) / "hls"), "--stream-profiles", "debug", "--dry-run"])

    def test_dry_run_propagates_configured_brb_image_path(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout") as stdout:
            rc = main(["--dry-run", "--env-file", str(Path(tmp) / "env"), "--unit-file", str(Path(tmp) / "unit"), "--output-root", str(Path(tmp) / "hls"), "--brb-image-path", "catalog/SkyOne/runtime/brb.png"])

        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertEqual(rc, 0)
        self.assertIn('FS42STREAM_BRB_IMAGE_PATH="catalog/SkyOne/runtime/brb.png"', output)
        self.assertIn('--brb-image-path "${FS42STREAM_BRB_IMAGE_PATH}"', output)

    def test_dry_run_propagates_loudnorm_audio_normalization(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout") as stdout:
            rc = main([
                "--env-file", str(Path(tmp) / "env"),
                "--unit-file", str(Path(tmp) / "unit"),
                "--output-root", str(Path(tmp) / "hls"),
                "--audio-normalization", "loudnorm",
                "--dry-run",
            ])

        self.assertEqual(rc, 0)
        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn('FS42STREAM_AUDIO_NORMALIZATION="loudnorm"', output)
        self.assertIn('--audio-normalization "${FS42STREAM_AUDIO_NORMALIZATION}"', output)

    def test_dry_run_propagates_jellyfin_pre_roll_lead_seconds(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout") as stdout:
            rc = main([
                "--env-file", str(Path(tmp) / "env"),
                "--unit-file", str(Path(tmp) / "unit"),
                "--output-root", str(Path(tmp) / "hls"),
                "--jellyfin-pre-roll-lead-seconds", "120",
                "--dry-run",
            ])

        self.assertEqual(rc, 0)
        output = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn('FS42STREAM_JELLYFIN_PRE_ROLL_LEAD_SECONDS="120"', output)
        self.assertIn('--jellyfin-pre-roll-lead-seconds "${FS42STREAM_JELLYFIN_PRE_ROLL_LEAD_SECONDS}"', output)

    def test_script_can_run_directly_from_repo_root(self):
        completed = subprocess.run(
            [sys.executable, "scripts/install_systemd_service.py", "--dry-run"],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("DRY_RUN", completed.stdout)
        self.assertIn("fs42stream.integrated_runner", completed.stdout)


if __name__ == "__main__":
    unittest.main()
