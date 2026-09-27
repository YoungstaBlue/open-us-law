"""Local offline knowledge base built from the published open-us-law parquet snapshot.

Three scripts, one pipeline:

    build_db.py   -> download MO + federal parquet, build ./legal_kb.duckdb
    search.py     -> find candidate sections locally (fast, offline, free-text or citation)
    verify.py     -> confirm a candidate against its live official source

See README.md in this directory for the quickstart. This package is a *consumer*
of the corpus (like anyone downloading the Hugging Face snapshot); it does not
scrape anything new. The only live HTTP calls it makes are in ``live_sources.py``,
and only from ``verify.py``, to re-check one section at a time against the
government site it came from.
"""
