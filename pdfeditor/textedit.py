"""In-place editing of existing PDF text.

Strategy
--------
* Text is read with MuPDF's structured-text extraction (chars with origins,
  bounding boxes and styles) and grouped into editable lines and paragraphs.
* An edit is diffed against the original text so unchanged characters keep
  their exact font, size, colour and position (including justified spacing).
* The original characters are removed with redactions that are first tried
  on a throw-away copy of the page and verified glyph by glyph, so
  neighbouring text is never damaged.
* New characters are written with the *original embedded font* whenever it
  contains the needed glyphs (we rebuild its Unicode cmap so the result stays
  searchable), otherwise with the closest installed system font.
"""

from __future__ import annotations

import bisect
import difflib
import hashlib
import html
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field, replace as _replace
from typing import NamedTuple

import pymupdf as fitz

from .fonts import (
    GENERIC_FAMILIES,
    SUBSTITUTES,
    SYSTEM_FONTS,
    UNICODE_FALLBACK_FAMILIES,
    base14,
    can_embed,
    css_family_for,
    parse_pdf_font_name,
    squash,
)
from . import blend
from .fontutil import GlyphTable, parse_sfnt, patch_cmap

TEXT_FLAGS = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP

# Scripts that need shaping (RTL, Indic, South-East Asian): written through MuPDF's HTML engine.
COMPLEX_SCRIPT_RE = re.compile(
    "[\u0590-\u08ff\u0900-\u0dff\u0e00-\u0fff\u1000-\u109f\u1780-\u17ff\ufb1d-\ufdff\ufe70-\ufeff]"
)
BULLET_RE = re.compile(r"^\s*([\u2022\u25e6\u25aa\u25cf\u25cb\u25a0\u25a1\u27a2\u2713\u2714\u2023\u2043\u00b7*\-\u2013]\s|\(?\d{1,3}[.)]\s|\(?[a-zA-Z][.)]\s)")

# Characters whose glyph availability we probe in embedded subset fonts.
REPERTOIRE = (
    list(range(0x20, 0x7F)) + list(range(0xA1, 0x180)) + list(range(0x2010, 0x2027))
    + list(range(0x2030, 0x203B)) + [0x20AC, 0x2122, 0x2190, 0x2191, 0x2192, 0x2193]
)


class EditError(Exception):
    """Raised for edits that cannot be applied (reported to the user)."""


def rgb_tuple(color: int) -> tuple[float, float, float]:
    return ((color >> 16) & 255) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255


def hex_color(color: int) -> str:
    return f"#{color & 0xFFFFFF:06x}"


def parse_hex(value: str | None) -> tuple[float, float, float] | None:
    if not value:
        return None
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


# --------------------------------------------------------------------------- model
@dataclass(frozen=True)
class Style:
    font: str
    size: float
    flags: int
    color: int
    alpha: int
    visible: bool
    bold: bool
    italic: bool
    serif: bool
    mono: bool

    @property
    def rgb(self) -> tuple[float, float, float]:
        return rgb_tuple(self.color)


@dataclass
class Char:
    c: str
    x0: float
    y0: float
    x1: float
    y1: float
    ox: float
    oy: float
    style: int
    synthetic: bool

    @property
    def is_glyph(self) -> bool:
        return not self.synthetic and not self.c.isspace()

    @property
    def key(self) -> tuple:
        return (self.c, round(self.ox, 1), round(self.oy, 1))

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)


@dataclass
class Line:
    id: str
    chars: list[Char]
    bbox: fitz.Rect
    horizontal: bool
    para: int = -1

    @property
    def text(self) -> str:
        return "".join(ch.c for ch in self.chars)

    def glyphs(self) -> list[Char]:
        return [c for c in self.chars if c.is_glyph]

    @property
    def main_style(self) -> int:
        counts = Counter(c.style for c in self.glyphs())
        return counts.most_common(1)[0][0] if counts else self.chars[0].style

    @property
    def baseline(self) -> float:
        counts = Counter(round(c.oy, 2) for c in self.glyphs())
        return counts.most_common(1)[0][0] if counts else self.chars[0].oy

    @property
    def text_x0(self) -> float:
        g = self.glyphs()
        return g[0].x0 if g else self.bbox.x0

    @property
    def text_x1(self) -> float:
        g = self.glyphs()
        return g[-1].x1 if g else self.bbox.x1


@dataclass
class Paragraph:
    id: int
    lines: list[Line]
    align: str = "left"
    indent: float = 0.0
    left: float = 0.0
    right: float = 0.0
    gap: float = 0.0

    @property
    def bbox(self) -> fitz.Rect:
        r = fitz.Rect(self.lines[0].bbox)
        for line in self.lines[1:]:
            r |= line.bbox
        return r


class PageText:
    """Structured, editable view of the text on one page."""

    def __init__(self, page: fitz.Page):
        self.page = page
        self.styles: list[Style] = []
        self.lines: list[Line] = []
        self.paragraphs: list[Paragraph] = []
        self._extract()
        self._group_paragraphs()

    # .......................................................... extraction
    def _style_index(self, span: dict) -> int:
        char_flags = span.get("char_flags", 16)
        visible = bool(char_flags & (16 | 32)) and span.get("alpha", 255) > 0
        key = (span["font"], round(span["size"], 3), span["flags"], span["color"], span.get("alpha", 255), visible)
        idx = self._style_keys.get(key)
        if idx is None:
            name = span["font"] or ""
            info = parse_pdf_font_name(name)
            flags = span["flags"]
            bold = bool(flags & 16) or bool(char_flags & 8) or info["bold"]
            italic = bool(flags & 2) or info["italic"]
            serif = bool(flags & 4)
            mono = bool(flags & 8)
            lowered = name.lower()
            if any(k in lowered for k in ("courier", "mono", "consol", "menlo")):
                mono = True
            if any(k in lowered for k in ("times", "georgia", "garamond", "cambria", "palatino", "roman", "serif")) and "sans" not in lowered:
                serif = True
            style = Style(name, span["size"], flags, span["color"], span.get("alpha", 255), visible, bold, italic, serif, mono)
            idx = len(self.styles)
            self.styles.append(style)
            self._style_keys[key] = idx
        return idx

    def _extract(self) -> None:
        self._style_keys: dict[tuple, int] = {}
        data = self.page.get_text("rawdict", flags=TEXT_FLAGS)
        raw: list[Line] = []
        for bi, block in enumerate(data.get("blocks", [])):
            if block.get("type") != 0:
                continue
            for li, line in enumerate(block.get("lines", [])):
                chars: list[Char] = []
                for span in line.get("spans", []):
                    sidx = self._style_index(span)
                    for ch in span.get("chars", []):
                        c = ch["c"]
                        if c in ("\xa0", "\u2002", "\u2003", "\u2009"):
                            c = " "
                        x0, y0, x1, y1 = ch["bbox"]
                        chars.append(Char(c, x0, y0, x1, y1, ch["origin"][0], ch["origin"][1], sidx, bool(ch.get("synthetic"))))
                if not chars or not any(c.is_glyph for c in chars):
                    continue
                dx, dy = line["dir"]
                horizontal = abs(dx - 1) < 0.01 and abs(dy) < 0.01 and line.get("wmode", 0) == 0
                raw.append(Line("", chars, fitz.Rect(line["bbox"]), horizontal))
        self.lines = self._merge_fragments(raw)
        for i, line in enumerate(self.lines):
            line.id = f"L{i}"

    def _merge_fragments(self, raw: list[Line]) -> list[Line]:
        """Join pieces of one visual line that MuPDF reports separately.

        Text written later in the content stream (e.g. by a previous edit) is
        extracted as its own line even when it continues an existing one.
        """
        merged: list[Line] = []
        used = [False] * len(raw)
        for i, line in enumerate(raw):
            if used[i]:
                continue
            used[i] = True
            if not line.horizontal:
                merged.append(line)
                continue
            group = [line]
            size = self.styles[line.main_style].size or 10
            base = line.baseline
            changed = True
            while changed:
                changed = False
                gx0 = min(g.text_x0 for g in group)
                gx1 = max(g.text_x1 for g in group)
                for j in range(i + 1, len(raw)):
                    other = raw[j]
                    if used[j] or not other.horizontal:
                        continue
                    osize = self.styles[other.main_style].size or 10
                    if abs(other.baseline - base) > 0.3 * max(size, osize):
                        continue
                    tol = 1.0 * max(size, osize)
                    if other.text_x0 <= gx1 + tol and other.text_x1 >= gx0 - tol:
                        group.append(other)
                        used[j] = True
                        changed = True
            if len(group) == 1:
                merged.append(line)
                continue
            chars: list[Char] = []
            for g in sorted(group, key=lambda g: g.text_x0):
                gchars = g.chars
                if chars and gchars:
                    prev, nxt = chars[-1], gchars[0]
                    gap = nxt.x0 - prev.x1
                    if gap > 0.12 * size and not prev.c.isspace() and not nxt.c.isspace():
                        chars.append(Char(" ", prev.x1, prev.y0, nxt.x0, prev.y1, prev.x1, prev.oy, prev.style, True))
                chars.extend(gchars)
            chars.sort(key=lambda c: c.x0 if c.synthetic else c.ox)
            bbox = fitz.Rect(group[0].bbox)
            for g in group[1:]:
                bbox |= g.bbox
            merged.append(Line("", chars, bbox, True))
        return merged

    # .......................................................... paragraphs
    def _size(self, line: Line) -> float:
        return self.styles[line.main_style].size

    def _same_paragraph(self, prev: Line, cur: Line, para: list[Line]) -> bool:
        if not (prev.horizontal and cur.horizontal):
            return False
        s_prev, s_cur = self._size(prev), self._size(cur)
        size = max(s_prev, s_cur)
        if size <= 0 or abs(s_prev - s_cur) > 0.15 * size:
            return False
        gap = cur.baseline - prev.baseline
        if not (0.8 * size <= gap <= 2.1 * size):
            return False
        if len(para) >= 2:
            prev_gap = prev.baseline - para[-2].baseline
            if abs(gap - prev_gap) > 0.25 * size:
                return False
        if cur.bbox.x0 > prev.bbox.x1 - 0.5 * size or cur.bbox.x1 < prev.bbox.x0 + 0.5 * size:
            return False
        if BULLET_RE.match(cur.text):
            return False
        st_prev, st_cur = self.styles[prev.main_style], self.styles[cur.main_style]
        if st_prev.bold != st_cur.bold and len({c.style for c in prev.glyphs()}) == 1:
            return False
        left = para[1].text_x0 if len(para) >= 2 else para[0].text_x0
        lefts_ok = abs(cur.text_x0 - left) <= 1.5 * size or (len(para) == 1 and cur.text_x0 < prev.text_x0 and prev.text_x0 - cur.text_x0 <= 4 * size)
        centers_ok = abs((cur.text_x0 + cur.text_x1) / 2 - (prev.text_x0 + prev.text_x1) / 2) <= 1.0 * size
        rights_ok = abs(cur.text_x1 - prev.text_x1) <= 1.0 * size
        if not (lefts_ok or centers_ok or rights_ok):
            return False
        if len(para) >= 2 and lefts_ok:
            widest = max(l.text_x1 for l in para)
            if prev.text_x1 < widest - 3.0 * size:
                return False  # previous line was a short, paragraph-ending line
        return True

    def _neighbour(self, line: Line, below: bool, by_baseline: list[tuple[float, int, Line]]) -> Line | None:
        size = self._size(line)
        keys = [b for b, _, _ in by_baseline]
        if below:
            lo = bisect.bisect_left(keys, line.baseline + 0.5 * size)
            hi = bisect.bisect_right(keys, line.baseline + 2.2 * size)
        else:
            lo = bisect.bisect_left(keys, line.baseline - 2.2 * size)
            hi = bisect.bisect_right(keys, line.baseline - 0.5 * size)
        best, best_key = None, None
        for _, _, other in by_baseline[lo:hi]:
            if other is line or not other.horizontal:
                continue
            overlap = min(line.bbox.x1, other.bbox.x1) - max(line.bbox.x0, other.bbox.x0)
            if overlap <= 0:
                continue
            key = (round(abs(other.baseline - line.baseline), 1), -overlap)
            if best_key is None or key < best_key:
                best, best_key = other, key
        return best

    def _group_paragraphs(self) -> None:
        """Chain lines into paragraphs using mutual nearest vertical neighbours (order independent)."""
        by_baseline = sorted(((l.baseline, i, l) for i, l in enumerate(self.lines)), key=lambda t: (t[0], t[1]))
        nxt: dict[int, Line] = {}
        for line in self.lines:
            if not line.horizontal:
                continue
            below = self._neighbour(line, True, by_baseline)
            if below is not None and self._neighbour(below, False, by_baseline) is line:
                nxt[id(line)] = below
        targets = {id(b) for b in nxt.values()}
        order = [l for l in self.lines if id(l) not in targets] + [l for l in self.lines if id(l) in targets]
        assigned: set[int] = set()
        groups: list[list[Line]] = []
        for line in order:
            if id(line) in assigned:
                continue
            chain = [line]
            assigned.add(id(line))
            cur = line
            while id(cur) in nxt:
                cand = nxt[id(cur)]
                if id(cand) in assigned or not self._same_paragraph(cur, cand, chain):
                    break
                chain.append(cand)
                assigned.add(id(cand))
                cur = cand
            groups.append(chain)
        position = {id(l): i for i, l in enumerate(self.lines)}
        groups.sort(key=lambda g: position[id(g[0])])
        for pid, lines in enumerate(groups):
            para = Paragraph(pid, lines)
            for line in lines:
                line.para = pid
            self._measure_paragraph(para)
            self.paragraphs.append(para)

    def _measure_paragraph(self, para: Paragraph) -> None:
        lines = para.lines
        size = self._size(lines[0])
        if len(lines) == 1:
            para.left, para.right = lines[0].text_x0, lines[0].text_x1
            para.gap = size * 1.2
            para.align = "left"
            return
        body_left = min(l.text_x0 for l in lines[1:])
        para.left = body_left
        para.indent = max(0.0, lines[0].text_x0 - body_left)
        para.right = max(l.text_x1 for l in lines)
        gaps = [b.baseline - a.baseline for a, b in zip(lines, lines[1:])]
        para.gap = statistics.median(gaps)
        tol = 0.6 * size
        lefts = [l.text_x0 for l in lines[1:]]
        rights = [l.text_x1 for l in lines[:-1]]
        centers = [(l.text_x0 + l.text_x1) / 2 for l in lines]
        left_ok = max(lefts) - min(lefts) <= tol
        right_ok = max(rights) - min(rights) <= tol
        all_rights = [l.text_x1 for l in lines]
        if len(lines) >= 3 and left_ok and right_ok:
            para.align = "justify"
        elif left_ok and abs(lines[0].text_x0 - body_left) <= 4 * size:
            para.align = "left"
        elif max(all_rights) - min(all_rights) <= tol:
            para.align = "right"
        elif max(centers) - min(centers) <= tol:
            para.align = "center"
        else:
            para.align = "left"

    # .......................................................... lookup
    def find_line(self, line_id: str, orig_text: str | None = None, bbox: list | None = None) -> Line | None:
        want = normalize_text(orig_text) if orig_text is not None else None
        for line in self.lines:
            if line.id == line_id and (want is None or normalize_text(line.text) == want):
                return line
        if want is not None:
            candidates = [l for l in self.lines if normalize_text(l.text) == want]
            if bbox and candidates:
                target = fitz.Rect(bbox)
                candidates.sort(key=lambda l: abs(l.bbox.x0 - target.x0) + abs(l.bbox.y0 - target.y0))
            if candidates:
                return candidates[0]
        return None

    # .......................................................... payload
    def payload(self) -> dict:
        embedded: set[str] = set()
        try:
            for f in self.page.get_fonts(full=True):
                if f[1] not in ("n/a", ""):
                    embedded.add(squash(_strip_subset(f[3])))
        except Exception:
            pass
        styles = []
        for st in self.styles:
            styles.append({
                "embedded": squash(st.font) in embedded,
                "font": st.font,
                "css": css_family_for(st.font, st.serif, st.mono),
                "size": round(st.size, 3),
                "color": hex_color(st.color),
                "bold": st.bold,
                "italic": st.italic,
                "serif": st.serif,
                "mono": st.mono,
                "visible": st.visible,
            })
        lines = []
        for line in self.lines:
            runs = []
            for i, ch in enumerate(line.chars):
                if runs and runs[-1][2] == ch.style:
                    runs[-1][1] = i + 1
                else:
                    runs.append([i, i + 1, ch.style])
            lines.append({
                "id": line.id,
                "bbox": [round(v, 2) for v in line.bbox],
                "text": line.text,
                "baseline": round(line.baseline, 2),
                "xs": [round(c.x0, 2) for c in line.chars] + [round(line.chars[-1].x1, 2)],
                "runs": runs,
                "style": line.main_style,
                "editable": line.horizontal and not _unreadable(line.text),
                "para": line.para,
            })
        paragraphs = []
        for para in self.paragraphs:
            paragraphs.append({
                "id": para.id,
                "lines": [l.id for l in para.lines],
                "bbox": [round(v, 2) for v in para.bbox],
                "align": para.align,
                "gap": round(para.gap, 2),
                "left": round(para.left, 2),
                "right": round(para.right, 2),
                "indent": round(para.indent, 2),
                "text": paragraph_text(para)[0],
            })
        scanned = False
        if not any(l["editable"] for l in lines):
            try:
                area = self.page.rect.get_area() or 1
                covered = sum((fitz.Rect(i["bbox"]) & self.page.rect).get_area() for i in self.page.get_image_info())
                scanned = covered / area > 0.4
            except Exception:
                scanned = False
        ocr = bool(self.lines) and not any(st.visible for st in self.styles)
        return {"styles": styles, "lines": lines, "paragraphs": paragraphs, "scanned": scanned, "ocr": ocr}


def normalize_text(text: str) -> str:
    return (text or "").replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")


def paragraph_text(para: Paragraph) -> tuple[str, list[Char | None]]:
    """Join paragraph lines into flowing text (soft line breaks become spaces)."""
    parts: list[str] = []
    seq: list[Char | None] = []
    for i, line in enumerate(para.lines):
        chars = list(line.chars)
        while chars and chars[-1].c.isspace():
            chars.pop()
        while chars and chars[0].c.isspace():
            chars.pop(0)
        if i > 0 and parts:
            prev = parts[-1]
            if prev.endswith("-") and len(prev) > 1 and prev[-2].isalpha() and chars and chars[0].c.islower():
                # de-hyphenate "exam-" + "ple"
                parts[-1] = prev[:-1]
                seq.pop()
            else:
                parts.append(" ")
                seq.append(None)
        parts.append("".join(c.c for c in chars))
        seq.extend(chars)
    return "".join(parts), seq


# --------------------------------------------------------------------------- fonts
class EmbeddedFont:
    """An embedded PDF font made writable again (see module docstring)."""

    def __init__(self, buffer: bytes, gmap: dict[int, int]):
        self.buffer = buffer
        self.gmap = dict(gmap)
        self.is_sfnt = parse_sfnt(buffer) is not None
        self.mapping: dict[int, int] = {}
        self._font: fitz.Font | None = None
        self.embeddable = False  # some fonts may be measured but not written into a PDF
        try:
            native = fitz.Font(fontbuffer=buffer)
        except Exception:
            native = None
        if self.is_sfnt:
            table = GlyphTable(buffer)
            mapping = dict(self.gmap)
            if native is not None and table.loca is not None:
                for u in REPERTOIRE:
                    if u in mapping:
                        continue
                    gid = native.has_glyph(u)
                    if gid and table.has_outline(gid):
                        mapping[u] = gid
            patched = patch_cmap(buffer, mapping)
            if patched:
                try:
                    font = fitz.Font(fontbuffer=patched)
                    if font.is_writable:
                        self._font, self.mapping = font, mapping
                        self.embeddable = can_embed(font)
                except Exception:
                    pass
        elif native is not None and native.is_writable:
            observed = [(u, g) for u, g in self.gmap.items() if u != 32]
            agree = sum(1 for u, g in observed if native.has_glyph(u) == g)
            if observed and agree >= 0.9 * len(observed):
                self._font = native
                self.embeddable = can_embed(native)
                self.mapping = dict(self.gmap)
                for u in REPERTOIRE:
                    gid = native.has_glyph(u)
                    if gid and u not in self.mapping:
                        self.mapping[u] = gid

    @property
    def font(self) -> fitz.Font | None:
        return self._font

    @property
    def usable(self) -> bool:
        return self._font is not None

    def covers(self, ch: str) -> bool:
        return self._font is not None and ord(ch) in self.mapping


class ResourceFont:
    """A font the page already carries, written by referring to its resource.

    Some fonts forbid re-embedding (the OS/2 licence bits), so MuPDF will not write
    them into a new PDF font.  The page already contains the font, so text can be
    written against that existing resource instead and keeps the document's own
    typeface.  Whether that works depends on the font's encoding, so it is tried
    once on a blank copy of the page and the result is remembered per character.
    """

    PROBE = "".join(chr(c) for c in range(33, 127))

    def __init__(self, page: fitz.Page, name: str, basefont: str, font: fitz.Font | None):
        self.page = page
        self.name = name  # resource name in the page's /Font dictionary
        self.basefont = basefont
        self.font = font  # for measuring only
        self._writable: set[str] | None = None

    def can_write(self, ch: str) -> bool:
        if self._writable is None:
            self._writable = self._probe()
        if not self._writable:
            return False
        return ch == " " or ch in self._writable

    def write(self, page: fitz.Page, point: tuple[float, float], text: str, ws: "WriteStyle",
              morph=None) -> None:
        page.insert_text(point, text, fontname="/" + self.name, fontsize=ws.size,
                         color=ws.color, fill_opacity=ws.alpha, morph=morph)

    def _probe(self) -> set[str]:
        """Write every printable character through the resource and read it back."""
        try:
            scratch = fitz.open()
            scratch.insert_pdf(self.page.parent, from_page=self.page.number, to_page=self.page.number,
                               annots=False)
            sp = scratch[0]
            for xref in sp.get_contents():  # blank the copy, keeping its resources
                scratch.update_stream(xref, b" ")
            sp.insert_text((sp.rect.x0 + 2, sp.rect.y0 + sp.rect.height / 2), self.PROBE,
                           fontname="/" + self.name, fontsize=6)
            spans = [s for b in sp.get_text("dict")["blocks"] for line in b.get("lines", [])
                     for s in line["spans"]]
            if not spans or any(_strip_subset(s["font"]) != self.basefont for s in spans):
                return set()
            got = "".join(s["text"] for s in spans)
            return {a for a, b in zip(self.PROBE, got) if a == b} if len(got) == len(self.PROBE) else set()
        except Exception:
            return set()


class FontMemory:
    """Per-document cache of embedded fonts, accumulated across edits."""

    def __init__(self) -> None:
        self.gmaps: dict[tuple[str, str], dict[int, int]] = {}
        self.fonts: dict[tuple[str, str, int], EmbeddedFont] = {}
        self.scans: dict[tuple, "ScanStyle | None"] = {}

    def get(self, name: str, buffer: bytes, gmap: dict[int, int]) -> EmbeddedFont:
        digest = hashlib.sha1(buffer).hexdigest()
        key = (name, digest)
        merged = self.gmaps.setdefault(key, {})
        for u, g in gmap.items():
            merged.setdefault(u, g)
        fkey = (name, digest, len(merged))
        if fkey not in self.fonts:
            self.fonts[fkey] = EmbeddedFont(buffer, merged)
        return self.fonts[fkey]


class FontChoice(NamedTuple):
    font: fitz.Font | None
    fake_bold: bool
    fake_italic: bool
    kind: str  # embedded | resource | system | base14 | fallback | html
    res: "ResourceFont | None" = None


@dataclass(frozen=True)
class WriteStyle:
    base: int | None  # index into PageText.styles (enables embedded-font reuse)
    family: str | None  # explicit system family chosen by the user
    bold: bool
    italic: bool
    size: float
    color: tuple[float, float, float]
    alpha: float
    generic: str = "sans"


def _strip_subset(name: str) -> str:
    return name[7:] if re.match(r"^[A-Z]{6}\+", name or "") else (name or "")


def _glyph_tables_digest(buffer: bytes) -> str:
    parsed = parse_sfnt(buffer)
    if not parsed:
        return hashlib.sha1(buffer).hexdigest()
    _, tables = parsed
    h = hashlib.sha1()
    for tag in (b"glyf", b"loca", b"CFF ", b"CFF2", b"hmtx"):
        h.update(tables.get(tag, b""))
    return h.hexdigest()


class FontBook:
    """Chooses a writable font for every character written on a page."""

    def __init__(self, doc: fitz.Document, page: fitz.Page, pt: PageText | None, memory: FontMemory):
        self.doc = doc
        self.page = page
        self.pt = pt
        self.memory = memory
        self._gmap_data: tuple[dict[str, dict[int, int]], set[str]] | None = None
        self._embedded: dict[str, EmbeddedFont | None] = {}
        self._cands: dict[WriteStyle, list] = {}
        self._choice: dict[tuple[WriteStyle, str], FontChoice] = {}
        self._fonts_on_page: list[tuple] | None = None
        self._resources: dict[str, ResourceFont | None] = {}
        self.safe_only = False  # set after a font refused to be written

    def _collect_gmaps(self) -> tuple[dict[str, dict[int, int]], set[str]]:
        gmaps: dict[str, dict[int, int]] = {}
        conflicts: set[str] = set()
        try:
            trace = self.page.get_texttrace()
        except Exception:
            return gmaps, conflicts
        for span in trace:
            name = span.get("font") or ""
            gm = gmaps.setdefault(name, {})
            for u, gid, *_ in span.get("chars", ()):
                if gid is None or gid < 0 or u < 32:
                    continue
                if u in (0xA0, 0x2002, 0x2003, 0x2009):
                    u = 32
                prev = gm.get(u)
                if prev is None:
                    gm[u] = gid
                elif prev != gid:
                    conflicts.add(name)
        return gmaps, conflicts

    def embedded(self, name: str) -> EmbeddedFont | None:
        if name in self._embedded:
            return self._embedded[name]
        if self._gmap_data is None:
            self._gmap_data = self._collect_gmaps()
        gmaps, conflicts = self._gmap_data
        result = None
        if name and name not in conflicts and gmaps.get(name):
            exact = [f for f in self._page_fonts() if _strip_subset(f[3]) == name]
            if not exact:
                exact = [f for f in self._page_fonts() if squash(_strip_subset(f[3])) == squash(name)]
            buffers = []
            for f in exact:
                try:
                    _, ext, _, buf = self.doc.extract_font(f[0])
                except Exception:
                    continue
                if buf:
                    buffers.append(buf)
            if buffers and len({_glyph_tables_digest(b) for b in buffers}) == 1:
                font = self.memory.get(name, buffers[0], gmaps[name])
                result = font if font.usable else None
        self._embedded[name] = result
        return result

    def _page_fonts(self) -> list[tuple]:
        if self._fonts_on_page is None:
            try:
                self._fonts_on_page = self.page.get_fonts(full=True)
            except Exception:
                self._fonts_on_page = []
        return self._fonts_on_page

    def resource(self, name: str, emb: EmbeddedFont) -> ResourceFont | None:
        """The page's own resource for a font that refuses to be embedded again."""
        if name in self._resources:
            return self._resources[name]
        found = None
        for f in self._page_fonts():
            if squash(_strip_subset(f[3])) == squash(name):
                found = ResourceFont(self.page, f[4], _strip_subset(f[3]), emb.font)
                break
        self._resources[name] = found
        return found

    def use_safe_fonts(self) -> None:
        """Lay text out again with fonts that are known to write into a PDF."""
        self.safe_only = True
        self._cands.clear()
        self._choice.clear()

    def _candidates(self, ws: WriteStyle) -> list[tuple[str, object]]:
        cached = self._cands.get(ws)
        if cached is not None:
            return cached
        cands: list[tuple[str, object]] = []
        st = self.pt.styles[ws.base] if (ws.base is not None and self.pt is not None) else None
        if st and st.visible and not ws.family and ws.bold == st.bold and ws.italic == st.italic \
                and not self.safe_only:
            emb = self.embedded(st.font)
            if emb and emb.embeddable:
                cands.append(("embedded", emb))
            elif emb:
                res = self.resource(st.font, emb)
                if res is not None:
                    cands.append(("resource", (emb, res)))
        names: list[str] = []
        if ws.family:
            names.append(ws.family)
        elif st and st.visible:
            info = parse_pdf_font_name(st.font)
            fams = SYSTEM_FONTS.families
            for key in info["candidates"]:
                if key in fams:
                    names.append(fams[key].name)
                    break
            for key in info["candidates"]:
                names.extend(SUBSTITUTES.get(key, []))
        generic = ws.generic
        names.extend(GENERIC_FAMILIES[generic])
        seen: set[str] = set()
        for name in names:
            key = squash(name)
            if key in seen:
                continue
            seen.add(key)
            found = SYSTEM_FONTS.font_for(name, ws.bold, ws.italic)
            if found:
                cands.append(("system", found))
        cands.append(("base14", (base14(generic, ws.bold, ws.italic), False, False)))
        for name in UNICODE_FALLBACK_FAMILIES:
            found = SYSTEM_FONTS.font_for(name, ws.bold, ws.italic)
            if found:
                cands.append(("fallback", found))
        try:
            cands.append(("fallback", (fitz.Font(ordering=0), ws.bold, ws.italic)))
        except Exception:
            pass
        self._cands[ws] = cands
        return cands

    def choice(self, ws: WriteStyle, ch: str) -> FontChoice:
        key = (ws, ch)
        found = self._choice.get(key)
        if found:
            return found
        result = None
        if COMPLEX_SCRIPT_RE.match(ch):
            result = FontChoice(None, False, False, "html")
        else:
            cands = self._candidates(ws)
            for kind, obj in cands:
                if kind == "embedded":
                    if obj.covers(ch) or ch == " ":  # type: ignore[union-attr]
                        result = FontChoice(obj.font, False, False, kind)  # type: ignore[union-attr]
                        break
                elif kind == "resource":
                    emb, res = obj  # type: ignore[misc]
                    if (emb.covers(ch) or ch == " ") and res.can_write(ch):
                        result = FontChoice(emb.font, False, False, kind, res)
                        break
                else:
                    font, fb, fi = obj  # type: ignore[misc]
                    if ch == " " or font.has_glyph(ord(ch)):
                        result = FontChoice(font, fb, fi, kind)
                        break
            if result is None:
                font, fb, fi = next(obj for kind, obj in cands if kind == "base14")  # type: ignore[misc]
                result = FontChoice(font, fb, fi, "base14")
        self._choice[key] = result
        return result

    def segments(self, ws: WriteStyle, text: str) -> list[tuple[str, FontChoice]]:
        out: list[tuple[str, FontChoice]] = []
        for ch in text:
            choice = self.choice(ws, ch)
            if out and out[-1][1] is choice or (out and out[-1][1] == choice and out[-1][1].font is choice.font):
                out[-1] = (out[-1][0] + ch, out[-1][1])
            else:
                out.append((ch, choice))
        return out

    def width(self, ws: WriteStyle, text: str) -> float:
        total = 0.0
        for seg, choice in self.segments(ws, text):
            if choice.font is None:
                total += 0.55 * ws.size * len(seg)
            else:
                total += choice.font.text_length(seg, fontsize=ws.size)
        return total

    def space_width(self, ws: WriteStyle) -> float:
        choice = self.choice(ws, " ")
        if choice.font is not None:
            w = choice.font.text_length(" ", fontsize=ws.size)
            if w > 0:
                return w
        return 0.25 * ws.size


# --------------------------------------------------------------------------- writing
@dataclass
class Run:
    x: float
    y: float
    text: str
    ws: WriteStyle
    end_x: float = 0.0


class Writer:
    def __init__(self, page: fitz.Page, book: FontBook):
        self.page = page
        self.book = book
        self.runs: list[Run] = []
        self.groups: dict[tuple, fitz.TextWriter] = {}
        self.html_items: list[tuple[float, float, str, WriteStyle]] = []
        self.res_items: list[tuple[float, float, str, WriteStyle, ResourceFont, float | None]] = []

    def add(self, run: Run) -> None:
        self.runs.append(run)
        self._place(run)

    def _place(self, run: Run) -> None:
        x = run.x
        for seg, choice in self.book.segments(run.ws, run.text):
            if choice.kind == "html":
                self.html_items.append((x, run.y, seg, run.ws))
                x += 0.55 * run.ws.size * len(seg)
                continue
            if seg.strip() == "":
                x += choice.font.text_length(seg, fontsize=run.ws.size) if choice.font else 0.25 * run.ws.size
                continue
            italic_pivot = round(run.y, 3) if choice.fake_italic else None
            if choice.kind == "resource":
                x = self._place_resource(x, run, seg, choice, italic_pivot)
                continue
            key = (run.ws.color, round(run.ws.alpha, 3), italic_pivot)
            tw = self.groups.get(key)
            if tw is None:
                tw = self.groups[key] = fitz.TextWriter(self.page.rect)
            # write word by word so no space glyphs are emitted (extractors infer spaces from gaps)
            pos = x
            for part in re.split(r"( +)", seg):
                if not part:
                    continue
                if part.startswith(" "):
                    pos += choice.font.text_length(part, fontsize=run.ws.size)
                    continue
                _, last = tw.append((pos, run.y), part, font=choice.font, fontsize=run.ws.size)
                if choice.fake_bold:
                    tw.append((pos + run.ws.size * 0.025, run.y), part, font=choice.font, fontsize=run.ws.size)
                pos = last.x
            x = pos

    def _place_resource(self, x: float, run: Run, seg: str, choice: FontChoice, pivot: float | None) -> float:
        pos = x
        for part in re.split(r"( +)", seg):
            if not part:
                continue
            width = choice.font.text_length(part, fontsize=run.ws.size) if choice.font else 0.0
            if not part.startswith(" "):
                self.res_items.append((pos, run.y, part, run.ws, choice.res, pivot))
            pos += width
        return pos

    @staticmethod
    def _morph(pivot: float | None):
        if pivot is None:
            return None
        return (fitz.Point(0, pivot), fitz.Matrix(1, 0, -0.2, 1, 0, 0))

    def _would_write(self) -> bool:
        """Try the text writers on a throw-away page first.

        A font can still refuse to be written (licence bits, damaged glyphs); finding
        out here means the real page never ends up with half a line on it.
        """
        try:
            probe = fitz.open()
            page = probe.new_page(width=self.page.rect.width, height=self.page.rect.height)
            for (color, alpha, pivot), tw in self.groups.items():
                tw.write_text(page, color=color, opacity=alpha, morph=self._morph(pivot))
        except Exception:
            return False
        return True

    def commit(self) -> None:
        if not self._would_write():
            # Lay the same text out again with fonts that are known to work, so an
            # edit is never lost because of the font it happened to land on.
            self.book.use_safe_fonts()
            self.groups.clear()
            self.res_items.clear()
            self.html_items.clear()
            for run in self.runs:
                self._place(run)
        # if a safe font still refuses, let it raise: the edit is rolled back whole
        for (color, alpha, pivot), tw in self.groups.items():
            tw.write_text(self.page, color=color, opacity=alpha, morph=self._morph(pivot))
        for x, y, text, ws, res, pivot in self.res_items:
            try:
                res.write(self.page, (x, y), text, ws, morph=self._morph(pivot))
            except Exception:
                pass
        for x, y, text, ws in self.html_items:
            size = ws.size
            r, g, b = (int(c * 255) for c in ws.color)
            weight = "bold" if ws.bold else "normal"
            style = "italic" if ws.italic else "normal"
            css = (f"* {{margin:0;padding:0}} body {{font-size:{size}px;line-height:1;color:rgb({r},{g},{b});"
                   f"font-weight:{weight};font-style:{style};white-space:nowrap}}")
            rect = fitz.Rect(x, y - size * 0.95, x + size * (len(text) + 4), y + size * 0.6)
            try:
                self.page.insert_htmlbox(rect, html.escape(text), css=css, opacity=ws.alpha)
            except Exception:
                pass


# --------------------------------------------------------------------------- redaction
def glyph_census(page: fitz.Page) -> Counter:
    counter: Counter = Counter()
    data = page.get_text("rawdict", flags=TEXT_FLAGS)
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    if ch.get("synthetic"):
                        continue
                    c = " " if ch["c"] in ("\xa0", "\u2002", "\u2003", "\u2009") else ch["c"]
                    counter[(c, round(ch["origin"][0], 1), round(ch["origin"][1], 1))] += 1
    return counter


def _unreadable(text: str) -> bool:
    """True for text that extracts as replacement characters (a layer with no /ToUnicode)."""
    core = [c for c in text if not c.isspace()]
    return bool(core) and sum(c == "\ufffd" for c in core) > 0.3 * len(core)


def _band(styles: list[Style], ch: Char, top: float, bottom: float) -> tuple[float, float]:
    size = styles[ch.style].size or (ch.y1 - ch.y0)
    return ch.oy - top * size, ch.oy - bottom * size


def _redaction_rects(styles: list[Style], chars: list[Char], strategy: str) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    if strategy == "normal":
        group: list[Char] = []

        def flush() -> None:
            if not group:
                return
            first, last = group[0], group[-1]
            y0 = min(_band(styles, c, 0.62, 0.05)[0] for c in group)
            y1 = max(_band(styles, c, 0.62, 0.05)[1] for c in group)
            x0 = first.x0 + 0.3 * first.width
            x1 = last.x1 - 0.3 * last.width
            if x1 <= x0:
                x1 = x0 + 0.1
            rects.append(fitz.Rect(x0, y0, x1, y1))
            group.clear()

        for ch in chars:
            if group:
                prev = group[-1]
                same = (abs(prev.oy - ch.oy) < 0.2 and prev.style == ch.style and 0 <= ch.x0 - prev.x1 < 0.5 * (styles[ch.style].size or 1))
                if not same:
                    flush()
            group.append(ch)
        flush()
    elif strategy == "narrow":
        for ch in chars:
            y0, y1 = _band(styles, ch, 0.45, 0.2)
            w = ch.width
            rects.append(fitz.Rect(ch.x0 + 0.4 * w, y0, max(ch.x1 - 0.4 * w, ch.x0 + 0.4 * w + 0.05), y1))
    else:  # wide
        for ch in chars:
            h = ch.y1 - ch.y0
            rects.append(fitz.Rect(ch.x0 + 0.05 * ch.width, ch.y0 + 0.1 * h, ch.x1 - 0.05 * ch.width, ch.y1 - 0.1 * h))
    return [r for r in rects if not r.is_empty]


def _apply(page: fitz.Page, rects: list[fitz.Rect]) -> None:
    for r in rects:
        page.add_redact_annot(r, fill=None, cross_out=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE, text=fitz.PDF_REDACT_TEXT_REMOVE)


def remove_characters(doc: fitz.Document, pno: int, styles: list[Style], targets: list[Char]) -> dict:
    """Remove exactly ``targets`` from page ``pno`` (trial-run on a scratch copy first)."""
    targets = [c for c in targets if not c.synthetic]
    if not targets:
        return {"collateral": 0, "missed": 0}
    want = Counter(c.key for c in targets)

    def trial(rects: list[fitz.Rect]) -> tuple[Counter, Counter]:
        scratch = fitz.open()
        scratch.insert_pdf(doc, from_page=pno, to_page=pno, links=False, annots=False, widgets=False)
        sp = scratch[0]
        before = glyph_census(sp)
        _apply(sp, rects)
        removed = before - glyph_census(sp)
        return removed - want, want - removed

    plans = []
    rects = _redaction_rects(styles, targets, "normal")
    collateral, missed = trial(rects)
    plans.append((len(collateral), len(missed), rects))
    if collateral:
        rects = _redaction_rects(styles, targets, "narrow")
        collateral, missed = trial(rects)
        plans.append((len(collateral), len(missed), rects))
    if missed and not collateral:
        missed_chars = [c for c in targets if c.key in missed]
        rects = rects + _redaction_rects(styles, missed_chars, "wide")
        c2, m2 = trial(rects)
        plans.append((len(c2), len(m2), rects))
    best = min(plans, key=lambda p: (p[0], p[1]))
    _apply(doc[pno], best[2])
    return {"collateral": best[0], "missed": best[1]}


def sample_colors(page: fitz.Page, rect: fitz.Rect) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Background and ink colour of a region (used when editing text that lives in a scanned image)."""
    rect = fitz.Rect(rect) & page.rect
    if rect.is_empty:
        return (1, 1, 1), (0, 0, 0)
    pix = page.get_pixmap(clip=rect, dpi=96, annots=False, colorspace=fitz.csRGB)
    w, h, n = pix.width, pix.height, pix.n
    samples = pix.samples
    border: Counter = Counter()
    allc: Counter = Counter()
    sums: dict[tuple, list[int]] = {}
    for y in range(h):
        for x in range(w):
            i = (y * w + x) * n
            r, g, b = samples[i], samples[i + 1], samples[i + 2]
            q = (r // 24, g // 24, b // 24)
            allc[q] += 1
            acc = sums.setdefault(q, [0, 0, 0])
            acc[0] += r
            acc[1] += g
            acc[2] += b
            if x in (0, w - 1) or y in (0, h - 1):
                border[q] += 1
    if not allc:
        return (1, 1, 1), (0, 0, 0)
    bg_q = border.most_common(1)[0][0] if border else allc.most_common(1)[0][0]
    acc, count = sums[bg_q], allc[bg_q]
    bg = tuple(v / count for v in acc)
    bg_lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    # refine: median of the border pixels that look like paper (quantisation buckets can bias the mean)
    paper = []
    for y in range(h):
        xs = range(w) if y in (0, 1, h - 2, h - 1) else (0, 1, w - 2, w - 1)
        for x in xs:
            if 0 <= x < w:
                i = (y * w + x) * n
                r_, g_, b_ = samples[i], samples[i + 1], samples[i + 2]
                if abs(0.299 * r_ + 0.587 * g_ + 0.114 * b_ - bg_lum) < 40:
                    paper.append((r_, g_, b_))
    if len(paper) >= 8:
        bg = tuple(statistics.median(p[k] for p in paper) for k in range(3))
        bg_lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    # ink = mean of the most contrasting pixels (the core of the glyph strokes, not anti-aliasing)
    pixels = []
    for i in range(0, w * h * n, n):
        r, g, b = samples[i], samples[i + 1], samples[i + 2]
        pixels.append((abs(0.299 * r + 0.587 * g + 0.114 * b - bg_lum), r, g, b))
    pixels.sort(reverse=True)
    top = pixels[: max(5, len(pixels) * 6 // 100)]
    if not top or top[0][0] < 40:
        ink = (0.0, 0.0, 0.0)
    else:
        ink = tuple(round(sum(p[k] for p in top) / len(top) / 255, 4) for k in (1, 2, 3))
    return tuple(round(v / 255, 4) for v in bg), ink  # type: ignore[return-value]


def _spot_colors(page: fitz.Page, rect: fitz.Rect) -> tuple[tuple, tuple, fitz.Rect] | None:
    """Ink colour, paper colour and the tight ink box of a few scanned characters.

    Sampled from the characters themselves at a resolution where their strokes are
    solid rather than anti-aliasing, and with rows and columns that run the whole way
    across thrown out, so a table rule beside the text cannot pass for ink.
    """
    rect = fitz.Rect(rect) & page.rect
    if rect.is_empty:
        return None
    pix = page.get_pixmap(clip=rect, dpi=300, annots=False, colorspace=fitz.csRGB)
    w, h, n, s = pix.width, pix.height, pix.n, pix.samples
    if w < 3 or h < 3:
        return None
    lum = [0.0] * (w * h)
    for j in range(w * h):
        i = j * n
        lum[j] = 0.299 * s[i] + 0.587 * s[i + 1] + 0.114 * s[i + 2]
    ordered = sorted(lum)
    paper_lum = ordered[int(len(ordered) * 0.75)]
    darkest = ordered[int(len(ordered) * 0.02)]
    if paper_lum - darkest < 25:
        return None  # nothing that looks like writing
    threshold = paper_lum - 0.45 * (paper_lum - darkest)
    rows = [sum(1 for x in range(w) if lum[y * w + x] < threshold) for y in range(h)]
    cols = [sum(1 for y in range(h) if lum[y * w + x] < threshold) for x in range(w)]
    ruled_rows = {y for y, c in enumerate(rows) if c > 0.85 * w}
    ruled_cols = {x for x, c in enumerate(cols) if c > 0.85 * h}
    dark = []
    for y in range(h):
        if y in ruled_rows:
            continue
        for x in range(w):
            if x in ruled_cols:
                continue
            j = y * w + x
            if lum[j] < threshold:
                dark.append((lum[j], x, y, j))
    if len(dark) < 6:
        return None
    dark.sort()
    core = dark[: max(4, len(dark) // 4)]  # the middle of the strokes, not their edges
    ink = tuple(round(statistics.median(s[d[3] * n + k] for d in core) / 255, 4) for k in range(3))
    paper_pixels = [j for j in range(w * h) if lum[j] > paper_lum - 12]
    paper = tuple(round(statistics.median(s[j * n + k] for j in paper_pixels) / 255, 4) for k in range(3))
    scale = 72 / 300
    box = fitz.Rect(rect.x0 + min(d[1] for d in dark) * scale, rect.y0 + min(d[2] for d in dark) * scale,
                    rect.x0 + (max(d[1] for d in dark) + 1) * scale, rect.y0 + (max(d[2] for d in dark) + 1) * scale)
    return ink, paper, box


# --------------------------------------------------------------------------- planning
@dataclass
class LineEdit:
    line_id: str
    orig_text: str
    new_text: str
    style: dict | None = None
    dx: float = 0.0
    dy: float = 0.0
    bbox: list | None = None


def _char_mapping(old: str, new: str) -> tuple[list[int | None], list[int | None]]:
    """For every char of ``new``: the equal old index (or None) and the old index providing its style."""
    mapping: list[int | None] = [None] * len(new)
    source: list[int | None] = [None] * len(new)
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                mapping[j1 + k] = i1 + k
                source[j1 + k] = i1 + k
        elif tag in ("replace", "insert"):
            if tag == "replace":
                src = i1
            else:
                src = i1 - 1 if i1 > 0 else (i1 if i1 < len(old) else None)
            for k in range(j2 - j1):
                source[j1 + k] = src
    return mapping, source


def _write_style(pt: PageText, style_idx: int, override: dict | None, ink: tuple | None = None) -> WriteStyle:
    st = pt.styles[style_idx]
    override = override or {}
    family = override.get("family") or None
    bold = override["bold"] if override.get("bold") is not None else st.bold
    italic = override["italic"] if override.get("italic") is not None else st.italic
    size = float(override.get("size") or st.size)
    color = parse_hex(override.get("color")) or (ink if (ink and not st.visible) else st.rgb)
    alpha = st.alpha / 255 if st.visible else 1.0
    # invisible OCR text carries no real font information
    generic = ("mono" if st.mono else "serif" if st.serif else "sans") if st.visible else "sans"
    return WriteStyle(style_idx, family, bool(bold), bool(italic), size, tuple(color), alpha, generic)  # type: ignore[arg-type]


def _layout_line(pt: PageText, book: FontBook, line: Line, new_text: str, mapping, source,
                 k_new: int, k_old: int, override: dict | None, dx: float, dy: float, ink,
                 anchor_words: bool | None = None) -> list[Run]:
    old = line.chars
    if k_old < len(old):
        pen = old[k_old].ox
    else:
        pen = old[-1].x1
    pen += dx
    runs: list[Run] = []
    run: Run | None = None
    delta = 0.0
    prev_old: int | None = None
    fallback_style = line.main_style

    def ws_for(idx: int | None) -> WriteStyle:
        style_idx = old[idx].style if idx is not None and idx < len(old) else fallback_style
        return _write_style(pt, style_idx, override, ink)

    def next_origin(j: int) -> float:
        if j + 1 < len(old) and abs(old[j + 1].oy - old[j].oy) < 0.5:
            return old[j + 1].ox
        return old[j].x1

    # Unchanged characters keep their original spacing (e.g. justified gaps) unless the
    # font metrics change, in which case the line is laid out naturally.
    anchor = not (override and any(k in override for k in ("family", "bold", "italic", "size")))
    if anchor_words is not None:
        anchor = anchor_words
    for i in range(k_new, len(new_text)):
        ch = new_text[i]
        j = mapping[i] if anchor else None
        s = source[i]
        ws = ws_for(s)
        base_y = (old[s].oy if s is not None and s < len(old) else line.baseline) + dy
        if j is not None and (prev_old is None or j != prev_old + 1):
            delta = pen - old[j].ox
        if ch.isspace():
            if run:
                runs.append(run)
                run = None
            natural = book.space_width(ws)
            pen = max(next_origin(j) + delta, pen + 0.5 * natural) if j is not None else pen + natural
            prev_old = j
            continue
        if run is None:
            if j is not None:
                x = old[j].ox + delta
                if x < pen - 0.01:
                    delta += pen - x
                    x = pen
            else:
                x = pen
        else:
            x = run.end_x
        width = book.width(ws, ch)
        if run is not None and run.ws == ws and abs(run.y - base_y) < 0.01 and abs(run.end_x - x) < 0.01:
            run.text += ch
        else:
            if run:
                runs.append(run)
            run = Run(x, base_y, ch, ws)
        run.end_x = x + width
        pen = x + width
        prev_old = j
    if run:
        runs.append(run)
    return runs


def _scan_scale(book: FontBook, ws: WriteStyle, marks: list[tuple[str, fitz.Rect]]) -> tuple[float, float] | None:
    """The size and baseline that make this font print like the scan.

    Every scanned character is compared with the *same* character drawn in the chosen
    font, so ascenders, x-height letters and digits each answer for themselves, and
    the median of those answers is taken - one speck or table rule cannot shift it.
    """
    from . import scanfont

    drawn: dict[str, scanfont._Word] = {}
    scales: list[float] = []
    baselines: list[float] = []
    for ch, box in marks:
        font = book.choice(ws, ch).font
        if font is None or box.height <= 0:
            continue
        try:
            word = drawn.get(ch) or scanfont._Word(font, ch)
        except Exception:
            continue
        drawn[ch] = word
        if word.ink is None:
            continue
        ink_h, _, base_below_top, _ = word.geometry()
        if ink_h <= 0:
            continue
        k = box.height / ink_h
        scales.append(k)
        baselines.append(box.y0 + base_below_top * k)
    if len(scales) < 2:
        return None
    k = statistics.median(scales)
    size = scanfont._Word.SIZE * k
    if not 0.3 * ws.size <= size <= 3 * ws.size:
        return None  # the measurement disagrees with the page too much to be trusted
    return size, statistics.median(baselines)


def _scan_place(book: FontBook, ws: WriteStyle, text: str, left: float,
                baseline: float) -> tuple[float, fitz.Rect] | None:
    """Pen position for ``text`` so its ink starts at ``left``, and where that ink lands."""
    from . import scanfont

    font = book.choice(ws, text[0]).font
    if font is None:
        return None
    try:
        word = scanfont._Word(font, text)
    except Exception:
        return None
    if word.ink is None:
        return None
    ink_h, ink_w, base_below_top, origin_left = word.geometry()
    k = ws.size / scanfont._Word.SIZE
    top = baseline - base_below_top * k
    return left - origin_left * k, fitz.Rect(left, top, left + ink_w * k, top + ink_h * k)


def _scan_spot_edit(page: fitz.Page, pt: PageText, book: FontBook, line: Line, new_text: str,
                    scan: "ScanStyle", override: dict) -> tuple[list, list, list, list] | None:
    """Redraw only the characters that changed, leaving the rest of the scan untouched.

    Changing one digit of a date should not re-render the whole date in a recognised
    font: every character that keeps its own scanned pixels is one that cannot look
    wrong.  What is redrawn is measured against the print around it - the size and
    baseline come from comparing this line's scanned characters with the same
    characters drawn in the chosen font - so the new ones sit where the old ones did.

    Returns (covers, removed characters, runs, stamps), or None when the change cannot
    be contained: a different length, a space, or a replacement too wide for its place.
    """
    old_text = line.text
    if len(new_text) != len(old_text) or len(line.chars) != len(old_text):
        return None
    differs = [i for i, (a, b) in enumerate(zip(old_text, new_text)) if a != b]
    if not differs:
        return None
    # changes that sit apart from each other are redrawn one by one, so correcting a
    # day and a year in the same date leaves everything between them as it was
    groups: list[list[int]] = []
    for i in differs:
        if groups and i == groups[-1][-1] + 1:
            groups[-1].append(i)
        else:
            groups.append([i])
    ws = _write_style(pt, line.main_style, override, scan.ink)
    band = scan.covers[0][0] if scan.covers else fitz.Rect(line.bbox)
    pad = max(0.4, 0.06 * ws.size)

    def look(i0: int, i1: int) -> tuple[tuple, tuple, fitz.Rect] | None:
        """Ink of characters i0..i1, without reaching into their neighbours."""
        x0 = line.chars[i0].x0 - pad
        if i0 > 0:  # character boxes can overlap slightly: split the difference
            x0 = max(x0, (line.chars[i0 - 1].x1 + line.chars[i0].x0) / 2)
        x1 = line.chars[i1 - 1].x1 + pad
        if i1 < len(line.chars):
            x1 = min(x1, (line.chars[i1 - 1].x1 + line.chars[i1].x0) / 2)
        return _spot_colors(page, fitz.Rect(x0, band.y0, x1, band.y1)) if x1 > x0 else None

    first = look(differs[0], differs[-1] + 1)
    if first is not None:  # the colour of these very characters, which the new ones match
        ws = _write_style(pt, line.main_style, override, first[0])

    # measure the print: every character of this line, where its ink really is
    marks: list[tuple[str, fitz.Rect]] = []
    for i, ch in enumerate(line.chars):
        if not ch.c.isalnum() or not ch.is_glyph:
            continue
        got = look(i, i + 1)
        if got is not None:
            marks.append((ch.c, got[2]))
    fitted = _scan_scale(book, ws, marks)
    if fitted is None:
        return None
    size, baseline = fitted
    ws = _replace(ws, size=size)

    covers: list[tuple] = []
    removed: list[Char] = []
    runs: list[Run] = []
    stamps: list[tuple] = []
    # characters that are staying put, to calibrate the look of the ones that are not
    keeping = [(c, box) for i, (c, box) in enumerate(marks)
               if not any(abs(box.x0 - line.chars[d].x0) < 0.01 for d in differs)]
    for group in groups:
        i0, i1 = group[0], group[-1] + 1
        span = line.chars[i0:i1]
        if any(not c.is_glyph for c in span) or any(new_text[i].isspace() for i in range(i0, i1)):
            return None
        text = new_text[i0:i1]
        # compare like with like: the width of a run of text is pen to pen, while a
        # character box is only the ink, which is narrower by its side bearings
        if i1 < len(line.chars):
            room = line.chars[i1].x0 - span[0].x0
        else:
            room = span[-1].x1 - span[0].x0 + 0.12 * ws.size
        if book.width(ws, text) > room * 1.05:
            return None
        spot = look(i0, i1)
        if spot is None:
            return None
        _, paper, ink_box = spot
        placed = _scan_place(book, ws, text, ink_box.x0, baseline)
        if placed is None:
            return None
        x_pen, lands = placed
        # cover only where ink actually is, old or new: on a scan every extra square of
        # flat colour shows, especially over a security pattern - and never spill into
        # the neighbouring characters
        limit_left = (line.chars[i0 - 1].x1 + span[0].x0) / 2 if i0 > 0 else span[0].x0 - pad
        limit_right = (span[-1].x1 + line.chars[i1].x0) / 2 if i1 < len(line.chars) else span[-1].x1 + pad
        patch = fitz.Rect(max(limit_left, min(ink_box.x0, lands.x0) - pad),
                          min(ink_box.y0, lands.y0) - pad,
                          min(limit_right, max(ink_box.x1, lands.x1) + pad),
                          max(ink_box.y1, lands.y1) + pad)
        removed.extend(span)
        # print it the way the scanner would have: same weight, same soft edges, over
        # paper taken from the page, so the correction does not read as pasted on
        picture = None
        font = book.choice(ws, text[0]).font
        if font is not None:
            near = min(keeping, key=lambda m: abs(m[1].x0 - ink_box.x0), default=None)
            reference = None
            if near is not None:
                placed_ref = _scan_place(book, ws, near[0], near[1].x0, baseline)
                if placed_ref is not None:
                    pad_ref = max(0.4, 0.06 * ws.size)
                    reference = (near[1] + (-pad_ref, -pad_ref, pad_ref, pad_ref),
                                 [(placed_ref[0], baseline, near[0], font, ws.size)])
            try:
                picture = blend.stamp(page, patch, [(x_pen, baseline, text, font, ws.size)], reference, ws.color)
            except Exception:
                picture = None
        if picture is not None:
            stamps.append((patch, picture))
            runs.append(Run(x_pen, baseline, text, _replace(ws, alpha=0.0)))  # for search and copy
        else:
            covers.append((patch, paper))
            runs.append(Run(x_pen, baseline, text, ws))
    return covers, removed, runs, stamps


def _layout_scan_line(pt: PageText, book: FontBook, line: Line, new_text: str, override: dict | None,
                      dx: float, dy: float, ink, word_bold: list | None = None, forced_bold: bool | None = None) -> list[Run]:
    """Lay out an edited line of scanned text.

    OCR positions are only reliable per word, so unchanged and replaced words keep the
    left edge of the word they correspond to (preserving columns and spacing), and
    words only move right when a longer word would otherwise collide.
    """
    old_words = line_words(line)
    new_words = new_text.split()
    base = dict(override or {})
    styles = {b: _write_style(pt, line.main_style, {**base, "bold": b}, ink) for b in (False, True)}
    ws = styles[bool(base.get("bold"))]
    space = book.space_width(ws)
    # words that were separated by a wide gap (tabs, table columns) keep their position;
    # ordinary words flow after the previous one like in a word processor
    column_anchor: list[float | None] = [None] * len(new_words)
    bold_of: list[bool | None] = [None] * len(new_words)
    matcher = difflib.SequenceMatcher(None, [w for w, _ in old_words], new_words, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("equal", "replace"):
            for k in range(min(i2 - i1, j2 - j1)):
                i = i1 + k
                if word_bold and i < len(word_bold):
                    bold_of[j1 + k] = word_bold[i]
                if i > 0 and old_words[i][1].x0 - old_words[i - 1][1].x1 > 2.5 * space:
                    column_anchor[j1 + k] = old_words[i][1].x0
            if tag == "replace" and word_bold and j2 - j1 > i2 - i1 and i2 - 1 < len(word_bold):
                # a replacement with more words than before continues in the replaced text's weight
                for j in range(j1 + (i2 - i1), j2):
                    bold_of[j] = word_bold[i2 - 1]
        elif tag == "insert" and word_bold:
            # new words take the weight of the word before them (or after, at the start)
            neighbour = i1 - 1 if i1 > 0 else i1
            if 0 <= neighbour < len(word_bold):
                for j in range(j1, j2):
                    bold_of[j] = word_bold[neighbour]
    runs: list[Run] = []
    baseline = line.baseline + dy
    pen: float | None = None
    x = (old_words[0][1].x0 if old_words else line.bbox.x0) + dx
    for word, anchor, bold in zip(new_words, column_anchor, bold_of):
        if pen is not None:
            x = pen + space
            if anchor is not None:
                x = max(x, anchor + dx)
        if forced_bold is not None:
            bold = forced_bold
        wws = styles[bool(base.get("bold")) if bold is None else bool(bold)]
        runs.append(Run(x, baseline, word, wws))
        pen = x + book.width(wws, word)
    return runs


def _layout_paragraph(pt: PageText, book: FontBook, para: Paragraph, new_text: str,
                      seq: list[Char | None], source, override: dict | None, dx: float, dy: float, ink) -> tuple[list[Run], float]:
    first_line = para.lines[0]
    main = first_line.main_style

    def style_at(i: int) -> int:
        s = source[i]
        if s is not None:
            k = s
            while k >= 0 and (k >= len(seq) or seq[k] is None):
                k -= 1
            if k < 0:
                k = s
                while k < len(seq) and seq[k] is None:
                    k += 1
            if 0 <= k < len(seq) and seq[k] is not None:
                return seq[k].style  # type: ignore[union-attr]
        return main

    # tokens: words (list of (char, ws)) and hard breaks
    tokens: list[object] = []
    word: list[tuple[str, WriteStyle]] = []
    for i, ch in enumerate(new_text):
        if ch == "\n":
            if word:
                tokens.append(word)
                word = []
            tokens.append("\n")
        elif ch.isspace():
            if word:
                tokens.append(word)
                word = []
        else:
            word.append((ch, _write_style(pt, style_at(i), override, ink)))
    if word:
        tokens.append(word)

    base_ws = _write_style(pt, main, override, ink)
    space = book.space_width(base_ws)
    left = para.left + dx
    right = para.right + dx
    width_limit = max(right - left, 10.0)

    def word_runs(w: list[tuple[str, WriteStyle]]) -> list[tuple[str, WriteStyle]]:
        out: list[tuple[str, WriteStyle]] = []
        for ch, ws in w:
            if out and out[-1][1] == ws:
                out[-1] = (out[-1][0] + ch, ws)
            else:
                out.append((ch, ws))
        return out

    def word_width(w) -> float:
        return sum(book.width(ws, t) for t, ws in word_runs(w))

    lines: list[tuple[list, bool]] = []  # (words, ends_with_hard_break)
    current: list = []
    current_w = 0.0
    for tok in tokens:
        if tok == "\n":
            lines.append((current, True))
            current, current_w = [], 0.0
            continue
        ww = word_width(tok)
        avail = width_limit - (para.indent if not lines else 0.0)
        extra = ww if not current else current_w + space + ww
        if current and extra > avail + 0.01:
            lines.append((current, False))
            current, current_w = [tok], ww
        else:
            current.append(tok)
            current_w = extra
    lines.append((current, True))

    runs: list[Run] = []
    gap = para.gap * (float(override["size"]) / pt.styles[main].size if override and override.get("size") else 1.0)
    y = first_line.baseline + dy
    for li, (words, hard) in enumerate(lines):
        indent = para.indent if li == 0 else 0.0
        widths = [word_width(w) for w in words]
        text_w = sum(widths) + space * max(0, len(words) - 1)
        avail = width_limit - indent
        align = para.align
        is_last = li == len(lines) - 1 or hard
        gap_w = space
        if align == "justify" and not is_last and len(words) > 1:
            gap_w = (avail - sum(widths)) / (len(words) - 1)
            x = left + indent
        elif align == "center":
            x = left + (width_limit - text_w) / 2
        elif align == "right":
            x = right - text_w
        else:
            x = left + indent
        for w, ww in zip(words, widths):
            cx = x
            for text, ws in word_runs(w):
                runs.append(Run(cx, y, text, ws))
                cx += book.width(ws, text)
            x += ww + gap_w
        y += gap
    bottom = y - gap
    return runs, bottom


# --------------------------------------------------------------------------- scanned text
@dataclass
class ScanStyle:
    """How text that only exists as pixels in a scan looks (from font recognition)."""
    match: "scanfont.ScanMatch | None"
    covers: list  # [(rect, background colour)]
    ink: tuple

    def auto_style(self) -> dict:
        if not self.match:
            return {}
        return {"family": self.match.family, "bold": self.match.bold, "italic": False, "size": round(self.match.size, 2)}

    def payload(self, lines: list["Line"]) -> dict:
        data = {"color": "#%02x%02x%02x" % tuple(round(c * 255) for c in self.ink), "matched": bool(self.match)}
        if self.match:
            data.update(self.match.as_dict())
            data["dy"] = round(self.match.baseline - lines[0].baseline, 2)
        return data


def line_words(line: Line) -> list[tuple[str, fitz.Rect]]:
    words: list[tuple[str, fitz.Rect]] = []
    text, rect = "", None
    for ch in line.chars + [None]:
        if ch is None or ch.c.isspace():
            if text and rect is not None:
                words.append((text, rect))
            text, rect = "", None
            continue
        r = fitz.Rect(ch.x0, ch.y0, ch.x1, ch.y1)
        text += ch.c
        rect = r if rect is None else rect | r
    return words


def scan_style(page: fitz.Page, pt: PageText, lines: list[Line], memory: FontMemory) -> ScanStyle | None:
    """Font, cover patches and ink colour for OCR text sitting on a scanned image (None for real text)."""
    glyphs = [c for line in lines for c in line.chars if c.is_glyph]
    if not glyphs or any(pt.styles[c.style].visible for c in glyphs):
        return None
    key = (page.xref, tuple((line.text, tuple(round(v, 1) for v in line.bbox)) for line in lines))
    if key in memory.scans:
        return memory.scans[key]
    from . import scanfont

    words = [w for line in lines for w in line_words(line)]
    try:
        match = scanfont.match_words(page, words)
    except Exception:
        match = None
    covers = []
    ink_rgb = None
    for line in lines:
        rect = None
        try:
            rect = scanfont.line_ink_rect(page, line.bbox)
        except Exception:
            rect = None
        if rect is None:
            rect = fitz.Rect(line.bbox)
        pad = max(1.0, rect.height * 0.14)
        rect = rect + (-pad, -pad, pad, pad)
        bg, ink = sample_colors(page, rect + (-2, -2, 2, 2))
        covers.append((rect, bg))
        ink_rgb = ink_rgb or ink
    style = ScanStyle(match, covers, ink_rgb or (0.0, 0.0, 0.0))
    memory.scans[key] = style
    return style


# --------------------------------------------------------------------------- operations
def _ink_for(page: fitz.Page, pt: PageText, chars: list[Char]):
    """Cover + ink colour for invisible (OCR) text, else None."""
    glyphs = [c for c in chars if c.is_glyph]
    if not glyphs or any(pt.styles[c.style].visible for c in glyphs):
        return None, None
    rect = fitz.Rect(glyphs[0].x0, glyphs[0].y0, glyphs[0].x1, glyphs[0].y1)
    for c in glyphs[1:]:
        rect |= fitz.Rect(c.x0, c.y0, c.x1, c.y1)
    pad_y = rect.height * 0.18
    rect = rect + (-2, -pad_y, 2, pad_y)
    bg, ink = sample_colors(page, rect)
    return (rect, bg), ink


def _ocr_size_override(pt: PageText, chars: list[Char], override: dict | None) -> dict | None:
    """OCR layers use a different size per word; normalise so the rewritten line looks uniform."""
    if override and override.get("size"):
        return override
    sizes = [pt.styles[c.style].size for c in chars if c.is_glyph]
    if not sizes:
        return override
    merged = dict(override or {})
    merged["size"] = round(statistics.median(sizes), 1)
    return merged


def edit_lines(doc: fitz.Document, pno: int, edits: list[LineEdit], memory: FontMemory) -> dict:
    page = doc[pno]
    pt = PageText(page)
    book = FontBook(doc, page, pt, memory)
    removals: list[Char] = []
    all_runs: list[Run] = []
    covers: list[tuple[fitz.Rect, tuple]] = []
    stamps: list[tuple] = []
    changed = 0
    for edit in edits:
        line = pt.find_line(edit.line_id, edit.orig_text, edit.bbox)
        if line is None:
            raise EditError("This text has changed since it was selected. Please try again.")
        if not line.horizontal:
            raise EditError("Rotated or vertical text can't be edited in place.")
        old_text = line.text
        new_text = normalize_text(edit.new_text).replace("\n", " ")
        override = {k: v for k, v in (edit.style or {}).items() if v is not None} or None
        moved = abs(edit.dx) > 1e-3 or abs(edit.dy) > 1e-3
        if new_text == old_text and not override and not moved:
            continue
        mapping, source = _char_mapping(old_text, new_text)
        scan = scan_style(page, pt, [line], memory)
        if scan and scan.match:
            # text in a scanned image: rewrite the whole line in the recognised font, at the
            # recognised size and baseline, over a patch of paper colour
            user = override or {}
            override = {**scan.auto_style(), **user}
            spot = None
            if not user and not moved:  # a small correction: touch as little of the scan as possible
                spot = _scan_spot_edit(page, pt, book, line, new_text, scan, override)
            if spot is not None:
                spot_covers, spot_removals, spot_runs, spot_stamps = spot
                covers.extend(spot_covers)
                removals.extend(spot_removals)
                all_runs.extend(spot_runs)
                stamps.extend(spot_stamps)
            else:
                covers.extend(scan.covers)
                removals.extend(line.chars)
                all_runs.extend(_layout_scan_line(pt, book, line, new_text, override, edit.dx + scan.match.x_shift,
                                                  edit.dy + scan.match.baseline - line.baseline, scan.ink,
                                                  scan.match.word_bold, user.get("bold")))
            changed += 1
            continue
        cover, ink = (scan.covers[0], scan.ink) if scan else _ink_for(page, pt, line.chars)
        if cover:
            override = _ocr_size_override(pt, line.chars, override)
        if override or moved or cover:
            k = 0
        else:
            k = 0
            while k < len(new_text) and k < len(old_text) and mapping[k] == k:
                k += 1
            # restart at the beginning of the word so words stay in one text object
            while k > 0 and not old_text[k - 1].isspace():
                k -= 1
        removed = line.chars[k:]
        if cover:
            covers.append(cover)
        removals.extend(removed)
        all_runs.extend(_layout_line(pt, book, line, new_text, mapping, source, k, k, override, edit.dx, edit.dy, ink))
        changed += 1
    if not changed:
        return {"changed": 0}
    # fonts are resolved before the page changes (texttrace of the original page)
    for run in all_runs:
        book.segments(run.ws, run.text)
    report = remove_characters(doc, pno, pt.styles, removals)
    page = doc[pno]
    for rect, color in covers:
        page.draw_rect(rect, color=None, fill=color, width=0)
    for rect, picture in stamps:
        try:
            page.insert_image(rect, pixmap=picture)
        except Exception:
            continue
    writer = Writer(page, book)
    for run in all_runs:
        writer.add(run)
    writer.commit()
    report["changed"] = changed
    return report


def edit_paragraph(doc: fitz.Document, pno: int, para_id: int, line_ids: list[str], orig_text: str,
                   new_text: str, style: dict | None, memory: FontMemory, dx: float = 0.0, dy: float = 0.0) -> dict:
    page = doc[pno]
    pt = PageText(page)
    para = None
    if 0 <= para_id < len(pt.paragraphs) and [l.id for l in pt.paragraphs[para_id].lines] == list(line_ids):
        para = pt.paragraphs[para_id]
    if para is None:
        for p in pt.paragraphs:
            if normalize_text(paragraph_text(p)[0]) == normalize_text(orig_text):
                para = p
                break
    if para is None:
        raise EditError("This paragraph has changed since it was selected. Please try again.")
    book = FontBook(doc, page, pt, memory)
    old_text, seq = paragraph_text(para)
    new_text = normalize_text(new_text).strip("\n")
    override = {k: v for k, v in (style or {}).items() if v is not None} or None
    if new_text == old_text and not override and not dx and not dy:
        return {"changed": 0}
    all_chars = [c for line in para.lines for c in line.chars]
    covers: list = []
    ink = None
    scan = scan_style(page, pt, para.lines, memory)
    if scan and scan.match:
        override = {**scan.auto_style(), **(override or {})}
        dx, dy = dx + scan.match.x_shift, dy + scan.match.baseline - para.lines[0].baseline
        covers, ink = scan.covers, scan.ink
    else:
        cover, ink = _ink_for(page, pt, all_chars)
        if cover:
            covers = [cover]
            override = _ocr_size_override(pt, all_chars, override)
    _, source = _char_mapping(old_text, new_text)
    runs, bottom = _layout_paragraph(pt, book, para, new_text, seq, source, override, dx, dy, ink)
    for run in runs:
        book.segments(run.ws, run.text)
    report = remove_characters(doc, pno, pt.styles, all_chars)
    page = doc[pno]
    for rect, color in covers:
        page.draw_rect(rect, color=None, fill=color, width=0)
    writer = Writer(page, book)
    for run in runs:
        writer.add(run)
    writer.commit()
    original_bottom = para.lines[-1].baseline
    report["changed"] = 1
    report["overflow"] = max(0.0, bottom - original_bottom)
    return report


def delete_lines(doc: fitz.Document, pno: int, items: list[dict], memory: FontMemory | None = None) -> dict:
    page = doc[pno]
    pt = PageText(page)
    removals: list[Char] = []
    covers = []
    for item in items:
        line = pt.find_line(item["line_id"], item.get("orig_text"), item.get("bbox"))
        if line is None:
            raise EditError("This text has changed since it was selected. Please try again.")
        removals.extend(line.chars)
        scan = scan_style(page, pt, [line], memory) if memory is not None else None
        if scan:
            covers.extend(scan.covers)
        else:
            cover, _ = _ink_for(page, pt, line.chars)
            if cover:
                covers.append(cover)
    report = remove_characters(doc, pno, pt.styles, removals)
    page = doc[pno]
    for rect, color in covers:
        page.draw_rect(rect, color=None, fill=color, width=0)
    report["changed"] = len(items)
    return report


def find_replace(doc: fitz.Document, find: str, replace: str, *, match_case: bool, whole_word: bool,
                 use_regex: bool, memory: FontMemory, pages: list[int] | None = None) -> dict:
    if not find:
        raise EditError("Enter the text to find.")
    pattern = find if use_regex else re.escape(find)
    if whole_word:
        pattern = rf"(?<!\w){pattern}(?!\w)"
    try:
        rx = re.compile(pattern, 0 if match_case else re.IGNORECASE)
    except re.error as exc:
        raise EditError(f"Invalid pattern: {exc}") from exc
    total = 0
    touched: list[int] = []
    skipped = 0
    for pno in pages if pages is not None else range(doc.page_count):
        page = doc[pno]
        pt = PageText(page)
        edits = []
        for line in pt.lines:
            text = line.text
            if not rx.search(text):
                continue
            if not line.horizontal:
                skipped += len(rx.findall(text))
                continue
            try:
                new_text, count = rx.subn(replace if use_regex else replace.replace("\\", "\\\\"), text)
            except re.error as exc:
                raise EditError(f"Invalid replacement: {exc}") from exc
            if count and new_text != text:
                total += count
                edits.append(LineEdit(line.id, text, new_text, bbox=list(line.bbox)))
        if edits:
            edit_lines(doc, pno, edits, memory)
            touched.append(pno)
    return {"replaced": total, "pages": touched, "skipped": skipped}


def embedded_font_for_preview(doc: fitz.Document, pno: int, font_name: str, memory: FontMemory) -> bytes | None:
    """Patched embedded font bytes so the browser can preview text in its original font."""
    page = doc[pno]
    pt = PageText(page)
    book = FontBook(doc, page, pt, memory)
    emb = book.embedded(font_name)
    if not emb or not emb.is_sfnt:
        return None
    patched = patch_cmap(emb.buffer, emb.mapping)
    return patched
