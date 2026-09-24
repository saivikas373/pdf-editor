"""Recognise the typeface of text inside scanned pages.

Scanned PDFs (and PDFs made searchable with OCR) only contain a picture of the
text; the OCR layer uses a placeholder font with no visual information. To make
edits blend in, we render the recognised words in each installed candidate font
and compare the renderings with the scan, pixel by pixel. The best match gives
the family, weight, size and exact baseline of the original text.

Words are compared as greyscale images normalised to a common height, using
normalised cross-correlation; the candidates are also tested with simulated
scan blur, because blur makes thin strokes look bold.
"""

from __future__ import annotations

import math
import operator
import re
import statistics
from collections import Counter
from dataclasses import dataclass

import pymupdf as fitz

from .fonts import SYSTEM_FONTS, squash

# Candidate families, most common in documents first (ties go to the earlier one).
CANDIDATES = [
    "Arial", "Helvetica", "Times New Roman", "Calibri", "Carlito", "Verdana", "Tahoma", "Georgia", "Courier New",
    "Helvetica Neue", "Segoe UI", "Roboto", "Open Sans", "Lato", "Trebuchet MS", "Cambria", "Garamond", "EB Garamond",
    "Palatino", "Book Antiqua", "Century Schoolbook", "Arial Narrow", "Gill Sans", "Futura", "Avenir Next", "Optima",
    "Lucida Grande", "Franklin Gothic Medium", "Source Sans Pro", "PT Sans", "Inter", "Baskerville", "Charter",
    "Hoefler Text", "Times", "Courier", "Menlo", "Consolas", "Andale Mono", "DejaVu Sans", "Liberation Sans",
    "Liberation Serif", "Noto Sans", "Noto Serif",
    # the metric-compatible clones, which is what a Linux server has instead of the
    # names above: Arimo/Tinos/Cousine for Arial/Times/Courier, URW for the PostScript
    # classics, and the open families that PDFs increasingly use directly
    "Arimo", "Tinos", "Cousine", "Liberation Mono", "DejaVu Serif", "DejaVu Sans Mono",
    "Nimbus Sans", "Nimbus Roman", "Nimbus Mono PS", "P052", "C059", "URW Bookman", "URW Gothic",
    "TeX Gyre Termes", "TeX Gyre Heros", "TeX Gyre Pagella", "Charis SIL", "Cantarell", "JetBrains Mono",
]

ASPECT_WEIGHT = 1.6  # penalty for a different width-to-height proportion
_TABLES: dict[int, bytes] = {}


def _threshold_table(thr: int) -> bytes:
    table = _TABLES.get(thr)
    if table is None:
        table = bytes(1 if v < thr else 0 for v in range(256))
        _TABLES[thr] = table
    return table


def _otsu(samples: bytes) -> int:
    counts = Counter(samples)
    hist = [counts.get(i, 0) for i in range(256)]
    total = len(samples)
    sum_all = sum(i * c for i, c in enumerate(hist))
    w_b = sum_b = 0
    best, thr = -1.0, 128
    for t in range(256):
        w_b += hist[t]
        if not w_b:
            continue
        w_f = total - w_b
        if not w_f:
            break
        sum_b += t * hist[t]
        diff = sum_b / w_b - (sum_all - sum_b) / w_f
        between = w_b * w_f * diff * diff
        if between > best:
            best, thr = between, t + 1
    return thr


@dataclass
class Ink:
    """A binarised, tightly cropped piece of text."""
    bits: int
    w: int
    h: int
    count: int
    x0: int  # position of the crop inside its source image (px)
    y0: int

    @property
    def density(self) -> float:
        return self.count / max(1, self.w * self.h)

    @property
    def aspect(self) -> float:
        return self.w / max(1, self.h)


def _row_run(rows_with_ink: list[bool], centre: int, max_gap: int) -> tuple[int, int] | None:
    """Contiguous band of inked rows around ``centre`` (small gaps such as i-dots allowed)."""
    n = len(rows_with_ink)
    if not any(rows_with_ink):
        return None
    centre = min(max(centre, 0), n - 1)
    if not rows_with_ink[centre]:
        near = [r for r in range(n) if rows_with_ink[r]]
        centre = min(near, key=lambda r: abs(r - centre))
    top = bottom = centre
    gap = 0
    r = centre - 1
    while r >= 0:
        if rows_with_ink[r]:
            top, gap = r, 0
        else:
            gap += 1
            if gap > max_gap:
                break
        r -= 1
    gap = 0
    r = centre + 1
    while r < n:
        if rows_with_ink[r]:
            bottom, gap = r, 0
        else:
            gap += 1
            if gap > max_gap:
                break
        r += 1
    return top, bottom


def _extract_ink(gray: bytes, w: int, h: int, thr: int, centre_row: int | None = None, max_gap: int = 2) -> Ink | None:
    mask = gray.translate(_threshold_table(thr))
    rows = [mask.find(b"\x01", r * w, (r + 1) * w) != -1 for r in range(h)]
    run = _row_run(rows, h // 2 if centre_row is None else centre_row, max_gap)
    if run is None:
        return None
    top, bottom = run
    col_bits = 0
    for r in range(top, bottom + 1):
        col_bits |= int.from_bytes(mask[r * w:(r + 1) * w], "little")
    if not col_bits:
        return None
    x0 = ((col_bits & -col_bits).bit_length() - 1) // 8
    x1 = (col_bits.bit_length() - 1) // 8
    cw, ch = x1 - x0 + 1, bottom - top + 1
    data = b"".join(mask[r * w + x0:r * w + x1 + 1] for r in range(top, bottom + 1))
    bits = int.from_bytes(data, "little")
    return Ink(bits, cw, ch, bits.bit_count(), x0, top)


def _gray_crop(gray: bytes, w: int, ink: Ink) -> bytes:
    return b"".join(gray[(ink.y0 + r) * w + ink.x0:(ink.y0 + r) * w + ink.x0 + ink.w] for r in range(ink.h))


CANON_H = 36  # words are compared at this height (px)
BLUR_LEVELS = (0.6, 0.42, 0.3, 0.22)  # rendered at a fraction of CANON_H*2, then scaled up
_INVERT = bytes(255 - v for v in range(256))


def _normalise_table(ink: int, paper: int) -> bytes:
    span = max(1, paper - ink)
    return bytes(max(0, min(255, round((paper - v) * 255 / span))) for v in range(256))


def _resize(gray: bytes, w: int, h: int, new_w: int, new_h: int) -> bytes | None:
    out = fitz.Pixmap(fitz.Pixmap(fitz.csGRAY, w, h, gray, 0), new_w, new_h)
    if (out.width, out.height) != (new_w, new_h):
        return None
    return out.samples


def _stats(a: bytes) -> tuple[int, int, int]:
    return len(a), sum(a), sum(map(operator.mul, a, a))


def _ncc(a: bytes, sa: tuple, b: bytes, sb: tuple) -> float:
    n, s1, ss1 = sa
    _, s2, ss2 = sb
    num = sum(map(operator.mul, a, b)) - s1 * s2 / n
    den = math.sqrt(max(1e-9, (ss1 - s1 * s1 / n) * (ss2 - s2 * s2 / n)))
    return num / den


@dataclass
class Probe:
    """One scanned word, normalised for comparison (ink = 255, paper = 0)."""
    text: str
    dpi: int
    ink: Ink  # tight ink box inside the crop (px)
    origin_x: float  # page coordinates of the crop's top-left
    origin_y: float
    canon: bytes
    canon_w: int
    stats: tuple
    pad_frac: float
    ocr_left: float  # where the OCR layer says the word starts

    def px_to_pt(self, v: float) -> float:
        return v * 72.0 / self.dpi

    @property
    def aspect(self) -> float:
        return self.ink.aspect


@dataclass
class ScanMatch:
    family: str
    bold: bool
    size: float
    baseline: float
    x_shift: float  # pen origin relative to the OCR word boxes
    score: float
    confidence: float
    blur: float
    alternatives: list
    word_bold: list  # weight of every input word (lines can mix bold and regular)

    def as_dict(self) -> dict:
        return {
            "family": self.family, "bold": self.bold, "size": round(self.size, 2), "baseline": round(self.baseline, 2),
            "score": round(self.score, 3), "confidence": round(self.confidence, 2), "alternatives": self.alternatives,
            "wordBold": self.word_bold,
        }


def candidate_faces() -> list[tuple[str, bool, fitz.Font]]:
    faces = []
    seen: set[str] = set()
    for name in CANDIDATES:
        fam = SYSTEM_FONTS.family(name)
        if not fam or squash(fam.name) in seen:
            continue
        seen.add(squash(fam.name))
        for bold in (False, True):
            face = fam.pick(bold, False)
            if face is None or face.bold != bold or face.italic:
                continue  # e.g. a family whose only upright face is semibold
            found = SYSTEM_FONTS.font_for(fam.name, bold, False)
            if not found:
                continue
            font, fake_bold, fake_italic = found
            if fake_bold or fake_italic:
                continue
            faces.append((fam.name, bold, font))
    return faces


def _percentile(samples: bytes, fraction: float) -> int:
    counts = Counter(samples)
    target = len(samples) * fraction
    acc = 0
    for v in range(256):
        acc += counts.get(v, 0)
        if acc >= target:
            return v
    return 255


def _probe(page: fitz.Page, text: str, rect: fitz.Rect) -> Probe | None:
    h_pt = max(rect.height, 2.0)
    dpi = int(min(600, max(150, 48 * 72 / (0.72 * h_pt))))
    clip = fitz.Rect(rect.x0 - 0.06 * h_pt, rect.y0 - 0.3 * h_pt, rect.x1 + 0.06 * h_pt, rect.y1 + 0.3 * h_pt) & page.rect
    if clip.is_empty:
        return None
    pix = page.get_pixmap(clip=clip, dpi=dpi, colorspace=fitz.csGRAY, annots=False)
    gray, w, h = pix.samples, pix.width, pix.height
    if len(gray) != w * h:
        return None
    ink_level, paper = _percentile(gray, 0.02), _percentile(gray, 0.8)
    if paper - ink_level < 40:
        return None  # no clear text here
    thr = _otsu(gray)
    centre = int(((rect.y0 + rect.y1) / 2 - clip.y0) * dpi / 72)
    ink = _extract_ink(gray, w, h, thr, centre, max_gap=max(2, int(0.1 * h_pt * dpi / 72)))
    if ink is None or ink.h < 6 or ink.w < 4:
        return None
    pad = max(1, int(ink.h * 0.08))
    x0, y0 = max(0, ink.x0 - pad), max(0, ink.y0 - pad)
    x1, y1 = min(w, ink.x0 + ink.w + pad), min(h, ink.y0 + ink.h + pad)
    crop = b"".join(gray[r * w + x0:r * w + x1] for r in range(y0, y1)).translate(_normalise_table(ink_level, paper))
    canon_w = max(4, round((x1 - x0) * CANON_H / (y1 - y0)))
    canon = _resize(crop, x1 - x0, y1 - y0, canon_w, CANON_H)
    if canon is None:
        return None
    return Probe(text, dpi, ink, clip.x0, clip.y0, canon, canon_w, _stats(canon), pad / ink.h, rect.x0)


class _Word:
    """A word rendered in one candidate font on a scratch page (re-rendered at several resolutions)."""
    SIZE = 40.0

    def __init__(self, font: fitz.Font, text: str):
        size = self.SIZE
        self.doc = fitz.open()
        self.page = self.doc.new_page(width=max(10.0, font.text_length(text, fontsize=size) + size * 2), height=size * 2.2)
        self.ox, self.oy = size, size * 1.4
        tw = fitz.TextWriter(self.page.rect)
        tw.append((self.ox, self.oy), text, font=font, fontsize=size)
        tw.write_text(self.page)
        pix = self.page.get_pixmap(dpi=144, colorspace=fitz.csGRAY)
        self.ink = _extract_ink(pix.samples, pix.width, pix.height, 128, int((self.oy - size * 0.3) * 2), max_gap=pix.height)

    def geometry(self) -> tuple[float, float, float, float]:
        """Ink height and width, baseline below ink top, pen origin left of the ink (pt at SIZE)."""
        k = 0.5  # 144 dpi -> pt
        return self.ink.h * k, self.ink.w * k, self.oy - self.ink.y0 * k, self.ox - self.ink.x0 * k

    def canon(self, probe: Probe, quality: float) -> tuple[bytes, float] | None:
        """Render so the ink is ``quality`` * 2 * CANON_H px tall, then scale to the probe's canonical size."""
        ink_h_pt = self.ink.h * 0.5
        dpi = max(8, int(round(72 * max(6.0, 2 * CANON_H * quality) / ink_h_pt)))
        pix = self.page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        w, h = pix.width, pix.height
        ink = _extract_ink(pix.samples, w, h, 128, int((self.oy - self.SIZE * 0.3) * dpi / 72), max_gap=h)
        if ink is None:
            return None
        pad = max(1, int(round(ink.h * probe.pad_frac)))
        x0, y0 = max(0, ink.x0 - pad), max(0, ink.y0 - pad)
        x1, y1 = min(w, ink.x0 + ink.w + pad), min(h, ink.y0 + ink.h + pad)
        gray = pix.samples.translate(_INVERT)
        crop = b"".join(gray[r * w + x0:r * w + x1] for r in range(y0, y1))
        canon = _resize(crop, x1 - x0, y1 - y0, probe.canon_w, CANON_H)
        return (canon, ink.aspect) if canon is not None else None


def _eligible(text: str) -> bool:
    core = text.strip(".,;:!?\"'()[]{}")
    return len(core) >= 2 and re.search(r"[A-Za-z0-9]", core) is not None


def match_words(page: fitz.Page, words: list[tuple[str, fitz.Rect]]) -> ScanMatch | None:
    """Find the installed font that best reproduces ``words`` (text + OCR rect) as they appear in the scan.

    The family is chosen for the whole line; the weight is decided per word, because
    lines often mix weights (a bold label followed by a regular value).
    """
    probes: list[Probe | None] = [(_probe(page, t, r) if _eligible(t) else None) for t, r in words[:40]]
    usable = [i for i, p in enumerate(probes) if p is not None]
    if not usable:
        return None
    ranked = sorted(usable, key=lambda i: -(sum(ch.isalpha() for ch in probes[i].text) * 2 + len(probes[i].text)))
    family_idx = ranked[:5]
    faces = candidate_faces()
    rendered: dict[tuple[int, str], _Word] = {}

    def word(font: fitz.Font, text: str) -> _Word:
        key = (id(font), text)
        if key not in rendered:
            rendered[key] = _Word(font, text)
        return rendered[key]

    def word_score(font: fitz.Font, p: Probe, quality: float) -> float | None:
        if any(not ch.isspace() and not font.has_glyph(ord(ch)) for ch in p.text):
            return None
        wd = word(font, p.text)
        if wd.ink is None:
            return None
        got = wd.canon(p, quality)
        if got is None:
            return None
        canon, aspect = got
        return _ncc(p.canon, p.stats, canon, _stats(canon)) - ASPECT_WEIGHT * abs(math.log(aspect / p.aspect))

    # per-word scores for every (face, blur) that was tried on the family probes
    table: dict[tuple[str, bool, float], list[float | None]] = {}

    def evaluate(family: str, bold: bool, font: fitz.Font, quality: float) -> None:
        table[(family, bold, quality)] = [word_score(font, probes[i], quality) for i in family_idx]

    for family, bold, font in faces:
        evaluate(family, bold, font, 1.0)

    def family_score(family: str) -> float:
        total = weights = 0.0
        for k, i in enumerate(family_idx):
            best = max((v[k] for (f, _, _), v in table.items() if f == family and v[k] is not None), default=None)
            if best is None:
                return -9.0
            total += best * len(probes[i].text)
            weights += len(probes[i].text)
        return total / weights if weights else -9.0

    families = list(dict.fromkeys(f for f, _, _ in faces))
    finalists = sorted(families, key=family_score, reverse=True)[:5]
    # scans are blurry, which makes strokes look heavier: re-test the likeliest families
    # in both weights with simulated blur
    for family, bold, font in faces:
        if family in finalists:
            for quality in BLUR_LEVELS:
                evaluate(family, bold, font, quality)
    ordered_families = sorted(finalists, key=family_score, reverse=True)
    best_family = ordered_families[0]
    for fam in ordered_families[1:3]:
        if family_score(best_family) - family_score(fam) < 0.004 and CANDIDATES.index(_canonical(fam)) < CANDIDATES.index(_canonical(best_family)):
            best_family = fam
    best_score = family_score(best_family)

    # the best blur level for each weight of the chosen family
    fonts = {bold: font for fam, bold, font in faces if fam == best_family}
    best_quality: dict[bool, float] = {}
    for bold in fonts:
        options = [(q, v) for (f, b, q), v in table.items() if f == best_family and b == bold]
        def mean(v):
            vals = [x for x in v if x is not None]
            return sum(vals) / len(vals) if vals else -9.0
        best_quality[bold] = max(options, key=lambda o: mean(o[1]))[0]

    # weight per word: compare both weights at the same simulated blur, so the difference
    # in stroke thickness is what decides
    def mean_score(bold: bool) -> float:
        v = table.get((best_family, bold, best_quality[bold]), [])
        vals = [x for x in v if x is not None]
        return sum(vals) / len(vals) if vals else -9.0

    common_quality = best_quality[max(fonts, key=mean_score)]
    levels = sorted({1.0, common_quality, 0.42}, reverse=True)
    word_bold: list[bool | None] = [None] * len(words)
    confident: set[int] = set()

    def core(text: str) -> str:
        return text.strip(".,;:!?\"'()[]{}")

    def is_number(text: str) -> bool:
        return core(text).replace(",", "").replace(".", "").replace("-", "").replace("/", "").isdigit()

    for i in usable:
        if len(fonts) == 1:
            word_bold[i] = next(iter(fonts))
            confident.add(i)
            continue
        margins = []
        for quality in levels:
            reg, bol = word_score(fonts[False], probes[i], quality), word_score(fonts[True], probes[i], quality)
            if reg is not None and bol is not None:
                margins.append(bol - reg)
        if not margins:
            continue
        margin = sum(margins) / len(margins)
        word_bold[i] = margin > 0
        # short words, and digits (similar in both weights), carry little evidence
        needed = (0.015 if len(core(probes[i].text)) >= 5 else 0.04) * (1.5 if is_number(probes[i].text) else 1.0)
        consistent = all(m > 0 for m in margins) or all(m < 0 for m in margins)
        if abs(margin) > needed or (consistent and abs(margin) > needed / 3):
            confident.add(i)

    weight_of = lambda i: len(probes[i].text) if probes[i] else 1  # noqa: E731
    bold_mass = sum(weight_of(i) for i in confident if word_bold[i])
    total_mass = sum(weight_of(i) for i in confident) or 1
    dominant = bold_mass * 2 > total_mass if confident else bool(next(iter(fonts)))
    for i in range(len(words)):
        if i in confident:
            continue
        # unsure words (e.g. "by", "$"): follow the nearest confident word of the same kind
        # (numbers follow numbers), preferring the word to the right (values follow labels)
        same_kind = [j for j in confident if is_number(words[j][0]) == is_number(words[i][0])]
        pool = same_kind or list(confident)
        nearest = min(pool, key=lambda j: (abs(j - i), j < i), default=None)
        word_bold[i] = word_bold[nearest] if nearest is not None else dominant

    sizes, baselines, shifts = [], [], []
    for i in usable:
        bold = bool(word_bold[i]) if bool(word_bold[i]) in fonts else next(iter(fonts))
        p = probes[i]
        ink_h, ink_w, base_below_top, origin_left = word(fonts[bold], p.text).geometry()
        # scale from the rendered word to the scanned one, measured both ways: a scan
        # only ever has ink added to it (a table rule, a speck, a neighbour's descender),
        # never taken away, so the smaller scale is the one that was not contaminated
        k = p.px_to_pt(p.ink.h) / ink_h
        if ink_w > 0:
            k = min(k, p.px_to_pt(p.ink.w) / ink_w)
        sizes.append((_Word.SIZE * k, len(p.text)))
        top = p.origin_y + p.px_to_pt(p.ink.y0)
        baselines.append((top + base_below_top * k, len(p.text)))
        shifts.append(p.origin_x + p.px_to_pt(p.ink.x0) - origin_left * k - p.ocr_left)
    runner = family_score(ordered_families[1]) if len(ordered_families) > 1 else 0.0
    confidence = max(0.0, min(1.0, 0.5 + (best_score - runner) * 8)) * max(0.0, min(1.0, best_score / 0.6))
    alternatives = [{"family": f, "score": round(family_score(f), 3)} for f in ordered_families[:5]]
    return ScanMatch(best_family, dominant, _weighted_median(sizes), _weighted_median(baselines),
                     statistics.median(shifts), best_score, confidence, best_quality.get(dominant, 1.0), alternatives,
                     [bool(b) for b in word_bold])


def _canonical(family: str) -> str:
    key = squash(family)
    for name in CANDIDATES:
        if squash(name) == key:
            return name
    return CANDIDATES[-1]


def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    pairs = sorted(pairs)
    half = sum(w for _, w in pairs) / 2
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= half:
            return value
    return pairs[-1][0]


def line_ink_rect(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect | None:
    """Tight bounding box of the scanned ink belonging to one text line."""
    h_pt = max(rect.height, 2.0)
    dpi = int(min(300, max(110, 36 * 72 / h_pt)))
    clip = fitz.Rect(rect.x0 - 0.3 * h_pt, rect.y0 - 0.35 * h_pt, rect.x1 + 0.3 * h_pt, rect.y1 + 0.35 * h_pt) & page.rect
    if clip.is_empty:
        return None
    pix = page.get_pixmap(clip=clip, dpi=dpi, colorspace=fitz.csGRAY, annots=False)
    gray = pix.samples
    if len(gray) != pix.width * pix.height:
        return None
    thr = _otsu(gray)
    centre = int(((rect.y0 + rect.y1) / 2 - clip.y0) * dpi / 72)
    ink = _extract_ink(gray, pix.width, pix.height, thr, centre, max_gap=max(2, int(0.12 * h_pt * dpi / 72)))
    if ink is None:
        return None
    k = 72.0 / dpi
    return fitz.Rect(clip.x0 + ink.x0 * k, clip.y0 + ink.y0 * k, clip.x0 + (ink.x0 + ink.w) * k, clip.y0 + (ink.y0 + ink.h) * k)
