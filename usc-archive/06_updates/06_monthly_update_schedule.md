# Update schedule — U.S. Code archive

## Cadence

- **Monthly, first week of the month:** `python3 scripts/update_usc.py --check`
  Takes seconds. Reads only the OLRC download page and compares the release point to the stored
  one. Writes `06_last_check.json`. Nothing else changes.
- **When the check reports a new release point:** `python3 scripts/update_usc.py`
  Downloads the three title zips for the new release point, splits them, hashes every section, and
  compares against the stored capture. Then run `scripts/validate.py` and `scripts/import_supabase.py`.
- **Before any filing that quotes a section:** run the check regardless of the calendar, and read
  the section on the official site once more. The archive is a research copy, not the controlling text.

Congress enacts laws in bursts, so most monthly checks will say "unchanged". That is the expected
result, not a failure.

## What a change does

| result | on disk | in Supabase | in the log |
|---|---|---|---|
| unchanged | `06_last_check.json` updated | `last_verified_at` bumped | nothing |
| modified | old capture kept under `02_raw/tier2-olrc-<old>/`, new one under `02_raw/tier2-olrc-<new>/` | old row kept with `is_current=false`, new row inserted | one row, `needs_legal_review=true` |
| new | new files only | new row | one row, `needs_legal_review=true` |
| repealed | old capture kept | old row marked not current | one row, `needs_legal_review=true` |

Old versions are never overwritten or deleted. Every changed section stays flagged until someone
reads the diff and clears it.

## Tier 1 (govinfo) sections

The seven cited sections were also captured from GPO govinfo's 2023 edition. govinfo publishes a
new edition roughly once a year, later than OLRC. When a new edition appears, re-run
`scripts/capture_metadata.py` against the new granule URLs and re-run the Tier 1 vs Tier 2
cross-check in `scripts/validate.py`. Two official publishers agreeing is the verification standard
for anything that goes into a filing.

## Extending to Missouri RSMo

The same three-file-per-section layout, hash comparison, and change-log format apply. The blocker
is source access: revisor.mo.gov has not been reachable for direct fetches from this environment.
When it is, add a `MO-RSMO` corpus under `02_raw/` and reuse `validate.py`, `import_supabase.py`
(the `jurisdiction` column already exists), and `update_usc.py` with a Revisor-specific fetcher.
