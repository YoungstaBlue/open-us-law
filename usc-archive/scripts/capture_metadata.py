import hashlib, json, re, html
from datetime import datetime, timezone, timedelta
from pathlib import Path

RAW = Path(__file__).resolve().parent.parent / "02_raw" / "tier1-govinfo-2023"
CST = timezone(timedelta(hours=-5))

SOURCES = {
    "18.241":   ("govinfo.gov", "https://www.govinfo.gov/content/pkg/USCODE-2023-title18/html/USCODE-2023-title18-partI-chap13-sec241.htm", True),
    "18.242":   ("govinfo.gov", "https://www.govinfo.gov/content/pkg/USCODE-2023-title18/html/USCODE-2023-title18-partI-chap13-sec242.htm", True),
    "18.1512":  ("govinfo.gov", "https://www.govinfo.gov/content/pkg/USCODE-2023-title18/html/USCODE-2023-title18-partI-chap73-sec1512.htm", True),
    "18.1519":  ("govinfo.gov", "https://www.govinfo.gov/content/pkg/USCODE-2023-title18/html/USCODE-2023-title18-partI-chap73-sec1519.htm", True),
    "42.1983":  ("govinfo.gov", "https://www.govinfo.gov/content/pkg/USCODE-2023-title42/html/USCODE-2023-title42-chap21-subchapI-sec1983.htm", False),
    "42.1985":  ("govinfo.gov", "https://www.govinfo.gov/content/pkg/USCODE-2023-title42/html/USCODE-2023-title42-chap21-subchapI-sec1985.htm", False),
    "34.12601": ("govinfo.gov", "https://www.govinfo.gov/content/pkg/USCODE-2023-title34/html/USCODE-2023-title34-subtitleI-chap121-subchapVIII-partB-sec12601.htm", False),
}
SOURCE_TYPE = {
    "govinfo.gov": "U.S. Government Publishing Office - govinfo.gov, United States Code 2023 Edition",
    "uscode.house.gov": "Office of the Law Revision Counsel, U.S. House of Representatives - uscode.house.gov (preliminary/current edition)",
}

def html_to_text(raw: str) -> str:
    body = re.search(r"<body[^>]*>(.*)</body>", raw, re.S | re.I)
    s = body.group(1) if body else raw
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|h\d|li|tr)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip() + "\n"

for name, (host, url, positive) in SOURCES.items():
    title, section = name.split(".")
    stem = f"US-USC-{title}-{section}"
    raw_path = RAW / f"{stem}.htm"
    raw = raw_path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    text = html_to_text(raw.decode("utf-8", errors="replace"))
    (RAW / f"{stem}.txt").write_text(text, encoding="utf-8")
    meta = {
        "section_id": stem, "section": f"{title} U.S.C. § {section}",
        "title_number": int(title),
        "section_number": section,
        "official_url": url,
        "retrieved_at": datetime.now(CST).isoformat(timespec="seconds"),
        "source_type": SOURCE_TYPE[host],
        "source_file": raw_path.name,
        "text_file": f"{stem}.txt",
        "positive_law_title": positive,
        "integrity_sha256": sha,
        "raw_bytes": len(raw),
    }
    (RAW / f"{stem}.metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"{name}: sha256={sha[:16]}... text={len(text)} chars")
