"""Make replacement characters look like they were scanned with the page.

Vector text drawn onto a scan always gives itself away: its strokes are crisp and
evenly dark, while scanned print is soft, uneven and lighter at its edges.  This
renders a replacement at the scan's own resolution, matches its weight and softness
to the characters beside it, and lays it over paper lifted from the page itself, so
the result carries the same grain as everything around it.

Everything is measured from the page: the strength of the ink, how far the strokes
bleed, and the colour of both.  Nothing here invents a look of its own.
"""

from __future__ import annotations

import pymupdf as fitz

SUPERSAMPLE = 4  # render this much finer than the scan, then average down
MIN_DPI, MAX_DPI = 120, 400


def scan_dpi(page: fitz.Page) -> int:
    """Resolution of the picture the page is made of, so a stamp can match it."""
    best = 0.0
    try:
        for info in page.get_image_info():
            rect = fitz.Rect(info["bbox"]) & page.rect
            if rect.width > 1 and info.get("width"):
                best = max(best, info["width"] * 72.0 / rect.width)
    except Exception:
        pass
    return int(max(MIN_DPI, min(MAX_DPI, best or 200)))


class Grey:
    """A small single-channel image, as a list of floats in 0..1."""

    def __init__(self, data: list[float], w: int, h: int):
        self.data, self.w, self.h = data, w, h

    def blur(self, radius: float) -> "Grey":
        """Separable box blur; radius is in pixels and may be fractional."""
        if radius <= 0.01:
            return self
        r = int(radius)
        frac = radius - r
        out = list(self.data)
        for axis in (0, 1):
            src, out = out, [0.0] * (self.w * self.h)
            for y in range(self.h):
                for x in range(self.w):
                    total = weight = 0.0
                    for d in range(-r - 1, r + 2):
                        f = 1.0 if abs(d) <= r else frac
                        if f <= 0:
                            continue
                        xx, yy = (x + d, y) if axis == 0 else (x, y + d)
                        xx = min(self.w - 1, max(0, xx))
                        yy = min(self.h - 1, max(0, yy))
                        total += src[yy * self.w + xx] * f
                        weight += f
                    out[y * self.w + x] = total / weight if weight else 0.0
        return Grey(out, self.w, self.h)

    def gamma(self, g: float) -> "Grey":
        if abs(g - 1.0) < 0.01:
            return self
        return Grey([v ** g if v > 0 else 0.0 for v in self.data], self.w, self.h)

    def scaled(self, factor: float) -> "Grey":
        return Grey([min(1.0, v * factor) for v in self.data], self.w, self.h)

    def resample(self, w: int, h: int) -> "Grey":
        """Average down to exactly w x h, the way a scanner would.

        The block each output pixel averages is worked out from the coordinates, so a
        rendering that is not a whole multiple of the target still lines up with it.
        """
        if w <= 0 or h <= 0:
            return Grey([], 0, 0)
        out = [0.0] * (w * h)
        for y in range(h):
            y0, y1 = y * self.h // h, max(y * self.h // h + 1, (y + 1) * self.h // h)
            for x in range(w):
                x0, x1 = x * self.w // w, max(x * self.w // w + 1, (x + 1) * self.w // w)
                total = 0.0
                for yy in range(y0, min(y1, self.h)):
                    row = yy * self.w
                    total += sum(self.data[row + x0:row + min(x1, self.w)])
                count = (min(y1, self.h) - y0) * (min(x1, self.w) - x0)
                out[y * w + x] = total / count if count else 0.0
        return Grey(out, w, h)

    @property
    def coverage(self) -> float:
        return sum(self.data) / len(self.data) if self.data else 0.0

    def softness(self) -> float:
        """Share of the ink that sits at a stroke's soft edge rather than its core."""
        marks = [v for v in self.data if v > 0.12]
        if len(marks) < 6:
            return 0.0
        return sum(1 for v in marks if v < 0.8) / len(marks)

    def stroke(self) -> float:
        """Roughly how thick the pen was, in pixels: ink area over half its outline.

        Thickness is what has to match, not how much ink there is: an 8 covers more
        paper than a 0 however faithfully it is printed.
        """
        ink = [v > 0.35 for v in self.data]
        area = sum(1 for v in ink if v)
        if area < 4:
            return 0.0
        edge = 0
        for y in range(self.h):
            for x in range(self.w):
                i = y * self.w + x
                if not ink[i]:
                    continue
                if (x == 0 or x == self.w - 1 or y == 0 or y == self.h - 1
                        or not ink[i - 1] or not ink[i + 1] or not ink[i - self.w] or not ink[i + self.w]):
                    edge += 1
        return 2.0 * area / edge if edge else 0.0

    def peak(self) -> float:
        marks = sorted(v for v in self.data if v > 0.12)
        return marks[int(len(marks) * 0.92)] if marks else 0.0


def _read(page: fitz.Page, rect: fitz.Rect, dpi: int) -> tuple[fitz.Pixmap, Grey]:
    """The page as it is here, plus how strongly each pixel is inked (0..1)."""
    pix = page.get_pixmap(clip=rect, dpi=dpi, colorspace=fitz.csRGB, annots=False)
    n, s = pix.n, pix.samples
    lum = [(0.299 * s[i * n] + 0.587 * s[i * n + 1] + 0.114 * s[i * n + 2]) for i in range(pix.width * pix.height)]
    ordered = sorted(lum)
    paper = ordered[int(len(ordered) * 0.8)] if ordered else 255.0
    darkest = ordered[int(len(ordered) * 0.01)] if ordered else 0.0
    span = max(20.0, paper - darkest)
    return pix, Grey([min(1.0, max(0.0, (paper - v) / span)) for v in lum], pix.width, pix.height)


def _render(page_rect: fitz.Rect, rect: fitz.Rect, runs: list, dpi: int) -> Grey | None:
    """The replacement text alone, on nothing, at the same place on the page."""
    doc = fitz.open()
    sheet = doc.new_page(width=page_rect.width, height=page_rect.height)
    writer = fitz.TextWriter(sheet.rect)
    drew = False
    for x, baseline, text, font, size in runs:
        try:
            writer.append((x, baseline), text, font=font, fontsize=size)
            drew = True
        except Exception:
            continue
    if not drew:
        return None
    writer.write_text(sheet, color=(0, 0, 0))
    pix = sheet.get_pixmap(clip=rect, dpi=dpi, colorspace=fitz.csGRAY, annots=False)
    return Grey([(255 - v) / 255 for v in pix.samples], pix.width, pix.height)


def _erase(pix: fitz.Pixmap, ink: Grey) -> list[list[int]]:
    """The paper under the old characters: every inked pixel replaced by paper near it."""
    n, s, w, h = pix.n, pix.samples, pix.width, pix.height
    out = [[s[i * n], s[i * n + 1], s[i * n + 2]] for i in range(w * h)]
    marked = [i for i, v in enumerate(ink.data) if v > 0.12]
    for i in marked:
        x, y = i % w, i // w
        picks: list[list[int]] = []
        for radius in (2, 4, 7):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    xx, yy = x + dx, y + dy
                    if 0 <= xx < w and 0 <= yy < h:
                        j = yy * w + xx
                        if ink.data[j] <= 0.08:
                            picks.append(out[j])
            if len(picks) >= 8:
                break
        if picks:
            picks.sort(key=lambda p: p[0] + p[1] + p[2])
            out[i] = list(picks[len(picks) // 2])
    return out


DEFAULT_LOOK = (1.2, 1.0, 0.85)  # blur radius, weight gamma, ink strength


def _calibrate(page: fitz.Page, reference: tuple, dpi: int) -> tuple[float, float, float]:
    """How much to soften and thicken a rendering so it prints like this scan.

    A character that is staying on the page is drawn in the chosen font and compared
    with its own scanned self, so the same shape is on both sides of the comparison.
    Whatever it takes to make that one match is what the replacement gets.
    """
    ref_rect, ref_runs = reference
    pix, scanned = _read(page, ref_rect, dpi)
    if pix.width < 3 or pix.height < 3 or scanned.coverage < 0.02:
        return DEFAULT_LOOK
    fine = _render(page.rect, ref_rect, ref_runs, dpi * SUPERSAMPLE)
    if fine is None or fine.coverage < 0.002:
        return DEFAULT_LOOK
    want_soft, want_stroke, want_peak = scanned.softness(), scanned.stroke(), scanned.peak()

    best = None
    for radius in (0.0, 0.6, 1.2, 2.0, 3.0, 4.5, 6.0):
        soft = fine.blur(radius * SUPERSAMPLE / 2)
        gap = abs(soft.resample(pix.width, pix.height).softness() - want_soft)
        if best is None or gap < best[0]:
            best = (gap, radius, soft)
    _, radius, soft = best

    gamma = 1.0
    mask = soft.resample(pix.width, pix.height)
    if want_stroke > 0.2:
        for _ in range(6):
            got = mask.stroke()
            if got <= 0.2:
                break
            ratio = want_stroke / got
            if 0.95 < ratio < 1.05:
                break
            gamma = max(0.4, min(2.5, gamma * (1.0 / ratio) ** 0.7))
            mask = soft.gamma(gamma).resample(pix.width, pix.height)
    return radius, gamma, want_peak or DEFAULT_LOOK[2]


def stamp(page: fitz.Page, rect: fitz.Rect, runs: list, reference: tuple | None,
          ink_rgb: tuple) -> fitz.Pixmap | None:
    """Draw ``runs`` into ``rect`` as picture, matched to the scan around them.

    ``runs`` are (x, baseline, text, font, size) in page coordinates.  ``reference`` is
    (rect, runs) for a character of the same print that is staying put, which is what
    the look is calibrated against.  Returns a pixmap to put on the page, or None.
    """
    dpi = scan_dpi(page)
    rect = fitz.Rect(rect) & page.rect
    if rect.is_empty or rect.width < 0.5 or rect.height < 0.5:
        return None
    pix, here = _read(page, rect, dpi)
    if pix.width < 3 or pix.height < 3:
        return None
    fine = _render(page.rect, rect, runs, dpi * SUPERSAMPLE)
    if fine is None:
        return None
    radius, gamma, peak = _calibrate(page, reference, dpi) if reference else DEFAULT_LOOK
    mask = fine.blur(radius * SUPERSAMPLE / 2).gamma(gamma).resample(pix.width, pix.height)
    if mask.coverage < 0.002:  # nothing landed where it should have
        return None
    if mask.peak() > 0.01:
        mask = mask.scaled(peak / mask.peak())

    paper = _erase(pix, here)
    ink = [c * 255 for c in ink_rgb]
    w, h = pix.width, pix.height
    out = bytearray(w * h * 3)
    for i in range(w * h):
        a = mask.data[i] if i < len(mask.data) else 0.0
        bg = paper[i]
        for c in range(3):
            out[i * 3 + c] = int(max(0, min(255, bg[c] * (1 - a) + ink[c] * a)))
    return fitz.Pixmap(fitz.csRGB, w, h, bytes(out), 0)
