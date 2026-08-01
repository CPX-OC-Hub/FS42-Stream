import unittest
from pathlib import Path

from fs42stream.systemd_service import ServiceConfig, render_cleanup_service_file, render_cleanup_timer_file, render_environment_file, render_unit_file


class SystemdServiceTests(unittest.TestCase):
    def test_service_default_duration_covers_long_fs42_blocks(self):
        config = ServiceConfig()

        env = render_environment_file(config)

        self.assertGreaterEqual(config.duration_limit, 5400.0)
        self.assertIn('FS42STREAM_DURATION_LIMIT="7200"', env)

    def test_service_defaults_to_jellyfin_only_and_passes_profile_switch_to_runner(self):
        config = ServiceConfig()

        env = render_environment_file(config)
        unit = render_unit_file(config)

        self.assertEqual(config.stream_profiles, "jellyfin")
        self.assertIn('FS42STREAM_STREAM_PROFILES="jellyfin"', env)
        self.assertIn('--stream-profiles "${FS42STREAM_STREAM_PROFILES}"', unit)
        self.assertEqual(config.audio_normalization, "off")
        self.assertIn('FS42STREAM_AUDIO_NORMALIZATION="off"', env)
        self.assertIn('--audio-normalization "${FS42STREAM_AUDIO_NORMALIZATION}"', unit)

    def test_service_can_render_loudnorm_audio_normalization(self):
        env = render_environment_file(ServiceConfig(audio_normalization="loudnorm"))

        self.assertIn('FS42STREAM_AUDIO_NORMALIZATION="loudnorm"', env)

    def test_service_renders_configurable_brb_image_path_and_passes_it_to_runner(self):
        config = ServiceConfig(brb_image_path="catalog/SkyOne/runtime/brb.png")

        self.assertIn('FS42STREAM_BRB_IMAGE_PATH="catalog/SkyOne/runtime/brb.png"', render_environment_file(config))
        self.assertIn('--brb-image-path "${FS42STREAM_BRB_IMAGE_PATH}"', render_unit_file(config))

    def test_service_can_render_direct_or_both_profile_selection(self):
        self.assertIn('FS42STREAM_STREAM_PROFILES="direct"', render_environment_file(ServiceConfig(stream_profiles="direct")))
        self.assertIn('FS42STREAM_STREAM_PROFILES="both"', render_environment_file(ServiceConfig(stream_profiles="both")))

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
            schedule_scheme="https",
            schedule_host="scheduler.example.test",
            schedule_port=443,
            schedule_base_path="api",
            public_base_url="https://stream.example.test",
            channel_slug="Retro_Movies",
            logo_filename="retro-movies.png",
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
        self.assertIn('FS42STREAM_SCHEDULE_SCHEME="https"', env)
        self.assertIn('FS42STREAM_SCHEDULE_HOST="scheduler.example.test"', env)
        self.assertIn('FS42STREAM_SCHEDULE_PORT="443"', env)
        self.assertIn('FS42STREAM_SCHEDULE_BASE_PATH="api"', env)
        self.assertIn('FS42STREAM_API_BASE_URL=""', env)
        self.assertIn('FS42STREAM_PUBLIC_BASE_URL="https://stream.example.test"', env)
        self.assertIn('FS42STREAM_CHANNEL_SLUG="Retro_Movies"', env)
        self.assertIn('FS42STREAM_LOGO_FILENAME="retro-movies.png"', env)

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
        self.assertIn("--schedule-scheme ${FS42STREAM_SCHEDULE_SCHEME}", unit)
        self.assertIn("--schedule-host ${FS42STREAM_SCHEDULE_HOST}", unit)
        self.assertIn("--schedule-port ${FS42STREAM_SCHEDULE_PORT}", unit)
        self.assertIn('--schedule-base-path "${FS42STREAM_SCHEDULE_BASE_PATH}"', unit)
        self.assertIn('--api-base-url "${FS42STREAM_API_BASE_URL}"', unit)
        self.assertIn('--public-base-url "${FS42STREAM_PUBLIC_BASE_URL}"', unit)
        self.assertIn("--channel-slug ${FS42STREAM_CHANNEL_SLUG}", unit)
        self.assertIn("--logo-filename ${FS42STREAM_LOGO_FILENAME}", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartSec=5", unit)
        self.assertIn("WantedBy=multi-user.target", unit)

    def test_renders_hls_cleanup_service_and_timer(self):
        config = ServiceConfig(output_root=Path("/var/lib/fs42stream/hls"), env_file=Path("/etc/fs42stream/fs42stream.env"))

        service = render_cleanup_service_file(config)
        timer = render_cleanup_timer_file(config)

        self.assertIn("Description=FS42-Stream HLS retention cleanup", service)
        self.assertIn("Type=oneshot", service)
        self.assertIn("EnvironmentFile=/etc/fs42stream/fs42stream.env", service)
        self.assertIn("ExecStart=/usr/bin/python3 -m fs42stream.hls_retention", service)
        self.assertIn("--output-root ${FS42STREAM_OUTPUT_ROOT}", service)
        self.assertIn("--channel-slug ${FS42STREAM_CHANNEL_SLUG}", service)
        self.assertIn("OnCalendar=*:0/15", timer)
        self.assertIn("WantedBy=timers.target", timer)


if __name__ == "__main__":
    unittest.main()
