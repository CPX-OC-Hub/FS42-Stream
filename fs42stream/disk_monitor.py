from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class DiskThresholds:
    warn_percent: float = 80.0
    degraded_percent: float = 90.0
    critical_percent: float = 95.0

    def state_for(self, used_percent: float) -> str:
        if used_percent >= self.critical_percent:
            return "critical"
        if used_percent >= self.degraded_percent:
            return "degraded"
        if used_percent >= self.warn_percent:
            return "warn"
        return "ok"


@dataclass(frozen=True)
class DiskUsage:
    path: Path
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_percent: float
    thresholds: DiskThresholds
    state: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "used_percent": self.used_percent,
            "state": self.state,
            "thresholds": asdict(self.thresholds),
        }


def disk_usage_report(path: Path | str, *, thresholds: DiskThresholds | None = None, usage_func: Callable[[Path], shutil._ntuple_diskusage] = shutil.disk_usage) -> DiskUsage:
    threshold_values = thresholds or DiskThresholds()
    resolved = Path(path).resolve(strict=False)
    usage = usage_func(resolved)
    used = int(usage.used)
    total = int(usage.total)
    free = int(usage.free)
    used_percent = round((used / total) * 100.0, 1) if total > 0 else 0.0
    return DiskUsage(
        path=resolved,
        total_bytes=total,
        used_bytes=used,
        free_bytes=free,
        used_percent=used_percent,
        thresholds=threshold_values,
        state=threshold_values.state_for(used_percent),
    )
