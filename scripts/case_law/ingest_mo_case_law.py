#!/usr/bin/env python3
"""Ingest Missouri case law (Supreme Court of Missouri + Missouri Court of Appeals).

corpus_type='case_law', document_type='opinion', act_id prefix 'CASE_MO_'.
state='mo'. One record per case; all opinions (majority, concurrences,
dissents) are kept in order in raw_text and listed in metadata.opinions.

Sources
-------
1. Caselaw Access Project (Harvard Law School Library), https://static.case.law
   -- 1821 through 2019, full text, no key. Missouri appears in four reporters:

     mo      Missouri Reports                  1821-1956   (all Missouri)
     mo-app  Missouri Appeal Reports           1876-1994   (all Missouri)
     sw2d    South Western Reporter 2d         1928-1999   (mixed states)
     sw3d    South Western Reporter 3d         1999-2019   (mixed states)

   Each reporter's VolumesMetadata.json lists the jurisdictions in every
   volume, so only volumes containing Missouri are fetched. Each volume is one
   zip of per-case JSON (json/<page>-<n>.json); cases are kept when
   jurisdiction.name_long == "Missouri". Pre-1956 Supreme Court cases appear in
   both Mo. and S.W.2d; they are merged into one record carrying both
   citations.

2. CourtListener (Free Law Project), https://www.courtlistener.com -- opinions
   filed after CAP's coverage ends. Enabled only when COURTLISTENER_API_TOKEN is
   set (free account: https://www.courtlistener.com/profile/api/): the search
   endpoint is anonymous, but opinion full text needs the token. Courts:
   'mo' (Supreme Court of Missouri) and 'moctapp' (Missouri Court of Appeals).

Resumable: finished CAP volumes are recorded in <out>.progress and skipped on
re-run; raw records accumulate in <out>.raw.jsonl and the final deduplicated
file is written at the end.

Case law is not "current law" the way a statute is: this snapshot carries no
subsequent history (reversed / overruled / abrogated). Check treatment before
citing anything.

Output: state_mo_case_law.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(os.environ.get("OUT_DIR", "./data"))
OUT = DATA_DIR / "state_mo_case_law.jsonl"

CAP = "https://static.case.law"
CAP_REPORTERS = ["mo", "mo-app", "sw2d", "sw3d"]
CL_API = "https://www.courtlistener.com/api/rest/v4"
CL_COURTS = {"mo": "Supreme Court of Missouri", "moctapp": "Missouri Court of Appeals"}
UA = "Mozilla/5.0 (open-us-law ingestion bot; +https://github.com/Vaquill-AI/open-us-law)"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})
SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16))


def _load_env() -> None:
    env_path = _PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def _get(url: str, retries: int = 5, **kw) -> requests.Response | None:
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=120, **kw)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "30") or 30))
                continue
            r.raise_for_status()
            return r
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _point_id(act_id: str, idx: int, text: str) -> str:
    seed = f"{act_id}::{idx}::{_sha1(text)[:12]}"
    return str(uuid.UUID(hashlib.md5(seed.encode()).hexdigest()))


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------------------
# Record shape
# ---------------------------------------------------------------------------
_OPINION_LABEL = {
    "majority": "Opinion", "concurrence": "Concurring opinion", "dissent": "Dissenting opinion",
    "concurring-in-part-and-dissenting-in-part": "Concurring in part and dissenting in part",
    "plurality": "Plurality opinion", "per-curiam": "Per curiam", "remittitur": "Remittitur",
    "rehearing": "On rehearing", "on-the-merits": "On the merits", "addendum": "Addendum",
}


def _court_key(court_name: str) -> str:
    return "moctapp" if "appeal" in court_name.lower() else "mo"


def build_record(case: dict) -> dict:
    """case: normalized dict (see cap_case / cl_case)."""
    court = case["court"]
    ckey = _court_key(court)
    cites = case["citations"]
    citation = cites[0] if cites else (case.get("docket_number") or "")
    year = int(case["decision_date"][:4]) if case.get("decision_date") else None
    short = f"{case['name_abbreviation']}, {citation}" + (f" ({case['court_abbrev']} {year})" if year else "")

    parts: list[str] = []
    if case.get("head_matter"):
        parts.append(case["head_matter"].strip())
    for op in case["opinions"]:
        label = _OPINION_LABEL.get(op.get("type") or "", (op.get("type") or "Opinion").replace("-", " ").capitalize())
        author = f" -- {op['author']}" if op.get("author") else ""
        parts.append(f"[{label}{author}]\n{op['text'].strip()}")
    raw_text = "\n\n".join(p for p in parts if p)

    act_id = case["act_id"]
    text_for_embedding = (
        f"Case law: {court} | US | Missouri\n"
        f"{case['name']}\n{'; '.join(cites) or case.get('docket_number', '')} | Decided {case.get('decision_date', '')}"
        + (f" | {case['docket_number']}" if case.get("docket_number") else "")
        + f"\n\n{raw_text}"
    )
    md = {
        "act_id": act_id,
        "corpus_type": "case_law",
        "category": "case_law",
        "document_type": "opinion",
        "jurisdiction": "US",
        "country_code": "US",
        "state": "mo",
        "title": "Missouri Case Law",
        "title_name": court,
        "title_code": f"mo_{ckey}",
        "top_level_title": f"caselaw-mo-{ckey}",
        "level_classifier": "case",
        "court": court,
        "court_abbrev": case["court_abbrev"],
        "court_id": ckey,
        "case_name": case["name"],
        "case_name_short": case["name_abbreviation"],
        "decision_date": case.get("decision_date") or None,
        "year": year,
        "docket_number": case.get("docket_number") or None,
        "section_number": citation,
        "section_title": case["name_abbreviation"],
        "citation": citation,
        "citations": cites,
        "citation_short": short,
        "display_label": citation,
        "display_title": case["name_abbreviation"],
        "display_path": f"Missouri Case Law / {court} / {year or ''} / {case['name_abbreviation']}",
        "breadcrumb": ["Missouri Case Law", court, str(year or ""), case["name_abbreviation"]],
        "judges": case.get("judges") or [],
        "parties": case.get("parties") or [],
        "attorneys": case.get("attorneys") or [],
        "opinions": [
            {"type": op.get("type"), "author": op.get("author"), "word_count": len(op["text"].split())}
            for op in case["opinions"]
        ],
        "cites_to": case.get("cites_to") or [],
        "cross_references_count": len(case.get("cites_to") or []),
        "cross_references_usc": [c for c in case.get("cites_to") or [] if "U.S.C" in c],
        "cross_references_cfr": [c for c in case.get("cites_to") or [] if "C.F.R" in c],
        "act_status": "published",  # no subsequent-history data: check treatment before citing
        "sort_key": f"{ckey}/{case.get('decision_date', '')}/{act_id}",
        "word_count": len(raw_text.split()),
        "source": case["source"],
        "source_ids": case["source_ids"],
        "source_url": case["source_url"],
        "raw_node_id": act_id,
        "parent_id": f"us/mo/case_law/court={ckey}",
        "full_text_sha1": _sha1(raw_text),
    }
    return {
        "point_id": _point_id(act_id, 0, raw_text),
        "text_for_embedding": text_for_embedding,
        "raw_text": raw_text,
        "metadata": md,
    }


# ---------------------------------------------------------------------------
# Caselaw Access Project
# ---------------------------------------------------------------------------
def cap_volumes(reporter: str) -> list[str]:
    r = _get(f"{CAP}/{reporter}/VolumesMetadata.json")
    if r is None:
        return []
    vols = []
    for v in r.json():
        juris = v.get("jurisdictions") or []
        if any((j.get("name_long") == "Missouri") for j in juris):
            vols.append(str(v.get("volume_folder") or v["volume_number"]))
    return vols


def _cap_opinions(casebody: dict) -> list[dict]:
    ops = []
    for op in casebody.get("opinions") or []:
        text = op.get("text") or ""
        if text.strip():
            ops.append({"type": op.get("type"), "author": _ws(op.get("author") or ""), "text": text})
    return ops


def cap_case(reporter: str, volume: str, d: dict) -> dict | None:
    body = d.get("casebody") or {}
    opinions = _cap_opinions(body)
    if not opinions:
        return None
    court = (d.get("court") or {})
    fname = d.get("file_name") or ""
    return {
        "act_id": f"CASE_MO_CAP_{d['id']}",
        "name": _ws(d.get("name") or ""),
        "name_abbreviation": _ws(d.get("name_abbreviation") or d.get("name") or ""),
        "decision_date": d.get("decision_date") or "",
        "docket_number": _ws(d.get("docket_number") or ""),
        "court": court.get("name") or "",
        "court_abbrev": court.get("name_abbreviation") or "",
        "citations": [c["cite"] for c in d.get("citations") or [] if c.get("cite")],
        "judges": body.get("judges") or [],
        "parties": body.get("parties") or [],
        "attorneys": body.get("attorneys") or [],
        "head_matter": body.get("head_matter") or "",
        "opinions": opinions,
        "cites_to": sorted({c["cite"] for c in d.get("cites_to") or [] if c.get("cite")}),
        "source": "cap",
        "source_ids": [f"cap:{d['id']}"],
        "source_url": f"{CAP}/{reporter}/{volume}/html/{fname}.html" if fname else f"{CAP}/{reporter}/{volume}/",
    }


def ingest_cap_volume(reporter: str, volume: str) -> tuple[str, list[dict], bool]:
    r = _get(f"{CAP}/{reporter}/{volume}.zip")
    if r is None:
        return volume, [], False
    out: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        for name in zf.namelist():
            if not (name.startswith("json/") and name.endswith(".json")):
                continue
            d = json.loads(zf.read(name))
            if (d.get("jurisdiction") or {}).get("name_long") != "Missouri":
                continue
            c = cap_case(reporter, volume, d)
            if c:
                out.append(c)
    return volume, out, True


# ---------------------------------------------------------------------------
# CourtListener (post-CAP)
# ---------------------------------------------------------------------------
def _strip_html(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(["p", "div", "blockquote", "h1", "h2", "h3", "h4", "li"]):
        tag.insert_after("\n")
    text = soup.get_text()
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _cl_opinion_text(op: dict) -> str:
    if (op.get("plain_text") or "").strip():
        return op["plain_text"]
    for key in ("html_with_citations", "html", "html_lawbox", "html_columbia", "xml_harvard"):
        if (op.get(key) or "").strip():
            return _strip_html(op[key])
    return ""


_CL_TYPE = {
    "010combined": "majority", "015unamimous": "majority", "020lead": "majority",
    "025plurality": "plurality", "030concurrence": "concurrence",
    "035concurrenceinpart": "concurring-in-part-and-dissenting-in-part",
    "040dissent": "dissent", "050addendum": "addendum", "060remittitur": "remittitur",
    "070rehearing": "rehearing", "080onthemerits": "on-the-merits",
}


def courtlistener_cases(token: str, filed_after: str, limit: int = 0) -> list[dict]:
    headers = {"Authorization": f"Token {token}"}
    url = (f"{CL_API}/search/?type=o&court={'+'.join(CL_COURTS)}"
           f"&filed_after={filed_after}&order_by=dateFiled+asc")
    hits: list[dict] = []
    while url:
        r = _get(url, headers=headers)
        if r is None:
            print(f"  [CL] search page failed: {url}", flush=True)
            break
        d = r.json()
        hits.extend(d.get("results") or [])
        url = d.get("next")
        print(f"  [CL] {len(hits)}/{d.get('count')} search hits", flush=True)
        if limit and len(hits) >= limit:
            hits = hits[:limit]
            break
        time.sleep(1)

    def one(h: dict) -> dict | None:
        opinions = []
        for o in h.get("opinions") or []:
            r = _get(f"{CL_API}/opinions/{o['id']}/", headers=headers)
            if r is None:
                continue
            op = r.json()
            text = _cl_opinion_text(op)
            if text.strip():
                opinions.append({"type": _CL_TYPE.get(op.get("type") or "", "majority"),
                                 "author": _ws(op.get("author_str") or ""), "text": text})
        if not opinions:
            return None
        court_id = h.get("court_id") or "mo"
        cites = [c for c in (h.get("citation") or []) if c]
        if h.get("neutralCite"):
            cites.append(h["neutralCite"])
        return {
            "act_id": f"CASE_MO_CL_{h['cluster_id']}",
            "name": _ws(h.get("caseNameFull") or h.get("caseName") or ""),
            "name_abbreviation": _ws(h.get("caseName") or ""),
            "decision_date": h.get("dateFiled") or "",
            "docket_number": _ws(h.get("docketNumber") or ""),
            "court": CL_COURTS.get(court_id, h.get("court") or ""),
            "court_abbrev": h.get("court_citation_string") or ("Mo." if court_id == "mo" else "Mo. Ct. App."),
            "citations": cites,
            "judges": [h["judge"]] if h.get("judge") else [],
            "parties": [], "attorneys": [h["attorney"]] if h.get("attorney") else [],
            "head_matter": "",
            "opinions": opinions,
            "cites_to": [],
            "source": "courtlistener",
            "source_ids": [f"cl:{h['cluster_id']}"],
            "source_url": f"https://www.courtlistener.com{h.get('absolute_url', '')}",
        }

    cases: list[dict] = []
    with ThreadPoolExecutor(max_workers=3) as ex:  # CL rate limits: stay gentle
        for i, fut in enumerate(as_completed([ex.submit(one, h) for h in hits]), start=1):
            c = fut.result()
            if c:
                cases.append(c)
            if i % 200 == 0:
                print(f"  [CL] {i}/{len(hits)} clusters fetched", flush=True)
    return cases


# ---------------------------------------------------------------------------
# Dedupe + write
# ---------------------------------------------------------------------------
def _dedupe_key(case: dict) -> str:
    # The same pre-1956 Supreme Court case is printed in both Mo. and S.W.2d
    # (and CAP/CourtListener overlap at the seam): same court, date and
    # opening words of the short name.
    words = re.findall(r"[a-z0-9]+", case["name_abbreviation"].lower())[:4]
    return f"{_court_key(case['court'])}|{case['decision_date']}|{' '.join(words)}"


def finalize(raw_path: Path, out_path: Path) -> dict[str, int]:
    best: dict[str, dict] = {}
    extra_cites: dict[str, list[str]] = {}
    extra_ids: dict[str, list[str]] = {}
    with open(raw_path, encoding="utf-8") as fh:
        for line in fh:
            c = json.loads(line)
            k = _dedupe_key(c)
            cur = best.get(k)
            extra_cites.setdefault(k, [])
            extra_ids.setdefault(k, [])
            for x in c["citations"]:
                if x not in extra_cites[k]:
                    extra_cites[k].append(x)
            for x in c["source_ids"]:
                if x not in extra_ids[k]:
                    extra_ids[k].append(x)
            size = sum(len(o["text"]) for o in c["opinions"])
            if cur is None or size > sum(len(o["text"]) for o in cur["opinions"]):
                best[k] = c
    counts: dict[str, int] = {}
    with open(out_path, "w", encoding="utf-8") as fh:
        for k in sorted(best, key=lambda k: (best[k]["decision_date"], k)):
            c = best[k]
            # Official state reporter cite first (Mo./Mo. App.), then S.W.
            c["citations"] = sorted(extra_cites[k], key=lambda x: (" S.W." in x, x))
            c["source_ids"] = extra_ids[k]
            rec = build_record(c)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            ck = rec["metadata"]["court_id"]
            counts[ck] = counts.get(ck, 0) + 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reporters", default=",".join(CAP_REPORTERS),
                    help="CAP reporter slugs (default: mo,mo-app,sw2d,sw3d). Empty to skip CAP.")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="At most N volumes per reporter / N CL hits (testing).")
    ap.add_argument("--courtlistener-after", default="2019-01-01",
                    help="Fetch CourtListener opinions filed after this date (needs COURTLISTENER_API_TOKEN).")
    ap.add_argument("--no-courtlistener", action="store_true")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    raw_path = args.out.with_suffix(".raw.jsonl")
    progress_path = args.out.with_suffix(".progress")
    done = set(progress_path.read_text().split()) if progress_path.exists() else set()
    t0 = time.time()

    reporters = [r.strip() for r in args.reporters.split(",") if r.strip()]
    for rep in reporters:
        vols = [v for v in cap_volumes(rep) if f"{rep}/{v}" not in done]
        if args.limit:
            vols = vols[: args.limit]
        print(f"[CAP {rep}] {len(vols)} volumes with Missouri cases to fetch", flush=True)
        n_cases = failed = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex, \
                open(raw_path, "a", encoding="utf-8") as raw, open(progress_path, "a") as prog:
            futs = [ex.submit(ingest_cap_volume, rep, v) for v in vols]
            for i, fut in enumerate(as_completed(futs), start=1):
                vol, cases, ok = fut.result()
                if not ok:
                    failed += 1
                    continue
                for c in cases:
                    raw.write(json.dumps(c, ensure_ascii=False) + "\n")
                raw.flush()
                prog.write(f"{rep}/{vol}\n")
                prog.flush()
                n_cases += len(cases)
                if i % 25 == 0 or i == len(vols):
                    print(f"  [CAP {rep}] {i}/{len(vols)} volumes, {n_cases:,} cases, "
                          f"{failed} failed, {time.time() - t0:.0f}s", flush=True)
        if failed:
            print(f"  [CAP {rep}] {failed} volumes failed -- re-run to retry them", flush=True)

    token = os.environ.get("COURTLISTENER_API_TOKEN", "")
    if args.no_courtlistener:
        pass
    elif not token:
        print("\n[CL] COURTLISTENER_API_TOKEN not set -- skipping opinions after CAP's coverage "
              f"(filed after {args.courtlistener_after}). Get a free token at "
              "https://www.courtlistener.com/profile/api/ and re-run.", flush=True)
    else:
        print(f"\n[CL] fetching Missouri opinions filed after {args.courtlistener_after}", flush=True)
        cl = courtlistener_cases(token, args.courtlistener_after, args.limit)
        with open(raw_path, "a", encoding="utf-8") as raw:
            for c in cl:
                raw.write(json.dumps(c, ensure_ascii=False) + "\n")
        print(f"[CL] {len(cl):,} cases", flush=True)

    if not raw_path.exists():
        print("nothing ingested", flush=True)
        return 1
    counts = finalize(raw_path, args.out)
    print(f"\n=== Done: {sum(counts.values()):,} Missouri cases {counts}, "
          f"{time.time() - t0:.0f}s ===\nJSONL: {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
