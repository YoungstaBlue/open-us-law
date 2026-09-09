"""Phase 3: validate every captured section and build the normalized dataset.

Reads  02_raw/tier1-govinfo-2023/  (7 cited sections, govinfo USCODE-2023 HTML granules)
       02_raw/tier2-olrc-119-102/title-*/  (OLRC USLM XML, canonical/current)
Writes 03_validated/03_usc_sections.csv        one row per section+edition (OLRC rows is_current=true)
       03_validated/03_validation_report.csv   every check, every section
       03_validated/03_tier1_vs_tier2_crosscheck.csv
       03_validated/needs_review.csv           anything that failed — never silently dropped
Raw files are never modified.
"""
import csv, hashlib, json, re, sys
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
T1 = ROOT / "02_raw" / "tier1-govinfo-2023"
T2 = ROOT / "02_raw" / "tier2-olrc-119-102"
OUT = ROOT / "03_validated"; OUT.mkdir(exist_ok=True)
U = "{http://xml.house.gov/schemas/uslm/1.0}"
csv.field_size_limit(10**9)

FIELDS = ["section_id", "jurisdiction", "title_number", "title_name", "chapter_number", "chapter_name",
          "section_number", "catchline", "verbatim_text", "source_credit", "notes_text", "effective_date_note",
          "status", "positive_law_title", "official_url", "bulk_source_url", "source_edition",
          "retrieved_at", "sha256", "source_file_path", "last_verified_at", "is_current"]

def norm(s):
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"—\s+", "—", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\b(\d)\s+\1\b", r"\1", s)   # footnote marker rendered twice in USLM
    s = re.sub(r",\s+(\d)\b", r",\1", s)         # footnote marker spacing after a comma
    return s.strip()

def letters_increasing(labels):
    """Top-level lettered subsections must strictly increase; gaps are real (e.g. 18 USC 922 has no (w))."""
    last = ""
    for l in labels:
        if not re.fullmatch(r"[a-z]", l): continue
        nxt = chr(ord(last) + 1) if last else "a"
        if l in "ivx" and l != nxt: continue      # nested roman-numeral clause, not a subsection
        if l <= last: return False
        last = l
    return True

def write(path, data, fields=None):
    if not data: path.write_text(""); return
    keys = fields or []
    for d in data:
        for k in d:
            if k not in keys: keys.append(k)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore"); w.writeheader(); w.writerows(data)

rows, report, review = [], [], []

# ---------- Tier 1: govinfo HTML granules ----------
def split_govinfo(text):
    m = re.search(r"^§\s*([\w\-]+)\.\s+(.+)$", text, re.M)
    if not m: return None
    after = text[m.end():]
    sc = re.search(r"^\((?:R\.S\.|Pub\. L\.|Added Pub\. L\.|June|Act|[A-Z][a-z]{2,8}\.? \d)[^\n]*\)\s*$", after, re.M)
    if sc: return m.group(2).strip(), after[:sc.start()].strip(), sc.group(0).strip(), after[sc.end():].strip()
    return m.group(2).strip(), after.strip(), "", ""

for meta_p in sorted(T1.glob("*.metadata.json")):
    meta = json.loads(meta_p.read_text()); key = meta_p.name.replace(".metadata.json", "")
    raw_p, txt_p = T1 / f"{key}.htm", T1 / f"{key}.txt"
    c = {"tier": 1, "source_file_exists": raw_p.exists(), "text_exists": txt_p.exists(), "metadata_exists": True}
    if not (raw_p.exists() and txt_p.exists()):
        review.append({"section": key, "reason": "missing artifacts"}); report.append({"section": key, "status": "FAIL", **c}); continue
    c["sha256_matches_metadata"] = hashlib.sha256(raw_p.read_bytes()).hexdigest() == meta["integrity_sha256"]
    c["has_official_url"] = bool(meta.get("official_url")); c["has_retrieved_at"] = bool(meta.get("retrieved_at"))
    parts = split_govinfo(txt_p.read_text())
    c["heading_identified"] = parts is not None
    if parts:
        catchline, body, credit, notes = parts
        c["body_nonempty"] = len(body) > 50; c["source_credit_identified"] = bool(credit)
        c["subsection_order_preserved"] = letters_increasing(re.findall(r"^\(([a-z])\)", body, re.M))
        eff = re.search(r"Effective Date[^\n]*", notes)
        rows.append({"section_id": key, "jurisdiction": "federal", "title_number": str(meta["title_number"]),
            "title_name": "", "chapter_number": "", "chapter_name": "", "section_number": meta["section_number"],
            "catchline": catchline, "verbatim_text": body, "source_credit": credit, "notes_text": notes,
            "effective_date_note": eff.group(0) if eff else "", "status": "operative",
            "positive_law_title": meta["positive_law_title"], "official_url": meta["official_url"], "bulk_source_url": "",
            "source_edition": "govinfo USCODE-2023", "retrieved_at": meta["retrieved_at"], "sha256": meta["integrity_sha256"],
            "source_file_path": str(raw_p.relative_to(ROOT)), "last_verified_at": meta["retrieved_at"], "is_current": False})
    failed = [k for k, v in c.items() if v is False]
    report.append({"section": key, "status": "PASS" if not failed else "FAIL", **c})
    if failed: review.append({"section": key, "reason": "failed: " + ", ".join(failed)})

# ---------- Tier 2: OLRC USLM ----------
def text_of(el): return " ".join(t for t in el.itertext()) if el is not None else ""
def body_and_notes(sec):
    body, notes, credit = [], [], ""
    for child in sec:
        tag = child.tag.replace(U, "")
        if tag == "sourceCredit": credit = norm(text_of(child))
        elif tag == "notes": notes.append(text_of(child))
        elif tag in ("num", "heading"): continue
        else: body.append(text_of(child))
    return norm(" ".join(body)), credit, "\n".join(notes).strip()

seen = set()
for meta_p in sorted(T2.glob("title-*/*.metadata.json")):
    meta = json.loads(meta_p.read_text()); key = meta_p.name.replace(".metadata.json", "")
    xml_p, txt_p = meta_p.with_name(f"{key}.xml"), meta_p.with_name(f"{key}.txt")
    c = {"tier": 2, "source_file_exists": xml_p.exists(), "text_exists": txt_p.exists(), "metadata_exists": True}
    if not (xml_p.exists() and txt_p.exists()):
        review.append({"section": key, "reason": "missing artifacts"}); report.append({"section": key, "status": "FAIL", **c}); continue
    raw = xml_p.read_bytes()
    c["sha256_matches_metadata"] = hashlib.sha256(raw).hexdigest() == meta["integrity_sha256"]
    c["has_official_url"] = bool(meta.get("official_url")); c["has_retrieved_at"] = bool(meta.get("retrieved_at"))
    sec = ET.fromstring(raw); operative = meta.get("status", "operative") == "operative"
    c["heading_identified"] = bool(meta.get("catchline")) or not operative
    body, credit, notes = body_and_notes(sec)
    c["body_nonempty"] = len(body) > 0 or not operative
    c["source_credit_identified"] = bool(credit) or not operative
    c["subsection_order_preserved"] = letters_increasing(
        [(ss.find(U + "num").get("value") or "") for ss in sec.findall(U + "subsection") if ss.find(U + "num") is not None])
    dup = (meta["title_number"], meta["section_number"]); c["no_duplicate"] = dup not in seen; seen.add(dup)
    c["chapter_grouping_present"] = bool(meta.get("chapter_number"))
    eff = re.search(r"Effective Date[^\n]*", notes)
    failed = [k for k, v in c.items() if v is False]
    report.append({"section": key, "status": "PASS" if not failed else "FAIL", **c})
    if failed: review.append({"section": key, "reason": "failed: " + ", ".join(failed)})
    rows.append({"section_id": key, "jurisdiction": "federal", "title_number": str(meta["title_number"]),
        "title_name": meta["title_name"], "chapter_number": meta["chapter_number"], "chapter_name": meta["chapter_name"],
        "section_number": meta["section_number"], "catchline": meta["catchline"], "verbatim_text": body,
        "source_credit": credit, "notes_text": notes, "effective_date_note": eff.group(0) if eff else "",
        "status": meta["status"], "positive_law_title": meta["positive_law_title"], "official_url": meta["official_url"],
        "bulk_source_url": meta["bulk_source_url"], "source_edition": "OLRC USLM " + meta["current_through"],
        "retrieved_at": meta["retrieved_at"], "sha256": meta["integrity_sha256"],
        "source_file_path": str(xml_p.relative_to(ROOT)), "last_verified_at": meta["retrieved_at"], "is_current": True})

# ---------- Cross-check: two official sources must agree on the cited sections ----------
strip_labels = lambda s: re.sub(r"\(\w{1,4}\)\s*", "", s)
strip_footnotes = lambda s: re.sub(r"\s*So in original\.(\s*(?:Probably|The|Comma|Section|Words?)[^.]*\.)?\s*", " ", s)
t1 = {r["section_id"]: r for r in rows if not r["is_current"]}
t2 = {r["section_id"]: r for r in rows if r["is_current"]}
xcheck = []
for k, a_row in t1.items():
    if k not in t2: xcheck.append({"section": k, "result": "NO TIER2 TWIN"}); review.append({"section": k, "reason": "no OLRC twin"}); continue
    a = norm(strip_footnotes(strip_labels(norm(a_row["verbatim_text"])))); b = norm(strip_footnotes(strip_labels(norm(t2[k]["verbatim_text"]))))
    same = a == b
    xcheck.append({"section": k, "result": "MATCH" if same else "DIFFER", "govinfo_2023_chars": len(a), "olrc_119_102_chars": len(b)})
    if not same: review.append({"section": k, "reason": "govinfo-2023 vs OLRC-119-102 body differs — check for post-2023 amendment"})

write(OUT / "03_usc_sections.csv", rows, FIELDS)
write(OUT / "03_validation_report.csv", report)
write(OUT / "03_tier1_vs_tier2_crosscheck.csv", xcheck)
write(OUT / "needs_review.csv", review)
fails = [r for r in report if r["status"] == "FAIL"]
print(f"{len(rows)} rows ({len(t2)} current OLRC + {len(t1)} govinfo audit copies); {len(fails)} FAIL; {len(review)} need review")
for x in xcheck: print(" ", x["section"], x["result"])
sys.exit(1 if fails or review else 0)
