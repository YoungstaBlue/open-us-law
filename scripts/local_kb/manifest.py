"""Corpus registry + snapshot manifest for the local_kb tool.

The published snapshot (see root README "Download the data") is mirrored on
Cloudflare R2 with a manifest at ``<BASE_URL>/latest.json`` that always points
at the current dated release (``v2026.08`` at time of writing) and lists every
parquet file with its size and sha256. We resolve corpus keys against that
manifest instead of hardcoding URLs, so a new snapshot needs no code change
here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

import requests

BASE_URL = "https://oss-data-us.vaquill.ai"
MANIFEST_URL = f"{BASE_URL}/latest.json"

# corpus key -> (parquet filename, jurisdiction tag, human label, opt-in?)
# jurisdiction matches the `state` column value used across the corpus
# (states use their two-letter code; federal law uses "federal").
CORPORA: dict[str, dict] = {
    "mo_statutes": {
        "file": "us_mo_statutes.parquet",
        "jurisdiction": "mo",
        "label": "Missouri Revised Statutes (RSMo)",
        "opt_in": False,
    },
    "mo_constitutions": {
        "file": "us_mo_constitutions.parquet",
        "jurisdiction": "mo",
        "label": "Missouri Constitution",
        "opt_in": False,
    },
    "mo_guidance": {
        "file": "us_mo_guidance.parquet",
        "jurisdiction": "mo",
        "label": "Missouri agency guidance",
        "opt_in": False,
    },
    "federal_statutes": {
        "file": "us_federal_statutes.parquet",
        "jurisdiction": "federal",
        "label": "United States Code (USC)",
        "opt_in": False,
    },
    "federal_constitutions": {
        "file": "us_federal_constitutions.parquet",
        "jurisdiction": "federal",
        "label": "US Constitution",
        "opt_in": False,
    },
    "federal_court_rules": {
        "file": "us_federal_court_rules.parquet",
        "jurisdiction": "federal",
        "label": "Federal Rules of Court",
        "opt_in": False,
    },
    "federal_regulations": {
        "file": "us_federal_regulations.parquet",
        "jurisdiction": "federal",
        "label": "Code of Federal Regulations (eCFR) -- ~2.8GB, opt-in only",
        "opt_in": True,
    },
}

# What `build_db.py` fetches with no --corpora flag: "MO + federal" statutes,
# the pair the quickstart is built around.
DEFAULT_CORPORA: tuple[str, ...] = ("mo_statutes", "federal_statutes")


@dataclass(frozen=True)
class ManifestEntry:
    key: str
    file: str
    url: str
    bytes: int
    sha256: str
    jurisdiction: str
    label: str


def fetch_manifest(*, timeout: float = 30.0) -> dict:
    """GET latest.json. Raises RuntimeError with a plain-English message on failure."""
    try:
        resp = requests.get(MANIFEST_URL, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"could not reach the snapshot manifest at {MANIFEST_URL}: {exc}\n"
            "(check your network connection; this is a public, unauthenticated URL)"
        ) from exc


def resolve(keys: Iterable[str], manifest: dict | None = None) -> list[ManifestEntry]:
    """Resolve corpus keys to their current download URL + sha256 via the manifest.

    Unknown keys raise KeyError with the full registry listed, so a typo fails
    loudly instead of silently building an incomplete database.
    """
    keys = list(keys)
    unknown = [k for k in keys if k not in CORPORA]
    if unknown:
        known = ", ".join(sorted(CORPORA))
        raise KeyError(f"unknown corpus key(s): {', '.join(unknown)}. Known corpora: {known}")

    manifest = manifest or fetch_manifest()
    by_file = {f["file"]: f for f in manifest.get("files", [])}

    out: list[ManifestEntry] = []
    for key in keys:
        spec = CORPORA[key]
        entry = by_file.get(spec["file"])
        if entry is None:
            raise RuntimeError(
                f"corpus '{key}' ({spec['file']}) is not in the current manifest "
                f"({manifest.get('version', '?')}) -- the snapshot layout may have changed."
            )
        out.append(
            ManifestEntry(
                key=key,
                file=entry["file"],
                url=entry["url"],
                bytes=entry["bytes"],
                sha256=entry["sha256"],
                jurisdiction=spec["jurisdiction"],
                label=spec["label"],
            )
        )
    return out


def describe_corpora() -> str:
    lines = ["available corpora:"]
    for key, spec in CORPORA.items():
        tag = " (opt-in, large)" if spec["opt_in"] else ""
        default = " [default]" if key in DEFAULT_CORPORA else ""
        lines.append(f"  {key:<24} {spec['label']}{tag}{default}")
    return "\n".join(lines)
