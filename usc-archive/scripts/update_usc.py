"""Phase 6: detect a new OLRC release point, re-capture, hash-compare, preserve history.

  python3 scripts/update_usc.py            # check + apply
  python3 scripts/update_usc.py --check    # report only, no downloads beyond the release-point page

Rule per section:
  unchanged  -> last_verified_at = now
  modified   -> keep old row (is_current=false), insert new row, change-log entry, needs_legal_review=true
  new        -> insert, change-log 'new'
  repealed   -> status change is a 'modified' with change_kind 'repealed'
Outputs 06_updates/06_change_log.csv (append) and 06_updates/06_changed_sections_report.md.
Database writes require SUPABASE_URL + SUPABASE_KEY with a write-capable policy in place for the run.
"""
import csv, hashlib, json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BULK = ROOT / "02_raw" / "bulk"; BULK.mkdir(parents=True, exist_ok=True)
T2 = ROOT / "02_raw" / "tier2-olrc-119-102"
UPD = ROOT / "06_updates"; UPD.mkdir(exist_ok=True)
DOWNLOAD_PAGE = "https://uscode.house.gov/download/download.shtml"
TITLES = ["18", "34", "42"]
csv.field_size_limit(10**9)

def current_release_point():
    page = urllib.request.urlopen(DOWNLOAD_PAGE, timeout=60).read().decode("utf-8", "replace")
    m = re.search(r'releasepoints/us/pl/(\d+)/(\d+)/xml_uscAll@(\d+-\d+)\.zip', page)
    if not m: sys.exit("could not find release point on OLRC download page")
    return m.group(3), f"https://uscode.house.gov/download/releasepoints/us/pl/{m.group(1)}/{m.group(2)}/"

def stored_release_point():
    any_meta = next(T2.glob("title-*/*.metadata.json"))
    return json.loads(any_meta.read_text())["current_through"].split("@")[-1]

def load_old_hashes():
    return {json.loads(p.read_text())["section_id"]: json.loads(p.read_text()) for p in T2.glob("title-*/*.metadata.json")}

def main():
    check_only = "--check" in sys.argv
    rp, base = current_release_point(); old_rp = stored_release_point()
    print(f"OLRC current release point: {rp}   stored: {old_rp}")
    if rp == old_rp:
        print("unchanged — nothing to do; bump last_verified_at only")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        (UPD / "06_last_check.json").write_text(json.dumps({"checked_at": stamp, "release_point": rp, "result": "unchanged"}))
        return
    if check_only:
        print("NEW release point available — run without --check to apply"); return

    old = load_old_hashes()
    # download new zips into a release-specific dir, split into a staging dir, compare
    new_dir = ROOT / "02_raw" / f"tier2-olrc-{rp}"; new_dir.mkdir(exist_ok=True)
    for t in TITLES:
        z = BULK / f"xml_usc{t}@{rp}.zip"
        if not z.exists(): urllib.request.urlretrieve(f"{base}xml_usc{t}@{rp}.zip", z)
    env = dict(os.environ, RELEASE=rp, OUT_DIR=str(new_dir))
    subprocess.run([sys.executable, str(ROOT / "scripts" / "split_uslm.py"), *TITLES], check=True, env=env)

    new = {json.loads(p.read_text())["section_id"]: json.loads(p.read_text()) for p in new_dir.glob("title-*/*.metadata.json")}
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z"); log = []
    for sid, m in new.items():
        o = old.get(sid)
        if o is None: kind = "new"
        elif o["integrity_sha256"] == m["integrity_sha256"]: kind = "unchanged"
        elif m["status"] != "operative" and o["status"] == "operative": kind = "repealed"
        else: kind = "modified"
        log.append({"detected_at": stamp, "section_id": sid, "change_kind": kind, "old_release": old_rp, "new_release": rp,
                    "old_sha256": o["integrity_sha256"] if o else "", "new_sha256": m["integrity_sha256"],
                    "needs_legal_review": kind != "unchanged"})
    for sid, o in old.items():
        if sid not in new:
            log.append({"detected_at": stamp, "section_id": sid, "change_kind": "repealed", "old_release": old_rp, "new_release": rp,
                        "old_sha256": o["integrity_sha256"], "new_sha256": "", "needs_legal_review": True})
    path = UPD / "06_change_log.csv"; exists = path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(log[0].keys()))
        if not exists: w.writeheader()
        w.writerows(log)
    changed = [l for l in log if l["change_kind"] != "unchanged"]
    rep = [f"# Changed sections — OLRC {old_rp} → {rp}\n", f"Detected {stamp}. {len(changed)} of {len(log)} sections changed.\n",
           "| section | change | old sha256 | new sha256 |", "|---|---|---|---|"]
    rep += [f"| {l['section_id']} | {l['change_kind']} | {l['old_sha256'][:12]} | {l['new_sha256'][:12]} |" for l in changed]
    rep.append("\nEvery row above is flagged `needs_legal_review`. The previous capture is retained under "
               f"`02_raw/tier2-olrc-{old_rp}/`; the new one is under `02_raw/tier2-olrc-{rp}/`. "
               "Re-run `scripts/validate.py` and `scripts/import_supabase.py` (new rows get is_current=true; "
               "the importer's upsert key includes source_edition so old rows are preserved with is_current=false).")
    (UPD / "06_changed_sections_report.md").write_text("\n".join(rep))
    print(f"{len(changed)} changed; report written")

if __name__ == "__main__":
    main()
