"""Phase 2 (Tier 2): split OLRC USLM XML titles into per-section verbatim files.

Input : 02_raw/bulk/xml_usc{NN}@119-102.zip  (Office of the Law Revision Counsel)
Output: 02_raw/tier2-olrc-119-102/title-{T}/US-USC-{T}-{S}.xml   raw <section> element, byte-exact
        .../US-USC-{T}-{S}.txt            normalized plain text (statute + notes)
        .../US-USC-{T}-{S}.metadata.json
        .../title-{T}/manifest.json
Scope filter: Title 18 = all; Title 42 = chapter 21 only; Title 34 = chapter 121 only.
"""
import hashlib, json, re, sys, zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
BULK = ROOT / "02_raw" / "bulk"          # gitignored; re-downloadable
OUT = ROOT / "02_raw" / "tier2-olrc-119-102"
NS = {"u": "http://xml.house.gov/schemas/uslm/1.0", "dc": "http://purl.org/dc/elements/1.1/"}
U = "{http://xml.house.gov/schemas/uslm/1.0}"
CST = timezone(timedelta(hours=-5))
RELEASE = "119-102"
SCOPE = {"18": None, "42": {"21"}, "34": {"121"}}   # None = whole title
TITLE_NAMES = {"18": "Crimes and Criminal Procedure", "42": "The Public Health and Welfare", "34": "Crime Control and Law Enforcement"}

def itertext_block(el):
    """Text with paragraph breaks at block-level USLM elements, entities decoded."""
    out = []
    def walk(e):
        tag = e.tag.replace(U, "")
        block = tag in ("p", "heading", "num", "subsection", "paragraph", "subparagraph", "clause", "subclause",
                        "item", "chapeau", "content", "note", "sourceCredit", "notes", "section", "quotedContent")
        if tag == "num" and e.text: out.append(e.text.strip() + " ")
        elif e.text: out.append(e.text)
        for c in e:
            walk(c)
            if c.tail: out.append(c.tail)
        if block: out.append("\n")
    walk(el)
    s = "".join(out)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" ?\n ?", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip() + "\n"

def process_title(tnum):
    zpath = BULK / f"xml_usc{tnum}@{RELEASE}.zip"
    with zipfile.ZipFile(zpath) as z:
        name = [n for n in z.namelist() if n.endswith(".xml")][0]
        raw = z.read(name)
    root = ET.fromstring(raw)
    positive = (root.find(".//u:meta/u:property[@role='is-positive-law']", NS).text or "").strip() == "yes"
    pub = root.find(".//u:meta/u:docPublicationName", NS)
    current_through = (pub.text or "").strip() if pub is not None else ""
    # Build ancestry map for chapter lookup
    parent = {c: p for p in root.iter() for c in p}
    def ancestor(el, kind):
        e = el
        while e in parent:
            e = parent[e]
            if e.tag == U + kind: return e
        return None
    def num_of(el):
        n = el.find("u:num", NS)
        return (n.get("value") or (n.text or "").strip()) if n is not None else ""
    def heading_of(el):
        h = el.find("u:heading", NS)
        return "".join(h.itertext()).strip() if h is not None else ""

    want = SCOPE[tnum]
    manifest = []
    for sec in root.iter(U + "section"):
        ident = sec.get("identifier", "")
        if not re.fullmatch(rf"/us/usc/t{tnum}/s[^/]+", ident): continue   # skip quoted/nested sections
        chap = ancestor(sec, "chapter")
        chap_num = num_of(chap) if chap is not None else ""
        if want is not None and chap_num not in want: continue
        secnum = ident.rsplit("/s", 1)[1]
        raw_xml = ET.tostring(sec, encoding="utf-8")
        sha = hashlib.sha256(raw_xml).hexdigest()
        key = f"US-USC-{tnum}-{secnum}"
        tdir = OUT / f"title-{tnum}"; tdir.mkdir(parents=True, exist_ok=True)
        (tdir / f"{key}.xml").write_bytes(raw_xml)
        text = itertext_block(sec)
        (tdir / f"{key}.txt").write_text(text, encoding="utf-8")
        credit = sec.find("u:sourceCredit", NS)
        meta = {
            "section_id": key, "section": f"{tnum} U.S.C. § {secnum}",
            "title_number": int(tnum), "title_name": TITLE_NAMES[tnum],
            "chapter_number": chap_num, "chapter_name": heading_of(chap) if chap is not None else "",
            "section_number": secnum, "catchline": heading_of(sec),
            "status": sec.get("status", "operative"),
            "uslm_identifier": ident,
            "official_url": f"https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title{tnum}-section{secnum}&num=0&edition=prelim",
            "bulk_source_url": f"https://uscode.house.gov/download/releasepoints/us/pl/119/102/xml_usc{tnum}@{RELEASE}.zip",
            "retrieved_at": datetime.now(CST).isoformat(timespec="seconds"),
            "source_type": "Office of the Law Revision Counsel, U.S. House of Representatives - USLM XML release point " + RELEASE,
            "current_through": current_through,
            "positive_law_title": positive,
            "source_credit": "".join(credit.itertext()).strip() if credit is not None else "",
            "integrity_sha256": sha, "raw_bytes": len(raw_xml),
            "source_file": f"{key}.xml", "text_file": f"{key}.txt",
        }
        (tdir / f"{key}.metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        manifest.append({k: meta[k] for k in ("section", "chapter_number", "catchline", "status", "integrity_sha256")})
    (OUT / f"title-{tnum}" / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    print(f"title {tnum}: {len(manifest)} sections written (positive_law={positive}, {current_through})")

for t in (sys.argv[1:] or ["18", "42", "34"]):
    process_title(t)
