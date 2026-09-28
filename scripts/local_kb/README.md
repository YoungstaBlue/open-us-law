# Local legal-knowledge base (Missouri + federal)

A thin local index over the Open US Law quarterly snapshot, scoped to
Missouri and federal law. Three scripts, no shared library between them
(each is a self-contained CLI, matching the rest of this repo):

- `build_db.py` -- downloads the relevant parquet files and loads them into
  a local DuckDB file (`legal_kb.duckdb`).
- `search.py` -- fast local keyword/citation search over that database.
- `verify.py` -- re-fetches a citation's live, official source and flags
  drift against the local snapshot.

**The local database is never authoritative on its own.** It is a
point-in-time snapshot that can lag an amendment, mis-parse a page, or
carry something already repealed. `search.py` always shows you the
`source_url` and `act_status` for anything it returns; `verify.py` is what
actually confirms a citation is current. Always run `verify.py` on a
citation before it goes into a filing -- don't reuse an old run's result,
and don't cite something `search.py` alone turned up.

## Quickstart

```
pip install -r requirements.txt
python scripts/local_kb/build_db.py          # fetches MO + federal parquet, builds legal_kb.duckdb
python scripts/local_kb/search.py "455.020"  # find candidate matches locally
python scripts/local_kb/verify.py "455.020"  # confirm against the live official source
```

`build_db.py --mo-only` skips the federal corpora entirely.
`build_db.py --skip-cfr` skips just the federal regulations parquet, which
has run several GB in past snapshots.

## What's actually in here

Pulled from the manifest at `https://oss-data-us.vaquill.ai/index.json`,
never hardcoded, so this list can shift as the dataset changes shape:

- Missouri statutes (`revisor.mo.gov`)
- Missouri constitution (`revisor.mo.gov`)
- US Code / federal statutes (`uscode.house.gov`)
- Code of Federal Regulations (`ecfr.gov`)

Missouri court rules and the Missouri Code of State Regulations are **not
in the published snapshot yet**, but this repo now ingests both. Build them
locally first and `build_db.py` loads them into the `mo_court_rules` and
`mo_regulations` tables (it looks in `./data` by default; override with
`--jsonl-dir`):

```bash
OUT_DIR=./data python scripts/court_rules/ingest_mo_court_rules.py   # ~1.3k rules, ~5 min
OUT_DIR=./data python scripts/regulations/ingest_mo_regulations.py   # all 23 CSR titles, ~10 min
OUT_DIR=./data python scripts/case_law/ingest_mo_case_law.py        # ~140k opinions, several GB download
python scripts/local_kb/build_db.py
```

- Missouri court rules (`courts.mo.gov`): Supreme Court Rules 1-160,
  including the Rules of Civil, Criminal, Juvenile, Probate and Appellate
  Procedure, plus the Court Operating Rules
- Missouri regulations (`sos.mo.gov`): Code of State Regulations, all titles,
  including rescinded/moved stubs tagged via `act_status`
- Missouri case law (`mo_case_law`): Supreme Court of Missouri and Court of
  Appeals opinions, 1821-2019 from the Caselaw Access Project; later opinions
  from CourtListener when `COURTLISTENER_API_TOKEN` is set (free account).
  **No subsequent history** -- `verify.py` confirms the text, not that the case
  is still good law. Run a citator before citing.

If you skip those ingesters, `build_db.py` reports the gap loudly, and
`search.py`/`verify.py` detect a court-rule- or CSR-shaped query and route you
straight to the real source instead of returning an empty or misleading
local result:

- Missouri court rules -> `https://www.courts.mo.gov/page.jsp?id=46`
  (Rules of Civil Procedure specifically: `id=676`)
- Missouri regulations -> `https://www.sos.mo.gov/adrules/csr/csr`

## Adding another state or corpus later

Nothing about the scripts is Missouri-specific by design -- `build_db.py`'s
`CORPORA` dict is the only place that names which parquet files to pull. To
add a state:

1. Confirm the file exists in the manifest (`us_<state>_<corpus>.parquet`
   naming, per the dataset's own convention) -- don't assume it does.
2. Add an entry to `CORPORA` in `build_db.py` with the matching table name
   and a `match` lambda for the new filename.
3. Re-run `build_db.py`. It downloads only what's missing (or changed, with
   `--force`) and adds the new table(s) alongside the existing ones --
   `search.py` and `verify.py` need no changes; they discover tables at
   runtime via `SHOW TABLES`.

If a new corpus uses different column names than the ones already handled,
add the alternate name to `FIELD_CANDIDATES` in both `search.py` and
`verify.py` (kept in sync by hand, since these scripts intentionally don't
share code).

## Refreshing the snapshot

Hugging Face publishes a new dated snapshot of this dataset roughly
quarterly. Refreshing is a re-download, not a rebuild:

```
python scripts/local_kb/build_db.py --force
```

This re-fetches the current manifest and re-downloads/reloads every table
already configured. There's no versioning of old snapshots locally --
`legal_kb.duckdb` always reflects whatever you last built. If you need to
know exactly what changed between quarters, that's a manual diff against
whatever you had cached before running `--force` (the parquet cache dir
keeps the old files until you overwrite them).

## How `verify.py` fits into the workflow

`search.py` gets you to candidate matches fast. `verify.py` is the step
before you actually rely on one:

```
python scripts/local_kb/verify.py "RSMo 565.081"
```

For each local match, it re-fetches the citation's `source_url` live,
strips it to plain text, and diffs that against the locally stored text
with a similarity ratio:

- **MATCH** (>=90% similar) -- local snapshot agrees with the live page.
- **POSSIBLE DRIFT** (60-90%) -- text differs; read the live page before
  citing.
- **SIGNIFICANT DRIFT** (<60%) -- treat this as amended, moved, or
  repealed since the snapshot was taken until you've read the live page
  yourself.

It also independently flags anything whose local `act_status` says
repealed/renumbered/reserved/expired/superseded, regardless of the text
comparison, and surfaces the caveat each official source publishes about
itself:

- `revisor.mo.gov` labels its own text uncertified/unofficial -- the
  certified text is the printed RSMo.
- `ecfr.gov` is explicitly not the official legal edition -- that's the
  annual print CFR via govinfo.gov.

If the live fetch itself fails (network issue, page moved, site down),
`verify.py` reports that as a failure too -- it never treats "couldn't
check" as "must be fine." Exit code is `0` only when every citation it
checked came back a clean match with a caveat you've now seen; anything
else (no local match, fetch failure, drift, a bad `act_status`) exits `1`.

For a corpus with no local table (Missouri court rules or regulations when
their ingesters haven't been run), `verify.py` skips the local lookup entirely
and points you straight at the official source -- see above. CSR rules are
verified against their chapter PDF on sos.mo.gov (fetched with a browser TLS
fingerprint via `curl_cffi`, since the site is behind Cloudflare), comparing
only the rule's own slice of the chapter.
