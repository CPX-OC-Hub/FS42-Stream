from __future__ import annotations

from pathlib import Path


class PathResolver:
    """Resolve FS42 schedule paths into canonical media roots only."""

    def __init__(self, *, fs42_root: str | Path = "/mnt/fs42", sdtv_root: str | Path = "/mnt/media/SDTV") -> None:
        self.fs42_root = Path(fs42_root)
        self.sdtv_root = Path(sdtv_root)
        self._roots = (self.fs42_root, self.sdtv_root)

    def resolve(self, raw_path: str | Path) -> Path:
        if raw_path is None or str(raw_path) == "":
            raise ValueError("media path is empty")
        path = Path(str(raw_path))
        candidate = path if path.is_absolute() else self.fs42_root / path
        normalized = self._normalize(candidate)
        if not any(self._is_relative_to(normalized, root) for root in self._roots):
            raise ValueError(f"path is outside allowed media roots: {raw_path}")
        return normalized

    @staticmethod
    def _normalize(path: Path) -> Path:
        # Pure lexical normalization; strict=False avoids requiring files to exist in tests/planning.
        return path.resolve(strict=False)

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            return False
