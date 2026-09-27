# Local knowledge base

An offline, searchable copy of a slice of the corpus, built from the published
parquet snapshot -- for looking things up locally without re-scraping
anything, plus a one-command check against the live official source.

```bash
pip install -r requirements.txt
python scripts/local_kb/build_db.py          # fetches MO + federal parquet, builds legal_kb.duckdb
python scripts/local_kb/search.py "455.020"  # find candidate matches locally
python scripts/local_kb/verify.py "455.020"  # confirm against the live official source
```

## What each script does

- **`build_db.py`** downloads the published parquet for the requested corpora
  (default: `mo_statutes` + `federal_statutes`, i.e. Missouri Revised Statutes
  + the US Code) from the same [snapshot mirror](https://oss-data-us.vaquill.ai)
  the root README links under "Download the data", verifies each file's
  sha256, and loads them into `legal_kb.duckdb` (next to this script) with a
  full-text index. Downloaded parquet is cached in `.cache/` so re-runs are
  fast. `--list-corpora` shows everything available (add state constitutions,
  MO guidance, federal court rules, ... or the full `federal_regulations`
  corpus, which is ~2.8GB and opt-in only).
- **`search.py`** queries that database. A bare section number or citation
  (`"455.020"`, `"10001"`) is matched directly against `section_number`/
  `citation`; anything else runs BM25 full-text search over the section text,
  title, and citation (`"order of protection"`, `"stalking"`). Offline, no
  network calls.
- **`verify.py`** takes a search result's section number and re-fetches that
  *one* section from its live government source right now -- revisor.mo.gov
  for Missouri, uscode.house.gov for the US Code -- and reports whether the
  local snapshot text still matches. `DIFFERS` means either the statute was
  amended after the snapshot date, or the snapshot text has an extraction
  artifact (seen on a handful of heavily-amended US Code sections with dense
  statutory notes) -- it prints the diff so you can tell which, and either way
  it's a flag to go read the live section yourself, not a bug report.

## Scope and caveats

- Only `mo` and `federal` (statutes) have a live-verification path today.
  Other jurisdictions in the database can still be searched; `verify.py` will
  print the section's `source_url` for you to check by hand instead of
  fetching it.
- This is a downstream consumer of the published snapshot, not a new
  scraper -- it doesn't touch revisor.mo.gov or uscode.house.gov except in
  `verify.py`, one section at a time, on demand.
- Same caveat as the rest of this repo: snapshots are point-in-time.
  **Always verify anything you rely on** -- that's what `verify.py` is for.
  This is legal information, not legal advice.
