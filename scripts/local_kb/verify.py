#!/usr/bin/env python3
"""
Verify a citation from the local legal-KB DuckDB database against its live,
official source. The local database (built by build_db.py) is a point-in-time
snapshot -- it can lag an amendment, mis-parse a page, or carry something
already repealed. This script never trusts that snapshot on its own: it
re-fetches the current page at the row's own source_url and flags drift.

Usage:
    python scripts/local_kb/verify.py "455.020"
    python scripts/local_kb/verify.py "RSMo 565.081" --corpus mo_statutes
    python scripts/local_kb/verify.py "Rule 55.03"          # MO court rule -- no local data, routed directly
    python scripts/local_kb/verify.py "10 CSR 10-6.020"     # MO regulation -- no local data, routed directly

Exit code is 0 only when every citation checked came back a clean match with
no local/live mismatch and no unresolved fetch failure. Anything else (no
local match, fetch failure, drift, repealed/renumbered status) exits 1.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from datetime import date
from pathlib import Path

import duckdb
import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

_HERE = Path(__file__).resolve().parent
DEFAULT_DB_PATH = _HERE / "legal_kb.duckdb"

# Same dual-naming resolution as search.py -- the dataset's README documents
# corpora with different column names for the same logical field.
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

# Caveats that MUST be surfaced whenever a citation from that corpus is
# verified -- these are properties of the official source itself, not of
# this tool, and dropping them would be misleading.
SOURCE_CAVEATS = {
    "mo_statutes": (
        "revisor.mo.gov publishes this text as UNCERTIFIED/UNOFFICIAL -- the "
        "legally certified text is the printed RSMo. This check confirms current "
        "content, not a certified-copy substitute."
    ),
    "mo_constitutions": (
        "revisor.mo.gov publishes this text as UNCERTIFIED/UNOFFICIAL -- cross-check "
        "against the Secretary of State's PDF (https://www.sos.mo.gov/pubs/constitution) "
        "for anything outcome-critical."
    ),
    "federal_statutes": (
        "uscode.house.gov (Office of the Law Revision Counsel) is the codifying "
        "authority -- prefer it over any mirror site for exact current text."
    ),
    "federal_regulations": (
        "eCFR is updated ~daily but is explicitly NOT the official legal edition -- "
        "the official edition is the annual print CFR via govinfo.gov."
    ),
}

REPEALED_STATUS_RE = re.compile(r"repeal|renumber|reserved|expired|superseded", re.IGNORECASE)


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


def find_rows(con: duckdb.DuckDBPyConnection, table: str, query: str, limit: int) -> list[dict]:
    fields = resolve_columns(con, table)
    if not fields["citation"] and not fields["text"] and not fields["title"]:
        return []

    where_parts = []
    for f in ("citation", "title", "text"):
        col = fields[f]
        if col:
            where_parts.append(f'"{col}" ILIKE ?')
    if not where_parts:
        return []

    select_cols = []
    for f in ("citation", "title", "status", "url", "text"):
        col = fields[f]
        select_cols.append(f'"{col}" AS {f}' if col else f"NULL AS {f}")

    # Prefer citation-column matches first (a verify target is almost always
    # an exact citation, not a keyword search), then fall back to title/text.
    order_expr = "CASE WHEN 1=1 THEN 0 ELSE 1 END"
    if fields["citation"]:
        order_expr = f'CASE WHEN "{fields["citation"]}" ILIKE ? THEN 0 ELSE 1 END'

    sql = f"""
        SELECT {", ".join(select_cols)}
        FROM {table}
        WHERE {" OR ".join(where_parts)}
        ORDER BY {order_expr}
        LIMIT {int(limit)}
    """
    params = [f"%{query}%"] * len(where_parts)
    if fields["citation"]:
        params.append(f"%{query}%")
    rows = con.execute(sql, params).fetchall()
    col_names = ["citation", "title", "status", "url", "text"]
    return [dict(zip(col_names, row)) for row in rows]


def normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def fetch_live_text(url: str, timeout: int) -> tuple[str | None, str | None]:
    """Returns (extracted_text, error). Exactly one is non-None."""
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "open-us-law-local-kb-verify/1.0 (research tool)"},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        return None, f"fetch failed: {exc}"
    try:
        soup = BeautifulSoup(resp.text, "lxml")
        text = soup.get_text(separator=" ", strip=True)
    except Exception as exc:  # malformed markup, etc -- report, don't crash
        return None, f"could not parse response body: {exc}"
    if not text:
        return None, "fetched page but extracted no text"
    return text, None


def compare_text(local_text: str | None, live_text: str | None) -> float:
    a, b = normalize(local_text), normalize(live_text)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def classify_ratio(ratio: float) -> str:
    if ratio >= 0.90:
        return "MATCH -- local snapshot text agrees with the live source"
    if ratio >= 0.60:
        return "POSSIBLE DRIFT -- text differs from the live source; review before citing"
    return "SIGNIFICANT DRIFT -- local snapshot does not match the live source (possibly amended, moved, or repealed since the snapshot was taken)"


def verify_row(table: str, row: dict, timeout: int) -> bool:
    """Prints a verification report for one row. Returns True if clean (no
    action needed), False if the caller should treat this as a problem."""
    ok = True
    log(f"\n=== {table}: {row['citation'] or '(no citation)'} ===")
    log(f"  title:      {row['title'] or '(none)'}")
    log(f"  act_status: {row['status'] or '(unknown)'} (local snapshot)")

    status = row["status"] or ""
    if REPEALED_STATUS_RE.search(status):
        ok = False
        log(
            f"  !! Local snapshot marks act_status = {status!r}. Do not rely on this "
            "citation without confirming current status at the live source below."
        )

    url = row["url"]
    caveat = SOURCE_CAVEATS.get(table)
    if not url:
        ok = False
        log("  source_url: (none on this row) -- cannot verify against a live source.")
        return ok

    log(f"  source_url: {url}")
    if caveat:
        log(f"  caveat:     {caveat}")

    live_text, err = fetch_live_text(url, timeout)
    if err:
        ok = False
        log(f"  LIVE FETCH FAILED: {err}")
        log("  Could not confirm this citation is current. Treat the local snapshot as unverified.")
        return ok

    if not row["text"]:
        log("  Local snapshot has no stored text for this row -- cannot diff content.")
        log(f"  Live page fetched successfully at {url}; read it directly to confirm current text.")
    else:
        ratio = compare_text(row["text"], live_text)
        verdict = classify_ratio(ratio)
        log(f"  comparison: {verdict} (similarity {ratio:.0%})")
        if ratio < 0.90:
            ok = False

    log(f"  -> {url} (live verified {date.today().isoformat()})")
    return ok


def report_gap(kind: str) -> None:
    if kind == "court_rule":
        log(
            "\nThis looks like a Missouri COURT RULE citation. This local database has no "
            "Missouri court-rules data (no scraper covers it, and the published snapshot "
            "does not carry one either). Skipping local lookup -- go directly to the source:\n"
            "  Missouri Supreme Court Rules hub: https://www.courts.mo.gov/page.jsp?id=46\n"
            "  Missouri Rules of Civil Procedure specifically: https://www.courts.mo.gov/page.jsp?id=676"
        )
    else:
        log(
            "\nThis looks like a Missouri REGULATION (CSR) citation. This local database has no "
            "Missouri regulations data. Skipping local lookup -- go directly to the source:\n"
            "  Missouri Secretary of State, Code of State Regulations: https://www.sos.mo.gov/adrules/csr/csr"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query", help="Citation (or close keyword) to verify")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="Path to legal_kb.duckdb (default: %(default)s)")
    parser.add_argument("--corpus", choices=["mo_statutes", "mo_constitutions", "mo_court_rules", "mo_regulations", "federal_statutes", "federal_regulations"], help="Restrict to one table")
    parser.add_argument("--limit", type=int, default=3, help="Max local matches to verify (default: %(default)s)")
    parser.add_argument("--timeout", type=int, default=20, help="Live-fetch timeout in seconds (default: %(default)s)")
    args = parser.parse_args()

    rule_like = looks_like_mo_court_rule(args.query)
    reg_like = looks_like_mo_regulation(args.query)

    if not args.db_path.exists():
        # Even with no database at all, still route known-gap corpora correctly
        # instead of just failing -- that's useful on its own.
        if rule_like or reg_like:
            report_gap("court_rule" if rule_like else "regulation")
            return 1
        raise SystemExit(f"ERROR: no database at {args.db_path} -- run build_db.py first.")

    con = duckdb.connect(str(args.db_path), read_only=True)
    tables = list_tables(con)

    # Corpora with no local data at all: skip the local lookup entirely and
    # point straight at the official source, per the KB's non-negotiable rule
    # that a gap must be reported loudly, never silently mistaken for "no hits".
    if rule_like and "mo_court_rules" not in tables:
        report_gap("court_rule")
        con.close()
        return 1
    if reg_like and "mo_regulations" not in tables:
        report_gap("regulation")
        con.close()
        return 1

    if args.corpus and args.corpus not in tables:
        log(f"'{args.corpus}' is not a table in this database (available: {', '.join(tables) or 'none'}).")
        con.close()
        return 1

    target_tables = [args.corpus] if args.corpus else tables
    all_rows: list[tuple[str, dict]] = []
    for table in target_tables:
        for row in find_rows(con, table, args.query, args.limit):
            all_rows.append((table, row))
    con.close()

    if not all_rows:
        log(f"No local match for: {args.query!r}")
        if rule_like:
            report_gap("court_rule")
        elif reg_like:
            report_gap("regulation")
        else:
            log("Nothing to verify locally. Double-check the citation, or search.py first to confirm it exists in this snapshot.")
        return 1

    clean = True
    for table, row in all_rows[: args.limit]:
        if not verify_row(table, row, args.timeout):
            clean = False

    log("\nRemember: this tool checks the live official source at the moment you ran it.")
    log("Re-run verify.py again before relying on any citation in a filing -- do not reuse an old run's result.")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
