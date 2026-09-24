"""Repair text layers that nothing can read.

Some producers — macOS Preview's OCR above all — write the recognised text with an
Identity-H subset font and no ``/ToUnicode`` map.  The characters are then simply
not in the file: every glyph extracts as U+FFFD, so the text cannot be searched,
copied or edited, even though it is plainly there on the page.

Subsetters keep the glyph ids of the font they subset, so the characters can be
recovered by looking those ids up in that font and written back as a proper
``/ToUnicode`` map.  A mapping is only accepted when the widths the page actually
draws match the widths of the font it was matched against, so a wrong guess is
rejected rather than turning the page into plausible-looking nonsense.
"""

from __future__ import annotations

import pymupdf as fitz

from .fonts import SYSTEM_FONTS, parse_pdf_font_name, squash
from .textedit import TEXT_FLAGS

REPLACEMENT = 0xFFFD
# ASCII first, so a character that shares a glyph with another (space and no-break
# space, say) is recovered as the plain one
REPERTOIRE = ([chr(c) for c in range(32, 127)] + [chr(c) for c in range(0xA0, 0x180)]
              + list("‐‑‒–—―‘’‚“”„†‡•…‰‹›⁄€™−≈≠≤≥√∞◊"))
FALLBACKS = ["Arial", "Helvetica", "Times New Roman", "Courier New", "Verdana", "Tahoma"]
MIN_COVERAGE = 0.6  # share of drawn glyphs a mapping has to explain
MIN_WIDTH_FIT = 0.7  # share of those glyphs whose drawn width matches the candidate
WIDTH_TOLERANCE = 0.08


def _strip_subset(name: str) -> str:
    return name[7:] if len(name) > 7 and name[6] == "+" and name[:6].isupper() else name


class Usage:
    """How one font is used on the pages: which glyph ids, and how wide they are drawn."""

    def __init__(self) -> None:
        self.gids: set[int] = set()
        self.widths: dict[int, list[float]] = {}  # only glyphs followed by another, for checking
        self.count = 0
        self.unreadable = 0

    def seen(self, readable: bool) -> None:
        """One extracted character, and whether it came out as a real one."""
        self.count += 1
        if not readable:
            self.unreadable += 1

    def drawn(self, gid: int, width: float | None) -> None:
        self.gids.add(gid)
        if width is not None and width > 0:
            self.widths.setdefault(gid, []).append(width)

    @property
    def broken(self) -> bool:
        return self.count > 0 and self.unreadable > 0.5 * self.count


def survey(doc: fitz.Document) -> dict[str, Usage]:
    """Per font: what its text extracts as, and which glyphs it draws how wide.

    Whether a font is readable is judged by what extraction actually returns, not by
    the glyph trace: MuPDF falls back to the font's own character map when there is
    no /ToUnicode, and a font it can already read must be left alone.
    """
    usage: dict[str, Usage] = {}
    for page in doc:
        try:
            # the same flags the editor reads text with: without them MuPDF fills
            # unknown characters in with the glyph id, which only looks like text
            blocks = page.get_text("rawdict", flags=TEXT_FLAGS)["blocks"]
        except Exception:
            blocks = []
        unreadable = False
        for block in blocks:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    name = _strip_subset(span.get("font") or "")
                    if not name:
                        continue
                    u = usage.setdefault(name, Usage())
                    for ch in span.get("chars", ()):
                        readable = ch.get("c") != "\ufffd"
                        unreadable = unreadable or not readable
                        u.seen(readable)
        if not unreadable:  # nothing to recover here, so skip the costly glyph trace
            continue
        try:
            trace = page.get_texttrace()
        except Exception:
            continue
        for span in trace:
            name = _strip_subset(span.get("font") or "")
            size = span.get("size") or 0
            chars = span.get("chars") or ()
            if not name or size <= 0:
                continue
            u = usage.setdefault(name, Usage())
            dx, dy = span.get("dir") or (1.0, 0.0)
            for i, ch in enumerate(chars):
                origin = ch[2]
                nxt = chars[i + 1] if i + 1 < len(chars) else None
                width = None
                if nxt is not None:  # advance along the writing direction, whatever the rotation
                    step = ((nxt[2][0] - origin[0]) * dx + (nxt[2][1] - origin[1]) * dy) / size
                    width = step if step > 0 else None
                u.drawn(ch[1], width)
    return usage


def _faces(basefont: str) -> list[fitz.Font]:
    """System fonts the subset may have been made from, likeliest first."""
    info = parse_pdf_font_name(basefont)
    names: list[str] = []
    fams = SYSTEM_FONTS.families
    for key in info["candidates"]:
        fam = fams.get(key)
        if fam and fam.name not in names:
            names.append(fam.name)
    names.extend(n for n in FALLBACKS if n not in names)
    fonts = []
    for name in names:
        found = SYSTEM_FONTS.font_for(name, info["bold"], info["italic"])
        if found:
            fonts.append(found[0])
    return fonts


def _mapping(font: fitz.Font, usage: Usage) -> tuple[dict[int, str], float, float]:
    """Map glyph ids to characters through one font, and say how well it fits."""
    by_gid: dict[int, str] = {}
    for ch in REPERTOIRE:
        gid = font.has_glyph(ord(ch))
        if gid and gid not in by_gid:
            by_gid[gid] = ch
    drawn = sum(len(w) for w in usage.widths.values()) or 1
    explained = fits = 0
    for gid, widths in usage.widths.items():
        ch = by_gid.get(gid)
        if ch is None:
            continue
        explained += len(widths)
        want = font.glyph_advance(ord(ch))
        for w in widths:
            if abs(w - want) <= WIDTH_TOLERANCE + 0.02 * want:
                fits += 1
    return by_gid, explained / drawn, (fits / explained if explained else 0.0)


def _cmap_stream(mapping: dict[int, str]) -> bytes:
    """A /ToUnicode CMap for Identity-H, where the code is the glyph id."""
    out = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    items = sorted(mapping.items())
    for start in range(0, len(items), 100):
        chunk = items[start:start + 100]
        out.append(f"{len(chunk)} beginbfchar")
        for gid, ch in chunk:
            out.append(f"<{gid:04X}> <{ch.encode('utf-16-be').hex().upper()}>")
        out.append("endbfchar")
    out += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return ("\n".join(out) + "\n").encode("latin-1")


def _font_xrefs(doc: fitz.Document, basefont: str) -> list[int]:
    """The font dictionaries a recovered map may be written to.

    Only fonts that carry no /ToUnicode at all are ever touched.  The name a span
    reports is the one inside the font program, which need not be the /BaseFont of
    the dictionary, so an unambiguous single candidate is accepted as well.
    """
    key = squash(basefont)
    named: list[int] = []
    others: list[int] = []
    for pno in range(doc.page_count):
        try:
            fonts = doc[pno].get_fonts(full=True)
        except Exception:
            continue
        for item in fonts:
            xref = item[0]
            if xref in named or xref in others:
                continue
            if doc.xref_get_key(xref, "ToUnicode")[0] != "null":
                continue  # this one can already be read
            if squash(_strip_subset(item[3] or "")) == key:
                named.append(xref)
            else:
                others.append(xref)
    if named:
        return named
    return others if len(others) == 1 else []


def repair(doc: fitz.Document) -> list[str]:
    """Give unreadable text layers a /ToUnicode map. Returns the fonts repaired."""
    repaired: list[str] = []
    done: set[int] = set()
    for name, usage in survey(doc).items():
        if not usage.broken or not usage.widths:  # widths are what makes a match checkable
            continue
        best: tuple[float, dict[int, str]] | None = None
        for font in _faces(name):
            mapping, coverage, fit = _mapping(font, usage)
            if coverage >= MIN_COVERAGE and fit >= MIN_WIDTH_FIT:
                score = coverage * fit
                if best is None or score > best[0]:
                    best = (score, mapping)
                if fit > 0.95:
                    break
        if best is None:
            continue
        used = {gid: ch for gid, ch in best[1].items() if gid in usage.gids}
        if not used:
            continue
        written = False
        for xref in _font_xrefs(doc, name):
            if xref in done:
                continue
            try:
                stream = doc.get_new_xref()
                doc.update_object(stream, "<<>>")
                doc.update_stream(stream, _cmap_stream(used), new=True, compress=True)
                doc.xref_set_key(xref, "ToUnicode", f"{stream} 0 R")
            except Exception:
                continue
            done.add(xref)
            written = True
        if written:
            repaired.append(name)
    return repaired
