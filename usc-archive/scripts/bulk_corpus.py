"""Load a small corpus from the vaquill/open-us-law v2026.08 bulk dataset straight into a validated shard.

  python3 scripts/bulk_corpus.py <dataset-file-stem> <jurisdiction> <shard-name>
  e.g. python3 scripts/bulk_corpus.py us_federal_constitutions federal federal-constitution

Input : _bulk_source/<stem>.parquet + _bulk_source/SHA256SUMS.json (hash must match or nothing is written)
Output: 03_validated/sections/<shard>.csv (same columns as validate.py, plus citation)
        03_validated/sections/<shard>.review.csv (rows that failed a check; never marked current)
Citation, title and section come from the dataset's own citation field, e.g. "Fed. R. Civ. P. 12" ->
title "Fed. R. Civ. P.", section "12". The dataset is a second-hand copy; official_url points at the publisher.
"""
import csv, hashlib, json, re, sys, time
from pathlib import Path
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "_bulk_source"
SHARDS = ROOT / "03_validated" / "sections"
EDITION = "Bulk vaquill/open-us-law v2026.08"
sys.path.insert(0, str(Path(__file__).parent))
FIELDS = ["section_id", "jurisdiction", "title_number", "title_name", "chapter_number", "chapter_name",
          "section_number", "catchline", "verbatim_text", "source_credit", "notes_text", "effective_date_note",
          "status", "positive_law_title", "official_url", "bulk_source_url", "source_edition",
          "retrieved_at", "sha256", "source_file_path", "last_verified_at", "is_current", "citation"]

def split_cite(c):
    m = re.match(r"^(.*?)[ ,§]+([0-9A-Za-z.()\-]+)$", c.strip())
    return (m.group(1).rstrip(" ,§"), m.group(2)) if m else (c, "")

def main(stem, juris, shard):
    fn = SRC / f"{stem}.parquet"
    want = next(r["sha256"] for r in json.load(open(SRC / "SHA256SUMS.json")) if r["file"] == fn.name)
    got = hashlib.sha256(fn.read_bytes()).hexdigest()
    if got != want: sys.exit(f"HASH MISMATCH {fn.name}: manifest {want} vs disk {got}")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    rows, review, seen = [], [], set()
    for r in pq.read_table(fn).to_pylist():
        cite = (r.get("citation") or "").strip()
        title, sec = split_cite(cite)
        text = (r.get("text") or "").strip()
        raw = json.dumps(r, ensure_ascii=False, sort_keys=True, default=str)
        problems = [p for p, bad in (("no citation", not cite), ("no section number", not sec),
                    ("empty text", len(text) < 20), ("duplicate citation", cite in seen)) if bad]
        seen.add(cite)
        status = "repealed" if r.get("act_status") == "repealed" else ("needs_review" if problems else "operative")
        sid = re.sub(r"[^A-Za-z0-9.]+", "-", f"{'US' if juris == 'federal' else 'MO'}-{cite}").strip("-")
        if problems: review.append({"section_id": sid, "reason": "; ".join(problems)})
        rows.append({"section_id": sid, "jurisdiction": juris, "title_number": title, "title_name": r.get("title_name") or "",
            "chapter_number": "", "chapter_name": "", "section_number": sec, "catchline": r.get("section_title") or "",
            "verbatim_text": text, "source_credit": "", "notes_text": "", "effective_date_note": "", "status": status,
            "positive_law_title": "", "official_url": r.get("source_url") or "", "bulk_source_url": f"https://oss-data-us.vaquill.ai/v2026.08/{fn.name}",
            "source_edition": EDITION, "retrieved_at": stamp, "sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "source_file_path": f"_bulk_source/{fn.name}#{r.get('act_id')}", "last_verified_at": stamp,
            "is_current": status == "operative", "citation": cite})
    SHARDS.mkdir(parents=True, exist_ok=True)
    with open(SHARDS / f"{shard}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, FIELDS); w.writeheader(); w.writerows(rows)
    rp = SHARDS / f"{shard}.review.csv"
    if review:
        with open(rp, "w", newline="") as f: w = csv.DictWriter(f, ["section_id", "reason"]); w.writeheader(); w.writerows(review)
    elif rp.exists(): rp.unlink()
    print(f"{fn.name}: hash OK; {len(rows)} rows, {sum(r['is_current'] for r in rows)} current, {len(review)} flagged -> {shard}.csv")

if __name__ == "__main__":
    if len(sys.argv) != 4: sys.exit(__doc__)
    main(*sys.argv[1:])
