# Statute Reference Guide: use this for every case

The verbatim archive in Supabase project `bayizqcstqdacbonudey`, table `statute_sections`, is the first place to look for any statute, rule, or constitutional text. Check it before quoting law in any filing, letter, or analysis.

## Look it up
```sql
select citation, catchline, verbatim_text, official_url, source_edition, sha256
  from search_statutes('assault fourth degree', 5);            -- words or phrases
select * from search_statutes('under color of', 5, 'federal');  -- one jurisdiction only
select * from get_statute('missouri', '565.056');               -- exact Missouri section
select * from get_statute('federal', '1983', '42');             -- exact U.S.C. section
```
Anyone can read the archive through the publishable key. In Claude, use the Supabase connector's `execute_sql`.

## Rules for using it
1. **Quote verbatim** from `verbatim_text`. Cite with the `citation` column (`§ 565.056, RSMo`, `42 U.S.C. § 1983`). Never paraphrase inside quotation marks.
2. **Only rows with `is_current = true` count as law in force.** Rows marked `false` are history, future-effective versions, or quarantined rows.
3. **`status = 'mislabeled_in_source'`** means the source dataset filed that text under the wrong section number (467 Missouri sections). Never quote those rows. Get the section from revisor.mo.gov.
4. **Before filing, re-read the section at `official_url`.** This archive is a research copy. Missouri text is a second-hand copy of revisor.mo.gov. Federal text comes from OLRC, which is official but not the certified print edition.
5. If a section is missing, say so. Don't fill the gap from memory. Add it to `06_updates/ingest_queue.yml` instead.

## What's in it
See `04_supabase/04_usc_manifest.json` and `06_updates/ingest_log.md`. A daily run adds the next items in `06_updates/ingest_queue.yml` until the database reaches 430 MB (the free plan's limit is 500 MB).
