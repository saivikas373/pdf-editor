"""Whole-document tools: watermark, page numbers, OCR, split, export."""

from __future__ import annotations

import datetime
import io
import os
import re
import shutil
import subprocess
import zipfile

import pymupdf as fitz

from .objects import text_font
from .textedit import EditError, parse_hex


def parse_page_spec(spec: str | None, count: int) -> list[int]:
    """'1-3, 5, 8-' -> zero-based page indices. Empty / 'all' -> every page."""
    if spec is None or str(spec).strip().lower() in ("", "all"):
        return list(range(count))
    pages: list[int] = []
    for part in re.split(r"[,;\s]+", str(spec).strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)", part)
        if m:
            start = int(m.group(1)) if m.group(1) else 1
            end = int(m.group(2)) if m.group(2) else count
        elif part.isdigit():
            start = end = int(part)
        elif part.lower() == "odd":
            pages.extend(i for i in range(count) if i % 2 == 0)
            continue
        elif part.lower() == "even":
            pages.extend(i for i in range(count) if i % 2 == 1)
            continue
        else:
            raise EditError(f"Invalid page range: {part!r}")
        if start < 1 or end > count or start > end:
            raise EditError(f"Page range {part!r} is outside 1-{count}.")
        pages.extend(range(start - 1, end))
    seen: set[int] = set()
    return [p for p in pages if not (p in seen or seen.add(p))]


def _color(value, default=(0, 0, 0)):
    parsed = parse_hex(value) if isinstance(value, str) else None
    return parsed if parsed is not None else default


def add_watermark(doc: fitz.Document, opts: dict, pages: list[int]) -> None:
    text = str(opts.get("text") or "").strip()
    if not text:
        raise EditError("Enter watermark text.")
    size = float(opts.get("size") or 60)
    color = _color(opts.get("color"), (0.8, 0.1, 0.1))
    opacity = max(0.02, min(1.0, float(opts.get("opacity") or 0.25)))
    angle = float(opts.get("angle") if opts.get("angle") is not None else 45)
    font, _, _ = text_font(opts.get("font") or "Helvetica", bool(opts.get("bold", True)), False)
    tile = opts.get("mode") == "tile"
    overlay = opts.get("layer", "over") != "under"
    text_w = font.text_length(text, fontsize=size)
    for pno in pages:
        page = doc[pno]
        r = page.rect
        center = fitz.Point(r.x0 + r.width / 2, r.y0 + r.height / 2)
        tw = fitz.TextWriter(page.rect)
        if tile:
            step_x = text_w + size * 2.5
            step_y = size * 4
            span = (r.width ** 2 + r.height ** 2) ** 0.5
            rows = int(span / step_y) + 2
            cols = int(span / step_x) + 2
            for row in range(-rows // 2, rows // 2 + 1):
                offset = (step_x / 2) if row % 2 else 0
                for col in range(-cols // 2, cols // 2 + 1):
                    x = center.x + col * step_x + offset - text_w / 2
                    y = center.y + row * step_y + size * 0.35
                    tw.append((x, y), text, font=font, fontsize=size)
        else:
            tw.append((center.x - text_w / 2, center.y + size * 0.35), text, font=font, fontsize=size)
        tw.write_text(page, color=color, opacity=opacity, overlay=overlay, morph=(center, fitz.Matrix(-angle)))


def add_text_stamp(doc: fitz.Document, opts: dict, pages: list[int], filename: str = "") -> None:
    """Page numbers / headers / footers from a template like 'Page {n} of {total}'."""
    template = str(opts.get("format") or "{n}")
    start = int(opts.get("start") or 1)
    size = float(opts.get("size") or 10)
    color = _color(opts.get("color"), (0.2, 0.2, 0.2))
    margin = float(opts.get("margin") if opts.get("margin") is not None else 28)
    position = str(opts.get("position") or "bottom-center")
    font, _, _ = text_font(opts.get("font") or "Helvetica", bool(opts.get("bold")), False)
    total = start + len(pages) - 1
    today = datetime.date.today().strftime("%b %d, %Y")
    vert, _, horiz = position.partition("-")
    for i, pno in enumerate(pages):
        page = doc[pno]
        r = page.rect
        text = (template.replace("{n}", str(start + i)).replace("{total}", str(total))
                .replace("{date}", today).replace("{filename}", filename))
        width = font.text_length(text, fontsize=size)
        if horiz == "left":
            x = r.x0 + margin
        elif horiz == "right":
            x = r.x1 - margin - width
        else:
            x = r.x0 + (r.width - width) / 2
        y = r.y0 + margin + size * 0.75 if vert == "top" else r.y1 - margin
        tw = fitz.TextWriter(page.rect)
        tw.append((x, y), text, font=font, fontsize=size)
        tw.write_text(page, color=color)


def tesseract_path() -> str | None:
    return shutil.which("tesseract") or next(
        (p for p in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/usr/bin/tesseract") if os.path.exists(p)), None)


def ensure_tessdata() -> str | None:
    if os.environ.get("TESSDATA_PREFIX") and os.path.isdir(os.environ["TESSDATA_PREFIX"]):
        return os.environ["TESSDATA_PREFIX"]
    exe = tesseract_path()
    if not exe:
        return None
    candidates = []
    real = os.path.realpath(exe)
    prefix = os.path.dirname(os.path.dirname(real))
    candidates += [os.path.join(prefix, "share", "tessdata"), os.path.join(prefix, "share", "tesseract-ocr", "5", "tessdata"),
                   "/opt/homebrew/share/tessdata", "/usr/local/share/tessdata", "/usr/share/tesseract-ocr/5/tessdata",
                   "/usr/share/tesseract-ocr/4.00/tessdata"]
    for c in candidates:
        if os.path.isdir(c):
            os.environ["TESSDATA_PREFIX"] = c
            return c
    return None


def ocr_languages() -> list[str]:
    exe = tesseract_path()
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    return [l.strip() for l in out.splitlines()[1:] if l.strip() and l.strip() != "osd"]


def ocr_pages(doc: fitz.Document, pages: list[int], language: str = "eng", dpi: int = 300, skip_text: bool = True) -> dict:
    if not ensure_tessdata():
        raise EditError("OCR needs Tesseract. Install it with: brew install tesseract")
    done, skipped = [], []
    for pno in pages:
        page = doc[pno]
        if skip_text and len(page.get_text().strip()) > 50:
            skipped.append(pno)
            continue
        pix = page.get_pixmap(dpi=dpi)
        try:
            ocr = fitz.open("pdf", pix.pdfocr_tobytes(language=language))
        except Exception as exc:
            raise EditError(f"OCR failed: {exc}") from exc
        opage = ocr[0]
        # keep only the invisible text layer
        opage.add_redact_annot(opage.rect)
        opage.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                               text=fitz.PDF_REDACT_TEXT_NONE)
        page.show_pdf_page(page.rect, ocr, 0, overlay=True)
        done.append(pno)
    return {"ocr": done, "skipped": skipped}


def _single_doc(doc: fitz.Document, pages: list[int]) -> bytes:
    out = fitz.open()
    for p in pages:
        out.insert_pdf(doc, from_page=p, to_page=p)
    try:
        out.subset_fonts()
    except Exception:
        pass
    return out.tobytes(garbage=4, deflate=True)


def split_document(doc: fitz.Document, mode: str, value: str, base: str) -> list[tuple[str, bytes]]:
    count = doc.page_count
    groups: list[list[int]] = []
    if mode == "each":
        groups = [[i] for i in range(count)]
    elif mode == "every":
        n = max(1, int(value or 1))
        groups = [list(range(i, min(i + n, count))) for i in range(0, count, n)]
    elif mode == "ranges":
        for part in str(value or "").split(","):
            if part.strip():
                groups.append(parse_page_spec(part, count))
    else:
        raise EditError("Unknown split mode.")
    if not groups:
        raise EditError("Nothing to split.")
    files = []
    for g in groups:
        label = f"{g[0] + 1}" if len(g) == 1 else f"{g[0] + 1}-{g[-1] + 1}"
        files.append((f"{base} (pages {label}).pdf", _single_doc(doc, g)))
    return files


def extract_pages(doc: fitz.Document, pages: list[int]) -> bytes:
    if not pages:
        raise EditError("Select at least one page.")
    return _single_doc(doc, pages)


def export_images(doc: fitz.Document, pages: list[int], fmt: str, dpi: int, base: str) -> list[tuple[str, bytes]]:
    fmt = "jpg" if fmt in ("jpg", "jpeg") else "png"
    dpi = max(36, min(600, int(dpi or 150)))
    files = []
    for pno in pages:
        pix = doc[pno].get_pixmap(dpi=dpi, alpha=False)
        data = pix.tobytes("jpeg", jpg_quality=90) if fmt == "jpg" else pix.tobytes("png")
        files.append((f"{base} - page {pno + 1}.{fmt}", data))
    return files


def extract_embedded_images(doc: fitz.Document, base: str) -> list[tuple[str, bytes]]:
    files, seen = [], set()
    for pno in range(doc.page_count):
        for img in doc[pno].get_images(full=True):
            xref = img[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                info = doc.extract_image(xref)
            except Exception:
                continue
            if not info or not info.get("image"):
                continue
            if info.get("width", 0) < 16 or info.get("height", 0) < 16:
                continue
            files.append((f"{base} - p{pno + 1} image {len(files) + 1}.{info.get('ext', 'png')}", info["image"]))
    return files


def extract_text(doc: fitz.Document) -> str:
    parts = []
    for pno in range(doc.page_count):
        parts.append(f"--- Page {pno + 1} ---\n" + doc[pno].get_text(sort=True).replace("\xa0", " "))
    return "\n".join(parts)


def zip_files(files: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            unique, i = name, 2
            while unique in names:
                stem, dot, ext = name.rpartition(".")
                unique = f"{stem} ({i}).{ext}" if dot else f"{name} ({i})"
                i += 1
            names.add(unique)
            zf.writestr(unique, data)
    return buf.getvalue()


def image_page_size(data: bytes) -> tuple[float, float]:
    pix = fitz.Pixmap(data)
    dpi_x = pix.xres if pix.xres and pix.xres > 1 else 96
    dpi_y = pix.yres if pix.yres and pix.yres > 1 else 96
    return pix.width * 72 / dpi_x, pix.height * 72 / dpi_y


def insert_image_pages(doc: fitz.Document, images: list[bytes], at: int, page_size: str = "fit") -> int:
    sizes = {"a4": (595.28, 841.89), "letter": (612, 792)}
    inserted = 0
    for data in images:
        try:
            w, h = image_page_size(data)
        except Exception as exc:
            raise EditError("One of the files is not a supported image.") from exc
        if page_size in sizes:
            pw, ph = sizes[page_size]
            if w > h:
                pw, ph = ph, pw
            page = doc.new_page(at + inserted, width=pw, height=ph)
            margin = 36
            page.insert_image(fitz.Rect(margin, margin, pw - margin, ph - margin), stream=data, keep_proportion=True)
        else:
            scale = min(1.0, 1440 / max(w, h))  # cap giant scans at 20 inches
            page = doc.new_page(at + inserted, width=w * scale, height=h * scale)
            page.insert_image(page.rect, stream=data, keep_proportion=True)
        inserted += 1
    return inserted
