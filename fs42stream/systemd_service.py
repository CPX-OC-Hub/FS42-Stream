from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from .client import DEFAULT_SCHEDULE_BASE_PATH, DEFAULT_SCHEDULE_HOST, DEFAULT_SCHEDULE_PORT, DEFAULT_SCHEDULE_SCHEME


@dataclass(frozen=True)
class ServiceConfig:
    service_name: str = "fs42stream"
    user: str = "fs42stream"
    install_dir: Path = Path("/opt/fs42stream")
    env_file: Path = Path("/etc/fs42stream/fs42stream.env")
    channel: str = "Example Channel"
    channel_slug: str = "Example_Channel"
    host: str = "0.0.0.0"
    port: int = 8088
    output_root: Path = Path("/var/lib/fs42stream/hls")
    max_blocks: int = 1000000
    duration_limit: float = 7200.0
    video_encoder: str = "h264_vaapi"
    vaapi_device: str = "/dev/dri/renderD128"
    schedule_timezone: str = "Europe/London"
    playout_mode: str = "ts-primary"
    schedule_scheme: str = DEFAULT_SCHEDULE_SCHEME
    schedule_host: str = DEFAULT_SCHEDULE_HOST
    schedule_port: int = DEFAULT_SCHEDULE_PORT
    schedule_base_path: str = DEFAULT_SCHEDULE_BASE_PATH
    api_base_url: str = ""
    public_base_url: str = ""
    logo_filename: str = "logo.png"
    stream_profiles: str = "jellyfin"


def render_environment_file(config: ServiceConfig) -> str:
    values = {
        "FS42STREAM_CHANNEL": config.channel,
        "FS42STREAM_CHANNEL_SLUG": config.channel_slug,
        "FS42STREAM_HOST": config.host,
        "FS42STREAM_PORT": str(config.port),
        "FS42STREAM_OUTPUT_ROOT": str(config.output_root),
        "FS42STREAM_MAX_BLOCKS": str(config.max_blocks),
        "FS42STREAM_DURATION_LIMIT": _num(config.duration_limit),
        "FS42STREAM_VIDEO_ENCODER": config.video_encoder,
        "FS42STREAM_VAAPI_DEVICE": config.vaapi_device,
        "FS42STREAM_SCHEDULE_TIMEZONE": config.schedule_timezone,
        "FS42STREAM_PLAYOUT_MODE": config.playout_mode,
        "FS42STREAM_SCHEDULE_SCHEME": config.schedule_scheme,
        "FS42STREAM_SCHEDULE_HOST": config.schedule_host,
        "FS42STREAM_SCHEDULE_PORT": str(config.schedule_port),
        "FS42STREAM_SCHEDULE_BASE_PATH": config.schedule_base_path,
        "FS42STREAM_API_BASE_URL": config.api_base_url,
        "FS42STREAM_PUBLIC_BASE_URL": config.public_base_url,
        "FS42STREAM_LOGO_FILENAME": config.logo_filename,
        "FS42STREAM_STREAM_PROFILES": config.stream_profiles,
    }
    lines = ["# Managed by FS42-Stream installer", "# Edit values here, then run: sudo systemctl restart fs42stream", ""]
    lines.extend(f'{key}="{_escape_env(value)}"' for key, value in values.items())
    return "\n".join(lines) + "\n"


def render_unit_file(config: ServiceConfig) -> str:
    python = "/usr/bin/python3"
    return f"""[Unit]
Description=FS42 schedule-following HLS streamer
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User={config.user}
WorkingDirectory={config.install_dir}
EnvironmentFile={config.env_file}
ExecStart={python} -m fs42stream.integrated_runner --channel "${{FS42STREAM_CHANNEL}}" --channel-slug ${{FS42STREAM_CHANNEL_SLUG}} --host ${{FS42STREAM_HOST}} --port ${{FS42STREAM_PORT}} --output-root ${{FS42STREAM_OUTPUT_ROOT}} --max-blocks ${{FS42STREAM_MAX_BLOCKS}} --duration-limit ${{FS42STREAM_DURATION_LIMIT}} --schedule-scheme ${{FS42STREAM_SCHEDULE_SCHEME}} --schedule-host ${{FS42STREAM_SCHEDULE_HOST}} --schedule-port ${{FS42STREAM_SCHEDULE_PORT}} --schedule-base-path "${{FS42STREAM_SCHEDULE_BASE_PATH}}" --api-base-url "${{FS42STREAM_API_BASE_URL}}" --public-base-url "${{FS42STREAM_PUBLIC_BASE_URL}}" --logo-filename ${{FS42STREAM_LOGO_FILENAME}} --video-encoder ${{FS42STREAM_VIDEO_ENCODER}} --vaapi-device ${{FS42STREAM_VAAPI_DEVICE}} --schedule-timezone ${{FS42STREAM_SCHEDULE_TIMEZONE}} --playout-mode ${{FS42STREAM_PLAYOUT_MODE}} --stream-profiles "${{FS42STREAM_STREAM_PROFILES}}"
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""


def render_cleanup_service_file(config: ServiceConfig) -> str:
    python = "/usr/bin/python3"
    return f"""[Unit]
Description=FS42-Stream HLS retention cleanup
After=local-fs.target

[Service]
Type=oneshot
User={config.user}
WorkingDirectory={config.install_dir}
EnvironmentFile={config.env_file}
ExecStart={python} -m fs42stream.hls_retention --output-root ${{FS42STREAM_OUTPUT_ROOT}} --channel-slug ${{FS42STREAM_CHANNEL_SLUG}}
"""


def render_cleanup_timer_file(config: ServiceConfig) -> str:
    return f"""[Unit]
Description=Run {config.service_name} HLS retention cleanup periodically

[Timer]
OnCalendar=*:0/15
Persistent=true
Unit={config.service_name}-hls-cleanup.service

[Install]
WantedBy=timers.target
"""


def _escape_env(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _num(value: float) -> str:
    return (f"{value:.6f}").rstrip("0").rstrip(".")
