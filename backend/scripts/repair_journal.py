#!/usr/bin/env python3
"""Repair script — dry-run by default; --apply requires confirmation + backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO / "backend"))

from sqlalchemy import create_engine, text


def _checksum(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sqlite_path(url: str) -> Path | None:
    if url.startswith("sqlite:///"):
        return Path(url.replace("sqlite:///", "", 1))
    return None


def _report(engine, *, apply: bool, backup_dir: Path | None) -> list[dict]:
    planned: list[dict] = []
    with engine.connect() as conn:
        print("=== Traido repair report ===\n")

        dup_watches = conn.execute(
            text(
                """
                SELECT symbol, strategy_version, COUNT(*) AS n
                FROM entry_watches
                WHERE status IN ('waiting','triggered','revalidating','admitted','converting')
                GROUP BY symbol, strategy_version
                HAVING COUNT(*) > 1
                """
            )
        ).fetchall()
        print(f"Duplicate active watches: {len(dup_watches)}")
        for row in dup_watches:
            print(f"  {row.symbol} / {row.strategy_version}: {row.n} rows")
            planned.append(
                {
                    "action": "report_duplicate_watch",
                    "symbol": row.symbol,
                    "strategy_version": row.strategy_version,
                    "count": row.n,
                }
            )

        test_symbols = conn.execute(
            text(
                """
                SELECT id, symbol, status FROM entry_watches
                WHERE symbol LIKE 'WAIT%' OR symbol LIKE 'TEST%'
                """
            )
        ).fetchall()
        print(f"\nTest fixture watches: {len(test_symbols)}")
        for row in test_symbols:
            print(f"  {row.symbol} {row.status} id={row.id}")
            planned.append(
                {
                    "action": "cancel_test_watch",
                    "id": str(row.id),
                    "symbol": row.symbol,
                    "status": row.status,
                }
            )

        legacy_opps = conn.execute(
            text(
                """
                SELECT id, symbol, status FROM opportunities
                WHERE legacy = 1 OR creation_admission_record_id IS NULL
                """
            )
        ).fetchall()
        print(f"\nLegacy opportunities (no creation admission): {len(legacy_opps)}")
        for row in legacy_opps[:20]:
            print(f"  {row.symbol} {row.status} id={row.id}")
        if len(legacy_opps) > 20:
            print(f"  ... and {len(legacy_opps) - 20} more")
        for row in legacy_opps:
            planned.append(
                {
                    "action": "mark_legacy_opportunity",
                    "id": str(row.id),
                    "symbol": row.symbol,
                }
            )

        incomplete = conn.execute(
            text(
                """
                SELECT id, symbol, status FROM entry_watches
                WHERE geometry_hash IS NULL
                  AND status IN ('waiting','triggered','revalidating','admitted','converting')
                """
            )
        ).fetchall()
        print(f"\nActive watches missing geometry_hash (will NOT convert): {len(incomplete)}")
        for row in incomplete:
            print(f"  {row.symbol} {row.status} id={row.id}")
            planned.append(
                {
                    "action": "invalidate_incomplete_geometry",
                    "id": str(row.id),
                    "symbol": row.symbol,
                }
            )

        if apply:
            print("\n=== APPLY ===")
            if incomplete:
                r = conn.execute(
                    text(
                        """
                        UPDATE entry_watches
                        SET status = 'invalidated'
                        WHERE geometry_hash IS NULL
                          AND status IN ('waiting','triggered','revalidating','admitted','converting')
                        """
                    )
                )
                conn.commit()
                print(f"Invalidated {r.rowcount} incomplete-geometry watches")
            if test_symbols:
                r = conn.execute(
                    text(
                        """
                        UPDATE entry_watches
                        SET status = 'cancelled'
                        WHERE symbol LIKE 'WAIT%' OR symbol LIKE 'TEST%'
                        """
                    )
                )
                conn.commit()
                print(f"Cancelled {r.rowcount} test fixture watches")
            r = conn.execute(
                text(
                    """
                    UPDATE opportunities SET legacy = 1
                    WHERE creation_admission_record_id IS NULL
                    """
                )
            )
            conn.commit()
            print(f"Marked {r.rowcount} opportunities as legacy")
            print("Trade journal / open positions were not touched.")
        else:
            print("\n(dry-run — pass --apply after confirming the list above)")

        if backup_dir is not None:
            report_path = backup_dir / "repair_audit_report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "generated_at": datetime.now(UTC).isoformat(),
                        "apply": apply,
                        "planned": planned,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"\nAudit report: {report_path}")

    return planned


def main() -> int:
    parser = argparse.ArgumentParser(description="Traido DB repair (dry-run by default)")
    parser.add_argument("--apply", action="store_true", help="Apply safe repairs")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required with --apply to confirm destructive writes",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy URL (default: from settings / sqlite journal)",
    )
    args = parser.parse_args()

    if args.apply and not args.yes:
        print("Refusing --apply without --yes confirmation.", file=sys.stderr)
        return 2

    if args.database_url:
        url = args.database_url
    else:
        from core.config import get_settings
        from database.session import resolve_sync_database_url

        url = resolve_sync_database_url(get_settings().database_url)

    backup_dir: Path | None = None
    db_path = _sqlite_path(url)
    if args.apply and db_path is not None and db_path.exists():
        backup_root = db_path.parent / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = backup_root / f"repair_{stamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / db_path.name
        shutil.copy2(db_path, dest)
        checksum = _checksum(dest)
        (backup_dir / "checksum.sha256").write_text(f"{checksum}  {dest.name}\n", encoding="utf-8")
        verify = _checksum(dest)
        if verify != checksum:
            print("Backup checksum mismatch — aborting.", file=sys.stderr)
            return 3
        print(f"Backup created: {dest}")
        print(f"Backup checksum: {checksum}")

    engine = create_engine(url)
    _report(engine, apply=args.apply, backup_dir=backup_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
