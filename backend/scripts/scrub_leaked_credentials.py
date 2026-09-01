"""One-off: remove credentials already written into the audit trail.

The audit is append-only by design, and rewriting it is a deliberate exception
rather than a precedent. The justification is narrow: a credential in a durable
row is a credential that keeps leaking for as long as the row exists, and unlike
a log it never rotates away. Only the secret is replaced — the event, its
timestamp, its type and the rest of the error text all survive, so the record of
*what happened* is unchanged and still reconciles with the JSONL mirror.

Rotating the key remains necessary regardless. This removes the copy; it cannot
un-disclose it.

    python scripts/scrub_leaked_credentials.py --dry-run
    python scripts/scrub_leaked_credentials.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.redaction import redact_secrets

REPO = Path(__file__).resolve().parents[1]
JSONL = REPO / "data" / "audit" / "events.jsonl"
DB = REPO / "data" / "traido_journal.db"


def _backup(path: Path) -> Path:
    """Keep the original next to it, so a bad scrub is recoverable."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_suffix(path.suffix + f".pre-scrub-{stamp}")
    shutil.copy2(path, target)
    return target


def scrub_jsonl(*, dry_run: bool) -> tuple[int, int]:
    if not JSONL.exists():
        return (0, 0)

    lines = JSONL.read_text().splitlines()
    cleaned, changed = [], 0
    for line in lines:
        scrubbed = redact_secrets(line)
        if scrubbed != line:
            changed += 1
            # Re-parse so a malformed scrub is caught here rather than by
            # whatever reads the mirror next.
            json.loads(scrubbed)
        cleaned.append(scrubbed)

    if changed and not dry_run:
        _backup(JSONL)
        JSONL.write_text("\n".join(cleaned) + ("\n" if lines else ""))
    return (len(lines), changed)


def scrub_db(*, dry_run: bool) -> tuple[int, int]:
    if not DB.exists():
        return (0, 0)

    connection = sqlite3.connect(DB)
    try:
        rows = connection.execute("SELECT id, payload FROM audit_events").fetchall()
    except sqlite3.OperationalError:
        connection.close()
        return (0, 0)

    updates = []
    for row_id, payload in rows:
        if not isinstance(payload, str):
            continue
        scrubbed = redact_secrets(payload)
        if scrubbed != payload:
            json.loads(scrubbed)
            updates.append((scrubbed, row_id))

    if updates and not dry_run:
        connection.close()
        _backup(DB)
        connection = sqlite3.connect(DB)
        connection.executemany("UPDATE audit_events SET payload = ? WHERE id = ?", updates)
        connection.commit()

    connection.close()
    return (len(rows), len(updates))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    total_lines, changed_lines = scrub_jsonl(dry_run=args.dry_run)
    total_rows, changed_rows = scrub_db(dry_run=args.dry_run)

    verb = "would scrub" if args.dry_run else "scrubbed"
    print(f"{JSONL.name}: {verb} {changed_lines} of {total_lines} lines")
    print(f"audit_events: {verb} {changed_rows} of {total_rows} rows")

    if not args.dry_run and (changed_lines or changed_rows):
        print("\nOriginals kept alongside as .pre-scrub-<timestamp>.")
        print("Delete them once you have confirmed the result — they still hold the key.")
        print("Rotate the credential regardless: this removed the copy, not the disclosure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
