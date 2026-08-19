from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, cast

from .api_server import DEFAULT_HOST, DEFAULT_LOGO_FILENAME, DEFAULT_OUTPUT_ROOT, DEFAULT_PORT, channel_slug_for_name, create_server
from .client import DEFAULT_SCHEDULE_BASE_PATH, DEFAULT_SCHEDULE_HOST, DEFAULT_SCHEDULE_PORT, DEFAULT_SCHEDULE_SCHEME, FS42ScheduleClient, build_schedule_api_base_url
from .ffmpeg import AudioNormalization, FFMpegHLSCommandBuilder, PlayoutMode, StreamProfile, normalize_audio_normalization
from .ffprobe import FFProbe
from .live_controller import LiveController, LiveControllerConfig, SharedScheduleBlockClock
from .paths import PathResolver
from .planner import DEFAULT_BRB_IMAGE_PATH, BlockPlanner
from .run_block import DEFAULT_API_BASE_URL, DEFAULT_CHANNEL, DEFAULT_FFMPEG, DEFAULT_FFPROBE, DEFAULT_FS42_ROOT, DEFAULT_SCHEDULE_TIMEZONE, DEFAULT_SDTV_ROOT, BlockRunner
from .systemd_service import ServiceConfig


DEFAULT_MAX_BLOCKS = ServiceConfig.max_blocks
DEFAULT_DURATION_LIMIT = ServiceConfig.duration_limit
DEFAULT_STREAM_PROFILES: tuple[StreamProfile, ...] = ("jellyfin",)


def parse_stream_profiles(value: str) -> tuple[StreamProfile, ...]:
    """Normalize the public CLI/environment stream-profile selection."""
    normalized = value.strip().lower()
    if normalized == "both":
        return ("direct", "jellyfin")
    profiles = tuple(part.strip() for part in normalized.split(",") if part.strip())
    if not profiles:
        raise ValueError("stream profiles must select at least one of: direct, jellyfin, both")
    invalid_profiles = [profile for profile in profiles if profile not in {"direct", "jellyfin"}]
    if invalid_profiles or len(set(profiles)) != len(profiles):
        raise ValueError("stream profiles must be 'direct', 'jellyfin', 'both', or a comma-separated direct,jellyfin list")
    return cast(tuple[StreamProfile, ...], profiles)


def parse_audio_normalization(value: str) -> AudioNormalization:
    """Normalize the public CLI/environment audio-normalization selection."""
    return normalize_audio_normalization(value)


@dataclass(frozen=True)
class IntegratedRunnerConfig:
    channel: str = DEFAULT_CHANNEL
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    output_root: Path = DEFAULT_OUTPUT_ROOT
    max_blocks: int = DEFAULT_MAX_BLOCKS
    duration_limit: float = DEFAULT_DURATION_LIMIT
    dry_run: bool = False
    schedule_scheme: str = DEFAULT_SCHEDULE_SCHEME
    schedule_host: str = DEFAULT_SCHEDULE_HOST
    schedule_port: int = DEFAULT_SCHEDULE_PORT
    schedule_base_path: str = DEFAULT_SCHEDULE_BASE_PATH
    api_base_url: str = ""
    channel_slug: str | None = None
    public_base_url: str | None = None
    logo_filename: str = DEFAULT_LOGO_FILENAME
    fs42_root: Path = Path(DEFAULT_FS42_ROOT)
    sdtv_root: Path = Path(DEFAULT_SDTV_ROOT)
    ffmpeg: str = DEFAULT_FFMPEG
    ffprobe: str = DEFAULT_FFPROBE
    video_encoder: str = "libx264"
    vaapi_device: str | None = None
    schedule_timezone: str | None = DEFAULT_SCHEDULE_TIMEZONE
    stream_profiles: tuple[StreamProfile, ...] = DEFAULT_STREAM_PROFILES
    playout_mode: PlayoutMode = "ts-primary"
    brb_image_path: str | Path = DEFAULT_BRB_IMAGE_PATH
    audio_normalization: AudioNormalization = "off"
    jellyfin_pre_roll_lead_seconds: float = 0.0
    jellyfin_pre_roll_min_buffer_seconds: float = 30.0
    jellyfin_pre_roll_max_publish_delay_seconds: float = 60.0


class IntegratedServer(Protocol):
    server_address: Any

    def serve_forever(self) -> None: ...
    def shutdown(self) -> None: ...
    def server_close(self) -> None: ...


class IntegratedController(Protocol):
    def run(self, config: LiveControllerConfig) -> Mapping[str, Any]: ...


ServerFactory = Callable[..., IntegratedServer]
ControllerFactory = Callable[[], IntegratedController]


def run_integrated(
    config: IntegratedRunnerConfig,
    *,
    server_factory: ServerFactory = create_server,
    controller_factory: ControllerFactory = LiveController,
) -> dict[str, Any]:
    """Run the API server and bounded live controller in one foreground lifecycle."""

    if config.max_blocks <= 0:
        raise ValueError("max_blocks must be positive")
    if config.duration_limit <= 0:
        raise ValueError("duration_limit must be positive")
    audio_normalization = normalize_audio_normalization(config.audio_normalization)
    stream_profiles = tuple(config.stream_profiles)
    if not stream_profiles:
        raise ValueError("at least one stream profile is required")
    invalid_profiles = [profile for profile in stream_profiles if profile not in {"direct", "jellyfin"}]
    if invalid_profiles:
        raise ValueError("stream_profiles must contain only 'direct' or 'jellyfin'")
    if len(set(stream_profiles)) != len(stream_profiles):
        raise ValueError("stream_profiles must not contain duplicates")

    output_root = Path(config.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    status_json = output_root / "status.json"

    _write_status(
        status_json,
        {
            "status": "starting",
            "channel": config.channel,
            "output_root": str(output_root),
            "max_blocks": config.max_blocks,
            "duration_limit": config.duration_limit,
            "stream_profiles": list(stream_profiles),
            "playout_mode": config.playout_mode,
            "audio_normalization": audio_normalization,
            "jellyfin_pre_roll_lead_seconds": config.jellyfin_pre_roll_lead_seconds,
            "jellyfin_pre_roll_min_buffer_seconds": config.jellyfin_pre_roll_min_buffer_seconds,
            "jellyfin_pre_roll_max_publish_delay_seconds": config.jellyfin_pre_roll_max_publish_delay_seconds,
            "schedule_timezone": config.schedule_timezone,
            "brb_image_path": str(config.brb_image_path),
            "updated_at": _utc_now(),
        },
    )

    channel_slug = config.channel_slug or channel_slug_for_name(config.channel)
    server = server_factory(
        host=config.host,
        port=config.port,
        output_root=output_root,
        status_json=status_json,
        api_base_url=_schedule_api_url(config),
        channel_name=config.channel,
        channel_slug=channel_slug,
        public_base_url=config.public_base_url,
        logo_filename=config.logo_filename,
        schedule_timezone=config.schedule_timezone,
    )
    thread = threading.Thread(target=server.serve_forever, name="fs42stream-api", daemon=True)
    thread.start()

    actual_host, actual_port = cast(tuple[str, int], server.server_address)
    running_status = {
        "status": "running",
        "channel": config.channel,
        "output_root": str(output_root),
        "status_json": str(status_json),
        "api_host": actual_host,
        "api_port": actual_port,
        "api_base_url": f"http://{actual_host}:{actual_port}",
        **_api_urls(actual_host, actual_port, channel_slug=channel_slug, stream_profiles=stream_profiles),
        "max_blocks": config.max_blocks,
        "blocks_completed": 0,
        "duration_limit": config.duration_limit,
        "stream_profiles": list(stream_profiles),
        "playout_mode": config.playout_mode,
        "audio_normalization": audio_normalization,
        "jellyfin_pre_roll_lead_seconds": config.jellyfin_pre_roll_lead_seconds,
        "schedule_timezone": config.schedule_timezone,
        "brb_image_path": str(config.brb_image_path),
        "updated_at": _utc_now(),
    }
    _write_status(status_json, running_status)

    try:
        primary_profile = "direct" if "direct" in stream_profiles else stream_profiles[0]

        def write_live_status(update: Mapping[str, Any]) -> None:
            live_status = dict(running_status)
            live_status.update(_normalize_live_status_update(update))
            live_status["stream_profiles"] = list(stream_profiles)
            live_status["updated_at"] = _utc_now()
            _write_status(status_json, live_status)

        results: dict[StreamProfile, dict[str, Any]] = {}
        errors: dict[StreamProfile, BaseException] = {}
        result_lock = threading.Lock()
        shared_schedule_clock = SharedScheduleBlockClock(profile_count=len(stream_profiles), timeout=max(90.0, config.duration_limit * 2.0))

        def run_profile(profile: StreamProfile) -> None:
            try:
                controller = controller_factory()
                profile_result = dict(
                    controller.run(
                        LiveControllerConfig(
                            channel=config.channel,
                            output_root=output_root,
                            max_blocks=config.max_blocks,
                            duration_limit=config.duration_limit,
                            dry_run=config.dry_run,
                            status_callback=write_live_status if profile == primary_profile else None,
                            schedule_timezone=config.schedule_timezone,
                            stream_profile=profile,
                            playout_mode=config.playout_mode,
                            shared_schedule_clock=shared_schedule_clock,
                            brb_image_path=config.brb_image_path,
                            jellyfin_pre_roll_lead_seconds=config.jellyfin_pre_roll_lead_seconds,
                            jellyfin_pre_roll_min_buffer_seconds=config.jellyfin_pre_roll_min_buffer_seconds,
                            jellyfin_pre_roll_max_publish_delay_seconds=config.jellyfin_pre_roll_max_publish_delay_seconds,
                        )
                    )
                )
            except BaseException as exc:
                with result_lock:
                    errors[profile] = exc
                return
            with result_lock:
                results[profile] = profile_result

        profile_threads = [threading.Thread(target=run_profile, args=(profile,), name=f"fs42stream-{profile}") for profile in stream_profiles]
        for profile_thread in profile_threads:
            profile_thread.start()
        for profile_thread in profile_threads:
            profile_thread.join()

        if errors:
            profile, exc = next(iter(errors.items()))
            raise RuntimeError(f"{profile} stream profile failed: {exc}") from exc

        result = dict(results[primary_profile])
        result["stream_profiles"] = list(stream_profiles)
        result["profile_statuses"] = {profile: results[profile] for profile in stream_profiles}
        final_status = dict(_read_status(status_json) or running_status)
        final_status.update(result)
        final_status["status"] = str(result.get("status") or "complete")
        final_status["updated_at"] = _utc_now()
        _write_status(status_json, final_status)
        return final_status
    except Exception as exc:
        error_status = dict(running_status)
        error_status.update({"status": "error", "error_type": type(exc).__name__, "message": str(exc), "updated_at": _utc_now()})
        _write_status(status_json, error_status)
        raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main(
    argv: Sequence[str] | None = None,
    *,
    server_factory: ServerFactory = create_server,
    controller_factory: ControllerFactory = LiveController,
) -> int:
    parser = argparse.ArgumentParser(description="Run integrated foreground FS42 API/HLS server plus bounded live controller.")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--max-blocks", type=int, default=DEFAULT_MAX_BLOCKS)
    parser.add_argument("--duration-limit", type=float, default=DEFAULT_DURATION_LIMIT)
    parser.add_argument("--schedule-scheme", default=DEFAULT_SCHEDULE_SCHEME)
    parser.add_argument("--schedule-host", default=DEFAULT_SCHEDULE_HOST)
    parser.add_argument("--schedule-port", type=int, default=DEFAULT_SCHEDULE_PORT)
    parser.add_argument("--schedule-base-path", default=DEFAULT_SCHEDULE_BASE_PATH)
    parser.add_argument("--api-base-url", default="", help="deprecated full schedule API URL override; prefer --schedule-* options")
    parser.add_argument("--channel-slug", default=None)
    parser.add_argument("--public-base-url", default=None, help="public base URL for IPTV/XMLTV metadata; defaults to the request Host")
    parser.add_argument("--logo-filename", default=DEFAULT_LOGO_FILENAME)
    parser.add_argument("--fs42-root", type=Path, default=Path(DEFAULT_FS42_ROOT))
    parser.add_argument("--sdtv-root", type=Path, default=Path(DEFAULT_SDTV_ROOT))
    parser.add_argument("--ffmpeg", default=DEFAULT_FFMPEG)
    parser.add_argument("--ffprobe", default=DEFAULT_FFPROBE)
    parser.add_argument("--video-encoder", default="libx264", help="video encoder, e.g. libx264 or h264_vaapi")
    parser.add_argument("--vaapi-device", help="VAAPI device path, e.g. /dev/dri/renderD128")
    parser.add_argument("--schedule-timezone", default=DEFAULT_SCHEDULE_TIMEZONE, help="timezone for naive FS42 schedule timestamps, e.g. Europe/London")
    parser.add_argument("--playout-mode", choices=("hls-primary", "ts-primary"), default="ts-primary", help="render directly to HLS or render TS first then package HLS")
    parser.add_argument("--stream-profiles", default=os.environ.get("FS42STREAM_STREAM_PROFILES", "jellyfin"), help="active output profiles: jellyfin (default), direct, both, or a comma-separated list")
    parser.add_argument("--brb-image-path", default=os.environ.get("FS42STREAM_BRB_IMAGE_PATH", str(DEFAULT_BRB_IMAGE_PATH)), help="fallback BRB image path relative to FS42 root or absolute under an allowed media root")
    parser.add_argument("--audio-normalization", type=parse_audio_normalization, default=os.environ.get("FS42STREAM_AUDIO_NORMALIZATION", "off"), metavar="{off,loudnorm}", help="audio normalization mode; default off")
    parser.add_argument("--jellyfin-pre-roll-lead-seconds", type=float, default=float(os.environ.get("FS42STREAM_JELLYFIN_PRE_ROLL_LEAD_SECONDS", "0")), help="private Jellyfin next-block staging lead time; zero disables it")
    parser.add_argument("--jellyfin-pre-roll-min-buffer-seconds", type=float, default=float(os.environ.get("FS42STREAM_JELLYFIN_PRE_ROLL_MIN_BUFFER_SECONDS", "30")), help="minimum staged Jellyfin duration required before a public boundary switch")
    parser.add_argument("--jellyfin-pre-roll-max-publish-delay-seconds", type=float, default=float(os.environ.get("FS42STREAM_JELLYFIN_PRE_ROLL_MAX_PUBLISH_DELAY_SECONDS", "60")), help="maximum filler-backed delay while waiting for the staged Jellyfin safe buffer")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    config = IntegratedRunnerConfig(
        channel=args.channel,
        host=args.host,
        port=args.port,
        output_root=args.output_root,
        max_blocks=args.max_blocks,
        duration_limit=args.duration_limit,
        dry_run=args.dry_run,
        schedule_scheme=args.schedule_scheme,
        schedule_host=args.schedule_host,
        schedule_port=args.schedule_port,
        schedule_base_path=args.schedule_base_path,
        api_base_url=args.api_base_url,
        channel_slug=args.channel_slug,
        public_base_url=args.public_base_url,
        logo_filename=args.logo_filename,
        fs42_root=args.fs42_root,
        sdtv_root=args.sdtv_root,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        video_encoder=args.video_encoder,
        vaapi_device=args.vaapi_device,
        schedule_timezone=args.schedule_timezone,
        playout_mode=args.playout_mode,
        stream_profiles=parse_stream_profiles(args.stream_profiles),
        brb_image_path=args.brb_image_path,
        audio_normalization=args.audio_normalization,
        jellyfin_pre_roll_lead_seconds=args.jellyfin_pre_roll_lead_seconds,
        jellyfin_pre_roll_min_buffer_seconds=args.jellyfin_pre_roll_min_buffer_seconds,
        jellyfin_pre_roll_max_publish_delay_seconds=args.jellyfin_pre_roll_max_publish_delay_seconds,
    )
    effective_controller_factory = controller_factory
    if controller_factory is LiveController:
        effective_controller_factory = lambda: _create_controller(config)
    try:
        result = run_integrated(config, server_factory=server_factory, controller_factory=effective_controller_factory)
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "message": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _create_controller(config: IntegratedRunnerConfig) -> LiveController:
    schedule_api_url = _schedule_api_url(config)
    schedule_client = FS42ScheduleClient(schedule_api_url)
    block_runner = BlockRunner(
        client=FS42ScheduleClient(schedule_api_url),
        planner=BlockPlanner(
            PathResolver(fs42_root=config.fs42_root, sdtv_root=config.sdtv_root),
            FFProbe(config.ffprobe),
            brb_image_path=config.brb_image_path,
        ),
        builder=FFMpegHLSCommandBuilder(
            config.ffmpeg,
            video_encoder=config.video_encoder,
            vaapi_device=config.vaapi_device,
            audio_normalization=config.audio_normalization,
        ),
    )
    return LiveController(schedule_client=schedule_client, block_runner=block_runner)


def _schedule_api_url(config: IntegratedRunnerConfig) -> str:
    if config.api_base_url:
        return config.api_base_url
    return build_schedule_api_base_url(
        scheme=config.schedule_scheme,
        host=config.schedule_host,
        port=config.schedule_port,
        base_path=config.schedule_base_path,
    )


def _normalize_live_status_update(update: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(update)
    supervisor = normalized.get("supervisor")
    if not isinstance(supervisor, Mapping):
        return normalized
    normalized["supervisor"] = dict(supervisor)
    normalized["schedule_now"] = supervisor.get("schedule_now", normalized.get("schedule_now"))
    normalized["active_block"] = dict(supervisor.get("current_block")) if isinstance(supervisor.get("current_block"), Mapping) else normalized.get("active_block")
    upcoming_blocks = supervisor.get("upcoming_blocks")
    if isinstance(upcoming_blocks, list):
        normalized["upcoming_blocks"] = [dict(block) for block in upcoming_blocks if isinstance(block, Mapping)]
    elif isinstance(supervisor.get("next_block"), Mapping):
        normalized["upcoming_blocks"] = [dict(supervisor["next_block"])]
    normalized["current_plan_item"] = dict(supervisor.get("current_item")) if isinstance(supervisor.get("current_item"), Mapping) else normalized.get("current_plan_item")
    normalized["next_plan_item"] = dict(supervisor.get("next_item")) if isinstance(supervisor.get("next_item"), Mapping) else normalized.get("next_plan_item")
    if isinstance(supervisor.get("catch_up"), Mapping):
        normalized["catch_up"] = dict(supervisor["catch_up"])
    if not isinstance(normalized.get("playout"), Mapping) and isinstance(supervisor.get("playout"), Mapping):
        normalized["playout"] = dict(supervisor["playout"])
    return normalized


def _api_urls(host: str, port: int, *, channel_slug: str, stream_profiles: Sequence[StreamProfile]) -> dict[str, str]:
    base_url = f"http://{host}:{port}"
    urls = {
        "status_url": f"{base_url}/api/channels/{channel_slug}/status",
        "schedule_url": f"{base_url}/api/channels/{channel_slug}/schedule",
        "runtime_url": f"{base_url}/api/channels/{channel_slug}/runtime",
        "health_url": f"{base_url}/api/channels/{channel_slug}/health",
        "events_url": f"{base_url}/api/channels/{channel_slug}/events",
        "epg_url": f"{base_url}/api/channels/{channel_slug}/epg",
        "hls_url": f"{base_url}/hls/{channel_slug}/",
        "xmltv_url": f"{base_url}/iptv/xmltv.xml",
    }
    if "direct" in stream_profiles:
        urls.update({"hls_playlist_url": f"{base_url}/hls/{channel_slug}/{channel_slug}.m3u8", "iptv_url": f"{base_url}/iptv/channels.m3u"})
    if "jellyfin" in stream_profiles:
        urls.update({"jellyfin_hls_playlist_url": f"{base_url}/hls/{channel_slug}/jellyfin/{channel_slug}.m3u8", "jellyfin_iptv_url": f"{base_url}/iptv/jellyfin/channels.m3u"})
    return urls


def _write_status(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_status(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, Mapping):
        return dict(payload)
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
