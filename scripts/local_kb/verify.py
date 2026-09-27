#!/usr/bin/env python3
"""Confirm a local candidate section against its live official source.

    python scripts/local_kb/verify.py "455.020"
    python scripts/local_kb/verify.py "455.020" --jurisdiction mo
    python scripts/local_kb/verify.py "10001" --jurisdiction federal

Looks the section up in legal_kb.duckdb (same resolution as search.py, but
requires exactly one match -- pass --jurisdiction to disambiguate), fetches
the current text straight from the government site (revisor.mo.gov for
Missouri, uscode.house.gov for the US Code), and reports MATCH / DIFFERS /
COULD NOT FETCH. Only these two jurisdictions are wired up today; anything
else prints its source_url so you can check it by hand.

A DIFFERS result means the statute was amended after the snapshot date printed
below, OR that the snapshot text has an extraction artifact (rare, but it
happens -- read the printed diff). Either way it's not "broken", just a
signal to go read the live section. This checks one section against one live
page; it is not a substitute for reading the section yourself.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import DEFAULT_DB_PATH, NOT_LEGAL_ADVICE, freshness_banner, load_meta, normalize_text, open_db  # noqa: E402
from live_sources import LiveResult, fetch_mo_section, fetch_usc_section  # noqa: E402

# NOTE: the parquet's own `jurisdiction` column is the country code ("US")
# for every row; `state` is the mo/federal/etc. split we actually want, but we
# alias it back to `jurisdiction` so results and the --jurisdiction flag speak
# the same (more intuitive) name.
COLUMNS = "act_id, corpus, state AS jurisdiction, citation, section_number, section_title, title_number, source_url, text"


def resolve_one(con, query: str, jurisdiction: str | None):
    clauses, params = ["(section_number = ? OR citation ILIKE ?)"], [query, f"%{query}%"]
    if jurisdiction:
        clauses.append("state = ?")
        params.append(jurisdiction)
    sql = f"SELECT {COLUMNS} FROM sections WHERE {' AND '.join(clauses)} ORDER BY section_number = ? DESC LIMIT 5"
    params.append(query)
    rows = con.execute(sql, params).fetchall()
    cols = [d[0] for d in con.description]
    return [dict(zip(cols, r)) for r in rows]


def fetch_live(row: dict) -> LiveResult:
    jurisdiction = row["jurisdiction"]
    if jurisdiction == "mo":
        return fetch_mo_section(row["section_number"])
    if jurisdiction == "federal":
        title = row.get("title_number")
        if not title:
            return LiveResult(ok=False, error="no title_number on this record -- can't build a uscode.house.gov URL")
        return fetch_usc_section(title, row["section_number"])
    return LiveResult(
        ok=False,
        error=f"live verification for jurisdiction '{jurisdiction}' isn't wired up yet",
        url=row["source_url"],
    )


def report(row: dict, live: LiveResult, meta: dict | None) -> int:
    print(f"local:  [{row['corpus']}] {row['citation']} -- {row['section_title'] or '(untitled)'}")
    print(f"        {row['source_url']}")
    print(freshness_banner(meta))
    print()

    if not live.ok:
        print(f"COULD NOT FETCH live source: {live.error}")
        if live.url:
            print(f"check it yourself: {live.url}")
        print(f"\n{NOT_LEGAL_ADVICE}")
        return 2

    local_norm = normalize_text(row["text"])
    live_norm = normalize_text(live.text)

    print(f"live:   {live.url}")
    if live.note:
        print(f"        ({live.note})")
    print()

    if local_norm == live_norm:
        print("MATCH -- local snapshot text is identical to the live official source right now.")
        rc = 0
    else:
        print("DIFFERS -- local snapshot text does not match the live official source.")
        print(
            "That can mean the statute was amended since the snapshot date above, or that "
            "the snapshot text has an extraction artifact -- read the diff and the live "
            "source below before drawing a conclusion.\n"
        )
        sm = difflib.SequenceMatcher(a=local_norm.split(), b=live_norm.split())
        shown = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal" or shown >= 8:
                continue
            shown += 1
            local_bit = " ".join(local_norm.split()[i1:i2]) or "∅"
            live_bit = " ".join(live_norm.split()[j1:j2]) or "∅"
            print(f"  - local:  ...{local_bit[:160]}...")
            print(f"  + live:   ...{live_bit[:160]}...")
        rc = 1

    print(f"\n{NOT_LEGAL_ADVICE}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--jurisdiction", default=None, help="e.g. mo, federal -- required if the query is ambiguous")
    args = ap.parse_args()

    try:
        con = open_db(args.db, read_only=True)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    meta = load_meta(con)
    candidates = resolve_one(con, args.query, args.jurisdiction)
    if not candidates:
        print(f"no local match for '{args.query}'" + (f" in jurisdiction '{args.jurisdiction}'" if args.jurisdiction else ""))
        print('try: python scripts/local_kb/search.py "%s"' % args.query)
        con.close()
        return 1

    distinct_jurisdictions = {c["jurisdiction"] for c in candidates}
    if len(distinct_jurisdictions) > 1 and not args.jurisdiction:
        print(f"ambiguous: '{args.query}' matches sections in {len(distinct_jurisdictions)} jurisdictions:")
        for c in candidates:
            print(f"  [{c['jurisdiction']}] {c['citation']} -- {c['section_title']}")
        print("\nre-run with --jurisdiction to pick one.")
        con.close()
        return 1

    row = candidates[0]
    live = fetch_live(row)
    rc = report(row, live, meta)
    con.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
