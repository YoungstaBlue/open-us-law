#!/usr/bin/env python3
"""Find candidate sections in the local knowledge base -- fast, offline.

    python scripts/local_kb/search.py "455.020"                # citation/section lookup
    python scripts/local_kb/search.py "order of protection"     # free-text search
    python scripts/local_kb/search.py "stalking" --jurisdiction mo
    python scripts/local_kb/search.py "10001" --jurisdiction federal --limit 5

A query that looks like a bare section number ("455.020", "10001") is matched
against section_number/citation first; anything else runs full-text search
over the section text, title, and citation. This only reads legal_kb.duckdb --
build it first with build_db.py. Results are candidates to confirm, not final
answers: feed the act_id or section number you want to rely on to verify.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import (  # noqa: E402
    DEFAULT_DB_PATH,
    NOT_LEGAL_ADVICE,
    freshness_banner,
    load_meta,
    looks_like_citation,
    open_db,
    snippet,
)

# NOTE: the parquet's own `jurisdiction` column is the country code ("US")
# for every row; `state` is the mo/federal/etc. split we actually want, but we
# alias it back to `jurisdiction` so results and the --jurisdiction flag speak
# the same (more intuitive) name.
COLUMNS = (
    "act_id, corpus, state AS jurisdiction, citation, section_number, section_title, "
    "act_status, source_url, word_count, text"
)


def _where(jurisdiction: str | None, corpus: str | None) -> tuple[str, list]:
    clauses, params = [], []
    if jurisdiction:
        clauses.append("state = ?")
        params.append(jurisdiction)
    if corpus:
        clauses.append("corpus = ?")
        params.append(corpus)
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def search_citation(con, query: str, jurisdiction: str | None, corpus: str | None, limit: int):
    extra, params = _where(jurisdiction, corpus)
    sql = f"""
        SELECT {COLUMNS},
               CASE
                   WHEN section_number = ? THEN 0
                   WHEN section_number LIKE ? THEN 1
                   ELSE 2
               END AS rank
        FROM sections
        WHERE (section_number = ? OR section_number LIKE ? OR citation ILIKE ?)
        {extra}
        ORDER BY rank, length(section_number)
        LIMIT ?
    """
    like = query + "%"
    cite_like = "%" + query + "%"
    params = [query, like, query, like, cite_like, *params, limit]
    return con.execute(sql, params).fetchall(), con.description


def search_fulltext(con, query: str, jurisdiction: str | None, corpus: str | None, limit: int):
    extra, params = _where(jurisdiction, corpus)
    try:
        sql = f"""
            SELECT {COLUMNS}, fts_main_sections.match_bm25(act_id, ?) AS score
            FROM sections
            WHERE score IS NOT NULL {extra}
            ORDER BY score DESC
            LIMIT ?
        """
        params = [query, *params, limit]
        rows = con.execute(sql, params).fetchall()
        return rows, con.description
    except Exception:
        # FTS index missing/broken (e.g. db built by an older version) -- fall
        # back to a plain substring scan so search still works, just slower.
        extra, params = _where(jurisdiction, corpus)
        sql = f"""
            SELECT {COLUMNS}
            FROM sections
            WHERE (text ILIKE ? OR section_title ILIKE ?) {extra}
            LIMIT ?
        """
        like = f"%{query}%"
        params = [like, like, *params, limit]
        return con.execute(sql, params).fetchall(), con.description


def print_results(rows, description) -> None:
    if not rows:
        print("no matches.")
        return
    cols = [d[0] for d in description]
    for row in rows:
        r = dict(zip(cols, row))
        print(f"[{r['corpus']}] {r['citation']}  --  {r['section_title'] or '(untitled)'}")
        print(f"  act_id: {r['act_id']}   status: {r['act_status'] or 'unknown'}   words: {r['word_count']}")
        print(f"  {snippet(r['text'])}")
        print(f"  source: {r['source_url']}")
        print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--jurisdiction", default=None, help="e.g. mo, federal")
    ap.add_argument("--corpus", default=None, help="e.g. mo_statutes, federal_statutes")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--full-text", action="store_true", help="force full-text search even for citation-shaped queries")
    args = ap.parse_args()

    try:
        con = open_db(args.db, read_only=True)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    meta = load_meta(con)
    print(freshness_banner(meta))
    print()

    use_citation = looks_like_citation(args.query) and not args.full_text
    if use_citation:
        rows, desc = search_citation(con, args.query, args.jurisdiction, args.corpus, args.limit)
        if not rows:
            print("(no citation match -- falling back to full-text search)\n")
            rows, desc = search_fulltext(con, args.query, args.jurisdiction, args.corpus, args.limit)
    else:
        rows, desc = search_fulltext(con, args.query, args.jurisdiction, args.corpus, args.limit)

    print_results(rows, desc)
    print(NOT_LEGAL_ADVICE)
    if rows:
        cols = [d[0] for d in desc]
        top = dict(zip(cols, rows[0]))
        print(f'\nnext: python scripts/local_kb/verify.py "{top["section_number"]}" --jurisdiction {top["jurisdiction"]}')
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
