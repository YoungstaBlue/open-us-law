# U.S. Code Verbatim Archive

A source-of-record archive of federal statutory text, captured verbatim from official publishers,
hashed, validated, searchable, and printable. Built for litigation research; usable in any state.

**Unofficial reference.** The Office of the Law Revision Counsel's online edition is current but not
the certified print edition; govinfo's edition is official but lags. Neither replaces checking the
controlling version before a filing.

## Labeling

Every section has one ID used in filenames, the database, PDFs, and the citation log:

```
{JURISDICTION}-{CORPUS}-{TITLE}-{SECTION}     US-USC-18-241   US-USC-42-2000e–2   (future) MO-RSMO-565.020
```

Each section has exactly three files with that stem:

| file | what it is |
|---|---|
| `.xml` / `.htm` | byte-exact copy of the publisher's source element/page — **the verbatim record** |
| `.txt` | readable plain-text rendering of the same content (not the record) |
| `.metadata.json` | official URL, retrieval time, publisher, edition/release point, positive-law flag, SHA-256 of the raw file |

## Layout

```
01_source_map/               what was set out to be captured, and why
02_raw/tier1-govinfo-2023/   7 sections cited in current filings — GPO govinfo.gov, USCODE 2023 ed. (official source #1)
02_raw/tier2-olrc-119-102/   1,634 sections — OLRC USLM XML release point 119-102 (official source #2, canonical/current)
    title-18/  all of Title 18 (Crimes and Criminal Procedure)
    title-34/  Chapter 121 (Violent Crime Control and Law Enforcement)
    title-42/  Chapter 21 (Civil Rights)
02_raw/bulk/                 downloaded OLRC zips — gitignored, re-downloadable
03_validated/                normalized CSV + validation report + cross-check + needs_review
04_supabase/                 schema, importer, full-text search function, manifest
05_print/                    6-pt landscape 3-column reference PDFs + master index
06_updates/                  update script, change log, review report, schedule
scripts/                     the pipeline
```

## Sources

- **OLRC** — https://uscode.house.gov/download/download.shtml (USLM XML per title, per release point)
- **GPO govinfo** — https://www.govinfo.gov/app/collection/uscode (per-section HTML granules)

The 7 cited sections were captured from *both* publishers and compared word-for-word
(`03_validated/03_tier1_vs_tier2_crosscheck.csv`). Two official sources agreeing is the strongest
verification available.

## Re-running

```
python3 scripts/capture_metadata.py     # Tier 1: hash + metadata for the govinfo granules in 02_raw/tier1-govinfo-2023
python3 scripts/split_uslm.py 18 42 34  # Tier 2: split OLRC zips in 02_raw/bulk into per-section files
python3 scripts/validate.py             # Phase 3: exits non-zero if anything fails or needs review
python3 scripts/import_supabase.py      # Phase 4: load 03_validated/03_usc_sections.csv
python3 scripts/build_print.py          # Phase 5: PDFs
python3 scripts/update_usc.py           # Phase 6: detect a new OLRC release point, diff by hash, log changes
```

Adding a title: one line in `SCOPE` in `scripts/split_uslm.py` plus one zip download.
