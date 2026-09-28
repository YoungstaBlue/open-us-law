#!/usr/bin/env python3
"""Ingest the Missouri Code of State Regulations (CSR).

corpus_type='state_regulation', document_type='regulation', act_id prefix
'STATE_MO_CSR_'. state='mo'. Same record shape as the other state-regulation
ingests.

Source
------
The Missouri Secretary of State publishes the current CSR monthly at
https://www.sos.mo.gov/adrules/csr/csr as one text-layered PDF per chapter
(a few older titles publish one PDF per division, and very large chapters are
split into segments). The site sits behind Cloudflare, which 403s plain
``requests`` from datacenter IPs; ``curl_cffi`` with a Chrome TLS fingerprint
passes without a proxy or a JS challenge.

Crawl: CSR index -> 23 title pages -> chapter PDF links (the link's
aria-label carries "division N - name | Chapter N - name"). Every PDF's own
link annotations are also followed, so a segment reachable only via the
"Next Section" button is still picked up.

Parse: each rule starts with a "<title> CSR <division>-<chapter>.<rule>"
header line followed by its heading, then PURPOSE, the numbered body, and an
AUTHORITY paragraph with the filing/effective history. Rescinded, moved and
transferred rules stay in the CSR as stubs ("(Rescinded July 30, 2004)",
"(Moved to 20 CSR 2010-1.010)"); they are ingested with act_status set
accordingly so citations to them still resolve.

Note (from the CSR itself): a rule published in the current edition is not
effective until 30 days after publication (section 536.021.8, RSMo). The
AUTHORITY paragraph carries each rule's effective date.

Output: state_mo_regulations.jsonl (embed with lib/embed_and_upsert.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

DATA_DIR = Path(os.environ.get("OUT_DIR", "./data"))
OUT = DATA_DIR / "state_mo_regulations.jsonl"

SOS = "https://www.sos.mo.gov/"
CSR_INDEX = urljoin(SOS, "adrules/csr/csr")

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
    "|Jan\\.|Feb\\.|Aug\\.|Sept\\.|Oct\\.|Nov\\.|Dec\\."
)


# ---------------------------------------------------------------------------
# HTTP (curl_cffi: Cloudflare blocks non-browser TLS fingerprints)
# ---------------------------------------------------------------------------
def _get(url: str, binary: bool = False, retries: int = 4):
    from curl_cffi import requests as cf_requests  # type: ignore

    for attempt in range(retries):
        try:
            r = cf_requests.get(url, impersonate="chrome", timeout=90, allow_redirects=True)
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                if binary:
                    return r.content if r.content[:4] == b"%PDF" else None
                return r.text
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _point_id(act_id: str, idx: int, text: str) -> str:
    seed = f"{act_id}::{idx}::{_sha1(text)[:12]}"
    return str(uuid.UUID(hashlib.md5(seed.encode()).hexdigest()))


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _title_case(s: str) -> str:
    s = _ws(s)
    return s[:1].upper() + s[1:] if s.islower() else s


# ---------------------------------------------------------------------------
# Discovery: index -> titles -> chapter PDFs
# ---------------------------------------------------------------------------
@dataclass
class PdfRef:
    url: str
    title: int
    title_name: str
    division_name: str = ""
    chapter_name: str = ""


_PDF_RE = re.compile(r"/adrules/csr/current/(\d+)csr/\d+c[\w-]+\.pdf$", re.I)


def list_titles() -> list[tuple[int, str, str]]:
    html = _get(CSR_INDEX) or ""
    out: dict[int, tuple[int, str, str]] = {}
    for href, label in re.findall(r'href="([^"]*adrules/csr/current/\d+csr[^"]*)"[^>]*>(.*?)</a>', html, re.S):
        m = re.search(r"current/(\d+)csr", href)
        if not m:
            continue
        t = int(m.group(1))
        name = _ws(re.sub(r"<[^>]+>", "", label))
        name = re.sub(r"^Title\s+\d+\s*-\s*", "", name)
        out.setdefault(t, (t, name, urljoin(SOS, href)))
    return [out[k] for k in sorted(out)]


def list_pdfs(title: int, title_name: str, url: str) -> list[PdfRef]:
    html = _get(url) or ""
    refs: dict[str, PdfRef] = {}
    for tag in re.findall(r"<a\b[^>]*>", html, re.I):
        hm = re.search(r'href="([^"]+\.pdf)"', tag, re.I)
        if not hm:
            continue
        pdf = urljoin(SOS, hm.group(1))
        if not _PDF_RE.search(pdf):
            continue
        am = re.search(r'aria-label="([^"]*)"', tag)
        div_name = chap_name = ""
        if am:
            parts = [p.strip() for p in am.group(1).split("|")]
            div_name = re.sub(r"(?i)^division\s+\d+\s*-\s*", "", parts[0]) if parts else ""
            if len(parts) > 1:
                chap_name = re.sub(r"(?i)^chapter\s+\d+\s*-\s*", "", parts[1])
        refs.setdefault(pdf.lower(), PdfRef(pdf, title, title_name,
                                            _title_case(div_name), _ws(chap_name)))
    return list(refs.values())


# ---------------------------------------------------------------------------
# PDF -> rules
# ---------------------------------------------------------------------------
@dataclass
class Reg:
    title: int
    division: int
    chapter: int
    rule: str             # "010"
    cite: str             # "1 CSR 10-1.010"
    heading: str
    raw_text: str
    status: str
    renumbered_to: str
    transferred_to: str
    statutory_authority: str
    history: str
    amendment_years: list[int]
    effective_date: str
    title_name: str
    division_name: str
    chapter_name: str
    source_url: str
    cfr_refs: list[str] = field(default_factory=list)
    usc_refs: list[str] = field(default_factory=list)


_JUNK_LINE = re.compile(
    r"^(?:CODE OF STATE REGULATIONS|Secretary of State|rules of|\d{1,4}|"
    r"\(\d{1,2}/\d{1,2}/\d{2,4}\)\*?|"
    r"(?:John R\.|Jay|Jason|Robin|Matt|Rebecca McDowell|Denny)\s+[A-Za-z]+(?:\s+\(\d{1,2}/\d{1,2}/\d{2,4}\)\*?)?)$",
    re.I,
)
_HDR = re.compile(r"(?m)^[ \t]*(\d{1,2}) CSR (\d{1,4})-(\d{1,3})\.(\d{3,4})\b[ \t]*(.*)$")
_STARTS_BLOCK = re.compile(
    r"^(?:\(\d+\)|\([A-Z]\)|\d+\.\s|PURPOSE:|AUTHORITY:|EDITOR|PUBLISHER|EMERGENCY|"
    r"\*|\((?:Rescinded|Moved|Transferred|Removed|Reserved))",
)


def _pdf_text_and_links(pdf_bytes: bytes) -> tuple[str, list[str]]:
    import pymupdf  # type: ignore

    pages: list[str] = []
    links: list[str] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            pages.append(page.get_text("text") or "")
            for ln in page.get_links():
                uri = ln.get("uri") or ""
                if uri.lower().endswith(".pdf"):
                    links.append(urljoin(SOS, uri))
    text = "\n".join(pages).replace("­", "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    # Two-column PDFs sometimes break a cite across lines: "4 \nCSR \n10-1.030"
    text = re.sub(r"\b(\d{1,2})\s*\n?\s*CSR\s*\n\s*(\d{1,4}-\d{1,3}\.\d{3,4})", r"\1 CSR \2", text)
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if not _JUNK_LINE.match(ln)]
    return "\n".join(lines), links


def _reflow(text: str) -> str:
    text = re.sub(r"([a-z])-\n([a-z])", r"\1\2", text)
    out: list[str] = []
    for ln in text.splitlines():
        if not ln:
            if out and out[-1] != "":
                out.append("")
            continue
        if out and out[-1] != "" and not _STARTS_BLOCK.match(ln):
            out[-1] = f"{out[-1]} {ln}"
        else:
            out.append(ln)
    return "\n".join(out).strip()


def _looks_like_body(after: str) -> bool:
    head = after[:700]
    return bool(re.search(r"PURPOSE:|\((?:Rescinded|Moved|Transferred|Removed)|AUTHORITY:|^\(1\)", head, re.M))


def _chapter_names(text: str) -> dict[int, str]:
    names: dict[int, str] = {}
    for m in re.finditer(r"(?m)^Chapter (\d+)\s*[—-]\s*(.+)$", text):
        names.setdefault(int(m.group(1)), _ws(m.group(2)))
    return names


def _division_name(text: str) -> str:
    m = re.search(r"(?m)^Division (\d+)\s*[—-]\s*(.+)$", text)
    return _ws(m.group(2)) if m else ""


def parse_pdf(ref: PdfRef, text: str) -> list[Reg]:
    fm = re.search(r"/(\d+)c(\d+)(?:-(\d+))?", ref.url, re.I)
    f_title = int(fm.group(1)) if fm else ref.title
    f_div = int(fm.group(2)) if fm else None
    f_chap = int(fm.group(3)) if fm and fm.group(3) else None

    # Choose one start per rule: the first occurrence that is followed by a
    # rule body (not a ToC line with dot leaders, not an inline cross-ref).
    first_any: dict[str, re.Match] = {}
    first_body: dict[str, re.Match] = {}
    for m in _HDR.finditer(text):
        t, d, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if t != f_title or (f_div is not None and d != f_div) or (f_chap is not None and c != f_chap):
            continue
        after = text[m.end(): m.end() + 200]
        if re.search(r"(?:\. ){3,}|\.{4,}", m.group(5) + after[:300]):
            continue  # table of contents
        key = f"{t}-{d}-{c}-{m.group(4)}"
        first_any.setdefault(key, m)
        if key not in first_body and _looks_like_body(text[m.end():]):
            first_body[key] = m
    starts = sorted(
        ((k, first_body.get(k, first_any[k])) for k in first_any),
        key=lambda kv: kv[1].start(),
    )
    # The PDF's own "Division 10—Name" is properly cased; the web page's
    # aria-label is often lowercase. For chapter names prefer the aria-label on
    # single-chapter files, since two-column PDFs wrap "Chapter N—..." lines.
    chap_names = _chapter_names(text)
    div_name = _division_name(text) or ref.division_name

    regs: list[Reg] = []
    for i, (key, m) in enumerate(starts):
        end = starts[i + 1][1].start() if i + 1 < len(starts) else len(text)
        seg = text[m.start():end].strip()
        t, d, c, r = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
        cite = f"{t} CSR {d}-{c}.{r}"

        # Heading: rest of the header line + continuation lines up to the
        # first block marker (PURPOSE:, "(Rescinded ...", "(1)", ...).
        heading = m.group(5).strip()
        for ln in seg.splitlines()[1:9]:
            if not ln or _STARTS_BLOCK.match(ln) or re.match(r"^[A-Z ]{2,}:", ln):
                break
            if heading.endswith("-") and ln[:1].islower():
                heading = heading[:-1] + ln  # "Com-" + "panies"
            else:
                heading = f"{heading} {ln}".strip()
            if len(heading) > 220:
                break
        heading = _ws(heading)
        body = _reflow(seg)
        if len(body) < 40:
            continue

        # Status markers sit right after the heading, sometimes on the same
        # line, so search the start of the body rather than past the heading.
        stub = body[:len(cite) + len(heading) + 200]
        status, ren_to, tr_to = "in_force", "", ""
        mm = re.search(r"\(Moved\s+[tf]o\s+(\d{1,2} CSR [\d.-]+\d)\)", stub)
        tm = re.search(r"\(Transferred\s+to\s+(\d{1,2} CSR [\d.-]+\d)\)", stub)
        if mm:
            status, ren_to = "renumbered", mm.group(1)
        elif tm:
            status, tr_to = "transferred", tm.group(1)
        elif re.search(r"\((?:Rescinded|Removed)\b", stub):
            status = "repealed"
        elif re.search(r"\(Reserved\)", stub):
            status = "reserved"
        elif len(body.split()) < 60 and re.search(r"\bexpired\b", body) and "(1)" not in body:
            status = "expired"  # lapsed emergency rule: history line only
        heading = re.sub(r"\s*\((?:Rescinded|Moved|Transferred|Removed)\b.*$", "", heading)
        heading = re.sub(r"\s+(?:Emergency rule filed|AUTHORITY:)\b.*$", "", heading)
        heading = re.sub(r"(\w)- (?=[A-Z])", r"\1-", heading)  # "Interest- Share"

        auth = ""
        history = ""
        am = re.search(r"AUTHORITY:\s*(.+)", body, re.S)
        if am:
            history = _ws(am.group(1))
            sm = re.match(r"(.+?)(?=\s(?:Original rule|This rule|This version|Emergency|Material in|Rescinded|Moved)\b)", history)
            auth = (sm.group(1) if sm else history[:300]).rstrip(" .*")
        eff = re.findall(rf"effective\s+((?:{_MONTHS})\s+\d{{1,2}},\s+(\d{{4}}))", history)
        years = sorted({int(y) for _, y in eff})
        effective_date = eff[-1][0] if eff else ""

        cfr = sorted(set(re.findall(r"\b\d{1,2} CFR (?:part |parts |sections? )?\d+(?:\.\d+)?", body)))
        usc = sorted(set(_ws(x) for x in re.findall(r"\b\d{1,2} U\.?S\.?C\.?\s*(?:§+\s*|sections?\s+)?\d+[a-z]?", body)))

        regs.append(Reg(
            title=t, division=d, chapter=c, rule=r, cite=cite, heading=heading,
            raw_text=body, status=status, renumbered_to=ren_to, transferred_to=tr_to,
            statutory_authority=auth, history=history, amendment_years=years,
            effective_date=effective_date, title_name=ref.title_name,
            division_name=div_name,
            chapter_name=(ref.chapter_name if f_chap is not None else "") or chap_names.get(c, ""),
            source_url=ref.url, cfr_refs=cfr, usc_refs=usc,
        ))
    return regs


# ---------------------------------------------------------------------------
# Record shape (mirrors the other state-regulation ingests)
# ---------------------------------------------------------------------------
def _act_id(r: Reg) -> str:
    return f"STATE_MO_CSR_T{r.title}_D{r.division}_C{r.chapter}_R{r.rule}"


def to_chunk_record(r: Reg) -> dict:
    act_id = _act_id(r)
    citation = r.cite
    section_title = f"{citation}. {r.heading}".strip().rstrip(".")
    text = r.raw_text
    status_label = {
        "in_force": "In Force", "repealed": "Rescinded", "renumbered": "Moved",
        "transferred": "Transferred", "reserved": "Reserved", "expired": "Expired",
    }.get(r.status, r.status)
    meta_lines: list[str] = []
    if r.effective_date:
        meta_lines.append(f"Effective: {r.effective_date}")
    if r.statutory_authority:
        meta_lines.append(f"Statutory Authority: {r.statutory_authority}")
    meta_header = ("\n" + "\n".join(meta_lines)) if meta_lines else ""
    text_for_embedding = (
        f"Regulation: Missouri Code of State Regulations | US | Missouri | {status_label}\n"
        f"Title {r.title} ({r.title_name}) / Division {r.division}: {r.division_name}"
        f" / Chapter {r.chapter}: {r.chapter_name}\n"
        f"{citation}. {r.heading}{meta_header}\n\n{text}"
    )
    parent = f"us/mo/regulations/title={r.title}/division={r.division}/chapter={r.chapter}"
    md = {
        "act_id": act_id,
        "corpus_type": "state_regulation",
        "category": "state_regulation",
        "document_type": "regulation",
        "jurisdiction": "US",
        "country_code": "US",
        "state": "mo",
        "title_number": r.title,
        "title_name": f"Missouri Code of State Regulations — Title {r.title} ({r.title_name})",
        "title": "Missouri Code of State Regulations",
        "title_code": f"csr_{r.title}",
        "top_level_title": str(r.title),
        "chapter": f"{r.title} CSR {r.division}-{r.chapter}",
        "chapter_name": r.chapter_name,
        "division": r.division,
        "division_name": r.division_name,
        "section_number": citation,
        "section_title": section_title,
        "year": r.amendment_years[-1] if r.amendment_years else None,
        "act_status": r.status,
        "renumbered_to": r.renumbered_to,
        "transferred_to": r.transferred_to,
        "level_classifier": "regulation",
        "effective_date": r.effective_date or None,
        "statutory_authority": r.statutory_authority or None,
        "history": r.history or None,
        "issuing_agency": r.division_name or r.title_name or None,
        "issuing_agency_code": f"{r.title} CSR {r.division}",
        "citation": citation,
        "citation_short": citation,
        "display_label": citation,
        "display_title": section_title,
        "display_path": (
            f"Missouri Code of State Regulations / Title {r.title} ({r.title_name}) / "
            f"Division {r.division} / Chapter {r.chapter} / {citation}"
        ),
        "breadcrumb": [
            {"type": "title", "num": str(r.title), "label": f"Title {r.title}", "name": r.title_name},
            {"type": "division", "num": str(r.division), "label": f"Division {r.division}",
             "name": r.division_name},
            {"type": "chapter", "num": str(r.chapter), "label": f"Chapter {r.chapter}",
             "name": r.chapter_name},
            {"type": "regulation", "num": citation, "label": citation, "name": r.heading},
        ],
        "sort_key": act_id,
        "word_count": len(text.split()),
        "subsection_count": 0,
        "subsection_letters": [],
        "numbered_paragraph_count": len(re.findall(r"(?m)^\(\d+\)", text)),
        "cross_references_count": len(r.cfr_refs) + len(r.usc_refs),
        "cross_references_usc": r.usc_refs,
        "cross_references_cfr": r.cfr_refs,
        "amendment_years": r.amendment_years,
        "amendments_count": len(r.amendment_years),
        "last_amended_year": r.amendment_years[-1] if r.amendment_years else None,
        "public_laws_referenced": [],
        "public_laws_count": 0,
        "source_url": r.source_url,
        "parent_id": parent,
        "raw_node_id": f"{parent}/rule={r.rule}",
        "parent_chunk_id": _point_id(act_id, -1, text),
        "full_text_sha1": _sha1(text),
    }
    return {
        "point_id": _point_id(act_id, 0, text),
        "text_for_embedding": text_for_embedding,
        "raw_text": text,
        "metadata": md,
    }


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------
def process_pdf(ref: PdfRef) -> tuple[list[Reg], list[str], bool]:
    pdf = _get(ref.url, binary=True)
    if not pdf:
        return [], [], False
    text, links = _pdf_text_and_links(pdf)
    return parse_pdf(ref, text), links, True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--titles", default="", help="Comma-separated title numbers (default: all 23).")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="At most N PDFs per title (testing).")
    ap.add_argument("--in-force-only", action="store_true",
                    help="Skip rescinded/moved/transferred stubs.")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    titles = list_titles()
    if args.titles:
        wanted = {int(t) for t in args.titles.split(",") if t.strip()}
        titles = [t for t in titles if t[0] in wanted]
    print(f"[CSR] {len(titles)} titles", flush=True)

    queue: list[PdfRef] = []
    for num, name, url in titles:
        pdfs = list_pdfs(num, name, url)
        if args.limit:
            pdfs = pdfs[: args.limit]
        print(f"  [title {num:>2} {name[:40]}] {len(pdfs)} PDFs", flush=True)
        queue.extend(pdfs)
    title_names = {num: name for num, name, _ in titles}

    seen = {p.url.lower() for p in queue}
    regs: list[Reg] = []
    failed: list[str] = []
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        pending = {ex.submit(process_pdf, p): p for p in queue}
        while pending:
            for fut in as_completed(list(pending)):
                ref = pending.pop(fut)
                done += 1
                try:
                    got, links, ok = fut.result()
                except Exception as e:
                    got, links, ok = [], [], False
                    print(f"  ! {ref.url}: {e}", flush=True)
                if not ok:
                    failed.append(ref.url)
                regs.extend(got)
                if not args.limit:
                    for link in links:
                        lm = _PDF_RE.search(link)
                        if lm and link.lower() not in seen and int(lm.group(1)) in title_names:
                            seen.add(link.lower())
                            t = int(lm.group(1))
                            new = PdfRef(link, t, title_names[t])
                            pending[ex.submit(process_pdf, new)] = new
                if done % 100 == 0:
                    print(f"  ... {done} PDFs, {len(regs):,} rules, {time.time() - t0:.0f}s", flush=True)
                break

    if failed:
        # sos.mo.gov intermittently drops requests under load; one slow
        # sequential pass recovers nearly all of them.
        print(f"[CSR] retrying {len(failed)} failed PDFs sequentially", flush=True)
        retry, failed = failed, []
        by_url = {p.url: p for p in queue}
        for url in retry:
            time.sleep(3)
            lm = _PDF_RE.search(url)
            ref = by_url.get(url) or PdfRef(url, int(lm.group(1)), title_names.get(int(lm.group(1)), ""))
            got, _, ok = process_pdf(ref)
            if not ok:
                failed.append(url)
            regs.extend(got)

    records: dict[str, dict] = {}
    for r in regs:
        if args.in_force_only and r.status != "in_force":
            continue
        rec = to_chunk_record(r)
        aid = rec["metadata"]["act_id"]
        cur = records.get(aid)
        if cur is None or len(rec["raw_text"]) > len(cur["raw_text"]):
            records[aid] = rec
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for aid in sorted(records):
            fh.write(json.dumps(records[aid], ensure_ascii=False) + "\n")

    by_status: dict[str, int] = {}
    for rec in records.values():
        s = rec["metadata"]["act_status"]
        by_status[s] = by_status.get(s, 0) + 1
    print(f"\n=== Done: {done} PDFs ({len(failed)} failed), {len(records):,} rules {by_status}, "
          f"{time.time() - t0:.0f}s ===", flush=True)
    for u in failed[:20]:
        print(f"  failed: {u}", flush=True)
    print(f"JSONL: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
