"""
Advisory schema verifier (O2).

Purpose: pre-deploy validation that the configured PG_DSN already has
(or can receive) the advisory schema, with all enum CHECK constraints
matching the Python source-of-truth tuples in `data.advisory_schema`.

Modes:
    --apply   Run init_advisory_schema() first (idempotent, safe to re-run).
    --check   (default) Only verify; don't modify schema.
    --json    Machine-readable output.

Exit codes:
    0  OK — every expected table exists and enum CHECKs match.
    1  Drift detected — at least one table is missing or an enum is out
       of sync with the Python tuples; details in stdout / JSON `errors`.
    2  Connection / hard failure (DSN bad, permission denied, etc).

Backup / rollback notes:
- All DDL is `CREATE TABLE IF NOT EXISTS` (idempotent additive); no
  destructive migration. Safe to run on a populated prod DB.
- If a CHECK drift is found, the recommended path is *manual*:
    pg_dump -t <tbl> > backup_<tbl>_<ts>.sql
    ALTER TABLE <tbl> DROP CONSTRAINT <chk_name>;
    ALTER TABLE <tbl> ADD CONSTRAINT <chk_name> CHECK (col IN (...));
- This script DOES NOT auto-rewrite CHECKs (would require destructive
  DROP+ADD); operator must approve.

Usage:
    LD_PRELOAD="" uv run scripts/advisory_verify_schema.py --check
    LD_PRELOAD="" uv run scripts/advisory_verify_schema.py --apply --json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from data.database import get_conn  # noqa: E402
from data import advisory_schema as ASCH  # noqa: E402

logger = logging.getLogger(__name__)


EXPECTED_TABLES = (
    "path_observation_snapshots",
    "settlement_feed_versions",
    "settlement_feed_records",
    "input_quote_snapshots",
    "path_views",
    "market_view_batches",
    "market_view_snapshots",
    "market_view_latest",
    "advisory_intents",
    "advisory_chain_fills",
    "advisory_chain_fills_poller_state",
)


# (table, column, expected_values_tuple)
EXPECTED_ENUM_CHECKS = (
    ("settlement_feed_versions", "refresh_status", ASCH.REFRESH_STATUSES),
    ("settlement_feed_records", "settlement_state", ASCH.SETTLEMENT_STATES),
    ("market_view_batches", "status", ASCH.BATCH_STATUSES),
    ("market_view_snapshots", "fair_value_status", ASCH.FAIR_VALUE_STATUSES),
    ("market_view_snapshots", "resolution_state", ASCH.RESOLUTION_STATES),
)


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s LIMIT 1",
        (table,),
    )
    return cur.fetchone() is not None


def _row_count(cur, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 — table from constant list
    return int(cur.fetchone()[0])


def _check_constraint_clauses(cur, table: str) -> list[str]:
    """Return raw clause strings (e.g. "(refresh_status = ANY (ARRAY['ok','partial','failed']))")
    for all CHECK constraints on the given table.
    """
    cur.execute(
        """
        SELECT pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        JOIN pg_namespace n ON t.relnamespace = n.oid
        WHERE n.nspname = 'public'
          AND t.relname = %s
          AND c.contype = 'c'
        """,
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def _extract_enum_values(clause: str, column: str) -> set[str] | None:
    """Parse a CHECK clause like
       "CHECK ((status = ANY (ARRAY['started'::text, 'complete'::text, ...])))"
    or "CHECK ((status IN ('started','complete','failed')))" and return the set
    of literals; None if this clause does not constrain `column`.
    """
    if column not in clause:
        return None
    import re
    body = clause
    m_arr = re.search(r"ARRAY\s*\[(.+?)\]", body, re.S)
    m_in = re.search(r"\bIN\s*\(\s*(.+?)\s*\)", body, re.S)
    raw = m_arr.group(1) if m_arr else (m_in.group(1) if m_in else None)
    if not raw:
        return None
    vals = set()
    for tok in re.findall(r"'([^']+)'", raw):
        vals.add(tok)
    return vals or None


def verify(apply: bool) -> dict:
    report: dict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "applied": False,
        "tables": {},
        "enum_checks": [],
        "errors": [],
    }

    if apply:
        ASCH.init_advisory_schema()
        report["applied"] = True

    with get_conn() as conn:
        cur = conn.cursor()

        for tbl in EXPECTED_TABLES:
            exists = _table_exists(cur, tbl)
            entry: dict = {"exists": exists}
            if exists:
                entry["row_count"] = _row_count(cur, tbl)
            else:
                report["errors"].append(f"missing table: {tbl}")
            report["tables"][tbl] = entry

        for tbl, col, expected in EXPECTED_ENUM_CHECKS:
            if not report["tables"].get(tbl, {}).get("exists"):
                continue
            clauses = _check_constraint_clauses(cur, tbl)
            found_values: set[str] | None = None
            for cl in clauses:
                vals = _extract_enum_values(cl, col)
                if vals is not None:
                    found_values = vals
                    break
            expected_set = set(expected)
            entry = {
                "table": tbl,
                "column": col,
                "expected": sorted(expected_set),
                "found": sorted(found_values) if found_values else None,
                "match": (found_values == expected_set),
            }
            report["enum_checks"].append(entry)
            if not entry["match"]:
                if found_values is None:
                    report["errors"].append(
                        f"missing CHECK on {tbl}.{col} (expected {sorted(expected_set)})"
                    )
                else:
                    missing = expected_set - found_values
                    extra = found_values - expected_set
                    report["errors"].append(
                        f"CHECK drift on {tbl}.{col}: missing={sorted(missing)} extra={sorted(extra)}"
                    )

    return report


def _print_text(report: dict) -> None:
    print("=" * 72)
    print("ADVISORY SCHEMA VERIFY")
    print("=" * 72)
    print(f"generated: {report['generated_at_utc']}  applied={report['applied']}")
    print()
    print("Tables:")
    for tbl, info in report["tables"].items():
        if info["exists"]:
            print(f"  ✓ {tbl:<32} rows={info['row_count']}")
        else:
            print(f"  ✗ {tbl:<32} MISSING")
    print()
    print("Enum CHECK constraints:")
    for e in report["enum_checks"]:
        mark = "✓" if e["match"] else "✗"
        print(f"  {mark} {e['table']}.{e['column']}")
        if not e["match"]:
            print(f"      expected: {e['expected']}")
            print(f"      found:    {e['found']}")
    print()
    if report["errors"]:
        print(f"ERRORS ({len(report['errors'])}):")
        for err in report["errors"]:
            print(f"  - {err}")
    else:
        print("No drift detected ✓")


def main():
    parser = argparse.ArgumentParser(description="Advisory schema verifier (O2)")
    parser.add_argument("--apply", action="store_true",
                        help="Run init_advisory_schema() before verifying (idempotent).")
    parser.add_argument("--check", action="store_true",
                        help="Verify only; do not modify schema (default).")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON instead of text table.")
    args = parser.parse_args()
    if args.apply and args.check:
        parser.error("--apply and --check are mutually exclusive")

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        report = verify(apply=args.apply)
    except Exception as exc:
        logger.exception("schema verify failed")
        if args.json:
            print(json.dumps({"fatal": str(exc)}, indent=2))
        else:
            print(f"FATAL: {exc}")
        return 2

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_text(report)

    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
