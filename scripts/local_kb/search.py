#!/usr/bin/env python3
"""
Search the local Missouri + federal legal-KB DuckDB database built by build_db.py.

Usage:
    python scripts/local_kb/search.py "455.020"
    python scripts/local_kb/search.py "domestic violence" --corpus mo_statutes
    python scripts/local_kb/search.py "RSMo 565.081"
    python scripts/local_kb/search.py --limit 20 "adverse possession"

This is a fast local index, not the authority. Always run verify.py on any
citation before relying on it in an actual filing.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import duckdb

sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

_HERE = Path(__file__).resolve().parent
DEFAULT_DB_PATH = _HERE / "legal_kb.duckdb"

# Candidate column names per logical field, in preference order. The dataset's
# own README documents dual-named fields (node_text/text, node_name/section_title,
# link/source_url) depending on corpus, so we resolve against whatever the
# table actually has rather than assuming one name.
FIELD_CANDIDATES = {
    "citation": ["citation"],
    "title": ["section_title", "node_name", "title"],
    "text": ["node_text", "text"],
    "status": ["act_status"],
    "url": ["source_url", "link"],
}

MO_COURT_RULE_RE = re.compile(
    r"\b(rule\s+\d|civil procedure|criminal procedure|supreme court rule|mo\.?\s*r\.\s*(civ|crim)|rules of (civil|criminal) procedure)\b",
    re.IGNORECASE,
)
MO_REGULATION_RE = re.compile(r"\b\d+\s*csr\b|code of state regulations", re.IGNORECASE)


def log(msg: str = "") -> None:
    print(msg, flush=True)


def list_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute("SHOW TABLES").fetchall()]


def resolve_columns(con: duckdb.DuckDBPyConnection, table: str) -> dict[str, str | None]:
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    resolved: dict[str, str | None] = {}
    for field, candidates in FIELD_CANDIDATES.items():
        resolved[field] = next((c for c in candidates if c in cols), None)
    return resolved


def looks_like_mo_court_rule(query: str) -> bool:
    return bool(MO_COURT_RULE_RE.search(query))


def looks_like_mo_regulation(query: str) -> bool:
    return bool(MO_REGULATION_RE.search(query))


def search_table(con: duckdb.DuckDBPyConnection, table: str, query: str, limit: int) -> list[dict]:
    fields = resolve_columns(con, table)
    if not fields["citation"] and not fields["text"] and not fields["title"]:
        return []  # table doesn't look like a legal-section table at all

    where_parts = []
    for f in ("citation", "title", "text"):
        col = fields[f]
        if col:
            where_parts.append(f'"{col}" ILIKE ?')
    if not where_parts:
        return []

    select_cols = []
    for f in ("citation", "title", "status", "url"):
        col = fields[f]
        select_cols.append(f'"{col}" AS {f}' if col else f"NULL AS {f}")

    sql = f"""
        SELECT {", ".join(select_cols)}
        FROM {table}
        WHERE {" OR ".join(where_parts)}
        LIMIT {int(limit)}
    """
    params = [f"%{query}%"] * len(where_parts)
    rows = con.execute(sql, params).fetchall()
    col_names = ["citation", "title", "status", "url"]
    return [dict(zip(col_names, row)) for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query", help="Keyword or citation to search for")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="Path to legal_kb.duckdb (default: %(default)s)")
    parser.add_argument("--corpus", choices=["mo_statutes", "mo_constitutions", "mo_court_rules", "mo_regulations", "federal_statutes", "federal_regulations"], help="Restrict to one table")
    parser.add_argument("--limit", type=int, default=15, help="Max results per table (default: %(default)s)")
    args = parser.parse_args()

    if not args.db_path.exists():
        raise SystemExit(f"ERROR: no database at {args.db_path} -- run build_db.py first.")

    con = duckdb.connect(str(args.db_path), read_only=True)
    tables = list_tables(con)
    if args.corpus and args.corpus not in tables:
        log(f"'{args.corpus}' is not a table in this database (available: {', '.join(tables) or 'none'}).")
        con.close()
        return 1

    target_tables = [args.corpus] if args.corpus else tables
    total_hits = 0
    for table in target_tables:
        rows = search_table(con, table, args.query, args.limit)
        if not rows:
            continue
        total_hits += len(rows)
        log(f"\n=== {table} ({len(rows)} match{'es' if len(rows) != 1 else ''}) ===")
        for r in rows:
            log(f"  citation:   {r['citation'] or '(none)'}")
            log(f"  title:      {r['title'] or '(none)'}")
            log(f"  act_status: {r['status'] or '(unknown)'}")
            log(f"  source_url: {r['url'] or '(none -- see Provenance note in README.md)'}")
            log("  ---")

    con.close()

    if total_hits == 0:
        log(f"No local matches for: {args.query!r}")
        rule_like = looks_like_mo_court_rule(args.query)
        reg_like = looks_like_mo_regulation(args.query)
        if rule_like or reg_like or "mo_court_rules" not in tables or "mo_regulations" not in tables:
            if rule_like:
                log(
                    "\nThis looks like a Missouri COURT RULE query. This local database has no "
                    "Missouri court-rules data (this repo has no MO court-rules scraper, and the "
                    "published snapshot does not carry one either). Go directly to the source:\n"
                    "  Missouri Supreme Court Rules hub: https://www.courts.mo.gov/page.jsp?id=46\n"
                    "  Missouri Rules of Civil Procedure specifically: https://www.courts.mo.gov/page.jsp?id=676"
                )
            if reg_like:
                log(
                    "\nThis looks like a Missouri REGULATION (CSR) query. This local database has no "
                    "Missouri regulations data. Go directly to the source:\n"
                    "  Missouri Secretary of State, Code of State Regulations: https://www.sos.mo.gov/adrules/csr/csr"
                )
        return 1

    log(f"\n{total_hits} total match(es). This is a point-in-time local index -- "
        f"run verify.py on any citation before relying on it in a filing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
