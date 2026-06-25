from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServiceConfig:
    service_name: str = "fs42stream"
    user: str = "hermes-admin"
    install_dir: Path = Path("/opt/fs42stream")
    env_file: Path = Path("/etc/fs42stream/fs42stream.env")
    channel: str = "Sky One"
    host: str = "0.0.0.0"
    port: int = 8088
    output_root: Path = Path("/var/lib/fs42stream/hls")
    max_blocks: int = 1000000
    duration_limit: float = 1800.0
    video_encoder: str = "h264_vaapi"
    vaapi_device: str = "/dev/dri/renderD128"


def render_environment_file(config: ServiceConfig) -> str:
    values = {
        "FS42STREAM_CHANNEL": config.channel,
        "FS42STREAM_HOST": config.host,
        "FS42STREAM_PORT": str(config.port),
        "FS42STREAM_OUTPUT_ROOT": str(config.output_root),
        "FS42STREAM_MAX_BLOCKS": str(config.max_blocks),
        "FS42STREAM_DURATION_LIMIT": _num(config.duration_limit),
        "FS42STREAM_VIDEO_ENCODER": config.video_encoder,
        "FS42STREAM_VAAPI_DEVICE": config.vaapi_device,
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
ExecStart={python} -m fs42stream.integrated_runner --channel "${{FS42STREAM_CHANNEL}}" --host ${{FS42STREAM_HOST}} --port ${{FS42STREAM_PORT}} --output-root ${{FS42STREAM_OUTPUT_ROOT}} --max-blocks ${{FS42STREAM_MAX_BLOCKS}} --duration-limit ${{FS42STREAM_DURATION_LIMIT}} --video-encoder ${{FS42STREAM_VIDEO_ENCODER}} --vaapi-device ${{FS42STREAM_VAAPI_DEVICE}}
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""


def _escape_env(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _num(value: float) -> str:
    return (f"{value:.6f}").rstrip("0").rstrip(".")
