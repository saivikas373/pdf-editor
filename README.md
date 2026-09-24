# PDF Editor

A local PDF editor that runs on your computer and opens in your browser. It can
**edit the text that's already in a PDF**, and add text, images, signatures,
highlights and drawings. It can also fill forms, organize pages, and export the
result. Your files never leave your machine.

## Start it

| How | What happens |
| --- | --- |
| Double-click **`PDF Editor.app`** | Opens the editor in your browser. No Terminal window, and it quits a few minutes after you close the tab. |
| Double-click **`start.command`** | Same, but with a Terminal window that shows logs. Close the window or press Ctrl+C to stop. The first run installs what it needs (about a minute). |
| `./start.command ~/Documents/file.pdf` | Opens that file. **Save** writes back to the same file. |
| Windows: double-click `start.bat` | Same as `start.command`. |

Requirements: Python 3.10+ (already installed on this Mac). For OCR you also need Tesseract (`brew install tesseract`), which this Mac already has.

Three kinds of page:

| Where | What it is |
| --- | --- |
| **`/`** | Every tool, grouped. Pick one and it takes you straight to it. |
| **`/t/<tool>`** | One job on its own page — merge, split, compress, protect, rotate, OCR and the rest. Only that job's options, one button, and the file downloads when it's done. |
| **`/editor`** | The editor, for the work that needs eyes on the page: editing text, signing, redacting, drawing, filling forms. |

Dropping a file on any of them works.

> Run `start.command` once before using `PDF Editor.app`, because the app uses the environment it creates.
> If you move this folder, rebuild the app with `.venv/bin/python scripts/make_mac_app.py`.

## What it can do

**Edit existing text**
- Click any text to edit it in place. The editor matches the font, size, colour and position. When the PDF's embedded font has the characters you type, the editor reuses it. Otherwise it picks the closest installed font.
- **Line mode** keeps everything else exactly where it was, including justified spacing, and mixed styles such as a bold word stay intact.
- **Paragraph mode** rewrites a whole paragraph and re-wraps its lines, keeping the alignment (left, centre, right or justified).
- Change the font, size, colour, bold or italic of existing text, or move or delete it.
- Find & replace across the whole document, keeping the original formatting.
- **Unreadable text layers**: some scanners and macOS Preview's own OCR write the recognised text without a character map, so every letter reads as "&#65533;" and the text can't be searched, copied or edited. The editor recovers the characters from the font the layer was made with when a document is opened, and only accepts the result when the letter widths on the page match. Nothing usable is changed.
- **Scanned pages**: if a page is only a picture, Edit Text offers to recognize it with OCR first. PDFs made searchable by other tools (Tesseract, ocrmypdf, Adobe) can be edited straight away.
  Small corrections (a digit in a date, a changed letter) redraw only the characters that changed, and are printed the way the scanner would have: the same weight and the same soft edges as the characters beside them, over paper lifted from the page, so they don't read as pasted on. Everything else keeps its original pixels.
  The OCR text layer carries no font information, so the editor **identifies the typeface from the scan itself**. It renders the words in every installed font, compares them pixel by pixel with the image, and uses the closest match (family, size, baseline, ink colour). Bold and regular are recognized word by word, so a bold label next to a regular value keeps both weights. The toolbar shows the result, e.g. "Arial (matched to scan)". You can still pick another font.

**Add things**: text boxes, images, signatures (draw, type or upload; saved for reuse), highlights, underlines and strikethroughs, freehand pen and marker, rectangles, ellipses, lines and arrows, sticky-note comments, and ✓ ✕ ● stamps for forms.
Anything you add stays movable, resizable and restylable until you download.

**Forms**: fill text fields, checkboxes, radio buttons and dropdowns directly on the page.

**Pages**: reorder by dragging, and rotate, delete, duplicate, insert blank pages, insert another PDF or images, crop, extract, and split.

**Document tools**: watermarks, page numbers, headers and footers, document properties, redaction (permanently removes the content under the box, including a search-and-mark-all option), and whiteout.

**Export**: download with optional compression, password protection (AES-256) with permissions, and flattening. You can also export pages as PNG or JPG, extract text or embedded images, and download split PDFs as a ZIP.

Everything is undoable (⌘Z / ⇧⌘Z). Press **?** in the editor for all keyboard shortcuts.

## Good to know

- Edits stay in memory until you **Download** (or **Save**). An orange dot next to the file name means there are unsaved changes. If the editor stops or quits (closing the Terminal window, restarting the computer), those edits are lost.
- Some fonts (common in tax, insurance and banking forms) forbid being copied into another PDF. Editing text in those documents still keeps the original typeface: the new text is written with the font the page already carries. If that isn't possible, the closest installed font is used rather than losing the edit.
- Text that has been converted to outlines or images can't be edited as text. Use OCR, or Whiteout plus Add Text.
- Rotated (vertical) text lines can't be edited in place.
- Scripts that need shaping, such as Telugu, Hindi and Arabic, render correctly in new text. Editing existing text in those scripts depends on how the PDF encodes it.
- When you rotate a page, items you added on it become permanent. Undo brings them back as editable items.

## Putting it on a server

`DEPLOY.md` covers it: a Dockerfile, a compose file with Caddy for HTTPS, and the
environment variables that turn the local tool into a public one — a hostname
allowlist, per-visitor documents, upload and OCR limits. `tests/test_public.py`
checks the parts that only matter when strangers can reach it.

## Development

```
pdfeditor/
  textedit.py   in-place text editing engine (diffing, redaction, font reuse, reflow)
  scanfont.py   recognises the typeface of text in scanned pages
  fontutil.py   rebuilds cmap tables so embedded subset fonts become writable again
  tounicode.py  recovers text layers written without a character map
  blend.py      prints corrections so they look scanned, not pasted on
  fonts.py      system font discovery (including .ttc collections)
  objects.py    burns added items (text, images, shapes, annotations) into pages
  document.py   open documents, undo/redo history, page operations
  geometry.py   lossless page-rotation normalisation
  tools.py      watermark, page numbers, OCR, split, export
  server.py     local HTTP API (standard library only)
static/         the web interface (plain HTML/CSS/JS, no build step)
  home.html     the tools page at /, handing files to the editor at /editor
tests/          python tests/make_samples.py && python tests/test_textedit.py && python tests/test_api.py
                python tests/test_scanfont.py   (font recognition on synthetic scans; needs Tesseract)
```

Built on [PyMuPDF](https://pymupdf.readthedocs.io/) (MuPDF), which is licensed under the AGPL. That's fine for personal use; if you distribute this software, the AGPL terms apply (or you'd need Artifex's commercial licence).
