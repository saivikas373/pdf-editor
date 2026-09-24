"""Burn overlay objects (added text, images, shapes, annotations...) into PDF pages.

All coordinates are PDF points in the page's display space (pages are
rotation-normalised when opened, so this equals ``page.rect``).
"""

from __future__ import annotations

import pymupdf as fitz

from .fonts import SYSTEM_FONTS, base14, squash
from .textedit import FontBook, FontMemory, Run, Writer, WriteStyle, parse_hex

CONTENT_TYPES = {"whiteout", "rect", "ellipse", "line", "arrow", "ink", "image", "text", "symbol"}
ANNOT_TYPES = {"markup", "note"}


def _rgb(value, default=None):
    parsed = parse_hex(value) if isinstance(value, str) else None
    return parsed if parsed is not None else default


def _num(obj: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(obj.get(key, default))
    except (TypeError, ValueError):
        return default


def _rect(obj: dict) -> fitz.Rect:
    x, y = _num(obj, "x"), _num(obj, "y")
    return fitz.Rect(x, y, x + max(0.0, _num(obj, "w")), y + max(0.0, _num(obj, "h")))


def generic_for_family(family: str) -> str:
    key = squash(family or "")
    if any(k in key for k in ("courier", "mono", "menlo", "consol")):
        return "mono"
    if any(k in key for k in ("times", "georgia", "garamond", "palatino", "baskerville", "didot", "serif", "cambria", "book")):
        return "serif"
    return "sans"


def text_font(family: str, bold: bool, italic: bool) -> tuple[fitz.Font, bool, bool]:
    found = SYSTEM_FONTS.font_for(family or "Helvetica", bold, italic)
    if found:
        return found
    return base14(generic_for_family(family), bold, italic), False, False


def wrap_text(text: str, width: float, measure) -> list[str]:
    """Greedy wrap similar to CSS ``white-space: pre-wrap; overflow-wrap: anywhere``."""
    lines: list[str] = []
    for para in (text or "").split("\n"):
        words = para.split(" ")
        current = ""
        for word in words:
            candidate = word if current == "" else current + " " + word
            if measure(candidate) <= width + 0.01 or current == "":
                if measure(candidate) > width + 0.01 and current == "":
                    # break an over-long word
                    piece = ""
                    for ch in word:
                        if piece and measure(piece + ch) > width:
                            lines.append(piece)
                            piece = ""
                        piece += ch
                    current = piece
                else:
                    current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def bake_text(page: fitz.Page, obj: dict, book: FontBook) -> None:
    rect = _rect(obj)
    opacity = max(0.0, min(1.0, _num(obj, "opacity", 1.0)))
    bg = _rgb(obj.get("bg"))
    if bg is not None and not rect.is_empty:
        page.draw_rect(rect, color=None, fill=bg, width=0, fill_opacity=opacity)
    size = max(1.0, _num(obj, "size", 14))
    family = obj.get("font") or "Helvetica"
    bold, italic = bool(obj.get("bold")), bool(obj.get("italic"))
    color = _rgb(obj.get("color"), (0, 0, 0))
    ws = WriteStyle(None, family, bold, italic, size, color, opacity, generic_for_family(family))
    pad = _num(obj, "padding", 2)
    line_height = size * max(0.5, _num(obj, "lineHeight", 1.25))
    layout = obj.get("layout") or {}
    lines = layout.get("lines")
    if not lines:
        font, _, _ = text_font(family, bold, italic)
        width = max(1.0, rect.width - 2 * pad)
        wrapped = wrap_text(obj.get("text", ""), width, lambda s: book.width(ws, s))
        asc, desc = font.ascender, -font.descender
        first = pad + (line_height - (asc + desc) * size) / 2 + asc * size
        align = obj.get("align", "left")
        lines = []
        for i, t in enumerate(wrapped):
            lw = book.width(ws, t.rstrip(" "))
            offset = pad
            if align == "center":
                offset = pad + (width - lw) / 2
            elif align == "right":
                offset = pad + width - lw
            lines.append({"text": t, "x": offset, "baseline": first + i * line_height})
    writer = Writer(page, book)
    for line in lines:
        text = str(line.get("text", "")).rstrip("\n")
        if not text.strip():
            continue
        x = rect.x0 + float(line.get("x", pad))
        y = rect.y0 + float(line.get("baseline", size))
        writer.add(Run(x, y, text, ws))
        deco = []
        if obj.get("underline"):
            deco.append(y + size * 0.12)
        if obj.get("strike"):
            deco.append(y - size * 0.28)
        if deco:
            lead = len(text) - len(text.lstrip(" "))
            start = x + (book.width(ws, text[:lead]) if lead else 0)
            end = x + book.width(ws, text.rstrip(" "))
            for dy in deco:
                page.draw_line((start, dy), (end, dy), color=color, width=max(0.5, size * 0.06), stroke_opacity=opacity)
    writer.commit()


def _image_bytes_with_opacity(data: bytes, opacity: float) -> fitz.Pixmap | None:
    try:
        pix = fitz.Pixmap(data)
    except Exception:
        return None
    if pix.colorspace and pix.colorspace.n not in (1, 3):
        pix = fitz.Pixmap(fitz.csRGB, pix)
    if opacity >= 0.999:
        return pix
    if not pix.alpha:
        pix = fitz.Pixmap(pix, 1)
    alpha = bytearray(pix.samples[pix.n - 1::pix.n])
    for i, v in enumerate(alpha):
        alpha[i] = int(v * opacity)
    pix.set_alpha(bytes(alpha))
    return pix


def bake_image(page: fitz.Page, obj: dict, assets: dict) -> None:
    asset = assets.get(obj.get("asset"))
    rect = _rect(obj)
    if not asset or rect.is_empty:
        return
    opacity = max(0.0, min(1.0, _num(obj, "opacity", 1.0)))
    if opacity >= 0.999:
        page.insert_image(rect, stream=asset["data"], keep_proportion=False, overlay=True)
    else:
        pix = _image_bytes_with_opacity(asset["data"], opacity)
        if pix is not None:
            page.insert_image(rect, pixmap=pix, keep_proportion=False, overlay=True)


def _smooth_path(shape: fitz.Shape, pts: list[tuple[float, float]]) -> None:
    if len(pts) == 1:
        x, y = pts[0]
        shape.draw_line((x, y), (x + 0.01, y + 0.01))
        return
    if len(pts) == 2:
        shape.draw_line(pts[0], pts[1])
        return
    cur = fitz.Point(pts[0])
    for i in range(1, len(pts) - 1):
        ctrl = fitz.Point(pts[i])
        mid = (ctrl + fitz.Point(pts[i + 1])) * 0.5
        c1 = cur + (ctrl - cur) * (2 / 3)
        c2 = mid + (ctrl - mid) * (2 / 3)
        shape.draw_bezier(cur, c1, c2, mid)
        cur = mid
    shape.draw_line(cur, pts[-1])


def bake_shape(page: fitz.Page, obj: dict) -> None:
    kind = obj.get("type")
    opacity = max(0.0, min(1.0, _num(obj, "opacity", 1.0)))
    stroke = _rgb(obj.get("stroke"))
    fill = _rgb(obj.get("fill"))
    sw = max(0.0, _num(obj, "strokeWidth", 2))
    if kind in ("rect", "ellipse", "whiteout"):
        rect = _rect(obj)
        if rect.is_empty:
            return
        if kind == "whiteout":
            page.draw_rect(rect, color=None, fill=_rgb(obj.get("fill"), (1, 1, 1)), width=0)
            return
        if stroke is None or sw <= 0:
            stroke, sw = None, 0
        inner = rect + (sw / 2, sw / 2, -sw / 2, -sw / 2) if sw and rect.width > sw and rect.height > sw else rect
        draw = page.draw_oval if kind == "ellipse" else page.draw_rect
        kwargs = dict(color=stroke, fill=fill, width=sw, stroke_opacity=opacity, fill_opacity=opacity)
        if kind == "rect" and _num(obj, "radius") > 0:
            kwargs["radius"] = min(0.5, _num(obj, "radius") / max(1.0, min(rect.width, rect.height)))
        draw(inner, **kwargs)
    elif kind in ("line", "arrow"):
        p1 = fitz.Point(_num(obj, "x1"), _num(obj, "y1"))
        p2 = fitz.Point(_num(obj, "x2"), _num(obj, "y2"))
        color = stroke or (0, 0, 0)
        sw = sw or 2
        end = p2
        if kind == "arrow" and abs(p2 - p1) > 0.1:
            direction = (p2 - p1) / abs(p2 - p1)
            head = max(8.0, sw * 4.0)
            base = p2 - direction * head
            perp = fitz.Point(-direction.y, direction.x) * (head * 0.5)
            page.draw_polyline([p2, base + perp, base - perp], color=color, fill=color, width=0.5, closePath=True,
                               stroke_opacity=opacity, fill_opacity=opacity)
            end = p2 - direction * (head * 0.8)
        page.draw_line(p1, end, color=color, width=sw, lineCap=1, stroke_opacity=opacity)
    elif kind == "ink":
        color = stroke or (0, 0, 0)
        shape = page.new_shape()
        for path in obj.get("paths", []):
            pts = [(float(p[0]), float(p[1])) for p in path if len(p) >= 2]
            if pts:
                _smooth_path(shape, pts)
        shape.finish(color=color, width=sw or 2, lineCap=1, lineJoin=1, closePath=False, stroke_opacity=opacity)
        shape.commit()
    elif kind == "symbol":
        rect = _rect(obj)
        if rect.is_empty:
            return
        color = _rgb(obj.get("color"), (0, 0, 0))
        w, h = rect.width, rect.height
        lw = max(1.0, min(w, h) * 0.12)
        sym = obj.get("kind", "check")
        pt = lambda fx, fy: fitz.Point(rect.x0 + fx * w, rect.y0 + fy * h)  # noqa: E731
        if sym == "check":
            page.draw_polyline([pt(0.12, 0.55), pt(0.4, 0.82), pt(0.9, 0.18)], color=color, width=lw, lineCap=1, lineJoin=1,
                               stroke_opacity=opacity)
        elif sym == "cross":
            page.draw_line(pt(0.18, 0.18), pt(0.82, 0.82), color=color, width=lw, lineCap=1, stroke_opacity=opacity)
            page.draw_line(pt(0.82, 0.18), pt(0.18, 0.82), color=color, width=lw, lineCap=1, stroke_opacity=opacity)
        elif sym == "dot":
            page.draw_oval(fitz.Rect(pt(0.25, 0.25), pt(0.75, 0.75)), color=None, fill=color, width=0, fill_opacity=opacity)


def bake_annotation(page: fitz.Page, obj: dict) -> None:
    kind = obj.get("type")
    opacity = max(0.05, min(1.0, _num(obj, "opacity", 1.0)))
    if kind == "markup":
        rects = [fitz.Rect(r) for r in obj.get("rects", []) if len(r) == 4]
        rects = [r for r in rects if not r.is_empty]
        if not rects:
            return
        quads = [r.quad for r in rects]
        style = obj.get("kind", "highlight")
        add = {
            "highlight": page.add_highlight_annot,
            "underline": page.add_underline_annot,
            "strike": page.add_strikeout_annot,
            "squiggly": page.add_squiggly_annot,
        }.get(style, page.add_highlight_annot)
        annot = add(quads)
        default = (1, 0.85, 0.2) if style == "highlight" else (0.85, 0.1, 0.1)
        annot.set_colors(stroke=_rgb(obj.get("color"), default))
        annot.set_opacity(opacity)
        annot.update()
    elif kind == "note":
        point = fitz.Point(_num(obj, "x"), _num(obj, "y"))
        annot = page.add_text_annot(point, obj.get("text", "") or " ", icon="Comment")
        annot.set_colors(stroke=_rgb(obj.get("color"), (1, 0.8, 0.1)))
        if obj.get("author"):
            annot.set_info(title=str(obj["author"]))
        annot.update()


def bake_page(doc: fitz.Document, pno: int, objects: list[dict], assets: dict, memory: FontMemory | None = None) -> None:
    """Burn all overlay objects of one page into the document."""
    if not objects:
        return
    page = doc[pno]
    redactions = [o for o in objects if o.get("type") == "redact"]
    if redactions:
        for obj in redactions:
            rect = _rect(obj)
            if not rect.is_empty:
                page.add_redact_annot(rect, fill=_rgb(obj.get("fill"), (0, 0, 0)), cross_out=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS, graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
        page = doc[pno]
    book = FontBook(doc, page, None, memory or FontMemory())
    for obj in objects:
        kind = obj.get("type")
        if kind == "text":
            bake_text(page, obj, book)
        elif kind == "image":
            bake_image(page, obj, assets)
        elif kind in CONTENT_TYPES:
            bake_shape(page, obj)
    for obj in objects:
        if obj.get("type") in ANNOT_TYPES:
            bake_annotation(page, obj)


def object_bounds(obj: dict) -> fitz.Rect:
    kind = obj.get("type")
    if kind in ("line", "arrow"):
        return fitz.Rect(min(_num(obj, "x1"), _num(obj, "x2")), min(_num(obj, "y1"), _num(obj, "y2")),
                         max(_num(obj, "x1"), _num(obj, "x2")), max(_num(obj, "y1"), _num(obj, "y2")))
    if kind == "ink":
        pts = [p for path in obj.get("paths", []) for p in path]
        if not pts:
            return fitz.Rect()
        return fitz.Rect(min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    if kind == "markup":
        rects = [fitz.Rect(r) for r in obj.get("rects", [])]
        out = fitz.Rect()
        for r in rects:
            out |= r
        return out
    if kind == "note":
        return fitz.Rect(_num(obj, "x"), _num(obj, "y"), _num(obj, "x") + 20, _num(obj, "y") + 20)
    return _rect(obj)


def transform_object(obj: dict, matrix: fitz.Matrix) -> dict:
    """Return a copy of ``obj`` with its geometry transformed (used when pages are rotated/cropped)."""
    out = dict(obj)
    kind = obj.get("type")

    def tp(x, y):
        p = fitz.Point(x, y) * matrix
        return p.x, p.y

    if kind in ("line", "arrow"):
        out["x1"], out["y1"] = tp(_num(obj, "x1"), _num(obj, "y1"))
        out["x2"], out["y2"] = tp(_num(obj, "x2"), _num(obj, "y2"))
    elif kind == "ink":
        out["paths"] = [[list(tp(p[0], p[1])) for p in path] for path in obj.get("paths", [])]
    elif kind == "markup":
        out["rects"] = [list(fitz.Rect(r) * matrix) for r in obj.get("rects", [])]
    elif kind == "note":
        out["x"], out["y"] = tp(_num(obj, "x"), _num(obj, "y"))
    else:
        r = _rect(obj) * matrix
        out.update(x=r.x0, y=r.y0, w=r.width, h=r.height)
    return out
