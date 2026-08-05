from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fs42stream.integrated_runner import parse_audio_normalization, parse_stream_profiles
from fs42stream.systemd_service import ServiceConfig, render_environment_file, render_unit_file


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the FS42-Stream systemd service files.")
    parser.add_argument("--service-name", default="fs42stream")
    parser.add_argument("--user", default="fs42stream")
    parser.add_argument("--install-dir", type=Path, default=Path("/opt/fs42stream"))
    parser.add_argument("--env-file", type=Path, default=Path("/etc/fs42stream/fs42stream.env"))
    parser.add_argument("--unit-file", type=Path, default=Path("/etc/systemd/system/fs42stream.service"))
    parser.add_argument("--channel", default="Example Channel")
    parser.add_argument("--channel-slug", default="Example_Channel")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--output-root", type=Path, default=Path("/var/lib/fs42stream/hls"))
    parser.add_argument("--max-blocks", type=int, default=1000000)
    parser.add_argument("--duration-limit", type=float, default=7200.0)
    parser.add_argument("--video-encoder", default="h264_vaapi")
    parser.add_argument("--vaapi-device", default="/dev/dri/renderD128")
    parser.add_argument("--schedule-timezone", default="Europe/London")
    parser.add_argument("--playout-mode", choices=("hls-primary", "ts-primary"), default="ts-primary")
    parser.add_argument("--schedule-scheme", default="http")
    parser.add_argument("--schedule-host", default="127.0.0.1")
    parser.add_argument("--schedule-port", type=int, default=4242)
    parser.add_argument("--schedule-base-path", default="")
    parser.add_argument("--api-base-url", default="", help="deprecated full schedule API URL override; prefer split schedule variables")
    parser.add_argument("--public-base-url", default="")
    parser.add_argument("--logo-filename", default="logo.png")
    parser.add_argument("--stream-profiles", default="jellyfin", help="active profiles: jellyfin (default), direct, both, or direct,jellyfin")
    parser.add_argument("--brb-image-path", default="runtime/brb.png", help="fallback BRB image path relative to FS42 root or absolute under an allowed media root")
    parser.add_argument("--audio-normalization", default="off", help="audio normalization mode: off (default) or loudnorm")
    parser.add_argument("--jellyfin-pre-roll-lead-seconds", type=float, default=0.0, help="private Jellyfin next-block staging lead time; zero disables it")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-systemctl", action="store_true", help="write files but do not run systemctl daemon-reload/enable/restart")
    args = parser.parse_args(argv)
    try:
        parse_stream_profiles(args.stream_profiles)
        parse_audio_normalization(args.audio_normalization)
    except ValueError as exc:
        parser.error(str(exc))

    config = ServiceConfig(
        service_name=args.service_name,
        user=args.user,
        install_dir=args.install_dir,
        env_file=args.env_file,
        channel=args.channel,
        channel_slug=args.channel_slug,
        host=args.host,
        port=args.port,
        output_root=args.output_root,
        max_blocks=args.max_blocks,
        duration_limit=args.duration_limit,
        video_encoder=args.video_encoder,
        vaapi_device=args.vaapi_device,
        schedule_timezone=args.schedule_timezone,
        playout_mode=args.playout_mode,
        schedule_scheme=args.schedule_scheme,
        schedule_host=args.schedule_host,
        schedule_port=args.schedule_port,
        schedule_base_path=args.schedule_base_path,
        api_base_url=args.api_base_url,
        public_base_url=args.public_base_url,
        logo_filename=args.logo_filename,
        stream_profiles=args.stream_profiles,
        brb_image_path=args.brb_image_path,
        audio_normalization=args.audio_normalization,
        jellyfin_pre_roll_lead_seconds=args.jellyfin_pre_roll_lead_seconds,
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
