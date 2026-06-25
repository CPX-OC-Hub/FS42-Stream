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
from .live_controller import LiveController, LiveControllerConfig
from .run_block import DEFAULT_CHANNEL


@dataclass(frozen=True)
class IntegratedRunnerConfig:
    channel: str = DEFAULT_CHANNEL
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    output_root: Path = DEFAULT_OUTPUT_ROOT
    max_blocks: int = 2
    duration_limit: float = 10.0
    dry_run: bool = False


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
        "status_url": f"http://{actual_host}:{actual_port}/api/channels/Sky_One/status",
        "hls_url": f"http://{actual_host}:{actual_port}/hls/Sky_One/",
        "max_blocks": config.max_blocks,
        "blocks_completed": 0,
        "duration_limit": config.duration_limit,
        "updated_at": _utc_now(),
    }
    _write_status(status_json, running_status)

    try:
        controller = controller_factory()
        result = dict(
            controller.run(
                LiveControllerConfig(
                    channel=config.channel,
                    output_root=output_root,
                    max_blocks=config.max_blocks,
                    duration_limit=config.duration_limit,
                    dry_run=config.dry_run,
                )
            )
        )
        final_status = dict(running_status)
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
    parser.add_argument("--max-blocks", type=int, default=2)
    parser.add_argument("--duration-limit", type=float, default=10.0)
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
    )
    try:
        result = run_integrated(config, server_factory=server_factory, controller_factory=controller_factory)
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "message": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _write_status(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
