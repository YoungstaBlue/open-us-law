---
name: statute-lookup
description: Look up verbatim statute, court-rule, or constitutional text for any of Tyler's cases from the Supabase archive (search_statutes / get_statute). Use before quoting or citing any Missouri or federal law.
---
Follow usc-archive/REFERENCE-GUIDE.md. Query Supabase project bayizqcstqdacbonudey with `search_statutes(q, lim, juris)` or `get_statute(juris, section, title)`. Quote `verbatim_text` exactly and cite with `citation`. Use only rows where `is_current = true`. Never quote a row whose status is `mislabeled_in_source`. Tell the user to re-check `official_url` before filing. If the text isn't in the archive, say so plainly.
