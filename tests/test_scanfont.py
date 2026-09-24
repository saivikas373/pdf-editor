"""Font recognition on synthetic scans.  Run: python tests/test_scanfont.py"""

import os
import sys
import time

import pymupdf as fitz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pdfeditor.fonts import SYSTEM_FONTS  # noqa: E402
from pdfeditor.scanfont import match_words  # noqa: E402
from pdfeditor.textedit import PageText  # noqa: E402
from pdfeditor.tools import ensure_tessdata  # noqa: E402

# (family, bold, size, text)
LINES = [
    ("Arial", False, 11, "Account statement for the period ending June 30"),
    ("Arial", True, 15, "Portfolio Summary and Holdings"),
    ("Times New Roman", False, 12, "Deposits and withdrawals are listed below"),
    ("Georgia", False, 11, "Transaction history includes every trade"),
    ("Courier New", False, 10, "Reference number 8f2a91 confirmed today"),
    ("Verdana", False, 9, "Total value locked across all protocols"),
    ("Tahoma", False, 10, "Staking rewards are distributed monthly"),
    ("Palatino", False, 12, "Important information about your account"),
    ("Trebuchet MS", False, 11, "Questions? Contact our support team"),
    ("Times New Roman", True, 13, "Annual Summary of Gains and Losses"),
    ("Helvetica", False, 8, "Values are shown in United States dollars"),
]

# families that are visually the same design (a match to either counts)
EQUIVALENT = [{"Arial", "Helvetica", "Helvetica Neue", "Liberation Sans"}, {"Times New Roman", "Times", "Liberation Serif"},
              {"Courier New", "Courier"}]


def same_family(a, b):
    return a == b or any(a in group and b in group for group in EQUIVALENT)


def make_scan(dpi_render=150, dpi_scan=300, quality=60):
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    y = 60
    positions = []
    for family, bold, size, text in LINES:
        font, _, _ = SYSTEM_FONTS.font_for(family, bold, False)
        tw = fitz.TextWriter(page.rect)
        tw.append((50, y), text, font=font, fontsize=size)
        tw.write_text(page, color=(0.1, 0.1, 0.12))
        positions.append(y)
        y += size * 2.6 + 10
    # blur by rendering low and scaling up, tint the paper, JPEG compress
    low = page.get_pixmap(dpi=dpi_render)
    high = fitz.Pixmap(low, low.width * dpi_scan / dpi_render, low.height * dpi_scan / dpi_render)
    tinted = fitz.Pixmap(fitz.csRGB, high.width, high.height, bytes(max(0, v - 14) for v in high.samples), 0)
    jpeg = tinted.tobytes("jpeg", jpg_quality=quality)
    scan = fitz.open()
    sp = scan.new_page(width=612, height=792)
    sp.insert_image(sp.rect, stream=jpeg)
    ensure_tessdata()
    ocr = fitz.open("pdf", sp.get_pixmap(dpi=dpi_scan).pdfocr_tobytes(language="eng"))
    return ocr, positions


VARIANTS = [
    ("clean scan (300 dpi)", dict(dpi_render=150, dpi_scan=300, quality=60)),
    ("low-res scan (200 dpi, JPEG q40)", dict(dpi_render=110, dpi_scan=200, quality=40)),
    ("blurry scan (heavy JPEG q25)", dict(dpi_render=90, dpi_scan=300, quality=25)),
]


def line_words(line):
    words, cur, rect = [], "", None
    for ch in line.chars + [None]:
        if ch is None or ch.c.isspace():
            if cur:
                words.append((cur, rect))
            cur, rect = "", None
            continue
        r = fitz.Rect(ch.x0, ch.y0, ch.x1, ch.y1)
        cur += ch.c
        rect = r if rect is None else rect | r
    return words


def run_variant(label, kw, verbose):
    ocr, positions = make_scan(**kw)
    page = ocr[0]
    pt = PageText(page)
    good_font = good_all = 0
    times = []
    for (family, bold, size, text), y in zip(LINES, positions):
        line = min(pt.lines, key=lambda l: abs(l.baseline - y))
        t = time.time()
        m = match_words(page, line_words(line))
        times.append(time.time() - t)
        font_ok = m is not None and same_family(m.family, family) and m.bold == bold
        size_ok = m is not None and abs(m.size - size) <= max(0.8, size * 0.1)
        base_ok = m is not None and abs(m.baseline - y) <= max(1.0, size * 0.12)
        good_font += font_ok
        good_all += font_ok and size_ok and base_ok
        if verbose or not (font_ok and size_ok and base_ok):
            got = f"{m.family}{' Bold' if m.bold else ''} {m.size:.1f}pt base={m.baseline:.1f} blur={m.blur}" if m else "None"
            alts = ", ".join(f"{a['family']}={a['score']}" for a in (m.alternatives[:3] if m else []))
            print(f"   {'ok  ' if font_ok else 'FAIL'} {'size-ok' if size_ok else 'SIZE?'} {'base-ok' if base_ok else 'BASE?'} "
                  f"want {family}{' Bold' if bold else ''} {size}pt base={y:.1f} | got {got} | {alts}")
    print(f"{label}: font {good_font}/{len(LINES)}, font+size+baseline {good_all}/{len(LINES)}, "
          f"avg {sum(times) / len(times):.2f}s per line")
    return good_font, good_all


def edit_check():
    """Editing OCR text must write the recognised font at the scanned baseline, over the old ink."""
    from pdfeditor.textedit import FontMemory, LineEdit, edit_lines

    ocr, positions = make_scan()
    memory = FontMemory()
    ok = True
    for (family, bold, size, text), y in zip(LINES, positions):
        if family not in ("Times New Roman", "Arial", "Georgia"):
            continue
        pt = PageText(ocr[0])
        line = min(pt.lines, key=lambda l: abs(l.baseline - y))
        words = line.text.split()
        new_text = " ".join(words[:-1] + ["edited"])
        edit_lines(ocr, 0, [LineEdit(line.id, line.text, new_text)], memory)
        spans = [sp for b in ocr[0].get_text("dict")["blocks"] for l in b.get("lines", []) for sp in l["spans"]
                 if sp["font"] != "GlyphLessFont" and abs(sp["origin"][1] - y) < 3]
        fonts = {sp["font"] for sp in spans}
        group = next((g for g in EQUIVALENT if family in g), {family})
        keys = {g.replace(" ", "").lower() for g in group}
        good = spans and all(any(k in f.replace(" ", "").replace("-", "").lower() for k in keys) for f in fonts)
        good = bool(good) and all(abs(sp["origin"][1] - y) <= max(1.0, size * 0.12) for sp in spans)
        good = good and all(abs(sp["size"] - size) <= max(0.8, size * 0.1) for sp in spans)
        bold_ok = all(("bold" in f.lower()) == bold for f in fonts)
        print(f"   edit {'ok  ' if good and bold_ok else 'FAIL'} {family}{' Bold' if bold else ''}: wrote {sorted(fonts)} "
              f"at y={[round(sp['origin'][1], 1) for sp in spans][:1]} size={[round(sp['size'], 1) for sp in spans][:1]}")
        ok = ok and good and bold_ok
    return ok


def mixed_check():
    """A bold label followed by regular text on the same line must keep per-word weights."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=300)
    cases = [("Arial", 11, [("Account Number:", True), ("7731 0042 9918", False)]),
             ("Times New Roman", 12, [("Prepared for", False), ("Acme Corporation", True), ("by the finance team", False)]),
             ("Verdana", 10, [("Status:", True), ("Completed without errors", False)])]
    y = 60
    for family, size, parts in cases:
        tw = fitz.TextWriter(page.rect)
        x = 50
        for text, bold in parts:
            font, _, _ = SYSTEM_FONTS.font_for(family, bold, False)
            _, last = tw.append((x, y), text + " ", font=font, fontsize=size)
            x = last.x
        tw.write_text(page, color=(0.1, 0.1, 0.1))
        y += 50
    pix = page.get_pixmap(dpi=150)
    high = fitz.Pixmap(pix, pix.width * 2, pix.height * 2)
    scan = fitz.open()
    sp = scan.new_page(width=612, height=300)
    sp.insert_image(sp.rect, stream=high.tobytes("jpeg", jpg_quality=60))
    ensure_tessdata()
    ocr = fitz.open("pdf", sp.get_pixmap(dpi=300).pdfocr_tobytes(language="eng"))
    pt = PageText(ocr[0])
    ok = True
    y = 60
    for family, size, parts in cases:
        line = min(pt.lines, key=lambda l: abs(l.baseline - y))
        words = line_words(line)
        m = match_words(ocr[0], words)
        expected = [bold for text, bold in parts for _ in text.split()]
        got = m.word_bold if m else []
        good = m is not None and same_family(m.family, family) and got == expected
        print(f"   mixed {'ok  ' if good else 'FAIL'} {family}: expected {expected} got {got} ({m.family if m else None})")
        ok = ok and good
        y += 50
    return ok


def main():
    verbose = "-v" in sys.argv
    results = [run_variant(label, kw, verbose) for label, kw in VARIANTS]
    clean_font, clean_all = results[0]
    ok = clean_all >= len(LINES) - 1 and all(font >= len(LINES) - 3 for font, _ in results)
    print("edits on a scanned page:")
    ok = edit_check() and ok
    print("lines mixing bold and regular words:")
    ok = mixed_check() and ok
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
