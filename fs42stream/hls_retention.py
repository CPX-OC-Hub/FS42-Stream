from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence
from urllib.parse import unquote


@dataclass(frozen=True)
class HLSRetentionConfig:
    output_root: Path
    channel_slug: str = "Example_Channel"
    max_age_seconds: float = 6 * 60 * 60
    max_segments_per_dir: int = 7200
    dry_run: bool = False


@dataclass(frozen=True)
class HLSRetentionResult:
    scanned_dirs: tuple[Path, ...]
    removed: list[Path]
    protected: tuple[Path, ...]
    bytes_removed: int
    dry_run: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "scanned_dirs": [str(path) for path in self.scanned_dirs],
            "removed": [str(path) for path in self.removed],
            "protected": [str(path) for path in self.protected],
            "removed_count": len(self.removed),
            "bytes_removed": self.bytes_removed,
            "dry_run": self.dry_run,
        }


def cleanup_hls_retention(config: HLSRetentionConfig) -> HLSRetentionResult:
    """Prune stale HLS .ts segments while preserving playlists/current live windows.

    Only the configured channel directory and its Jellyfin subdirectory are scanned.
    Playlists and non-HLS assets are never deleted by this rolling retention pass.
    """

    channel_slug = _validate_channel_slug(config.channel_slug)
    if config.max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")
    if config.max_segments_per_dir < 0:
        raise ValueError("max_segments_per_dir must be non-negative")

    output_root = Path(config.output_root).resolve(strict=False)
    channel_dir = (output_root / channel_slug).resolve(strict=False)
    _ensure_child(channel_dir, output_root)
    scan_dirs = tuple(path for path in (channel_dir, channel_dir / "jellyfin") if path.is_dir())
    now = time.time()
    removed: list[Path] = []
    protected: set[Path] = set()
    bytes_removed = 0

    for directory in scan_dirs:
        root = directory.resolve(strict=False)
        dir_protected = _protected_segments(root)
        protected.update(dir_protected)
        segments = [path for path in sorted(root.iterdir()) if _safe_hls_segment(path, root)]
        newest_allowed = set(_newest_segments(segments, limit=config.max_segments_per_dir))
        for segment in segments:
            resolved_segment = segment.resolve(strict=False)
            if resolved_segment in dir_protected:
                continue
            too_old = now - segment.stat().st_mtime > config.max_age_seconds
            over_count = resolved_segment not in newest_allowed
            if not (too_old or over_count):
                continue
            try:
                size = segment.stat().st_size
            except OSError:
                size = 0
            if not config.dry_run:
                segment.unlink()
            removed.append(segment)
            bytes_removed += size

    return HLSRetentionResult(scanned_dirs=scan_dirs, removed=removed, protected=tuple(sorted(protected)), bytes_removed=bytes_removed, dry_run=config.dry_run)


def _validate_channel_slug(value: str) -> str:
    if not value or value.startswith("/") or "\\" in value:
        raise ValueError("channel_slug must be a single safe path component")
    pure = PurePosixPath(value)
    if pure.is_absolute() or len(pure.parts) != 1 or pure.parts[0] in {".", ".."}:
        raise ValueError("channel_slug must be a single safe path component")
    return pure.parts[0]


def _ensure_child(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"refusing to operate outside output root: {path}") from exc


def _safe_hls_segment(path: Path, root: Path) -> bool:
    if path.suffix != ".ts" or not path.is_file() or path.is_symlink():
        return False
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"refusing to remove HLS segment outside channel directory: {path}") from exc
    return True


def _protected_segments(directory: Path) -> set[Path]:
    protected: set[Path] = set()
    for playlist in sorted(directory.glob("*.m3u8")):
        if not playlist.is_file() or playlist.is_symlink():
            continue
        try:
            lines = playlist.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            candidate = _safe_playlist_reference(directory, text)
            if candidate is not None and candidate.suffix == ".ts" and candidate.is_file():
                protected.add(candidate.resolve(strict=False))
    return protected


def _safe_playlist_reference(directory: Path, text: str) -> Path | None:
    decoded = unquote(text)
    if decoded.startswith("/") or "\x00" in decoded or "//" in decoded:
        return None
    pure = PurePosixPath(decoded)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    candidate = (directory / Path(*pure.parts)).resolve(strict=False)
    try:
        candidate.relative_to(directory)
    except ValueError:
        return None
    return candidate


def _newest_segments(segments: Sequence[Path], *, limit: int) -> list[Path]:
    if limit <= 0:
        return []
    return [path.resolve(strict=False) for path in sorted(segments, key=lambda path: (path.stat().st_mtime, path.name), reverse=True)[:limit]]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune stale FS42-Stream HLS segments while preserving active playlist windows.")
    parser.add_argument("--output-root", type=Path, default=Path("/var/lib/fs42stream/hls"))
    parser.add_argument("--channel-slug", default="Example_Channel")
    parser.add_argument("--max-age-seconds", type=float, default=6 * 60 * 60)
    parser.add_argument("--max-segments-per-dir", type=int, default=7200)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = cleanup_hls_retention(
        HLSRetentionConfig(
            output_root=args.output_root,
            channel_slug=args.channel_slug,
            max_age_seconds=args.max_age_seconds,
            max_segments_per_dir=args.max_segments_per_dir,
            dry_run=args.dry_run,
        )
    )
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
