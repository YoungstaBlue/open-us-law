#!/usr/bin/env python3
"""Download published corpus parquet and build a local searchable DuckDB.

Pulls files straight from the open-us-law snapshot mirror (the same data the
root README points at under "Download the data") -- no scraping, no API key.
By default it fetches Missouri statutes + the US Code and builds
``legal_kb.duckdb`` next to this script, with a full-text index so
``search.py`` works on both citations and free text.

    python scripts/local_kb/build_db.py                     # MO + federal statutes (default)
    python scripts/local_kb/build_db.py --corpora mo_statutes,mo_constitutions,federal_statutes
    python scripts/local_kb/build_db.py --list-corpora       # show every available corpus
    python scripts/local_kb/build_db.py --refresh            # re-download even if cached

Re-run any time to pick up a newer quarterly snapshot; the manifest is
resolved fresh on every run (see manifest.py), so this always builds from
whatever ``latest.json`` currently points at.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

import duckdb
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as M  # noqa: E402
from db import DEFAULT_CACHE_DIR, DEFAULT_DB_PATH  # noqa: E402


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(entry: M.ManifestEntry, cache_dir: Path, *, refresh: bool) -> Path:
    dest = cache_dir / entry.file
    if dest.exists() and not refresh:
        if _sha256(dest) == entry.sha256:
            print(f"  [cached] {entry.file} ({entry.bytes / 1e6:.1f} MB)")
            return dest
        print(f"  [stale cache] {entry.file} sha256 mismatch, re-downloading")

    print(f"  downloading {entry.file} ({entry.bytes / 1e6:.1f} MB) ...", flush=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    t0 = time.time()
    with requests.get(entry.url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        written = 0
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
                written += len(chunk)
    got_sha = _sha256(tmp)
    if got_sha != entry.sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"sha256 mismatch downloading {entry.file}: expected {entry.sha256}, got {got_sha}"
        )
    tmp.rename(dest)
    rate = written / 1e6 / max(time.time() - t0, 0.001)
    print(f"    done ({rate:.1f} MB/s, sha256 verified)")
    return dest


def build(
    corpora: list[str],
    *,
    out_path: Path,
    cache_dir: Path,
    refresh: bool,
) -> None:
    print(f"resolving {len(corpora)} corpus/corpora against the snapshot manifest...")
    man = M.fetch_manifest()
    print(f"  snapshot {man.get('version')} ({man.get('snapshot_date')})")
    entries = M.resolve(corpora, manifest=man)

    cache_dir.mkdir(parents=True, exist_ok=True)
    local_paths: list[tuple[M.ManifestEntry, Path]] = []
    for entry in entries:
        path = _download(entry, cache_dir, refresh=refresh)
        local_paths.append((entry, path))

    print(f"building {out_path} ...")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    con = duckdb.connect(str(out_path))

    union_sql = " UNION ALL ".join(
        f"SELECT *, '{entry.key}' AS corpus FROM read_parquet('{path.as_posix()}')"
        for entry, path in local_paths
    )
    con.execute(f"CREATE TABLE sections AS {union_sql}")
    row_count = con.execute("SELECT count(*) FROM sections").fetchone()[0]
    print(f"  loaded {row_count:,} sections across {len(local_paths)} file(s)")

    print("  building full-text index (act_id, text, section_title, citation)...")
    con.execute("INSTALL fts; LOAD fts;")
    con.execute(
        "PRAGMA create_fts_index('sections', 'act_id', 'text', 'section_title', "
        "'citation', overwrite=1)"
    )

    con.execute("CREATE TABLE IF NOT EXISTS _meta (built_at TIMESTAMP, snapshot_version VARCHAR, "
                "snapshot_date VARCHAR, corpora VARCHAR, row_count BIGINT, manifest_url VARCHAR)")
    con.execute(
        "INSERT INTO _meta VALUES (now(), ?, ?, ?, ?, ?)",
        [man.get("version"), man.get("snapshot_date"), ",".join(corpora), row_count, M.MANIFEST_URL],
    )

    print("\n  per-corpus counts:")
    for row in con.execute(
        # NOTE: the parquet's own `jurisdiction` column is the country code
        # ("US") for every row -- `state` is the mo/federal/etc. split.
        "SELECT corpus, state, count(*) FROM sections GROUP BY 1, 2 ORDER BY 1"
    ).fetchall():
        print(f"    {row[0]:<24} ({row[1]:<8}) {row[2]:,}")

    con.close()
    print(f"\ndone -> {out_path}")
    print('next: python scripts/local_kb/search.py "455.020"')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--corpora",
        default=",".join(M.DEFAULT_CORPORA),
        help=f"comma-separated corpus keys (default: {','.join(M.DEFAULT_CORPORA)})",
    )
    ap.add_argument("--out", type=Path, default=DEFAULT_DB_PATH, help="output duckdb file")
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="downloaded-parquet cache")
    ap.add_argument("--refresh", action="store_true", help="re-download even if a cached copy verifies")
    ap.add_argument("--list-corpora", action="store_true", help="print available corpora and exit")
    args = ap.parse_args()

    if args.list_corpora:
        print(M.describe_corpora())
        return 0

    corpora = [c.strip() for c in args.corpora.split(",") if c.strip()]
    opt_in_requested = [c for c in corpora if M.CORPORA.get(c, {}).get("opt_in")]
    if opt_in_requested:
        print(f"note: {', '.join(opt_in_requested)} is large -- this may take a while.\n")

    try:
        build(corpora, out_path=args.out, cache_dir=args.cache_dir, refresh=args.refresh)
    except (RuntimeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
