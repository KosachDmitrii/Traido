#!/usr/bin/env python3
"""Create a safe review archive excluding secrets and build artifacts (TZ NFR-006)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

EXCLUDE = [
    ".env",
    ".git",
    ".venv",
    ".venv312",
    "node_modules",
    "dist",
    "backend/data/*.db",
    "backend/data/*.db-*",
    "backend/data/backups",
    "backend/data/logs",
    "backend/data/watch_telemetry.jsonl",
    "__pycache__",
    "*.pyc",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-o",
        "--output",
        default="traido-review.zip",
        help="Output zip path",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    out = Path(args.output)
    if not out.is_absolute():
        out = root / out

    cmd = ["zip", "-r", str(out), "."]
    for pattern in EXCLUDE:
        cmd.extend(["-x", pattern])

    print("Creating archive:", out)
    print("Excluded:", ", ".join(EXCLUDE))
    subprocess.run(cmd, cwd=root, check=True)
    print("Done:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
