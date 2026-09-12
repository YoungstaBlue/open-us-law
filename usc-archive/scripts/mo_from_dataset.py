"""Missouri RSMo, Tier 1, from this project's own dataset (vaquill/open-us-law, us_mo_statutes.parquet).

Provenance is second-hand: the dataset was captured from revisor.mo.gov by scripts/statutes/ingest_mo_bulk.py
on a proxied box. Every emitted metadata file says so. This is not a direct fetch of the official site.

  python3 scripts/mo_from_dataset.py inspect  PARQUET                 # print schema + 2 rows, no writes
  python3 scripts/mo_from_dataset.py verify   PARQUET SHA256SUMS.json  # hash check against the dataset manifest
  python3 scripts/mo_from_dataset.py extract  PARQUET SHA256SUMS.json [--all]
        writes 02_raw/mo-rsmo-<snapshot>/MO-RSMO-<section>.{json,txt,metadata.json}
        Tier 1 (01_source_map/01_mo_rsmo_source_map.csv) by default; --all = every row.

Column mapping is guessed from common names and printed; override with --col text=body_text etc.
Nothing is written unless `verify` passes or --skip-hash-check is given explicitly.
"""
import csv, hashlib, json, sys, time, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_MAP = ROOT / "01_source_map" / "01_mo_rsmo_source_map.csv"
DATASET = "vaquill/open-us-law"
FILE = "us_mo_statutes.parquet"

GUESS = {  # role -> candidate column names, first present wins
    "section": ["section", "section_number", "section_id", "citation", "id", "act_id", "point_id"],
    "chapter": ["chapter", "chapter_number"],
    "heading": ["heading", "catchline", "title", "name", "section_title"],
    "text": ["text", "body", "body_text", "content", "verbatim_text", "chunk_text"],
    "history": ["history", "history_note", "source_credit", "notes"],
    "url": ["url", "source_url", "official_url", "canonical_url"],
    "snapshot": ["snapshot", "snapshot_date", "retrieved_at", "scraped_at", "captured_at", "date"],
}

def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()

def load(parquet):
    import pyarrow.parquet as pq
    return pq.read_table(parquet)

def colmap(names, overrides):
    m = {}
    for role, cands in GUESS.items():
        for c in cands:
            if c in names: m[role] = c; break
    m.update(overrides)
    return m

def cmd_inspect(parquet, overrides):
    t = load(parquet)
    print("rows:", t.num_rows); print("columns:", t.column_names)
    m = colmap(t.column_names, overrides); print("column map:", m)
    for r in t.slice(0, 2).to_pylist():
        print(json.dumps({k: (str(v)[:160] if v is not None else None) for k, v in r.items()}, indent=1))

def cmd_verify(parquet, sums):
    manifest = json.load(open(sums))
    want = manifest.get(FILE) or manifest.get("files", {}).get(FILE) or next((v for k, v in manifest.items() if k.endswith(FILE)), None)
    if isinstance(want, dict): want = want.get("sha256") or want.get("hash")
    got = sha256(parquet)
    print(f"manifest: {want}\non disk:  {got}")
    if want != got: sys.exit("HASH MISMATCH — do not use this file")
    print("hash OK"); return got

def norm_section(s):
    s = str(s)
    m = re.search(r"(\d{1,3}\.\d{3,4}[A-Za-z]?)", s)
    return m.group(1) if m else s

def cmd_extract(parquet, sums, overrides, everything, skip_hash):
    file_hash = "UNVERIFIED" if skip_hash else cmd_verify(parquet, sums)
    t = load(parquet); m = colmap(t.column_names, overrides)
    for need in ("section", "text"):
        if need not in m: sys.exit(f"cannot find a '{need}' column; pass --col {need}=<name>. columns: {t.column_names}")
    tier1 = {r["section_number"]: r for r in csv.DictReader(open(SRC_MAP))}
    rows = t.to_pylist()
    snap = None
    if "snapshot" in m:
        vals = [str(r[m["snapshot"]])[:10] for r in rows[:2000] if r.get(m["snapshot"])]
        snap = max(vals) if vals else None
    snap = snap or time.strftime("%Y-%m-%d")
    out = ROOT / "02_raw" / f"mo-rsmo-dataset-{snap}"; out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z"); n = 0; seen = set()
    for r in rows:
        sec = norm_section(r[m["section"]])
        if not everything and sec not in tier1: continue
        if sec in seen: continue
        seen.add(sec)
        sid = f"MO-RSMO-{sec}"
        raw = json.dumps(r, ensure_ascii=False, sort_keys=True, default=str)
        (out / f"{sid}.json").write_text(raw, encoding="utf-8")
        text = r[m["text"]] or ""
        (out / f"{sid}.txt").write_text(text, encoding="utf-8")
        meta = {
            "section_id": sid, "jurisdiction": "missouri", "section": f"RSMo § {sec}",
            "chapter_number": str(r.get(m.get("chapter"), sec.split(".")[0])),
            "section_number": sec, "catchline": r.get(m.get("heading")),
            "official_url": r.get(m.get("url")) or f"https://revisor.mo.gov/main/OneSection.aspx?section={sec}",
            "history_text": r.get(m.get("history")),
            "retrieved_at": stamp,
            "source_type": f"Second-hand: Hugging Face dataset {DATASET}/{FILE} (snapshot {snap}), captured from revisor.mo.gov by open-us-law ingest_mo_bulk.py. Not a direct fetch of the official site.",
            "dataset_file_sha256": file_hash,
            "source_file": f"{sid}.json", "text_file": f"{sid}.txt",
            "integrity_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "positive_law_title": None, "status": "operative",
            "citation_log_id": tier1.get(sec, {}).get("citation_log_id"),
        }
        (out / f"{sid}.metadata.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
        n += 1
    missing = sorted(set(tier1) - seen) if not everything else []
    print(f"wrote {n} sections to {out}")
    if missing: print("TIER 1 NOT FOUND IN DATASET:", missing)

if __name__ == "__main__":
    a = sys.argv[1:]
    if not a: sys.exit(__doc__)
    overrides = dict(x.split("=", 1) for x in a if x.startswith("--col=")) if False else {}
    for i, x in enumerate(a):
        if x == "--col": overrides.update([a[i + 1].split("=", 1)])
    pos = [x for x in a if not x.startswith("--") and (a[a.index(x) - 1] != "--col" if a.index(x) else True)]
    cmd = pos[0]
    if cmd == "inspect": cmd_inspect(pos[1], overrides)
    elif cmd == "verify": cmd_verify(pos[1], pos[2])
    elif cmd == "extract": cmd_extract(pos[1], pos[2], overrides, "--all" in a, "--skip-hash-check" in a)
    else: sys.exit(__doc__)
