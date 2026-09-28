#!/usr/bin/env python3
"""Ingest Missouri court rules.

corpus_type='state_rules', document_type='court_rule', act_id prefix
'SRULES_MO_'. Same record shape as the other court-rules ingests.

Source
------
The Supreme Court of Missouri publishes its rules in a public Lotus Domino
database on courts.mo.gov ("Clerk Handbooks", ClerkHandbooksP2RulesOnly.nsf).
Two views are ingested:

  - publicSupremeCourtRules    Supreme Court Rules (Rules 1-140: Bar and
                               Judiciary, Civil, Criminal, Juvenile, Probate,
                               Appellate, Traffic, Municipal, ...)
  - publicCourtOperatingRules  Court Operating Rules (COR 1-28)

Domino exposes every view as XML via ``?ReadViewEntries``: category rows carry
the rule-level heading ("Rule 85 - Rules of Civil Procedure - ... -
Attachments") and document rows carry the section number, topic and document
UNID. Each document is then fetched with ``/0/<UNID>?OpenDocument`` (plain
HTML; header table of Section/Subject/Topic/Adopted/Effective, then the rule
body). No proxy or browser is needed; the portal's other pages
(page.jsp?id=...) sit behind a Cloudflare challenge, but the .nsf database
does not.

Output: state_mo_court_rules.jsonl (embed with lib/embed_and_upsert.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

DATA_DIR = Path(os.environ.get("OUT_DIR", "./data"))
OUT = DATA_DIR / "state_mo_court_rules.jsonl"

MO_DB = "https://www.courts.mo.gov/courts/ClerkHandbooksP2RulesOnly.nsf"
UA = "Mozilla/5.0 (open-us-law ingestion bot; +https://github.com/Vaquill-AI/open-us-law)"
PAGE = 500  # Domino caps ReadViewEntries Count; page with Start

RULE_SETS: dict[str, dict] = {
    "scr": {
        "view": "publicSupremeCourtRules",
        "name": "Missouri Supreme Court Rules",
        "citation_prefix": "Mo. Sup. Ct. R.",
    },
    "cor": {
        "view": "publicCourtOperatingRules",
        "name": "Missouri Court Operating Rules",
        "citation_prefix": "Mo. Ct. Op. R.",
    },
}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})


def fetch(url: str, retries: int = 4) -> str | None:
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=45)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.text
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(1.5 * (2 ** attempt))
    return None


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _point_id(act_id: str, chunk_idx: int, text: str) -> str:
    seed = f"{act_id}::{chunk_idx}::{_sha1(text)[:12]}"
    return str(UUID(hashlib.md5(seed.encode()).hexdigest()))


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------------------
# View index (Domino ReadViewEntries XML)
# ---------------------------------------------------------------------------
@dataclass
class RuleRef:
    slug: str
    unid: str
    section: str        # "85.10", "COR 1", "Canon 3", "4-1.1"
    topic: str
    subject: str        # parent category, e.g. "Rule 85 - Rules of Civil Procedure - ..."


def _entry_text(ve: ET.Element, name: str) -> str:
    for ed in ve.findall("entrydata"):
        if ed.get("name") == name:
            return _ws(" ".join(t.text or "" for t in ed.iter("text")))
    return ""


def list_view(slug: str) -> list[RuleRef]:
    view = RULE_SETS[slug]["view"]
    refs: list[RuleRef] = []
    categories: dict[str, str] = {}
    start = 1
    while True:
        xml = fetch(f"{MO_DB}/{view}?ReadViewEntries&ExpandView&Start={start}&Count={PAGE}")
        if not xml:
            break
        root = ET.fromstring(xml.encode("utf-8"))
        entries = root.findall("viewentry")
        if not entries:
            break
        for ve in entries:
            pos = ve.get("position", "")
            unid = ve.get("unid")
            if not unid:  # category row
                categories[pos] = _entry_text(ve, "Subject")
                continue
            parent = pos.rsplit(".", 1)[0] if "." in pos else ""
            refs.append(RuleRef(
                slug=slug, unid=unid,
                section=_entry_text(ve, "Section"),
                topic=_entry_text(ve, "Topic"),
                subject=categories.get(parent, ""),
            ))
        # Positions are hierarchical ("12.3"); Start takes the last one to
        # continue after it. Domino re-returns the start row, so dedupe below.
        last = entries[-1].get("position", "")
        if len(entries) < PAGE or not last or last == str(start):
            break
        start = last  # type: ignore[assignment]
    seen: set[str] = set()
    out = [r for r in refs if not (r.unid in seen or seen.add(r.unid))]
    print(f"  [MO {slug}] {len(out)} rule documents in view", flush=True)
    return out


# ---------------------------------------------------------------------------
# Rule document -> text
# ---------------------------------------------------------------------------
@dataclass
class Rule:
    ref: RuleRef
    rule_id: str
    section_title: str
    adopted: str
    effective: str
    raw_text: str
    source_url: str
    amendment_years: list[int] = field(default_factory=list)


def _norm_section(s: str) -> str:
    # "4- 1. 0" -> "4-1.0"; "COR  1" -> "COR 1"
    s = _ws(s)
    return re.sub(r"\s*([-.])\s*", r"\1", s)


def _header_field(soup: BeautifulSoup, label: str) -> str:
    for b in soup.find_all("b"):
        if _ws(b.get_text()).rstrip(":").lower() == label.lower():
            td = b.find_parent("td")
            nxt = td.find_next_sibling("td") if td else None
            if nxt is not None:
                return _ws(nxt.get_text(" "))
    return ""


def _body_text(html: str) -> str:
    # The body follows the header table's closing <hr size="7">.
    parts = re.split(r'<hr[^>]*size="7"[^>]*>', html, flags=re.I)
    body_html = parts[-1] if len(parts) > 2 else html
    body_html = body_html.split("</form>")[0]
    body_html = re.sub(r"(?is)<script.*?</script>", "", body_html)
    soup = BeautifulSoup(body_html, "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(["div", "p", "li", "tr", "ul"]):
        tag.insert_after("\n")
    text = soup.get_text()
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_rule(ref: RuleRef) -> Rule | None:
    url = f"{MO_DB}/0/{ref.unid}?OpenDocument"
    html = fetch(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    section = _norm_section(_header_field(soup, "Section/Rule") or ref.section)
    topic = _header_field(soup, "Topic") or ref.topic
    adopted = _header_field(soup, "Publication / Adopted Date")
    effective = _header_field(soup, "Revised / Effective Date")
    body = _body_text(html)
    if not body or not section:
        return None
    years = sorted({int(y) for y in re.findall(
        r"(?:Adopted|Amended|eff\.|effective)[^()]*?\b(1[89]\d\d|20\d\d)\b", body)})
    return Rule(
        ref=ref, rule_id=section, section_title=topic, adopted=adopted,
        effective=effective, raw_text=body, source_url=url, amendment_years=years,
    )


# ---------------------------------------------------------------------------
# Record shape (mirrors the other court-rules ingests)
# ---------------------------------------------------------------------------
def _status(rule: Rule) -> str:
    head = f"{rule.section_title} {rule.raw_text[:300]}".lower()
    if re.search(r"\brepealed\b", head):
        return "repealed"
    if re.search(r"\breserved\b", head):
        return "reserved"
    return "in_force"


def _subject_parts(subject: str) -> tuple[str, str]:
    """'Rule 85 - Rules of Civil Procedure - ... - Attachments' -> ('85', 'Rule 85 - ...')."""
    m = re.match(r"(?i)\s*(?:rule|court operating rule)\s+(\d+)\b", subject)
    return (m.group(1) if m else ""), _ws(subject)


def _to_chunk_record(rule: Rule) -> dict:
    meta = RULE_SETS[rule.ref.slug]
    set_name = meta["name"]
    rid = rule.rule_id
    act_id = f"SRULES_MO_{rule.ref.slug.upper()}_R{_safe(rid)}"
    title_label = "Missouri Rules of Court"
    if rule.ref.slug == "cor":
        num = re.sub(r"(?i)^COR\s*", "", rid)
        citation = f"{meta['citation_prefix']} {num}"
    else:
        citation = f"{meta['citation_prefix']} {rid}"
    rule_no, subject = _subject_parts(rule.ref.subject)
    label = f"Rule {rid}" if rid[:1].isdigit() else rid  # "Canon 2", "COR 12"
    chapter = rule_no or rid.split(".")[0]
    dates = "; ".join(x for x in (
        f"Adopted: {rule.adopted}" if rule.adopted else "",
        f"Effective: {rule.effective}" if rule.effective else "",
    ) if x)
    text_for_embedding = (
        f"{title_label} | {set_name} | {citation}\n"
        f"{subject}\n{rid}. {rule.section_title}"
        + (f"\n{dates}" if dates else "")
        + f"\n\n{rule.raw_text}"
    )
    years = rule.amendment_years
    md = {
        "act_id": act_id,
        "corpus_type": "state_rules",
        "category": "state_rules",
        "document_type": "court_rule",
        "jurisdiction": "US",
        "country_code": "US",
        "state": "mo",
        "title_name": set_name,
        "title": title_label,
        "title_code": f"mo_{rule.ref.slug}",
        "top_level_title": f"rules-mo-{rule.ref.slug}",
        "level_classifier": "rule",
        "chapter": chapter,
        "chapter_name": subject or set_name,
        "subchapter": None,
        "subchapter_name": subject or set_name,
        "section_number": rid,
        "section_title": f"{label}. {rule.section_title}",
        "citation": citation,
        "citation_short": citation,
        "display_label": citation,
        "display_title": rule.section_title,
        "display_path": f"{set_name} / {subject or chapter} / {rid}",
        "breadcrumb": [title_label, set_name, subject or f"Rule {chapter}", label],
        "sort_key": act_id,
        "act_status": _status(rule),
        "renumbered_to": "",
        "transferred_to": "",
        "year": years[-1] if years else None,
        "adopted_date": rule.adopted or None,
        "effective_date": rule.effective or None,
        "word_count": len(rule.raw_text.split()),
        "subsection_count": 0,
        "subsection_letters": [],
        "numbered_paragraph_count": 0,
        "amendments_count": len(years),
        "amendment_years": years,
        "last_amended_year": years[-1] if years else None,
        "cross_references_count": 0,
        "cross_references_usc": [],
        "cross_references_cfr": [],
        "public_laws_count": 0,
        "public_laws_referenced": [],
        "source_url": rule.source_url,
        "parent_id": None,
        "raw_node_id": act_id,
        "full_text_sha1": _sha1(rule.raw_text),
    }
    return {
        "point_id": _point_id(act_id, 0, rule.raw_text),
        "text_for_embedding": text_for_embedding,
        "raw_text": rule.raw_text,
        "metadata": md,
    }


def _write_jsonl(path: Path, rules: list[Rule]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    records: dict[str, dict] = {}
    for r in rules:
        rec = _to_chunk_record(r)
        cur = records.get(rec["metadata"]["act_id"])
        if cur is None or len(rec["raw_text"]) > len(cur["raw_text"]):
            records[rec["metadata"]["act_id"]] = rec
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records.values():
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(records)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", default="", help="Comma-separated slugs: scr,cor (default: all).")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="Fetch at most N docs per set (testing).")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    slugs = [s.strip() for s in args.sets.split(",") if s.strip()] or list(RULE_SETS)
    print(f"=== Missouri court-rules ingest: {slugs} ===", flush=True)
    all_rules: list[Rule] = []
    for slug in slugs:
        refs = list_view(slug)
        if args.limit:
            refs = refs[: args.limit]
        failed = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(fetch_rule, r): r for r in refs}
            for i, fut in enumerate(as_completed(futs), start=1):
                rule = fut.result()
                if rule is None:
                    failed += 1
                else:
                    all_rules.append(rule)
                if i % 200 == 0 or i == len(refs):
                    print(f"  [MO {slug}] {i}/{len(refs)} fetched, {failed} failed", flush=True)
    n = _write_jsonl(args.out, all_rules)
    print(f"\n[MO] done: {len(all_rules)} rules, {n} unique act_ids\n=> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
