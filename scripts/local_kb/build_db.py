#!/usr/bin/env python3
"""
Build a local DuckDB research database, scoped to Missouri + federal data.

This is a thin local index over the Open US Law quarterly snapshot published
on Hugging Face and mirrored on Cloudflare R2 (oss-data-us.vaquill.ai). It is
NOT a replacement for the official sources -- see scripts/local_kb/README.md
and scripts/local_kb/verify.py.

What it does:
  1. Fetches https://oss-data-us.vaquill.ai/index.json (the real, current
     file manifest). Parquet filenames/paths are NEVER hardcoded -- they are
     always resolved from this manifest at runtime, because the snapshot
     version (and therefore the file layout) changes quarterly.
  2. Downloads the Missouri statutes + Missouri constitution parquet files,
     plus the federal USC (statutes) and federal CFR (regulations) parquet
     files, using the exact paths the manifest gives.
  3. Specifically checks the manifest for a Missouri court-rules file and a
     Missouri regulations file. This repo's scrapers do not cover either
     (see README.md's "State court rules" / "State regulations" tables --
     Missouri is not listed in either), so if the manifest doesn't have them
     either, we say so loudly instead of silently skipping.
  4. Loads whatever was actually found into a single local DuckDB file, one
     table per corpus. DuckDB reads parquet natively -- no pandas needed.

Usage:
    python scripts/local_kb/build_db.py
    python scripts/local_kb/build_db.py --skip-cfr        # CFR parquet is ~2-3 GB
    python scripts/local_kb/build_db.py --mo-only         # skip federal entirely
    python scripts/local_kb/build_db.py --force           # re-download even if cached
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urljoin

import duckdb
import requests

sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

MANIFEST_URL = "https://oss-data-us.vaquill.ai/index.json"
R2_BASE = "https://oss-data-us.vaquill.ai/"

_HERE = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = _HERE / "parquet_cache"
DEFAULT_DB_PATH = _HERE / "legal_kb.duckdb"

# Corpus name -> DuckDB table name -> matcher used against manifest filenames.
# Matching is done against the basename of every *.parquet path found in the
# manifest, so it does not depend on the manifest's exact JSON shape.
CORPORA = {
    "mo_statutes": {
        "table": "mo_statutes",
        "match": lambda name: name == "us_mo_statutes.parquet",
        "required": True,
        "note": "Missouri statutes (revisor.mo.gov)",
    },
    "mo_constitutions": {
        "table": "mo_constitutions",
        "match": lambda name: name == "us_mo_constitutions.parquet",
        "required": True,
        "note": "Missouri constitution (revisor.mo.gov)",
    },
    "mo_court_rules": {
        "table": "mo_court_rules",
        "match": lambda name: name == "us_mo_court_rules.parquet",
        "required": False,
        "note": "Missouri court rules -- NOT SCRAPED by this repo",
    },
    "mo_regulations": {
        "table": "mo_regulations",
        "match": lambda name: name == "us_mo_regulations.parquet",
        "required": False,
        "note": "Missouri administrative regulations -- NOT SCRAPED by this repo",
    },
    "federal_statutes": {
        "table": "federal_statutes",
        "match": lambda name: name == "us_federal_statutes.parquet",
        "required": True,
        "note": "US Code (uscode.house.gov / govinfo USLM)",
        "federal": True,
    },
    "federal_regulations": {
        "table": "federal_regulations",
        "match": lambda name: name == "us_federal_regulations.parquet",
        "required": False,
        "note": "Code of Federal Regulations / eCFR",
        "federal": True,
        "heavy": True,  # multi-GB in past snapshots -- opt out with --skip-cfr
    },
}


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_manifest(manifest_url: str, timeout: int = 30) -> dict:
    """Fetch and parse index.json. Raises SystemExit with a clear message on failure."""
    log(f"Fetching manifest: {manifest_url}")
    try:
        resp = requests.get(manifest_url, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SystemExit(
            f"ERROR: could not fetch the file manifest at {manifest_url}: {exc}\n"
            "build_db.py refuses to guess parquet filenames without it -- "
            "check network access and try again."
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise SystemExit(f"ERROR: manifest at {manifest_url} was not valid JSON: {exc}")


def find_parquet_paths(node, _out: dict | None = None) -> dict:
    """
    Recursively walk the parsed manifest JSON and collect every reference to a
    *.parquet file, regardless of the manifest's exact shape (list of dicts,
    dict-of-dicts, nested "files" key, etc). Returns {basename: path_or_url}.

    This is deliberately shape-agnostic: the manifest schema is not documented
    anywhere in this repo, and the one thing we must not do is hardcode a
    guess at it. Every *.parquet string value (or value under a common
    "path"/"file"/"filename"/"key"/"url"/"name" key) is treated as a candidate.
    """
    if _out is None:
        _out = {}
    if isinstance(node, str):
        if node.endswith(".parquet"):
            _out[node.rsplit("/", 1)[-1]] = node
    elif isinstance(node, dict):
        for key in ("path", "file", "filename", "key", "url", "name"):
            v = node.get(key)
            if isinstance(v, str) and v.endswith(".parquet"):
                _out[v.rsplit("/", 1)[-1]] = v
        for v in node.values():
            find_parquet_paths(v, _out)
    elif isinstance(node, list):
        for item in node:
            find_parquet_paths(item, _out)
    return _out


def resolve_url(path_or_url: str, manifest_url: str) -> str:
    """Resolve a manifest-given path to a full download URL.

    Paths are used exactly as the manifest gives them -- resolved relative to
    the manifest's own URL (so both "us_mo_statutes.parquet" and
    "v2026.08/us_mo_statutes.parquet" and absolute URLs all work correctly).
    """
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    return urljoin(manifest_url, path_or_url)


def download_file(url: str, dest: Path, force: bool = False) -> Path:
    if dest.exists() and not force:
        size_mb = dest.stat().st_size / 1_000_000
        log(f"  already cached: {dest.name} ({size_mb:.1f} MB) -- use --force to re-download")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"  downloading {url} -> {dest}")
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        written = 0
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                written += len(chunk)
                if total:
                    pct = 100 * written / total
                    print(f"\r    {written / 1_000_000:.1f} / {total / 1_000_000:.1f} MB ({pct:.0f}%)", end="", flush=True)
        if total:
            print()
    tmp.rename(dest)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest-url", default=MANIFEST_URL, help="Override the manifest URL (default: %(default)s)")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="Output DuckDB file (default: %(default)s)")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="Local parquet cache dir (default: %(default)s)")
    parser.add_argument("--mo-only", action="store_true", help="Skip federal corpora entirely (USC + CFR)")
    parser.add_argument("--skip-cfr", action="store_true", help="Skip the federal regulations (CFR) parquet -- it can be several GB")
    parser.add_argument("--force", action="store_true", help="Re-download parquet files even if already cached")
    args = parser.parse_args()

    manifest = fetch_manifest(args.manifest_url)
    available = find_parquet_paths(manifest)
    log(f"Manifest resolved {len(available)} parquet file(s).")

    wanted = dict(CORPORA)
    if args.mo_only:
        wanted = {k: v for k, v in wanted.items() if not v.get("federal")}
    if args.skip_cfr:
        wanted.pop("federal_regulations", None)

    loaded: dict[str, Path] = {}
    missing_required: list[str] = []
    missing_optional: list[str] = []

    for corpus_key, spec in wanted.items():
        match_fn = spec["match"]
        hit = next((path for name, path in available.items() if match_fn(name)), None)
        if hit is None:
            if spec["required"]:
                missing_required.append(f"{corpus_key} ({spec['note']})")
            else:
                missing_optional.append(f"{corpus_key} ({spec['note']})")
            continue
        if spec.get("heavy"):
            log(f"NOTE: {corpus_key} is flagged heavy (can be multi-GB) -- downloading anyway; pass --skip-cfr to skip it.")
        url = resolve_url(hit, args.manifest_url)
        dest = args.cache_dir / hit.rsplit("/", 1)[-1]
        try:
            path = download_file(url, dest, force=args.force)
        except requests.RequestException as exc:
            log(f"ERROR: failed to download {corpus_key} from {url}: {exc}")
            if spec["required"]:
                missing_required.append(f"{corpus_key} (download failed: {exc})")
            else:
                missing_optional.append(f"{corpus_key} (download failed: {exc})")
            continue
        loaded[spec["table"]] = path

    # Explicit, unmissable reporting on the two known-absent MO corpora.
    if "mo_court_rules" not in loaded:
        log(
            "\n"
            "==> Missouri COURT RULES: not found in the manifest, and this repo has no\n"
            "    Missouri court-rules scraper (see README.md 'State court rules' table --\n"
            "    only MN, NV, FL, TX, NJ, and a CA/MT multi-state script are covered).\n"
            "    No local Missouri court-rules table was created. Go straight to the\n"
            "    Missouri Supreme Court Rules hub: https://www.courts.mo.gov/page.jsp?id=46\n"
        )
    if "mo_regulations" not in loaded:
        log(
            "\n"
            "==> Missouri REGULATIONS: not found in the manifest, and this repo has no\n"
            "    Missouri regulations scraper (see README.md 'State regulations' table --\n"
            "    Missouri is not among the 14 states listed).\n"
            "    No local Missouri regulations table was created. The official source is\n"
            "    the Missouri Secretary of State's Code of State Regulations:\n"
            "    https://www.sos.mo.gov/adrules/csr/csr\n"
        )

    if not loaded:
        raise SystemExit("ERROR: nothing was downloaded/loaded -- aborting without touching the DuckDB file.")

    args.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(args.db_path))
    log(f"\nLoading {len(loaded)} parquet file(s) into {args.db_path} ...")
    row_counts = {}
    for table, parquet_path in loaded.items():
        con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_parquet(?)", [str(parquet_path)])
        (count,) = con.execute(f"SELECT count(*) FROM {table}").fetchone()
        row_counts[table] = count
        log(f"  {table}: {count:,} rows  (source: {parquet_path.name})")
    con.close()

    log("\n=== build_db.py summary ===")
    for table, count in row_counts.items():
        log(f"  {table}: {count:,} rows")
    if missing_required:
        log("REQUIRED corpora that could not be loaded:")
        for m in missing_required:
            log(f"  - {m}")
    if missing_optional and "mo_court_rules" not in [m.split(" ")[0] for m in missing_optional]:
        pass  # mo_court_rules / mo_regulations already reported explicitly above
    log(f"\nDatabase ready: {args.db_path}")
    log("Remember: this is a point-in-time snapshot, not current law. Run verify.py")
    log("before citing anything from search.py in an actual filing.")

    return 1 if missing_required else 0


if __name__ == "__main__":
    raise SystemExit(main())
