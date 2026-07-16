import unittest
from pathlib import Path

from fs42stream.systemd_service import ServiceConfig, render_environment_file, render_unit_file


class SystemdServiceTests(unittest.TestCase):
    def test_service_default_duration_covers_long_fs42_blocks(self):
        config = ServiceConfig()

        env = render_environment_file(config)

        self.assertGreaterEqual(config.duration_limit, 5400.0)
        self.assertIn('FS42STREAM_DURATION_LIMIT="7200"', env)

    def test_renders_environment_file_with_service_defaults(self):
        config = ServiceConfig(
            channel="Sky One",
            host="0.0.0.0",
            port=8088,
            output_root=Path("/var/lib/fs42stream/hls"),
            max_blocks=1000000,
            duration_limit=7200.0,
            video_encoder="h264_vaapi",
            vaapi_device="/dev/dri/renderD128",
        )

        env = render_environment_file(config)

        self.assertIn('FS42STREAM_CHANNEL="Sky One"', env)
        self.assertIn('FS42STREAM_HOST="0.0.0.0"', env)
        self.assertIn('FS42STREAM_PORT="8088"', env)
        self.assertIn('FS42STREAM_OUTPUT_ROOT="/var/lib/fs42stream/hls"', env)
        self.assertIn('FS42STREAM_MAX_BLOCKS="1000000"', env)
        self.assertIn('FS42STREAM_DURATION_LIMIT="7200"', env)
        self.assertIn('FS42STREAM_VIDEO_ENCODER="h264_vaapi"', env)
        self.assertIn('FS42STREAM_VAAPI_DEVICE="/dev/dri/renderD128"', env)
        self.assertIn('FS42STREAM_PLAYOUT_MODE="ts-primary"', env)

    def test_renders_systemd_unit_with_env_file_and_restart_policy(self):
        config = ServiceConfig(
            install_dir=Path("/opt/fs42stream"),
            env_file=Path("/etc/fs42stream/fs42stream.env"),
            user="hermes-admin",
            service_name="fs42stream",
        )

        unit = render_unit_file(config)

        self.assertIn("Description=FS42 schedule-following HLS streamer", unit)
        self.assertIn("After=network-online.target", unit)
        self.assertIn("Wants=network-online.target", unit)
        self.assertIn("User=hermes-admin", unit)
        self.assertIn("WorkingDirectory=/opt/fs42stream", unit)
        self.assertIn("EnvironmentFile=/etc/fs42stream/fs42stream.env", unit)
        self.assertIn("ExecStart=/usr/bin/python3 -m fs42stream.integrated_runner", unit)
        self.assertIn('--channel "${FS42STREAM_CHANNEL}"', unit)
        self.assertIn("--video-encoder ${FS42STREAM_VIDEO_ENCODER}", unit)
        self.assertIn("--vaapi-device ${FS42STREAM_VAAPI_DEVICE}", unit)
        self.assertIn("--playout-mode ${FS42STREAM_PLAYOUT_MODE}", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartSec=5", unit)
        self.assertIn("WantedBy=multi-user.target", unit)


if __name__ == "__main__":
    unittest.main()
