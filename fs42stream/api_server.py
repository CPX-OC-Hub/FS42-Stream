from __future__ import annotations

import argparse
import json
import mimetypes
import posixpath
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence, cast
from urllib.parse import unquote, urlsplit


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8088
DEFAULT_OUTPUT_ROOT = Path("/tmp/fs42stream-live")
DEFAULT_CHANNEL_NAME = "Sky One"
DEFAULT_CHANNEL_SLUG = "Sky_One"


@dataclass(frozen=True)
class APIServerConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    output_root: Path = DEFAULT_OUTPUT_ROOT
    status_json: Path | None = None


class FS42APIHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], RequestHandlerClass: type[BaseHTTPRequestHandler], config: APIServerConfig):
        super().__init__(server_address, RequestHandlerClass)
        self.config = config
        self.output_root = config.output_root.resolve(strict=False)
        self.status_json = (config.status_json or (config.output_root / "status.json")).resolve(strict=False)


class FS42APIRequestHandler(BaseHTTPRequestHandler):
    def _api_server(self) -> FS42APIHTTPServer:
        return cast(FS42APIHTTPServer, self.server)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlsplit(self.path)
        request_path = parsed.path
        if request_path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "fs42stream-api",
                    "channel": DEFAULT_CHANNEL_NAME,
                    "output_root": str(self._api_server().output_root),
                },
            )
            return

        if request_path == "/api/channels":
            self._send_json(
                HTTPStatus.OK,
                {
                    "channels": [
                        {
                            "name": DEFAULT_CHANNEL_NAME,
                            "slug": DEFAULT_CHANNEL_SLUG,
                            "status_url": f"/api/channels/{DEFAULT_CHANNEL_SLUG}/status",
                            "schedule_url": f"/api/channels/{DEFAULT_CHANNEL_SLUG}/schedule",
                            "hls_url": f"/hls/{DEFAULT_CHANNEL_SLUG}/",
                        }
                    ]
                },
            )
            return

        if request_path.startswith("/api/channels/") and request_path.endswith("/status"):
            self._handle_channel_status(request_path)
            return

        if request_path.startswith("/api/channels/") and request_path.endswith("/schedule"):
            self._handle_channel_schedule(request_path)
            return

        if request_path.startswith("/hls/"):
            self._handle_hls(request_path)
            return

        self._send_error(HTTPStatus.NOT_FOUND, "not_found", f"no route for {request_path}")

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "only GET is supported")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "only GET is supported")

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "only GET is supported")

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "only GET is supported")

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle_channel_status(self, request_path: str) -> None:
        channel_slug = self._validated_channel_slug(request_path, expected_leaf="status")
        if channel_slug is None:
            return
        payload = self._read_status_payload()
        if payload is None:
            return
        self._send_json(HTTPStatus.OK, payload)

    def _handle_channel_schedule(self, request_path: str) -> None:
        channel_slug = self._validated_channel_slug(request_path, expected_leaf="schedule")
        if channel_slug is None:
            return
        status_payload = self._read_status_payload()
        if status_payload is None:
            return
        self._send_json(HTTPStatus.OK, _derived_schedule_payload(status_payload, channel_slug=channel_slug))

    def _validated_channel_slug(self, request_path: str, *, expected_leaf: str) -> str | None:
        parts = request_path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "channels"] or parts[3] != expected_leaf:
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", f"no route for {request_path}")
            return None
        channel_slug = unquote(parts[2])
        if channel_slug != DEFAULT_CHANNEL_SLUG:
            self._send_error(HTTPStatus.NOT_FOUND, "channel_not_found", f"unknown channel: {channel_slug}")
            return None
        return channel_slug

    def _read_status_payload(self) -> Mapping[str, Any] | None:
        status_path = self._api_server().status_json
        if not status_path.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, "status_not_found", f"status JSON does not exist: {status_path}")
            return None
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self._send_error(HTTPStatus.BAD_GATEWAY, "invalid_status_json", f"status JSON is invalid: {exc.msg}")
            return None
        except OSError as exc:
            self._send_error(HTTPStatus.BAD_GATEWAY, "status_unreadable", str(exc))
            return None
        if not isinstance(payload, Mapping):
            self._send_error(HTTPStatus.BAD_GATEWAY, "invalid_status_json", "status JSON must be an object")
            return None
        return payload

    def _handle_hls(self, request_path: str) -> None:
        prefix = f"/hls/{DEFAULT_CHANNEL_SLUG}/"
        if request_path == f"/hls/{DEFAULT_CHANNEL_SLUG}":
            self._send_error(HTTPStatus.BAD_REQUEST, "unsafe_hls_path", "HLS file path is required")
            return
        if not request_path.startswith(prefix):
            slug = unquote(request_path.removeprefix("/hls/").split("/", 1)[0])
            self._send_error(HTTPStatus.NOT_FOUND, "channel_not_found", f"unknown channel: {slug}")
            return

        raw_relative = request_path[len(prefix) :]
        safe_relative = _safe_hls_relative_path(raw_relative)
        if safe_relative is None:
            self._send_error(HTTPStatus.BAD_REQUEST, "unsafe_hls_path", "HLS path must stay under the channel output directory")
            return

        channel_root = (self._api_server().output_root / DEFAULT_CHANNEL_SLUG).resolve(strict=False)
        candidate = (channel_root / safe_relative).resolve(strict=False)
        try:
            candidate.relative_to(channel_root)
        except ValueError:
            self._send_error(HTTPStatus.BAD_REQUEST, "unsafe_hls_path", "HLS path resolves outside the channel output directory")
            return

        if candidate.suffix not in {".m3u8", ".ts"}:
            self._send_error(HTTPStatus.NOT_FOUND, "hls_not_found", "only .m3u8 playlists and .ts segments are served")
            return
        if not candidate.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, "hls_not_found", f"HLS file not found: {safe_relative}")
            return

        try:
            body = candidate.read_bytes()
        except OSError as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "hls_unreadable", str(exc))
            return

        content_type = _hls_content_type(candidate)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: Mapping[str, Any] | list[Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, error: str, message: str) -> None:
        self._send_json(status, {"error": error, "message": message, "status": int(status)})


def _safe_hls_relative_path(raw_relative: str) -> Path | None:
    if not raw_relative or raw_relative.startswith("/") or "//" in raw_relative:
        return None
    decoded = unquote(raw_relative)
    if decoded.startswith("/") or "\x00" in decoded or "//" in decoded:
        return None
    normalized = posixpath.normpath(decoded)
    if normalized in {".", ""}:
        return None
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    if normalized != decoded:
        return None
    return Path(*pure.parts)


def _derived_schedule_payload(status_payload: Mapping[str, Any], *, channel_slug: str) -> dict[str, Any]:
    events = status_payload.get("events")
    event_list = [event for event in events if isinstance(event, Mapping)] if isinstance(events, list) else []
    block_events: list[dict[str, Any]] = []
    for event in event_list:
        block = event.get("block")
        if not isinstance(block, Mapping):
            continue
        block_events.append({
            "event": event.get("event"),
            "block_number": event.get("block_number"),
            "block": dict(block),
            "plan_item_count": event.get("plan_item_count"),
            "playlist": event.get("playlist"),
        })

    active_event = _last_block_start_event(block_events) or (block_events[-1] if block_events else None)
    active_index = _block_identity(active_event.get("block")) if active_event else None
    previous_blocks: list[dict[str, Any]] = []
    upcoming_blocks: list[dict[str, Any]] = []
    seen_previous: set[tuple[Any, Any]] = set()
    seen_upcoming: set[tuple[Any, Any]] = set()
    for item in block_events:
        block = item.get("block")
        if not isinstance(block, Mapping):
            continue
        identity = _block_identity(block)
        normalized = dict(block)
        if active_index is not None and identity == active_index:
            continue
        if active_event is not None and item is active_event:
            continue
        if active_index is not None and _block_sort_key(identity) < _block_sort_key(active_index):
            if identity not in seen_previous:
                previous_blocks.append(normalized)
                seen_previous.add(identity)
        elif identity not in seen_upcoming:
            upcoming_blocks.append(normalized)
            seen_upcoming.add(identity)

    playlist = status_payload.get("playlist")
    if not playlist:
        raw_hls = status_payload.get("hls")
        if isinstance(raw_hls, Mapping):
            playlist = raw_hls.get("playlist")

    raw_active_block = status_payload.get("active_block")
    if isinstance(raw_active_block, Mapping):
        active_block = dict(raw_active_block)
    else:
        active_block = dict(active_event.get("block")) if active_event and isinstance(active_event.get("block"), Mapping) else None
    raw_upcoming_blocks = status_payload.get("upcoming_blocks")
    if isinstance(raw_upcoming_blocks, list):
        upcoming_blocks = [dict(block) for block in raw_upcoming_blocks if isinstance(block, Mapping)]
    return {
        "source": "fs42stream-status",
        "channel": status_payload.get("channel") or DEFAULT_CHANNEL_NAME,
        "slug": channel_slug,
        "status": status_payload.get("status"),
        "active_block": active_block,
        "previous_blocks": previous_blocks,
        "upcoming_blocks": upcoming_blocks,
        "recent_events": block_events[-10:],
        "hls": {
            "playlist": playlist,
            "channel_output_dir": status_payload.get("channel_output_dir"),
            "output_root": status_payload.get("output_root"),
        },
    }


def _last_block_start_event(block_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(block_events):
        if event.get("event") == "block_start":
            return event
    return None


def _block_identity(block: Any) -> tuple[Any, Any]:
    if isinstance(block, Mapping):
        return block.get("index"), block.get("start_time")
    return None, None


def _block_sort_key(identity: tuple[Any, Any]) -> tuple[int, str]:
    index, start_time = identity
    if isinstance(index, int):
        return index, str(start_time or "")
    return 10**9, str(start_time or "")


def _hls_content_type(path: Path) -> str:
    if path.suffix == ".m3u8":
        return "application/vnd.apple.mpegurl"
    if path.suffix == ".ts":
        return "video/mp2t"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def create_server(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, output_root: Path | str = DEFAULT_OUTPUT_ROOT, status_json: Path | str | None = None) -> FS42APIHTTPServer:
    output_root_path = Path(output_root)
    status_json_path = Path(status_json) if status_json is not None else None
    config = APIServerConfig(host=host, port=port, output_root=output_root_path, status_json=status_json_path)
    output_root_path.mkdir(parents=True, exist_ok=True)
    return FS42APIHTTPServer((host, port), FS42APIRequestHandler, config)


def main(argv: Sequence[str] | None = None, *, server_factory: Callable[..., FS42APIHTTPServer] = create_server) -> int:
    parser = argparse.ArgumentParser(description="Run the foreground FS42-Stream API/status/HLS server.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--status-json", type=Path, default=None)
    args = parser.parse_args(argv)

    server = server_factory(host=args.host, port=args.port, output_root=args.output_root, status_json=args.status_json)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
