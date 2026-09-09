"""Phase 5: compact print edition from the VALIDATED corpus (03_validated/03_usc_sections.csv).

Landscape Letter, 3 columns, 6-pt body, narrow printer-safe margins, running page headers,
title/chapter breaks, and a section-number index. Unicode font (DejaVu) so §, em-dashes and
curly quotes print correctly.

  python3 scripts/build_print.py tier1     -> 05_print/05_USC_Tier1_Verbatim_6pt.pdf  (the 7 cited sections)
  python3 scripts/build_print.py index     -> 05_print/05_USC_Master_Index_6pt.pdf   (all current sections)
  python3 scripts/build_print.py full      -> 05_print/05_USC_Verbatim_Complete_6pt.pdf (all current sections)
  python3 scripts/build_print.py html      -> 05_print/05_USC_Tier1_Proof.html       (normal-size proofreading copy)
"""
import csv, html, re, sys
from pathlib import Path
from fpdf import FPDF

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "03_validated" / "03_usc_sections.csv"
OUT = ROOT / "05_print"; OUT.mkdir(exist_ok=True)
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
csv.field_size_limit(10**9)
TIER1 = {"US-USC-18-241", "US-USC-18-242", "US-USC-18-1512", "US-USC-18-1519", "US-USC-42-1983", "US-USC-42-1985", "US-USC-34-12601"}

def sec_sort_key(r):
    m = re.match(r"(\d+)(.*)", r["section_number"]); return (int(r["title_number"]), int(m.group(1)), m.group(2))

def load(current_only=True):
    rows = [r for r in csv.DictReader(open(CSV, encoding="utf-8")) if (r["is_current"] == "True" or not current_only)]
    return sorted(rows, key=sec_sort_key)

class Print(FPDF):
    def __init__(self, header_text):
        super().__init__(orientation="L", unit="mm", format="Letter")
        self.header_text = header_text
        self.add_font("DV", "", str(FONT_DIR / "DejaVuSerif.ttf"))
        self.add_font("DV", "B", str(FONT_DIR / "DejaVuSerif-Bold.ttf"))
        self.add_font("DM", "", str(FONT_DIR / "DejaVuSansMono.ttf"))
        self.set_margins(8, 10, 8); self.set_auto_page_break(True, margin=8)
        self.cols = 3; self.gap = 4
        self.col_w = (self.w - self.l_margin - self.r_margin - self.gap * (self.cols - 1)) / self.cols
        self.col = 0
    def header(self):
        self.set_font("DM", "", 6); self.set_text_color(90)
        self.cell(0, 4, self.header_text, align="L"); self.cell(0, 4, f"page {self.page_no()}", align="R", new_x="LMARGIN", new_y="NEXT")
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y()); self.ln(1)
        self.set_text_color(0); self.top_y = self.get_y(); self.col = 0; self.set_x(self.col_x())
    def col_x(self): return self.l_margin + self.col * (self.col_w + self.gap)
    def accept_page_break(self):
        if self.col < self.cols - 1:
            self.col += 1; self.set_y(self.top_y); self.set_x(self.col_x()); return False
        return True
    def para(self, text, style="", size=6, lh=None):
        self.set_font("DV", style, size); self.set_x(self.col_x())
        self.multi_cell(self.col_w, lh or size * 0.42, text, new_x="LEFT", new_y="NEXT"); self.set_x(self.col_x())
    def section(self, r):
        self.ln(1.2)
        self.set_font("DV", "B", 7); self.set_x(self.col_x())
        self.multi_cell(self.col_w, 3, f"{r['title_number']} U.S.C. § {r['section_number']}  {r['catchline']}", new_x="LEFT", new_y="NEXT")
        self.set_font("DM", "", 5); self.set_text_color(90); self.set_x(self.col_x())
        hdr = f"{r['section_id']} · Title {r['title_number']} {r['title_name']}" + (f" · Ch. {r['chapter_number']} {r['chapter_name']}" if r['chapter_number'] else "")
        hdr += f" · {r['status']} · {r['source_edition']} · retrieved {r['retrieved_at'][:10]}\n{r['official_url']}\nsha256 {r['sha256'][:16]}…"
        self.multi_cell(self.col_w, 2.2, hdr, new_x="LEFT", new_y="NEXT"); self.set_text_color(0)
        if r["effective_date_note"]:
            self.set_font("DM", "", 5); self.set_x(self.col_x()); self.multi_cell(self.col_w, 2.2, r["effective_date_note"][:300], new_x="LEFT", new_y="NEXT")
        body = r["verbatim_text"] or "[no operative text — see status]"
        body = re.sub(r"\s(\([a-z]\)|\(\d+\)|\([A-Z]\)|\([ivx]+\))\s", r"\n\1 ", " " + body)
        self.para(body.strip())
        if r["source_credit"]: self.para(r["source_credit"], size=5)

def build_pdf(rows, name, header):
    pdf = Print(header); pdf.add_page()
    last_title, last_ch = None, None
    for r in rows:
        if r["title_number"] != last_title:
            pdf.ln(2); pdf.para(f"TITLE {r['title_number']} — {r['title_name'].upper()}", "B", 8, 4); last_title, last_ch = r["title_number"], None
        if r["chapter_number"] and r["chapter_number"] != last_ch:
            pdf.para(f"Chapter {r['chapter_number']} — {r['chapter_name']}", "B", 6.5, 3); last_ch = r["chapter_number"]
        pdf.section(r)
    pdf.output(str(OUT / name)); print("wrote", OUT / name, pdf.page_no(), "pages")

def build_index(rows, name):
    pdf = Print("U.S. Code Verbatim Archive — Master Section Index"); pdf.add_page()
    last = None
    for r in rows:
        if r["title_number"] != last:
            pdf.ln(1); pdf.para(f"TITLE {r['title_number']} — {r['title_name']}", "B", 7, 3.5); last = r["title_number"]
        st = "" if r["status"] == "operative" else f" [{r['status']}]"
        pdf.set_font("DV", "", 5.5); pdf.set_x(pdf.col_x())
        pdf.multi_cell(pdf.col_w, 2.5, f"§ {r['section_number']}  {r['catchline']}{st}  · ch. {r['chapter_number']}", new_x="LEFT", new_y="NEXT")
    pdf.output(str(OUT / name)); print("wrote", OUT / name, pdf.page_no(), "pages")

def build_html(rows, name):
    parts = ["<meta charset='utf-8'><title>U.S. Code Verbatim — cited sections (proof copy)</title>",
             "<style>body{font:15px/1.5 Georgia,serif;max-width:52em;margin:2em auto;padding:0 1em}h2{margin-top:2.5em}"
             ".m{font:12px/1.4 monospace;color:#555;white-space:pre-wrap}.b{white-space:pre-wrap}.c{font-size:13px;color:#333}</style>",
             "<h1>U.S. Code Verbatim Archive — cited sections (proofreading copy)</h1>",
             "<p class=m>Source of record: OLRC USLM release point 119-102, cross-checked word-for-word against GPO govinfo USCODE-2023. "
             "Unofficial reference — verify the controlling version before filing.</p>"]
    for r in rows:
        body = re.sub(r"\s(\([a-z]\)|\(\d+\)|\([A-Z]\)|\([ivx]+\))\s", r"\n\1 ", " " + r["verbatim_text"]).strip()
        parts.append(f"<h2>{r['title_number']} U.S.C. § {html.escape(r['section_number'])} — {html.escape(r['catchline'])}</h2>")
        parts.append(f"<div class=m>{r['section_id']} · Title {r['title_number']} {html.escape(r['title_name'])} · Ch. {r['chapter_number']} {html.escape(r['chapter_name'])}\n"
                     f"{r['source_edition']} · retrieved {r['retrieved_at'][:10]} · sha256 {r['sha256']}\n<a href='{html.escape(r['official_url'])}'>{html.escape(r['official_url'])}</a></div>")
        parts.append(f"<div class=b>{html.escape(body)}</div><p class=c>{html.escape(r['source_credit'])}</p>")
    (OUT / name).write_text("\n".join(parts), encoding="utf-8"); print("wrote", OUT / name)

mode = sys.argv[1] if len(sys.argv) > 1 else "tier1"
rows = load()
if mode == "tier1": build_pdf([r for r in rows if r["section_id"] in TIER1], "05_USC_Tier1_Verbatim_6pt.pdf", "U.S. Code Verbatim Archive — cited sections · OLRC 119-102 · unofficial reference")
elif mode == "index": build_index(rows, "05_USC_Master_Index_6pt.pdf")
elif mode == "full": build_pdf(rows, "05_USC_Verbatim_Complete_6pt.pdf", "U.S. Code Verbatim Archive — Titles 18 / 34 ch.121 / 42 ch.21 · OLRC 119-102 · unofficial reference")
elif mode == "html": build_html([r for r in rows if r["section_id"] in TIER1], "05_USC_Tier1_Proof.html")
