"""Generate sample PDFs that exercise the editor (run: python tests/make_samples.py)."""

import os
import struct
import sys

import pymupdf as fitz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdfeditor.fontutil import build_sfnt, parse_sfnt  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "samples")
SUP = "/System/Library/Fonts/Supplemental/"

LOREM = (
    "Quarterly revenue grew by twelve percent, driven by strong demand in the enterprise "
    "segment and improved retention across all regions. Operating expenses remained flat as "
    "the team completed the migration to the new billing platform. We expect continued "
    "momentum in the next quarter as three major customers finish their rollouts."
)


def font(name, fallback):
    path = os.path.join(SUP, name)
    return fitz.Font(fontfile=path) if os.path.exists(path) else fitz.Font(fallback)


def make_report(path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    arial, arial_b = font("Arial.ttf", "helv"), font("Arial Bold.ttf", "hebo")
    georgia_b = font("Georgia Bold.ttf", "tibo")

    page.draw_rect(fitz.Rect(0, 0, 595, 110), color=None, fill=(0.12, 0.23, 0.45))
    tw = fitz.TextWriter(page.rect)
    tw.append((50, 70), "Annual Business Report 2026", font=georgia_b, fontsize=26)
    tw.write_text(page, color=(1, 1, 1))

    tw = fitz.TextWriter(page.rect)
    tw.append((50, 150), "Prepared for: ", font=arial, fontsize=12)
    tw.append(tw.last_point, "Acme Corporation", font=arial_b, fontsize=12)
    tw.append(tw.last_point, " by the finance team", font=arial, fontsize=12)
    tw.write_text(page, color=(0.2, 0.2, 0.2))

    tw = fitz.TextWriter(page.rect)
    tw.append((50, 195), "1. Financial Summary", font=arial_b, fontsize=15)
    tw.write_text(page, color=(0.12, 0.23, 0.45))

    tw = fitz.TextWriter(page.rect)
    tw.fill_textbox(fitz.Rect(50, 205, 545, 330), LOREM, font=arial, fontsize=11, align=fitz.TEXT_ALIGN_JUSTIFY)
    tw.write_text(page, color=(0, 0, 0))

    # highlighted call-out box on a coloured background
    page.draw_rect(fitz.Rect(50, 340, 545, 400), color=(0.9, 0.7, 0.2), fill=(1, 0.96, 0.8), width=1)
    tw = fitz.TextWriter(page.rect)
    tw.append((65, 365), "Net profit: $4.2 million", font=arial_b, fontsize=14)
    tw.append((65, 386), "Up from $3.1 million last year", font=arial, fontsize=11)
    tw.write_text(page, color=(0.35, 0.25, 0.0))

    # bullet list
    tw = fitz.TextWriter(page.rect)
    y = 430
    for item in ["Launched two new products", "Opened offices in Berlin and Tokyo", "Hired 85 new employees"]:
        tw.append((60, y), "•", font=arial, fontsize=11)
        tw.append((75, y), item, font=arial, fontsize=11)
        y += 18
    tw.write_text(page, color=(0, 0, 0))

    # non-embedded base-14 Helvetica + Times (common in older PDFs)
    page.insert_text((50, 520), "Contact: finance@acme.example  |  Phone: +1 555 0100", fontname="helv", fontsize=10)
    page.insert_text((50, 540), "Confidential - for internal use only", fontname="tiro", fontsize=10, color=(0.5, 0, 0))

    # superscript footnote marker inside a line
    tw = fitz.TextWriter(page.rect)
    _, p = tw.append((50, 580), "Growth figures are audited", font=arial, fontsize=11)
    _, p = tw.append((p.x, p.y - 4), "1", font=arial, fontsize=7)
    tw.append((p.x, 580), " and final.", font=arial, fontsize=11)
    tw.write_text(page, color=(0, 0, 0))

    # simple two-column table row
    tw = fitz.TextWriter(page.rect)
    for i, (k, v) in enumerate([("Region", "Revenue"), ("North America", "$12.4M"), ("Europe", "$8.9M")]):
        f = arial_b if i == 0 else arial
        tw.append((60, 630 + i * 20), k, font=f, fontsize=11)
        tw.append((360, 630 + i * 20), v, font=f, fontsize=11)
    tw.write_text(page, color=(0, 0, 0))
    for i in range(4):
        page.draw_line((50, 616 + i * 20), (545, 616 + i * 20), color=(0.7, 0.7, 0.7), width=0.5)

    # page 2: form fields + rotated page content
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((50, 80), "Application Form", fontname="hebo", fontsize=20)
    labels = ["Full name", "Email", "Country"]
    for i, lab in enumerate(labels):
        p2.insert_text((50, 130 + i * 40), lab, fontname="helv", fontsize=11)
        w = fitz.Widget()
        w.field_name = lab.lower().replace(" ", "_")
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.rect = fitz.Rect(150, 117 + i * 40, 400, 135 + i * 40)
        w.text_fontsize = 11
        p2.add_widget(w)
    cb = fitz.Widget()
    cb.field_name = "subscribe"
    cb.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
    cb.rect = fitz.Rect(150, 237, 164, 251)
    p2.add_widget(cb)
    p2.insert_text((170, 248), "Subscribe to newsletter", fontname="helv", fontsize=11)

    p3 = doc.new_page(width=842, height=595)
    p3.insert_text((60, 80), "This landscape page is stored rotated", fontname="helv", fontsize=18)
    p3.add_highlight_annot(fitz.Rect(60, 62, 300, 86))
    p3.set_rotation(90)

    doc.subset_fonts()
    doc.save(path, garbage=3, deflate=True)


def restrict_embedding(buffer):
    """Return the same font with its OS/2 licence bits set to "no embedding"."""
    version, tables = parse_sfnt(buffer)
    os2 = bytearray(tables[b"OS/2"])
    struct.pack_into(">H", os2, 8, (struct.unpack_from(">H", os2, 8)[0] & ~0xF) | 0x0002)
    tables[b"OS/2"] = bytes(os2)
    return build_sfnt(version, tables)


def make_restricted(path):
    """A form whose font forbids re-embedding, like tax and insurance software produces.

    MuPDF refuses to write such a font into a new PDF font resource, so editing text
    in these documents has to reuse the font the page already carries.
    """
    doc = fitz.open()
    page = doc.new_page(width=612, height=300)
    arial, arial_b = font("Arial.ttf", "helv"), font("Arial Bold.ttf", "hebo")
    tw = fitz.TextWriter(page.rect)
    tw.append((50, 60), "TAXPAYER ANNUAL EARNINGS STATEMENT", font=arial_b, fontsize=13)
    tw.append((50, 100), "Employee name", font=arial_b, fontsize=9)
    tw.append((50, 118), "Jordan Fields", font=arial, fontsize=9)
    tw.append((50, 150), "Gross compensation reported for the year", font=arial, fontsize=9)
    tw.append((400, 150), "126,676.00", font=arial, fontsize=9)
    tw.append((50, 175), "Total tax withheld by the employer", font=arial, fontsize=9)
    tw.append((400, 175), "5,594.00", font=arial, fontsize=9)
    tw.write_text(page, color=(0.05, 0.05, 0.1))
    doc.save(path, garbage=3, deflate=True)

    # swap every embedded font file for a copy that forbids embedding
    doc = fitz.open(path)
    for xref in range(1, doc.xref_length()):
        if not doc.xref_is_stream(xref) or doc.xref_get_key(xref, "Length1")[0] == "null":
            continue
        data = restrict_embedding(doc.xref_stream(xref))
        doc.update_stream(xref, data)
        doc.xref_set_key(xref, "Length1", str(len(data)))
    doc.save(path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)


def make_unreadable(path):
    """Text whose characters are not in the file: Identity-H glyphs with no /ToUnicode.

    This is what macOS Preview's OCR produces - the words are on the page but every
    character extracts as U+FFFD until the editor recovers them from the font.
    """
    doc = fitz.open()
    page = doc.new_page(width=460, height=220)
    arial = font("Arial.ttf", "helv")
    tw = fitz.TextWriter(page.rect)
    for i, text in enumerate(("Gross pay for the period", "Total hours worked: 86.50",
                              "Net deposit $2,481.37 (no. 4471)")):
        tw.append((40, 60 + i * 34), text, font=arial, fontsize=12)
    tw.write_text(page, color=(0.1, 0.1, 0.12))
    doc.save(path, garbage=3, deflate=True)

    doc = fitz.open(path)  # drop the character map, keeping the glyphs
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "ToUnicode")[0] != "null":
            doc.xref_set_key(xref, "ToUnicode", "null")
    doc.save(path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)


def make_spotscan(path):
    """A scanned form whose text layer positions every character, as macOS Preview's OCR does.

    Tesseract spreads a word's width evenly over its letters, but Preview records where
    each one really is, which is what lets the editor redraw a single character.
    """
    doc = fitz.open()
    page = doc.new_page(width=340, height=120)
    typed = font("Courier New.ttf", "cour")
    label = font("Arial Bold.ttf", "hebo")
    green = (0.16, 0.42, 0.34)
    page.draw_rect(fitz.Rect(20, 24, 320, 86), color=green, width=0.9)
    page.draw_line(fitz.Point(170, 24), fitz.Point(170, 86), color=green, width=0.9)
    page.draw_line(fitz.Point(20, 48), fitz.Point(320, 48), color=green, width=0.9)
    tw = fitz.TextWriter(page.rect)
    tw.append((30, 42), "START PERIOD", font=label, fontsize=9)
    tw.append((180, 42), "END PERIOD", font=label, fontsize=9)
    tw.append((30, 74), "7/10/2022", font=typed, fontsize=15)
    tw.append((180, 74), "7/16/2022", font=typed, fontsize=15)
    tw.write_text(page, color=green)

    chars = [(ch["c"], ch["origin"], span["size"])
             for b in page.get_text("rawdict")["blocks"] for line in b.get("lines", [])
             for span in line["spans"] for ch in span["chars"] if not ch["c"].isspace()]
    pix = page.get_pixmap(dpi=200)  # the "scan"

    out = fitz.open()
    sp = out.new_page(width=page.rect.width, height=page.rect.height)
    sp.insert_image(sp.rect, pixmap=pix)
    arial = font("Arial.ttf", "helv")  # the OCR layer's own font, as Preview writes it
    tw = fitz.TextWriter(sp.rect)
    for ch, origin, size in chars:
        tw.append(origin, ch, font=arial, fontsize=size)
    tw.write_text(sp, render_mode=3)  # invisible, over the picture
    out.save(path, garbage=3, deflate=True)


def make_scanned(src, path):
    """Rasterise page 1 and OCR it, producing an image PDF with an invisible text layer."""
    src_doc = fitz.open(src)
    pix = src_doc[0].get_pixmap(dpi=150)
    try:
        ocr_pdf = pix.pdfocr_tobytes(language="eng")
    except Exception as exc:  # tesseract missing
        print("skipping scanned sample:", exc)
        return
    with open(path, "wb") as f:
        f.write(ocr_pdf)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    report = os.path.join(OUT, "report.pdf")
    make_report(report)
    make_scanned(report, os.path.join(OUT, "scanned.pdf"))
    make_restricted(os.path.join(OUT, "restricted.pdf"))
    make_unreadable(os.path.join(OUT, "unreadable.pdf"))
    make_spotscan(os.path.join(OUT, "spotscan.pdf"))
    print("wrote samples to", OUT)
    sys.exit(0)
