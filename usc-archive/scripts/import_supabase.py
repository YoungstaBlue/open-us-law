"""Phase 4: load 03_validated/03_usc_sections.csv into Supabase `statute_sections` via PostgREST.

Requires a temporary insert policy on the table (applied and dropped around the run by the operator).
Env: SUPABASE_URL, SUPABASE_KEY (anon/publishable key is enough while the temp policy exists).
Upsert key: (jurisdiction, title_number, section_number, source_edition) — re-running is idempotent.
Writes 04_supabase/04_usc_manifest.json with counts and CSV hash.
"""
import csv, hashlib, json, os, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "03_validated" / "03_usc_sections.csv"
URL = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/statute_sections"
KEY = os.environ["SUPABASE_KEY"]
BATCH = int(os.environ.get("BATCH", "200"))
csv.field_size_limit(10**9)

COLS = ["jurisdiction", "title_number", "title_name", "chapter_number", "chapter_name", "section_number", "catchline",
        "verbatim_text", "source_credit", "notes_text", "effective_date", "status", "positive_law_title", "official_url",
        "bulk_source_url", "source_edition", "retrieved_at", "sha256", "source_file_path", "last_verified_at", "is_current"]

def to_row(r):
    d = {k: r.get(k, "") for k in COLS if k not in ("effective_date", "positive_law_title", "is_current")}
    d["effective_date"] = r.get("effective_date_note", "") or None
    d["positive_law_title"] = r["positive_law_title"] == "True"
    d["is_current"] = r["is_current"] == "True"
    d["section_id"] = r["section_id"]
    for k in ("title_name", "chapter_number", "chapter_name", "catchline", "source_credit", "notes_text", "bulk_source_url"):
        if d[k] == "": d[k] = None
    return d

def post(rows):
    body = json.dumps(rows).encode()
    req = urllib.request.Request(URL + "?on_conflict=jurisdiction,title_number,section_number,source_edition", data=body, method="POST",
        headers={"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=minimal"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:500]
            if e.code in (502, 503, 504) and attempt < 3: time.sleep(2 ** attempt); continue
            sys.exit(f"HTTP {e.code}: {msg}")

rows = [to_row(r) for r in csv.DictReader(open(CSV, encoding="utf-8"))]
print(f"{len(rows)} rows, batch {BATCH}")
sent = 0
for i in range(0, len(rows), BATCH):
    status = post(rows[i:i + BATCH]); sent += len(rows[i:i + BATCH])
    print(f"  {sent}/{len(rows)} -> {status}")

manifest = {
    "archive": "US-USC", "rows_loaded": sent, "current_rows": sum(r["is_current"] for r in rows),
    "audit_rows": sum(not r["is_current"] for r in rows),
    "by_title": {t: sum(1 for r in rows if r["title_number"] == t and r["is_current"]) for t in sorted({r["title_number"] for r in rows})},
    "csv_sha256": hashlib.sha256(CSV.read_bytes()).hexdigest(), "supabase_project": "bayizqcstqdacbonudey",
    "loaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
}
(ROOT / "04_supabase" / "04_usc_manifest.json").write_text(json.dumps(manifest, indent=2))
print(json.dumps(manifest, indent=2))
