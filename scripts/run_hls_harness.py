#!/usr/bin/env python3
"""Run the FS42-Stream fixture-backed HLS integration harness."""

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fs42stream.hls_harness import main


if __name__ == "__main__":
    raise SystemExit(main())
