"""Live official-source fetchers used only by verify.py, one section at a time.

This intentionally does NOT reuse the full bulk-ingest pipeline (proxy pool,
R2 upload, chunking) -- verify.py checks one section against the government
site right now, on whatever machine the user is running it from. For Missouri
it reuses the existing ``scripts/statutes/mo_bulk`` HTML client/parser (the
same official pages the bulk ingester walks), since duplicating that parsing
logic would just be a second place for it to drift. For the US Code there is
no existing single-section fetcher in this repo (``download_usc.py`` pulls
whole-title ZIPs from the GovInfo API), so this adds a small one against
uscode.house.gov's public, keyless per-section view.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

_HERE = Path(__file__).resolve().parent
_STATUTES = _HERE.parent / "statutes"
_SCRAPERS = _HERE.parent / "state_scrapers"


@dataclass
class LiveResult:
    ok: bool
    text: str = ""
    note: str = ""  # e.g. "laws in effect as of ..."
    url: str = ""
    error: str = ""


def fetch_mo_section(section_number: str) -> LiveResult:
    """Fetch a Missouri Revised Statutes section straight from revisor.mo.gov.

    Reuses scripts/statutes/mo_bulk (client.py + parse.py), the same official
    pages ingest_mo_bulk.py walks. revisor.mo.gov geo-blocks some non-US hosts;
    if that's the failure mode here too, set VAQUILL_USE_PROXY=1 (see
    mo_bulk/client.py) with proxy credentials configured, same as the ingester.
    """
    for p in (str(_STATUTES), str(_SCRAPERS)):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        from mo_bulk import client as C
        from mo_bulk import parse as P
    except ImportError as exc:
        return LiveResult(ok=False, error=f"could not import mo_bulk: {exc}")

    url = f"{C.BASE}/OneSection.aspx?section={section_number}"
    try:
        html = C.section(section_number)
    except Exception as exc:
        return LiveResult(
            ok=False,
            url=url,
            error=(
                f"{exc}\n"
                "  (revisor.mo.gov geo-blocks some non-US egress; if this keeps failing, "
                "retry with VAQUILL_USE_PROXY=1 and proxy credentials set -- see "
                "scripts/statutes/mo_bulk/client.py)"
            ),
        )

    paragraphs, history = P.section_content(html)
    if not paragraphs:
        return LiveResult(
            ok=False, url=url, error="page fetched but no section body found (repealed/reserved/renumbered?)"
        )
    text = "\n\n".join(paragraphs + ([history] if history else []))
    return LiveResult(ok=True, text=text, note=history, url=url)


_GRANULE_RE = re.compile(r"USC-(?P<edition>\w+)-title(?P<title>\w+)-section(?P<section>[\w.-]+)")


def fetch_usc_section(title_number: str, section_number: str, *, edition: str = "prelim") -> LiveResult:
    """Fetch a US Code section from uscode.house.gov's public per-granule viewer.

    No API key needed (unlike the GovInfo API used by download_usc.py). URL
    scheme: ``view.xhtml?req=granuleid:USC-{edition}-title{T}-section{S}``.
    ``edition="prelim"`` is the House Office of the Law Revision Counsel's
    continuously-updated preliminary text -- the closest thing to "current law"
    this site publishes without a classification cycle lag.
    """
    granule = f"USC-{edition}-title{title_number}-section{section_number}"
    url = f"https://uscode.house.gov/view.xhtml?req=granuleid:{granule}&num=0&edition={edition}"
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "open-us-law-local-kb/1.0"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        return LiveResult(ok=False, url=url, error=str(exc))

    soup = BeautifulSoup(resp.text, "html.parser")
    # The published corpus's `text` field is the operative statute PLUS the
    # source-credit line and any statutory notes (amendment history,
    # effective-date provisos, short-title notes, ...) concatenated as
    # paragraphs -- see node_to_payload/mo_bulk for the same pattern. Match
    # that shape here (statutory-body, source-credit, note-head, note-body, in
    # document order) or a real amendment will look identical to "the local
    # copy is just missing the notes this page always carries".
    parts = soup.select("p.statutory-body, p.source-credit, h4.note-head, p.note-body")
    if not parts:
        return LiveResult(
            ok=False,
            url=url,
            error=(
                "no statutory-body text found -- the section may not exist under this "
                "title/section number, or the site's markup changed"
            ),
        )
    text = "\n\n".join(p.get_text(" ", strip=True) for p in parts)

    note_span = soup.select_one("span.lawsInEffect")
    note = note_span.get_text(" ", strip=True) if note_span else ""

    return LiveResult(ok=True, text=text, note=note, url=url)
