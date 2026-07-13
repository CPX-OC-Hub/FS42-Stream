from __future__ import annotations

import argparse
import json
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, cast

from .api_server import DEFAULT_HOST, DEFAULT_OUTPUT_ROOT, DEFAULT_PORT, create_server
from .client import FS42ScheduleClient
from .ffmpeg import FFMpegHLSCommandBuilder, PlayoutMode, StreamProfile
from .ffprobe import FFProbe
from .live_controller import LiveController, LiveControllerConfig
from .paths import PathResolver
from .planner import BlockPlanner
from .run_block import DEFAULT_API_BASE_URL, DEFAULT_CHANNEL, DEFAULT_FFMPEG, DEFAULT_FFPROBE, DEFAULT_FS42_ROOT, DEFAULT_SCHEDULE_TIMEZONE, DEFAULT_SDTV_ROOT, BlockRunner
from .systemd_service import ServiceConfig


DEFAULT_MAX_BLOCKS = ServiceConfig.max_blocks
DEFAULT_DURATION_LIMIT = ServiceConfig.duration_limit


@dataclass(frozen=True)
class IntegratedRunnerConfig:
    channel: str = DEFAULT_CHANNEL
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    output_root: Path = DEFAULT_OUTPUT_ROOT
    max_blocks: int = DEFAULT_MAX_BLOCKS
    duration_limit: float = DEFAULT_DURATION_LIMIT
    dry_run: bool = False
    api_base_url: str = DEFAULT_API_BASE_URL
    fs42_root: Path = Path(DEFAULT_FS42_ROOT)
    sdtv_root: Path = Path(DEFAULT_SDTV_ROOT)
    ffmpeg: str = DEFAULT_FFMPEG
    ffprobe: str = DEFAULT_FFPROBE
    video_encoder: str = "libx264"
    vaapi_device: str | None = None
    schedule_timezone: str | None = DEFAULT_SCHEDULE_TIMEZONE
    stream_profiles: tuple[StreamProfile, ...] = ("direct", "jellyfin")
    playout_mode: PlayoutMode = "hls-primary"


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
            "schedule_timezone": config.schedule_timezone,
            "updated_at": _utc_now(),
        },
    )

    server = server_factory(host=config.host, port=config.port, output_root=output_root, status_json=status_json)
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
        **_api_urls(actual_host, actual_port),
        "max_blocks": config.max_blocks,
        "blocks_completed": 0,
        "duration_limit": config.duration_limit,
        "stream_profiles": list(stream_profiles),
        "playout_mode": config.playout_mode,
        "schedule_timezone": config.schedule_timezone,
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
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    parser.add_argument("--fs42-root", type=Path, default=Path(DEFAULT_FS42_ROOT))
    parser.add_argument("--sdtv-root", type=Path, default=Path(DEFAULT_SDTV_ROOT))
    parser.add_argument("--ffmpeg", default=DEFAULT_FFMPEG)
    parser.add_argument("--ffprobe", default=DEFAULT_FFPROBE)
    parser.add_argument("--video-encoder", default="libx264", help="video encoder, e.g. libx264 or h264_vaapi")
    parser.add_argument("--vaapi-device", help="VAAPI device path, e.g. /dev/dri/renderD128")
    parser.add_argument("--schedule-timezone", default=DEFAULT_SCHEDULE_TIMEZONE, help="timezone for naive FS42 schedule timestamps, e.g. Europe/London")
    parser.add_argument("--playout-mode", choices=("hls-primary", "ts-primary"), default="hls-primary", help="render directly to HLS or render TS first then package HLS")
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
        api_base_url=args.api_base_url,
        fs42_root=args.fs42_root,
        sdtv_root=args.sdtv_root,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        video_encoder=args.video_encoder,
        vaapi_device=args.vaapi_device,
        schedule_timezone=args.schedule_timezone,
        playout_mode=args.playout_mode,
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
    schedule_client = FS42ScheduleClient(config.api_base_url)
    block_runner = BlockRunner(
        client=FS42ScheduleClient(config.api_base_url),
        planner=BlockPlanner(
            PathResolver(fs42_root=config.fs42_root, sdtv_root=config.sdtv_root),
            FFProbe(config.ffprobe),
        ),
        builder=FFMpegHLSCommandBuilder(
            config.ffmpeg,
            video_encoder=config.video_encoder,
            vaapi_device=config.vaapi_device,
        ),
    )
    return LiveController(schedule_client=schedule_client, block_runner=block_runner)


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


def _api_urls(host: str, port: int) -> dict[str, str]:
    base_url = f"http://{host}:{port}"
    return {
        "status_url": f"{base_url}/api/channels/Sky_One/status",
        "schedule_url": f"{base_url}/api/channels/Sky_One/schedule",
        "runtime_url": f"{base_url}/api/channels/Sky_One/runtime",
        "health_url": f"{base_url}/api/channels/Sky_One/health",
        "events_url": f"{base_url}/api/channels/Sky_One/events",
        "epg_url": f"{base_url}/api/channels/Sky_One/epg",
        "hls_url": f"{base_url}/hls/Sky_One/",
        "hls_playlist_url": f"{base_url}/hls/Sky_One/Sky_One.m3u8",
        "jellyfin_hls_playlist_url": f"{base_url}/hls/Sky_One/jellyfin/Sky_One.m3u8",
        "iptv_url": f"{base_url}/iptv/channels.m3u",
        "jellyfin_iptv_url": f"{base_url}/iptv/jellyfin/channels.m3u",
        "xmltv_url": f"{base_url}/iptv/xmltv.xml",
    }


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
