# Changed sections — no update applied yet

Baseline capture: OLRC release point **119-102** (Tier 2, 1,634 sections across Titles 18, 34, 42)
and GPO govinfo **USCODE 2023 edition** (Tier 1, 7 sections).

Last check: see `06_last_check.json`. Result on 2026-09-09: **unchanged** — the OLRC download page
still lists 119-102 as the current release point, so no section was re-fetched and no hash moved.

This file is overwritten by `scripts/update_usc.py` the first time a new release point is detected.
It will then list every section whose SHA-256 differs from the stored capture, with the old and new
hashes, and mark each one `needs_legal_review`. The previous capture directory is never deleted.

| section | change | old sha256 | new sha256 |
|---|---|---|---|
| *(none)* | | | |
