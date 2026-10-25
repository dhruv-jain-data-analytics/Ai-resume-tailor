"""
pdf_tailor.py - tailor a resume PDF IN PLACE.

The original PDF page is kept (layout, colours, lines, images, fonts, hyperlinks).
Only the text of bullet points / summary / skills lines is replaced, in the
same position, same font, same size and same colour. Headings, job titles,
companies, dates, education and contact details are never touched.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from statistics import median

import pymupdf

BULLET_CHARS = set("•●▪■◦○◆◇➢➤►▶‣⁃∙·")
DASH_BULLETS = set("-–—*")

SECTION_RE = re.compile(
    r"summary|profile|objective|about|skill|technolog|tools|competenc|experience|"
    r"employment|work history|internship|project|education|academic|qualification|"
    r"certif|course|training|award|achiev|honou?r|activit|extracurricular|volunteer|"
    r"language|interest|hobb|publication|reference|declaration|contact|personal|strength",
    re.I)
WORK_RE = re.compile(r"project|experience|intern|employment|work", re.I)
SUMMARY_RE = re.compile(r"summary|profile|objective|about", re.I)
SKILLS_RE = re.compile(r"skill|technolog|tools|competenc", re.I)
FROZEN_RE = re.compile(
    r"education|academic|qualification|certif|course|award|achiev|honou?r|language|"
    r"interest|hobb|reference|declaration|contact|personal|publication|training", re.I)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(\+?\d[\d\s\-()]{8,}\d)")
URL_RE = re.compile(r"(https?://|www\.|linkedin\.com|github\.com)", re.I)

SUBST = {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-",
         "\u2014": "-", "\u2022": "-", "\u2192": "->", "\u00a0": " ", "\u2026": "...",
         "\ufb01": "fi", "\ufb02": "fl"}


# ----------------------------------------------------------------------------
# Data classes
# ----------------------------------------------------------------------------
@dataclass
class Span:
    text: str
    font: str
    size: float
    flags: int
    color: int
    x0: float
    y0: float
    x1: float
    y1: float
    ox: float
    oy: float
    frag: bool = False     # first span of a piece that was joined onto the line


@dataclass
class Line:
    spans: list            # all spans in the line
    bullet: str            # '', 'separate', 'merged'
    lead: int              # number of leading spans that are kept untouched (bullet / label)
    tabbed: bool
    text: str              # editable text of this line
    baseline: float
    x_text: float          # where the editable text starts
    x_bullet: float        # where a merged bullet glyph starts
    x1: float
    has_label: bool
    bold_all: bool
    heading: bool          # section heading (caps / larger / short bold + keyword)
    size: float
    limit: float = 0.0     # right edge available to this line (column / box aware)


@dataclass
class Para:
    id: int
    page: int
    lines: list
    section: str
    mode: str
    kind: str              # bullet | summary | skills | text | frozen
    text: str
    font: str = ""
    size: float = 0.0
    flags: int = 0
    color: int = 0
    right: float = 0.0
    max_chars: int = 0
    editable: bool = False

    @property
    def n_lines(self):
        return len(self.lines)


@dataclass
class ResumeDoc:
    paragraphs: list
    full_text: str
    n_pages: int
    has_text: bool
    n_links: int
    warnings: list = field(default_factory=list)


# ----------------------------------------------------------------------------
# Parsing the PDF
# ----------------------------------------------------------------------------
def _is_bullet_char(c: str, font: str = "") -> bool:
    if not c:
        return False
    f = font.lower()
    return (c in BULLET_CHARS or c in DASH_BULLETS or 0xE000 <= ord(c) <= 0xF8FF
            or any(k in f for k in ("symbol", "wingding", "zapf", "dingbat")))


_BULLET_STRIP = re.compile(r"^\s*[%s\ue000-\uf8ff]\s*" % re.escape("".join(BULLET_CHARS | DASH_BULLETS)))


def _join_spans(spans):
    out = ""
    for i, sp in enumerate(spans):
        t = sp.text
        if i and out and not out[-1].isspace() and not t[:1].isspace():
            if sp.frag or sp.x0 - spans[i - 1].x1 > 0.12 * max(sp.size, 6):
                out += " "
        out += t
    return out


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _make_line(ld, body_size):
    spans = []
    for s in ld["spans"]:
        if not s["text"].strip():
            continue
        x0, y0, x1, y1 = s["bbox"]
        ox, oy = s["origin"]
        spans.append(Span(s["text"], s["font"], s["size"], s["flags"], s["color"],
                          x0, y0, x1, y1, ox, oy, bool(s.get("_frag"))))
    if not spans:
        return None
    dx, dy = ld.get("dir", (1, 0))
    if abs(dy) > 0.2:      # rotated text: ignore
        return None

    first = spans[0].text.strip()
    bullet, lead = "", 0
    if first and len(first) <= 2 and _is_bullet_char(first[0], spans[0].font) \
            and len(spans) > 1:
        bullet, lead = "separate", 1
    elif first and len(first) > 1 and (first[0] in BULLET_CHARS or 0xE000 <= ord(first[0]) <= 0xF8FF):
        bullet = "merged"
    elif first and first[0] in DASH_BULLETS and len(first) > 1 and first[1] == " ":
        bullet = "merged"

    # bold "Label:" followed by normal text -> keep the label untouched
    rest = spans[lead:]
    has_label = False
    if len(rest) >= 2 and (rest[0].flags & 16) and rest[0].text.strip().endswith(":") \
            and not all(sp.flags & 16 for sp in rest):
        lead += 1
        has_label = True

    edit_spans = spans[lead:]
    text = _join_spans(edit_spans)
    x_bullet = spans[0].ox
    if bullet == "merged":
        text = _BULLET_STRIP.sub("", text)
    text = _norm_space(text)

    tabbed = False
    for a, b in zip(spans, spans[1:]):
        if b.x0 - a.x1 > 3 * max(a.size, 8):
            tabbed = True
    if any("\t" in sp.text for sp in spans):
        tabbed = True

    size = max(sp.size for sp in spans)
    bold_all = all(sp.flags & 16 for sp in spans)
    plain = _norm_space(_join_spans(spans))
    letters = re.sub(r"[^A-Za-z]", "", plain)
    heading = False
    if not bullet and 3 <= len(plain) <= 50:
        if len(letters) >= 3 and re.fullmatch(r"[A-Z][A-Z &/,'()\-]+", plain) and not tabbed:
            heading = True
        elif size >= body_size + 1.0 and not tabbed:
            heading = True
        elif bold_all and len(plain) <= 30 and SECTION_RE.search(plain) and not tabbed:
            heading = True

    return Line(spans=spans, bullet=bullet, lead=lead, tabbed=tabbed, text=text,
                baseline=median(sp.oy for sp in spans),
                x_text=edit_spans[0].ox if edit_spans else spans[-1].ox,
                x_bullet=x_bullet, x1=max(sp.x1 for sp in spans), has_label=has_label, bold_all=bold_all,
                heading=heading, size=size)


def _merge_bullet_lines(blocks):
    """A bullet glyph is often its own 'line'. Attach it to the text on the same baseline."""
    all_lines = [(b, ld) for b in blocks for ld in b["lines"]]
    for b, ld in all_lines:
        sp = [x for x in ld["spans"] if x["text"].strip()]
        if len(sp) != 1:
            continue
        t = sp[0]["text"].strip()
        if not (len(t) <= 2 and t and _is_bullet_char(t[0], sp[0]["font"])):
            continue
        by = sp[0]["origin"][1]
        bx1 = sp[0]["bbox"][2]
        best = None
        for b2, ld2 in all_lines:
            if ld2 is ld:
                continue
            first = next((x for x in ld2["spans"] if x["text"].strip()), None)
            if first is None:
                continue
            gap = first["bbox"][0] - bx1
            if abs(first["origin"][1] - by) < 2.5 and -1 <= gap < 30:
                if best is None or gap < best[0]:
                    best = (gap, b2, ld2)
        if best:
            best[2]["spans"].insert(0, sp[0])
            ld["spans"] = []
    for b in blocks:
        b["lines"] = [ld for ld in b["lines"] if ld["spans"]]


def _find_split(block_lines, W):
    """x position that cleanly separates a sidebar/second column, or None."""
    ls = [l for bl in block_lines for l in bl]
    best = None
    for S in range(int(0.15 * W), int(0.85 * W), 3):
        left = [l for l in ls if l.x1 < S]
        right = [l for l in ls if l.spans[0].x0 > S]
        crossing = [l for l in ls if l.spans[0].x0 <= S <= l.x1]
        if crossing or len(left) < 3 or len(right) < 3:
            continue
        gap = min(l.spans[0].x0 for l in right) - max(l.x1 for l in left)
        if gap >= 12 and (best is None or gap > best[0]):
            best = (gap, S)
    return best[1] if best else None


def _assign_limits(page, block_lines):
    """Right edge each line may use: page margin, but never into a neighbouring column/box."""
    W = page.rect.width
    lines = [ln for bl in block_lines for ln in bl]
    page_right = max((l.x1 for l in lines), default=W - 36)
    rects = []
    try:
        for d in page.get_drawings():
            r, fill = d.get("rect"), d.get("fill")
            if fill is not None and r is not None and 60 < r.width < 0.9 * W and r.height > 40:
                rects.append(r)
    except Exception:
        pass
    for ln in lines:
        lim = page_right
        for o in lines:
            if o is ln:
                continue
            ox = o.spans[0].x0
            if ox > ln.x1 + 2 and abs(o.baseline - ln.baseline) < 0.8 * max(ln.size, o.size):
                lim = min(lim, ox - 8)
        for r in rects:
            if r.x0 - 2 <= ln.x_text <= r.x1 and r.y0 - 2 <= ln.baseline <= r.y1 + 2:
                lim = min(lim, r.x1 - 8)
        ln.limit = max(lim, ln.x_text + 20)


def _merge_fragments(blocks):
    """Some PDFs split one visual line into several pieces (slightly different baselines).
    Join pieces that sit on the same baseline and are only a word-space apart."""
    lines = [ld for b in blocks for ld in b["lines"] if ld["spans"]]

    def info(ld):
        sp = [x for x in ld["spans"] if x["text"].strip()]
        if not sp or abs(ld.get("dir", (1, 0))[1]) > 0.2:
            return None
        return (sp[0]["origin"][1], sp[0]["bbox"][0], max(x["bbox"][2] for x in sp),
                max(x["size"] for x in sp))

    changed = True
    while changed:
        changed = False
        for a in lines:
            ia = info(a)
            if not a["spans"] or ia is None:
                continue
            best = None
            for c in lines:
                if c is a or not c["spans"]:
                    continue
                ic = info(c)
                if ic is None:
                    continue
                gap = ic[1] - ia[2]
                if abs(ic[0] - ia[0]) <= 0.4 * ia[3] and -1.0 <= gap <= 0.9 * ia[3]:
                    if best is None or gap < best[0]:
                        best = (gap, c)
            if best:
                for x in best[1]["spans"]:
                    if x["text"].strip():
                        x["_frag"] = True
                        break
                a["spans"] = a["spans"] + best[1]["spans"]
                best[1]["spans"] = []
                changed = True
    for b in blocks:
        b["lines"] = [ld for ld in b["lines"] if ld["spans"]]


def _dominant(lines):
    c = Counter()
    for ln in lines:
        for sp in ln.spans[ln.lead:]:
            c[(sp.font, round(sp.size, 1), sp.color, sp.flags & ~1)] += len(sp.text.strip())
    if not c:
        return ("", 0.0, 0, 0)
    return c.most_common(1)[0][0]


def _section_mode(section: str) -> str:
    if not section:
        return "frozen"
    if WORK_RE.search(section):
        return "work"
    if SUMMARY_RE.search(section):
        return "summary"
    if SKILLS_RE.search(section):
        return "skills"
    return "frozen"


def analyze_pdf(pdf_bytes: bytes) -> ResumeDoc:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    warnings, paragraphs, all_text = [], [], []
    n_links = 0
    pid = 0
    section = ""

    for pno, page in enumerate(doc):
        n_links += len(page.get_links())
        link_rects = [pymupdf.Rect(l["from"]) for l in page.get_links()]
        data = page.get_text("dict")
        blocks = [b for b in data["blocks"] if b.get("type") == 0]
        _merge_bullet_lines(blocks)
        _merge_fragments(blocks)

        # body font size = most common size, weighted by characters
        sizes = Counter()
        for b in blocks:
            for ld in b["lines"]:
                for s in ld["spans"]:
                    sizes[round(s["size"], 1)] += len(s["text"].strip())
        body_size = sizes.most_common(1)[0][0] if sizes else 10.0

        W = page.rect.width
        # lines first, so we can look for a clean column split
        block_lines = []
        for b in blocks:
            ls = [ln for ln in (_make_line(ld, body_size) for ld in b["lines"]) if ln]
            if ls:
                block_lines.append(ls)
        split = _find_split(block_lines, W)
        if split:
            warnings.append("Two-column layout detected: each column is edited inside its own width.")
            block_lines.sort(key=lambda ls: (0 if ls[0].spans[0].x0 < split else 1, ls[0].baseline))
        else:
            block_lines.sort(key=lambda ls: (round(ls[0].baseline, 0), ls[0].spans[0].x0))
        _assign_limits(page, block_lines)

        page_paras = []
        for lines in block_lines:
            cur = []

            def flush():
                nonlocal pid
                if cur:
                    page_paras.append(list(cur))
                    cur.clear()

            for ln in lines:
                if not cur:
                    cur.append(ln)
                    continue
                prev = cur[-1]
                gap = ln.baseline - prev.baseline
                style_changed = (abs(ln.size - prev.size) > 0.6 or
                                 ln.spans[ln.lead].color != prev.spans[prev.lead].color or
                                 ln.heading != prev.heading or ln.bold_all != prev.bold_all)
                prev_full = prev.x1 >= prev.x_text + 0.78 * (prev.limit - prev.x_text)
                new_para = (ln.bullet or ln.has_label or ln.tabbed or prev.tabbed or style_changed
                            or gap > 1.55 * ln.size or gap < 0 or ln.heading or not prev_full)
                if new_para:
                    flush()
                cur.append(ln)
            flush()

        # classify paragraphs in reading order
        last_col = None
        for plines in page_paras:
            col = (0 if plines[0].spans[0].x0 < split else 1) if split else 0
            if col != last_col:
                section = ""
                last_col = col
            ptext = _norm_space(" ".join(l.text for l in plines))
            font, size, color, flags = _dominant(plines)
            is_heading = all(l.heading for l in plines)
            if is_heading:
                section = _norm_space(" ".join(
                    "".join(sp.text for sp in l.spans) for l in plines))
            mode = _section_mode(section)
            first = plines[0]
            has_bullet = bool(first.bullet)
            tabbed = any(l.tabbed for l in plines)
            full = _norm_space(" ".join("".join(sp.text for sp in l.spans) for l in plines))
            all_text.append(full)
            overlaps_link = False
            for l in plines:
                r = pymupdf.Rect(min(sp.x0 for sp in l.spans), min(sp.y0 for sp in l.spans),
                                 max(sp.x1 for sp in l.spans), max(sp.y1 for sp in l.spans))
                if any(r.intersects(lr) for lr in link_rects):
                    overlaps_link = True
            contact = bool(EMAIL_RE.search(full) or URL_RE.search(full) or
                           (PHONE_RE.search(full) and len(full) < 80))

            kind, editable = "frozen", False
            piped = ptext.count("|") >= 2 or ptext.count(" | ") >= 1 and len(ptext) < 110
            if not (is_heading or tabbed or overlaps_link or contact or piped or mode == "frozen"):
                if mode == "summary" and len(ptext) >= 40:
                    kind = "summary"
                elif mode == "skills" and len(ptext) >= 25:
                    kind = "skills"
                elif mode == "work":
                    if has_bullet and len(ptext) >= 25:
                        kind = "bullet"
                    elif (not has_bullet and len(ptext) >= 60 and
                          not all(l.bold_all for l in plines)):
                        kind = "text"
                editable = kind != "frozen"

            p = Para(id=pid, page=pno, lines=plines, section=section, mode=mode,
                     kind=kind, text=ptext, font=font, size=size, flags=flags,
                     color=color, editable=editable)
            pid += 1
            paragraphs.append(p)

        # right bound (wrap width) per x-column
        page_paras_flat = [p for p in paragraphs if p.page == pno]
        for p in page_paras_flat:
            p.right = min(l.limit for l in p.lines)
            if p.editable:
                widths, chars = 0.0, 0
                for l in p.lines:
                    ed = l.spans[l.lead:]
                    widths += sum(sp.x1 - sp.x0 for sp in ed)
                    chars += sum(len(sp.text) for sp in ed)
                avg_cw = widths / chars if chars else 5.0
                cap = 0
                for l in p.lines:
                    cap += int(max(0.0, p.right - l.x_text) / avg_cw)
                p.max_chars = max(int(cap * 0.96), len(p.text))

    full_text = "\n".join(all_text)
    has_text = len(full_text.strip()) > 100
    if not has_text:
        warnings.append("No readable text found - this looks like a scanned/image PDF.")
    if len(doc) > 1:
        warnings.append(f"Your master resume has {len(doc)} pages; the output keeps the "
                        f"same page count. Upload a 1-page master for a 1-page result.")
    doc.close()
    return ResumeDoc(paragraphs=paragraphs, full_text=full_text, n_pages=len(paragraphs and
                     {p.page for p in paragraphs} or []), has_text=has_text,
                     n_links=n_links, warnings=warnings)


# ----------------------------------------------------------------------------
# Fonts
# ----------------------------------------------------------------------------
_FONT_DIRS = [r"C:\Windows\Fonts", "/usr/share/fonts", "/usr/local/share/fonts",
              os.path.expanduser("~/.fonts"), os.path.expanduser("~/Library/Fonts"),
              "/Library/Fonts", "/System/Library/Fonts", "/System/Library/Fonts/Supplemental"]
_FONT_INDEX = None

# regular, bold, italic, bold-italic  (Windows names first, then Linux look-alikes)
_EXPLICIT = {
    "calibri": [("calibri.ttf", "carlito-regular.ttf"), ("calibrib.ttf", "carlito-bold.ttf"),
                ("calibrii.ttf", "carlito-italic.ttf"), ("calibriz.ttf", "carlito-bolditalic.ttf")],
    "arial": [("arial.ttf", "liberationsans-regular.ttf"), ("arialbd.ttf", "liberationsans-bold.ttf"),
              ("ariali.ttf", "liberationsans-italic.ttf"), ("arialbi.ttf", "liberationsans-bolditalic.ttf")],
    "times": [("times.ttf", "liberationserif-regular.ttf"), ("timesbd.ttf", "liberationserif-bold.ttf"),
              ("timesi.ttf", "liberationserif-italic.ttf"), ("timesbi.ttf", "liberationserif-bolditalic.ttf")],
    "cambria": [("cambria.ttc", "caladea-regular.ttf"), ("cambriab.ttf", "caladea-bold.ttf"),
                ("cambriai.ttf", "caladea-italic.ttf"), ("cambriaz.ttf", "caladea-bolditalic.ttf")],
    "georgia": [("georgia.ttf",), ("georgiab.ttf",), ("georgiai.ttf",), ("georgiaz.ttf",)],
    "verdana": [("verdana.ttf",), ("verdanab.ttf",), ("verdanai.ttf",), ("verdanaz.ttf",)],
    "tahoma": [("tahoma.ttf",), ("tahomabd.ttf",), ("tahoma.ttf",), ("tahomabd.ttf",)],
    "segoeui": [("segoeui.ttf",), ("segoeuib.ttf",), ("segoeuii.ttf",), ("segoeuiz.ttf",)],
    "trebuchetms": [("trebuc.ttf",), ("trebucbd.ttf",), ("trebucit.ttf",), ("trebucbi.ttf",)],
    "couriernew": [("cour.ttf", "liberationmono-regular.ttf"), ("courbd.ttf", "liberationmono-bold.ttf"),
                   ("couri.ttf", "liberationmono-italic.ttf"), ("courbi.ttf", "liberationmono-bolditalic.ttf")],
}
_ALIAS = {"helvetica": "arial", "arimo": "arial", "liberationsans": "arial", "arialmt": "arial",
          "timesnewroman": "times", "timesnewromanps": "times", "liberationserif": "times",
          "tinos": "times", "carlito": "calibri", "caladea": "cambria", "trebuchet": "trebuchetms",
          "segoe": "segoeui", "segoeuiregular": "segoeui", "courier": "couriernew",
          "liberationmono": "couriernew", "cousine": "couriernew"}


def _font_index():
    global _FONT_INDEX
    if _FONT_INDEX is None:
        idx = {}
        for base in _FONT_DIRS:
            if not os.path.isdir(base):
                continue
            for root, _dirs, files in os.walk(base):
                if root[len(base):].count(os.sep) > 4:
                    continue
                for f in files:
                    if f.lower().endswith((".ttf", ".otf", ".ttc")):
                        idx.setdefault(f.lower(), os.path.join(root, f))
        _FONT_INDEX = idx
    return _FONT_INDEX


def _family(basefont: str):
    name = basefont.split("+")[-1]
    fam = re.sub(r"[^a-z0-9]", "", re.split(r"[-,]", name)[0].lower())
    fam = re.sub(r"(psmt|mt|ps)$", "", fam) or fam
    return _ALIAS.get(fam, fam), name.lower()


def _find_system_font(basefont: str, flags: int):
    fam, low = _family(basefont)
    bold = bool(flags & 16) or "bold" in low or "black" in low or "semibold" in low
    italic = bool(flags & 2) or "italic" in low or "oblique" in low
    style = (2 if italic else 0) + (1 if bold else 0)   # 0 reg, 1 bold, 2 italic, 3 bi
    order = {0: 0, 1: 1, 2: 2, 3: 3}[style]
    idx = _font_index()
    if fam in _EXPLICIT:
        for fname in _EXPLICIT[fam][order]:
            if fname in idx:
                return idx[fname]
        return None
    # generic: Lato-Regular.ttf, OpenSans-Bold.ttf ...
    best = None
    for fname, path in idx.items():
        stem = re.sub(r"[^a-z0-9]", "", os.path.splitext(fname)[0])
        if not stem.startswith(fam) or len(fam) < 3:
            continue
        rest = stem[len(fam):]
        f_bold = "bold" in rest or rest.startswith("bd")
        f_ital = "italic" in rest or "oblique" in rest
        if f_bold == bold and f_ital == italic and not any(
                t in rest for t in ("light", "thin", "medium", "condensed", "narrow", "black")):
            if best is None or len(rest) < best[0]:
                best = (len(rest), path)
    return best[1] if best else None


def _covers(font, chars) -> bool:
    try:
        return all(font.has_glyph(ord(c)) for c in chars if c != " ")
    except Exception:
        return False


class FontManager:
    def __init__(self, doc):
        self.doc = doc
        self.embedded = {}
        for page in doc:
            for f in page.get_fonts(full=True):
                xref, ext, _typ, basefont = f[0], f[1], f[2], f[3]
                self.embedded.setdefault(basefont, (xref, ext))
        self._cache = {}

    def _load(self, key, factory):
        if key not in self._cache:
            try:
                self._cache[key] = factory()
            except Exception:
                self._cache[key] = None
        return self._cache[key]

    def get(self, basefont: str, flags: int, chars: set):
        """Return (Font, kind) where kind is 'embedded', 'system' or 'fallback'."""
        # 1) the exact embedded font (if it contains every glyph we need)
        if basefont in self.embedded:
            xref, ext = self.embedded[basefont]
            if ext not in ("n/a", ""):
                def mk():
                    return pymupdf.Font(fontbuffer=self.doc.extract_font(xref)[3])
                f = self._load(("emb", xref), mk)
                if f is not None and _covers(f, chars):
                    return f, "embedded"
        # 2) the same family from the operating system
        path = _find_system_font(basefont, flags)
        if path:
            f = self._load(("sys", path), lambda: pymupdf.Font(fontfile=path))
            if f is not None and _covers(f, chars):
                return f, "system"
        # 3) closest built-in font
        low = basefont.lower()
        serif = bool(flags & 4) or any(k in low for k in
                                       ("times", "serif", "georgia", "garamond", "cambria",
                                        "book", "minion", "palatino"))
        if flags & 8 or "mono" in low or "courier" in low:
            fam = "co"
        elif serif:
            fam = "ti"
        else:
            fam = "he"
        bold = bool(flags & 16) or "bold" in low
        italic = bool(flags & 2) or "italic" in low or "oblique" in low
        names = {"he": ("helv", "hebo", "heit", "hebi"), "ti": ("tiro", "tibo", "tiit", "tibi"),
                 "co": ("cour", "cobo", "coit", "cobi")}[fam]
        name = names[(2 if italic else 0) + (1 if bold else 0)]
        f = self._load(("base", name), lambda: pymupdf.Font(name))
        return f, "fallback"


def _clean_for_font(text: str, font) -> str:
    out = []
    for c in text:
        if c == " " or font.has_glyph(ord(c)):
            out.append(c)
        elif c in SUBST and all(font.has_glyph(ord(x)) or x == " " for x in SUBST[c]):
            out.append(SUBST[c])
        # else: drop the character
    return "".join(out)


def _wrap(text, font, size, widths):
    """Greedy word wrap. widths[i] = available width of line i. Returns lines or None."""
    words = text.split(" ")
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w) if cur else w
        i = len(lines)
        if i >= len(widths):
            return None
        if font.text_length(trial, size) <= widths[i] or not cur:
            if not cur and font.text_length(w, size) > widths[i]:
                return None
            cur = trial
        else:
            lines.append(cur)
            cur = w
            if len(lines) >= len(widths) or font.text_length(cur, size) > widths[len(lines)]:
                return None
    if cur:
        lines.append(cur)
    return lines if len(lines) <= len(widths) else None


# ----------------------------------------------------------------------------
# Applying edits
# ----------------------------------------------------------------------------
def _norm_word(w: str) -> str:
    return re.sub(r"[^a-z0-9]", "", w.lower())


def _line_rect(p, l, merged):
    es = l.spans if merged and l is p.lines[0] else l.spans[l.lead:]
    x0 = min(s.x0 for s in es) - 1
    x1 = max(max(s.x1 for s in es) + 1, l.limit)
    return pymupdf.Rect(x0, l.baseline - 0.72 * p.size, x1, l.baseline + 0.22 * p.size)


def _render(pdf_bytes, plans):
    """Redact the old text of the planned lines, then write the new text. Returns the doc."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    by_page = {}
    for plan in plans:
        by_page.setdefault(plan[0].page, []).append(plan)
    for pno, items in by_page.items():
        page = doc[pno]
        for p, _f, _k, _pl, _xs, merged in items:
            for l in p.lines:
                try:
                    page.add_redact_annot(_line_rect(p, l, merged), fill=False)
                except Exception:
                    page.add_redact_annot(_line_rect(p, l, merged))
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    for pno, items in by_page.items():
        page = doc[pno]
        fm = FontManager(doc)
        for p, font, _kind, (size, lines, _scale), xs, merged in items:
            tw = pymupdf.TextWriter(page.rect)
            if merged:
                bf, _ = fm.get(p.font, p.flags, {"\u2022"})
                glyph = "\u2022" if bf.has_glyph(0x2022) else "-"
                tw.append(pymupdf.Point(p.lines[0].x_bullet, p.lines[0].baseline), glyph,
                          font=bf, fontsize=size)
            for i, txt in enumerate(lines):
                tw.append(pymupdf.Point(xs[i], p.lines[i].baseline), txt, font=font,
                          fontsize=size)
            tw.write_text(page, color=pymupdf.sRGB_to_pdf(p.color))
    return doc


def _verify(doc, plans):
    """Ids of paragraphs whose edited area still shows old text or overlapping words."""
    bad = set()
    for p, _font, _kind, (_size, lines, _scale), _xs, merged in plans:
        page = doc[p.page]
        expected = {_norm_word(w) for w in " ".join(lines).split()}
        expected.discard("")
        found = []
        for l in p.lines:
            r = _line_rect(p, l, merged)
            r = pymupdf.Rect(l.x_text - 0.5 if not merged else r.x0, r.y0, r.x1, r.y1)
            found += [(pymupdf.Rect(w[:4]), _norm_word(w[4])) for w in
                      page.get_text("words", clip=r)]
        for rect, word in found:
            if word and word not in expected:
                bad.add(p.id)                     # leftover of the old text
        for i in range(len(found)):               # overlapping words
            for j in range(i + 1, len(found)):
                a, b = found[i][0], found[j][0]
                inter = a & b
                if inter.is_empty:
                    continue
                if inter.get_area() > 0.3 * min(a.get_area(), b.get_area()):
                    bad.add(p.id)
    return bad


def apply_edits(pdf_bytes: bytes, resume: ResumeDoc, edits: dict,
                linkedin_url: str = "", github_url: str = ""):
    """
    edits: {paragraph_id: new_text}
    Returns (new_pdf_bytes, applied, skipped, links_added)
    """
    doc0 = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    fm0 = FontManager(doc0)
    by_id = {p.id: p for p in resume.paragraphs}
    skipped, plans = [], []

    for pid, new_text in edits.items():
        p = by_id.get(pid)
        if p is None or not p.editable:
            skipped.append({"id": pid, "reason": "not an editable line"})
            continue
        new_text = _norm_space(str(new_text))
        new_text = _BULLET_STRIP.sub("", new_text)
        if not new_text or new_text == p.text:
            continue
        chars = {c for c in new_text if c.isalnum()}
        font, kind = fm0.get(p.font, p.flags, chars)
        if font is None:
            skipped.append({"id": pid, "reason": "font unavailable"})
            continue
        new_text = _clean_for_font(new_text, font)

        merged = p.lines[0].bullet == "merged"
        x_first = p.lines[0].x_text
        if merged:   # text of a merged bullet starts where its continuation lines start
            if len(p.lines) > 1:
                x_first = p.lines[1].x_text
            else:
                x_first = p.lines[0].x_bullet + font.text_length("\u2022  ", p.size)
        xs = [x_first] + [l.x_text for l in p.lines[1:]]
        placed = None
        for scale in (1.0, 0.98, 0.96, 0.94, 0.92):
            size = p.size * scale
            widths = [(p.right - x) * 0.985 for x in xs]
            lines = _wrap(new_text, font, size, widths)
            if lines:
                placed = (size, lines, scale)
                break
        if placed is None:
            skipped.append({"id": pid, "reason": "new text did not fit in the original space"})
            continue
        plans.append((p, font, kind, placed, xs, merged))
    doc0.close()

    excluded = set()
    doc = None
    for _attempt in range(3):
        active = [pl for pl in plans if pl[0].id not in excluded]
        doc = _render(pdf_bytes, active)
        bad = _verify(doc, active)
        if not bad:
            break
        excluded |= bad
        doc.close()
        doc = None
    if doc is None:                                # give up: return the original untouched
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        excluded = {pl[0].id for pl in plans}
    for pid in excluded:
        skipped.append({"id": pid, "reason": "could not be edited cleanly, kept original"})

    applied = [{"id": p.id, "old": p.text, "new": " ".join(lines), "font_kind": kind,
                "scale": scale, "section": p.section}
               for p, _f, kind, (_size, lines, scale), _xs, _m in plans if p.id not in excluded]
    links_added = ensure_links(doc, linkedin_url, github_url)
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out, applied, skipped, links_added


def ensure_links(doc, linkedin_url="", github_url=""):
    """Make sure LinkedIn / GitHub / e-mail / URL text is clickable."""
    added = 0
    for page in doc:
        existing = [pymupdf.Rect(l["from"]) for l in page.get_links()]
        for w in page.get_text("words"):
            rect = pymupdf.Rect(w[:4])
            tok = w[4].strip(".,;:()[]|<>")
            low = tok.lower()
            url = None
            if re.fullmatch(r"https?://\S+", tok, re.I):
                url = tok
            elif re.match(r"(www\.|linkedin\.com|github\.com)\S+", low):
                url = "https://" + tok
            elif EMAIL_RE.fullmatch(tok):
                url = "mailto:" + tok
            elif low == "linkedin" and linkedin_url:
                url = linkedin_url
            elif low == "github" and github_url:
                url = github_url
            if url and not any(rect.intersects(r) for r in existing):
                page.insert_link({"kind": pymupdf.LINK_URI, "from": rect, "uri": url})
                existing.append(rect)
                added += 1
    return added


def render_pages(pdf_bytes: bytes, dpi: int = 110):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    out = [page.get_pixmap(dpi=dpi).tobytes("png") for page in doc]
    doc.close()
    return out


# ----------------------------------------------------------------------------
# Gemini (ONE call does job analysis + all rewrites)
# ----------------------------------------------------------------------------
def call_gemini(api_key: str, model: str, prompt: str, retries: int = 3) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    attempts = [dict(thinking=True), dict(thinking=False)]
    last = None
    for mode in attempts:
        cfg = dict(temperature=0.2, response_mime_type="application/json")
        if mode["thinking"]:
            cfg["thinking_config"] = types.ThinkingConfig(thinking_level="low")
        for i in range(retries):
            try:
                r = client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(**cfg))
                return r.text or ""
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e)
                if "thinking" in msg.lower():
                    break                      # retry without thinking config
                if any(c in msg for c in ("503", "UNAVAILABLE", "429")) and i < retries - 1:
                    time.sleep(2 * (i + 1))
                    continue
                raise
    raise last


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            return json.loads(text[s:e + 1])
        raise


def build_prompt(resume: ResumeDoc, jd_text: str) -> str:
    rows = []
    for p in resume.paragraphs:
        if p.editable:
            rows.append(f"[EDIT {p.id}] ({p.kind}, section: {p.section}, "
                        f"max {p.max_chars} chars) {p.text}")
        else:
            t = _norm_space(" ".join("".join(sp.text for sp in l.spans) for l in p.lines))
            rows.append(f"(context) {t}")
    listing = "\n".join(rows)
    return f"""
You are an expert resume editor. Tailor the resume to the job description by
rewriting ONLY the lines marked [EDIT n]. Everything marked (context) is shown
so you understand the resume; you cannot change it.

STRICT RULES
1. Use ONLY facts that already exist in the resume. Never add a skill, tool,
   certification, employer, project, number, percentage or achievement that is
   not already there.
2. Reword to mirror the job description's language ONLY where the resume
   already shows that skill/experience. Put the most job-relevant point first
   and use strong action verbs.
3. LENGTH: every rewritten line must be at most its "max" characters and ideally
   90-100% of the original length. Never exceed it. Plain text only: no bullet
   symbols, no markdown, no line breaks.
4. skills lines: keep the label before ":" exactly as it is; keep every item,
   but put the items most relevant to the job first.
5. summary: rewrite in the same number of sentences, from resume facts only.
6. Return only the lines you actually changed. Omit lines that are already good.
7. If the job asks for something the resume does not show, simply ignore it.

Return ONLY this JSON:
{{"role_title": "string", "company": "string",
  "must_have_skills": ["short names"],
  "keywords": ["15-25 short ATS keywords from the job description"],
  "edits": [{{"id": 12, "text": "rewritten line"}}]}}

JOB DESCRIPTION:
\"\"\"{jd_text}\"\"\"

RESUME:
{listing}
"""


def llm_tailor(api_key: str, model: str, jd_text: str, resume: ResumeDoc) -> dict:
    data = parse_json(call_gemini(api_key, model, build_prompt(resume, jd_text)))
    edits = {}
    for e in data.get("edits", []):
        try:
            edits[int(e["id"])] = str(e["text"])
        except (KeyError, ValueError, TypeError):
            continue
    data["edits"] = edits
    return data


# ----------------------------------------------------------------------------
# Match report + fact check (no AI needed, instant)
# ----------------------------------------------------------------------------
def keyword_match(keywords, resume_text):
    low = resume_text.lower()
    matched, missing, seen = [], [], set()
    for kw in keywords:
        k = str(kw).strip()
        if not k or k.lower() in seen:
            continue
        seen.add(k.lower())
        pat = r"(?<![a-z0-9])" + re.escape(k.lower()) + r"(?![a-z0-9])"
        (matched if re.search(pat, low) else missing).append(k)
    return matched, missing


def _numbers(text):
    return {n.rstrip(".,") for n in re.findall(r"\d[\d,]*\.?\d*%?", text)}


def fact_check(master_text: str, applied: list):
    """Flag numbers and tool-like capitalised words that are not in the master resume."""
    master_low = master_text.lower()
    master_tokens = set(re.findall(r"[a-z0-9+#.]+", master_low))
    new_numbers, new_terms = set(), set()
    for a in applied:
        new = a["new"]
        new_numbers |= _numbers(new) - _numbers(master_text)
        words = new.split(" ")
        for w in words[1:]:                      # skip first word (sentence case)
            core = w.strip(".,;:()[]")
            if len(core) < 2:
                continue
            tool_like = (core[0].isupper() or any(c.isupper() for c in core[1:])
                         or any(c.isdigit() for c in core))
            if tool_like and core.lower() not in master_tokens \
                    and core.lower() not in master_low:
                new_terms.add(core)
    return sorted(new_numbers), sorted(new_terms)


def ai_fact_check(api_key: str, model: str, master_text: str, applied: list):
    changed = "\n".join(f"- {a['new']}" for a in applied)
    prompt = f"""
Compare the REWRITTEN lines with the MASTER resume. List every skill, tool,
certification, employer, project, achievement or number in REWRITTEN that is
NOT supported by MASTER. Rewording is fine. Return ONLY JSON:
{{"unsupported": [{{"claim": "...", "why": "..."}}]}}
Empty list if everything is supported.

MASTER:
\"\"\"{master_text}\"\"\"

REWRITTEN:
{changed}
"""
    try:
        return parse_json(call_gemini(api_key, model, prompt)).get("unsupported", [])
    except Exception:  # noqa: BLE001
        return None
