"""Exercise the text-editing engine on the generated samples and render results.

Run:  python tests/test_textedit.py [outdir]
"""

import os
import sys
import time

import pymupdf as fitz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdfeditor.textedit import (  # noqa: E402
    FontMemory,
    LineEdit,
    PageText,
    delete_lines,
    edit_lines,
    edit_paragraph,
    find_replace,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def find_line(pt, startswith):
    for line in pt.lines:
        if line.text.strip().startswith(startswith):
            return line
    raise AssertionError(f"line starting with {startswith!r} not found")


def page_text(doc, pno=0):
    """Text as the editor sees it (fragments of one visual line merged)."""
    return "\n".join(line.text for line in PageText(doc[pno]).lines)


def snapshot(doc, name, pno=0, clip=None):
    doc[pno].get_pixmap(dpi=110, clip=clip).save(os.path.join(OUT, name))


memory = FontMemory()
doc = fitz.open(os.path.join(SAMPLES, "report.pdf"))
pt = PageText(doc[0])
print("styles:", [(s.font, round(s.size, 1), s.bold) for s in pt.styles])
print("paragraphs:", [(p.id, len(p.lines), p.align) for p in pt.paragraphs if len(p.lines) > 1])
snapshot(doc, "00_original.png")

# 1. mixed-style line: bold company name must stay bold
t = time.time()
line = find_line(pt, "Prepared for")
r = edit_lines(doc, 0, [LineEdit(line.id, line.text, line.text.replace("Acme Corporation", "Globex Industries"))], memory)
print("1 mixed style", r, round(time.time() - t, 3), "s")
pt = PageText(doc[0])
line = find_line(pt, "Prepared for")
bold_text = "".join(c.c for c in line.chars if pt.styles[c.style].bold)
check("Globex Industries" in page_text(doc), "replacement text is extractable")
check(bold_text.strip() == "Globex Industries", f"bold run preserved: {bold_text!r}")

# 2. word change in the middle of a justified paragraph line
line = find_line(pt, "Quarterly revenue")
r = edit_lines(doc, 0, [LineEdit(line.id, line.text, line.text.replace("twelve", "fifteen"))], memory)
print("2 justified line", r)
check("fifteen percent" in page_text(doc), "justified line edit extractable")
check(r["collateral"] == 0 and r["missed"] == 0, "no collateral damage in paragraph")

# 3. non-embedded Helvetica line
pt = PageText(doc[0])
line = find_line(pt, "Contact:")
r = edit_lines(doc, 0, [LineEdit(line.id, line.text, line.text.replace("0100", "0199 ext. 42"))], memory)
check("0199 ext. 42" in page_text(doc), "base-14 Helvetica line edited")

# 4. white title on dark background with characters not in the subset
pt = PageText(doc[0])
line = find_line(pt, "Annual Business")
r = edit_lines(doc, 0, [LineEdit(line.id, line.text, "Annual Business Review 2027 (Draft)")], memory)
print("4 title", r)
check("Annual Business Review 2027 (Draft)" in page_text(doc), "title replaced")

# 5. superscript line
pt = PageText(doc[0])
line = find_line(pt, "Growth figures")
r = edit_lines(doc, 0, [LineEdit(line.id, line.text, line.text.replace("audited", "independently audited"))], memory)
check("independently audited" in page_text(doc), "superscript line edited")

# 6. table cell
pt = PageText(doc[0])
line = [l for l in pt.lines if l.text.strip() == "$12.4M"][0]
r = edit_lines(doc, 0, [LineEdit(line.id, line.text, "$13.05M")], memory)
check("$13.05M" in page_text(doc) and "North America" in page_text(doc), "table cell edited, row label intact")

# 7. style override + move
pt = PageText(doc[0])
line = find_line(pt, "Hired 85")
r = edit_lines(doc, 0, [LineEdit(line.id, line.text, "Hired 92 new employees", style={"bold": True, "color": "#c0392b"}, dx=10)], memory)
pt = PageText(doc[0])
line = find_line(pt, "Hired 92")
check(pt.styles[line.main_style].bold and pt.styles[line.main_style].color == 0xC0392B, "bold + colour override applied")

# 8. delete a line
pt = PageText(doc[0])
line = find_line(pt, "Confidential")
delete_lines(doc, 0, [{"line_id": line.id, "orig_text": line.text}])
check("Confidential" not in page_text(doc), "line deleted")
check("Contact:" in page_text(doc), "neighbour line intact after delete")
snapshot(doc, "01_line_edits.png")

# 9. paragraph reflow
pt = PageText(doc[0])
para = [p for p in pt.paragraphs if p.lines[0].text.startswith("Quarterly")][0]
from pdfeditor.textedit import paragraph_text  # noqa: E402

old, _ = paragraph_text(para)
new = old.replace("Operating expenses remained flat", "Operating expenses decreased slightly, by roughly two percent,")
t = time.time()
r = edit_paragraph(doc, 0, para.id, [l.id for l in para.lines], old, new, None, memory)
print("9 paragraph", r, round(time.time() - t, 3), "s")
check("decreased slightly" in page_text(doc).replace("\n", " "), "paragraph reflow text present")
snapshot(doc, "02_paragraph.png", clip=fitz.Rect(30, 170, 565, 420))

# 10. find & replace across document
r = find_replace(doc, "million", "M USD", match_case=False, whole_word=True, use_regex=False, memory=memory)
print("10 find/replace", r)
check(r["replaced"] == 2 and "$4.2 M USD" in page_text(doc), "find & replace")
snapshot(doc, "03_after_all.png")

# roundtrip save and reopen
data = doc.tobytes(garbage=3, deflate=True)
doc2 = fitz.open("pdf", data)
check("Globex Industries" in page_text(doc2), "survives save + reopen")
doc2.subset_fonts()
small = doc2.tobytes(garbage=4, deflate=True)
print("sizes: session", len(data), "subset", len(small))

# 11. scanned page with invisible OCR text
scan_path = os.path.join(SAMPLES, "scanned.pdf")
if os.path.exists(scan_path):
    sdoc = fitz.open(scan_path)
    spt = PageText(sdoc[0])
    line = find_line(spt, "Net profit")
    print("scanned line:", repr(line.text), "visible:", spt.styles[line.main_style].visible)
    r = edit_lines(sdoc, 0, [LineEdit(line.id, line.text, "Net profit: $5.0 million")], memory)
    check("5.0 million" in page_text(sdoc), "scanned page edit")
    snapshot(sdoc, "04_scanned.png", clip=fitz.Rect(30, 320, 565, 420))

# 12. a form whose font forbids re-embedding (what tax and insurance software produces)
rest_path = os.path.join(SAMPLES, "restricted.pdf")
if os.path.exists(rest_path):
    rdoc = fitz.open(rest_path)
    rpt = PageText(rdoc[0])
    line = find_line(rpt, "Gross compensation")
    want = {rpt.styles[line.main_style].font}
    r = edit_lines(rdoc, 0, [LineEdit(line.id, line.text, "Gross compensation reported for the year 2021")],
                   FontMemory())
    print("12 restricted font", r)
    got = {sp["font"] for b in rdoc[0].get_text("dict")["blocks"] for l in b.get("lines", [])
           for sp in l["spans"] if abs(sp["origin"][1] - line.baseline) < 1}
    check("for the year 2021" in page_text(rdoc), "restricted font: edit applied")
    check(got == want, f"restricted font: kept the document's own font (wrote {sorted(got)}, want {sorted(want)})")
    snapshot(rdoc, "05_restricted.png")

    # and with no usable resource on the page it must still write, in a substitute
    from pdfeditor.textedit import FontBook  # noqa: E402

    keep = FontBook.resource
    FontBook.resource = lambda self, name, emb: None
    try:
        rdoc2 = fitz.open(rest_path)
        line2 = find_line(PageText(rdoc2[0]), "Total tax withheld")
        edit_lines(rdoc2, 0, [LineEdit(line2.id, line2.text, "Total tax withheld by the employer in 2021")],
                   FontMemory())
        check("employer in 2021" in page_text(rdoc2), "restricted font: substitutes rather than failing")
    finally:
        FontBook.resource = keep

# 13. a text layer whose characters are missing (Identity-H with no /ToUnicode)
un_path = os.path.join(SAMPLES, "unreadable.pdf")
if os.path.exists(un_path):
    from pdfeditor import tounicode  # noqa: E402
    from pdfeditor.document import DocSession  # noqa: E402
    from pdfeditor.fonts import SYSTEM_FONTS  # noqa: E402

    broken = fitz.open(un_path)
    # read it the way the editor does: plain get_text() fills unknown characters in
    # with the glyph id, which looks like text but is not
    raw = "".join(l.text for l in PageText(broken[0]).lines)
    check(raw.count("\ufffd") > 10, f"sample is unreadable to start with ({raw.count(chr(0xFFFD))} replacement chars)")
    sess = DocSession(open(un_path, "rb").read(), "unreadable.pdf")
    got = page_text(sess.doc)
    print("13 recovered fonts", sess.recovered_fonts)
    check(bool(sess.recovered_fonts), "text layer repaired on open")
    check("Gross pay for the period" in got, "letters recovered")
    check("Net deposit $2,481.37 (no. 4471)" in got, "digits and punctuation recovered")
    check("\ufffd" not in got, "nothing left unreadable")
    check(all(l["editable"] for l in PageText(sess.doc[0]).payload()["lines"]), "recovered lines are editable")

    # the width check must reject a font the glyphs did not come from
    usage = tounicode.survey(fitz.open(un_path))
    name, use = next(iter(usage.items()))
    wrong = SYSTEM_FONTS.font_for("Courier New", False, False)
    if wrong:
        _, _, fit = tounicode._mapping(wrong[0], use)
        check(fit < tounicode.MIN_WIDTH_FIT, f"a wrong font is rejected (width fit {fit:.2f})")

# 14. correcting one character of scanned text must leave the other characters alone
spot_path = os.path.join(SAMPLES, "spotscan.pdf")
if os.path.exists(spot_path):
    from pdfeditor.document import DocSession  # noqa: E402

    sess = DocSession(open(spot_path, "rb").read(), "spotscan.pdf")
    pid = sess.state.page_ids[0]
    spt = PageText(sess.doc[0])
    line = [l for l in spt.lines if l.text.startswith("7/")][0]
    edited = line.chars[0]
    clip = fitz.Rect(15, 20, 325, 90)
    was = sess.doc[0].get_pixmap(clip=clip, dpi=200, colorspace=fitz.csGRAY)
    sess.edit_text(pid, [{"lineId": line.id, "origText": line.text, "text": "8" + line.text[1:]}])
    now = sess.doc[0].get_pixmap(clip=clip, dpi=200, colorspace=fitz.csGRAY)
    scale = 200 / 72
    ex0 = int((edited.x0 - 1.5 - clip.x0) * scale)
    ex1 = int((edited.x1 + 1.5 - clip.x0) * scale)
    outside = moved = 0
    for y in range(was.height):
        row = y * was.stride
        for x in range(was.width):
            if ex0 <= x <= ex1:
                continue
            outside += 1
            moved += abs(was.samples[row + x] - now.samples[row + x]) > 40
    after_text = [l.text for l in PageText(sess.doc[0]).lines]
    print("14 spot edit: %d of %d pixels outside the character changed" % (moved, outside))
    check(any(t.startswith("8/") for t in after_text), "the digit was replaced")

    # the redrawn character has to sit as heavily on the paper as the scanned ones
    from pdfeditor import blend  # noqa: E402

    spot_line = [l for l in PageText(sess.doc[0]).lines if l.text.startswith("8/")][0]
    box = lambda c: fitz.Rect(c.x0, spot_line.bbox.y0, c.x1, spot_line.bbox.y1)  # noqa: E731
    _, drawn = blend._read(sess.doc[0], box(spot_line.chars[0]), 300)
    scanned = [blend._read(sess.doc[0], box(c), 300)[1].coverage
               for c in spot_line.chars[1:] if c.c.isdigit()]
    ratio = drawn.coverage / (sum(scanned) / len(scanned)) if scanned else 0
    print("   redrawn character carries %.2fx the ink of the scanned ones" % ratio)
    check(0.55 < ratio < 1.8, f"the redrawn character is not heavier or fainter than the print ({ratio:.2f}x)")
    check(any(t.startswith("7/ 16") or t.startswith("7/16") for t in after_text), "the other cell is untouched")
    check(moved < outside * 0.005, f"the rest of the scan is left alone ({100 * moved / outside:.2f}% changed)")
    snapshot(sess.doc, "06_spot_edit.png", clip=clip)

print("\nFAILURES:" if failures else "\nall checks passed", *failures, sep="\n  ")
