"""Shared helpers for the local_kb duckdb: paths, connection, query heuristics."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
DEFAULT_DB_PATH = HERE / "legal_kb.duckdb"
DEFAULT_CACHE_DIR = HERE / ".cache"

# A query "looks like a citation" when it's mostly digits with the punctuation
# statute numbers actually use (dots, dashes, an optional trailing letter) and
# no spaces -- "455.020", "10001", "12-3-45", "212a" all match; "order of
# protection" does not, and falls through to full-text search instead.
_CITATION_RE = re.compile(r"^[0-9]+[0-9A-Za-z.\-()]*$")


def looks_like_citation(query: str) -> bool:
    return bool(_CITATION_RE.match(query.strip()))


def open_db(db_path: Path | str = DEFAULT_DB_PATH, *, read_only: bool = True) -> duckdb.DuckDBPyConnection:
    db_path = Path(db_path)
    if read_only and not db_path.exists():
        raise FileNotFoundError(
            f"no database at {db_path}. Build it first:\n"
            f"  python scripts/local_kb/build_db.py"
        )
    con = duckdb.connect(str(db_path), read_only=read_only)
    return con


def load_meta(con: duckdb.DuckDBPyConnection) -> dict | None:
    try:
        row = con.execute("SELECT * FROM _meta ORDER BY built_at DESC LIMIT 1").fetchone()
        cols = [d[0] for d in con.description]
    except duckdb.CatalogException:
        return None
    if row is None:
        return None
    return dict(zip(cols, row))


def freshness_banner(meta: dict | None) -> str:
    if not meta:
        return "(no build metadata found -- database may predate this schema)"
    return (
        f"local snapshot: {meta.get('snapshot_version', '?')} "
        f"(published {meta.get('snapshot_date', '?')}, built locally {meta.get('built_at', '?')}) "
        f"-- corpora: {meta.get('corpora', '?')}"
    )


_WS_RE = re.compile(r"\s+")


_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?)\]])")
_SPACE_AFTER_OPEN_RE = re.compile(r"([\[(])\s+")
_SPACED_HYPHEN_RE = re.compile(r"\s*-\s*")


def normalize_text(text: str) -> str:
    """Collapse whitespace and unify quote/dash variants for text comparison.

    Also closes up "space before punctuation" / "space after an opening
    bracket" -- an artifact of joining HTML text nodes with BeautifulSoup's
    ``get_text(" ", ...)`` (a live-source fetch) that a plain-text parquet
    field never has, which otherwise makes verify.py report a byte-identical
    section as DIFFERS.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = (
        text.replace("‘", "'").replace("’", "'")
        .replace("“", '"').replace("”", '"')
        .replace("–", "-").replace("—", "-")
        .replace("\xa0", " ")
    )
    text = _WS_RE.sub(" ", text).strip()
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    text = _SPACE_AFTER_OPEN_RE.sub(r"\1", text)
    # A live fetch's HTML tag boundaries sometimes land right next to a bare
    # hyphen (public-law numbers like "108-136", date ranges), inserting a
    # space get_text() has no way to know isn't real -- collapse it so a pure
    # rendering artifact doesn't masquerade as a text difference.
    text = _SPACED_HYPHEN_RE.sub("-", text)
    return text


def snippet(text: str, width: int = 240) -> str:
    text = normalize_text(text)
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


NOT_LEGAL_ADVICE = (
    "Legal information, not legal advice. This is a point-in-time snapshot -- "
    "always confirm anything you rely on against the live official source "
    "(that's what verify.py is for)."
)
