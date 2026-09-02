#!/usr/bin/env python3
"""Create a safe review archive excluding secrets and build artifacts."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

# Path segments / names that must never enter a review archive.
FORBIDDEN_NAMES = {
    ".env",
    ".git",
    ".venv",
    ".venv312",
    "node_modules",
    "dist",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
FORBIDDEN_SUFFIXES = (".pyc", ".db", ".db-wal", ".db-shm", ".db-journal")
FORBIDDEN_PREFIXES = (
    "backend/data/backups/",
    "backend/data/logs/",
    "backend/data/audit/",
)


def _is_forbidden(rel: Path) -> bool:
    parts = set(rel.parts)
    if parts & FORBIDDEN_NAMES:
        return True
    name = rel.name
    if name in FORBIDDEN_NAMES:
        return True
    if name.endswith(FORBIDDEN_SUFFIXES):
        return True
    posix = rel.as_posix()
    if any(posix.startswith(p) or f"/{p}" in f"/{posix}" for p in FORBIDDEN_PREFIXES):
        return True
    if name == "watch_telemetry.jsonl":
        return True
    return bool("events.jsonl" in posix and "audit" in posix)


def build_archive(root: Path, out: Path) -> int:
    count = 0
    skipped = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if _is_forbidden(rel):
                skipped += 1
                continue
            zf.write(path, arcname=rel.as_posix())
            count += 1
    print(f"Creating archive: {out}")
    print(f"Included {count} files; skipped {skipped} forbidden paths")
    print(
        "NOTE: If this archive left a trusted environment, rotate Alpaca/Finnhub/"
        "API credentials. This script does not change keys."
    )
    print(f"Done: {out}")
    return 0


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
    if out.exists():
        out.unlink()
    return build_archive(root, out)


if __name__ == "__main__":
    raise SystemExit(main())
