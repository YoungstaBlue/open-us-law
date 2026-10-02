"""Missouri RSMo Tier 1 from the Legal Data Hunter capture (02_raw/mo-rsmo-ldh-<date>/_ldh_capture.json).

Writes, per section:  MO-RSMO-<sec>.json (the record as received), .txt (statutory text), .metadata.json
Then cross-checks every verbatim quote in scratchpad/citation-log.md for these sections against the text.
Provenance is second-hand (Legal Data Hunter -> revisor.mo.gov). Every metadata file says so.

  python3 scripts/mo_from_ldh.py 02_raw/mo-rsmo-ldh-2026-09-09
"""
import csv, hashlib, json, re, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_MAP = ROOT / "01_source_map" / "01_mo_rsmo_source_map.csv"
CIT = Path("/tmp/claude-0/-home-user-open-us-law/0f18e076-e43f-5b43-80da-6b563c3f3686/scratchpad/citation-log.md")

def norm(s):  # collapse whitespace and unify quotes/dashes for comparison only; raw files are untouched
    s = s.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    s = s.replace("—", "-").replace("–", "-")
    s = re.sub(r"[\"']", "", s)  # the log re-quotes inner "voluntary act" as 'voluntary act'
    return re.sub(r"\s+", " ", s).strip().lower()

def main(d):
    d = ROOT / d
    cap = json.load(open(d / "_ldh_capture.json", encoding="utf-8"))
    tier1 = {r["section_number"]: r for r in csv.DictReader(open(SRC_MAP))}
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z"); seen = {}
    for doc in cap["documents"]:
        sec = doc["source_id"].split("/")[-1]; sid = f"MO-RSMO-{sec}"
        raw = json.dumps(doc, ensure_ascii=False, sort_keys=True)
        (d / f"{sid}.json").write_text(raw, encoding="utf-8")
        (d / f"{sid}.txt").write_text(doc["text"], encoding="utf-8")
        text = doc["text"]
        hist = re.search(r"\n(\((?:L\.|RSMo)[^\n]*\))", text)
        eff = re.search(r"\n(Effective [^\n]*)", text)
        meta = {
            "section_id": sid, "jurisdiction": "missouri", "section": f"RSMo § {sec}",
            "chapter_number": sec.split(".")[0], "section_number": sec,
            "catchline": doc["title"].split(" - ", 1)[1] if " - " in doc["title"] else doc["title"],
            "official_url": doc["url"], "history_text": hist.group(1) if hist else None,
            "effective_date_note": eff.group(1) if eff else None,
            "source_type": cap["source"], "source_record_date": doc["date"], "retrieved_at": cap["captured_at"],
            "source_file": f"{sid}.json", "text_file": f"{sid}.txt",
            "integrity_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "positive_law_title": None, "status": "operative",
            "citation_log_id": tier1.get(sec, {}).get("citation_log_id"),
            "flags": [],
        }
        if re.search(r"A\.L\. 202[56]", text):
            meta["flags"].append("amended by 2025/2026 session law; confirm which version governs the offense/order date")
        if eff and re.search(r"Effective \d-\d\d-2[7-9]", eff.group(1)):
            meta["flags"].append("displayed text has a FUTURE effective date; the currently operative version is the prior one")
        (d / f"{sid}.metadata.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
        seen[sec] = norm(text)
    missing = sorted(set(tier1) - set(seen))
    print(f"wrote {len(seen)} sections; missing from capture: {missing}")

    # cross-check citation-log quotes
    rows = []
    for line in open(CIT, encoding="utf-8"):
        m = re.match(r"\|\s*(C-\d+[^|]*)\|\s*RSMo § ([0-9.]+)[^|]*\|\s*(.*?)\s*\|", line)
        if not m: continue
        cid, cell = m.group(1).strip(), m.group(3)
        sec = ".".join(m.group(2).strip(".").split(".")[:2])
        quotes = [q.strip(". …") for q in re.findall(r"[\"“]([^\"”]{25,})[\"”]", cell)]
        if sec not in seen:
            rows.append({"citation_id": cid, "section": sec, "quotes_checked": len(quotes), "result": "SECTION NOT CAPTURED"}); continue
        # a log quote may be fragmented with "..." / "…" or bracketed paraphrase; every fragment >= 20 chars must appear
        def frags(q):
            return [f.strip(" .;:,") for f in re.split(r"\.{3}|…|\[[^\]]*\]", q) if len(f.strip(" .;:,")) >= 20]
        bad = [f for q in quotes for f in frags(q) if norm(f) not in seen[sec]]
        rows.append({"citation_id": cid, "section": sec, "quotes_checked": len(quotes),
                     "result": "MATCH" if quotes and not bad else ("NO QUOTE IN LOG" if not quotes else "MISMATCH"),
                     "first_mismatch": bad[0][:120] if bad else ""})
    out = ROOT / "03_validated" / "03_mo_citation_log_crosscheck.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["citation_id", "section", "quotes_checked", "result", "first_mismatch"]); w.writeheader(); w.writerows(rows)
    for r in rows: print(f'{r["citation_id"]:<28} {r["section"]:<9} {r["result"]:<22} {r.get("first_mismatch","")}')

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "02_raw/mo-rsmo-ldh-2026-09-09")
