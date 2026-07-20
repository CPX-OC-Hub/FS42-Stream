from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fs42stream.systemd_service import ServiceConfig, render_environment_file, render_unit_file


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the FS42-Stream systemd service files.")
    parser.add_argument("--service-name", default="fs42stream")
    parser.add_argument("--user", default="hermes-admin")
    parser.add_argument("--install-dir", type=Path, default=Path("/opt/fs42stream"))
    parser.add_argument("--env-file", type=Path, default=Path("/etc/fs42stream/fs42stream.env"))
    parser.add_argument("--unit-file", type=Path, default=Path("/etc/systemd/system/fs42stream.service"))
    parser.add_argument("--channel", default="Sky One")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--output-root", type=Path, default=Path("/var/lib/fs42stream/hls"))
    parser.add_argument("--max-blocks", type=int, default=1000000)
    parser.add_argument("--duration-limit", type=float, default=7200.0)
    parser.add_argument("--video-encoder", default="h264_vaapi")
    parser.add_argument("--vaapi-device", default="/dev/dri/renderD128")
    parser.add_argument("--schedule-timezone", default="Europe/London")
    parser.add_argument("--playout-mode", choices=("hls-primary", "ts-primary"), default="ts-primary")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-systemctl", action="store_true", help="write files but do not run systemctl daemon-reload/enable/restart")
    args = parser.parse_args(argv)

    config = ServiceConfig(
        service_name=args.service_name,
        user=args.user,
        install_dir=args.install_dir,
        env_file=args.env_file,
        channel=args.channel,
        host=args.host,
        port=args.port,
        output_root=args.output_root,
        max_blocks=args.max_blocks,
        duration_limit=args.duration_limit,
        video_encoder=args.video_encoder,
        vaapi_device=args.vaapi_device,
        schedule_timezone=args.schedule_timezone,
        playout_mode=args.playout_mode,
    )
    env_text = render_environment_file(config)
    unit_text = render_unit_file(config)

    if args.dry_run:
        print("DRY_RUN environment file:")
        print(env_text)
        print("DRY_RUN unit file:")
        print(unit_text)
        return 0

    args.env_file.parent.mkdir(parents=True, exist_ok=True)
    args.unit_file.parent.mkdir(parents=True, exist_ok=True)
    args.output_root.mkdir(parents=True, exist_ok=True)
    args.env_file.write_text(env_text, encoding="utf-8")
    args.unit_file.write_text(unit_text, encoding="utf-8")

    if not args.skip_systemctl:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", args.service_name], check=True)
        subprocess.run(["systemctl", "restart", args.service_name], check=True)

    print(f"wrote {args.env_file}")
    print(f"wrote {args.unit_file}")
    if args.skip_systemctl:
        print("skipped systemctl")
    else:
        print(f"restarted {args.service_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
