"""An open document: the PDF, overlay objects, page identities and undo history."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field

import pymupdf as fitz

from . import objects as objmod
from . import textedit, tools
from . import config
from .geometry import normalize_rotation
from . import tounicode
from .textedit import EditError

MAX_HISTORY = 100
MAX_OBJECT_JSON = 2_000_000

# PyMuPDF is not thread-safe: every document operation runs under this lock.
LOCK = threading.RLock()


class NeedsPassword(Exception):
    pass


@dataclass(frozen=True)
class State:
    pdf: bytes
    page_ids: tuple[str, ...]
    page_revs: tuple[int, ...]
    objects: dict = field(default_factory=dict)  # page_id -> list[dict]; treated as immutable
    label: str = ""


def _clean_filename(name: str) -> str:
    name = os.path.basename(name or "document.pdf").strip() or "document.pdf"
    return re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)


def _open_any(data: bytes, filename: str, password: str | None) -> tuple[fitz.Document, bool]:
    ext = os.path.splitext(filename.lower())[1].lstrip(".")
    is_pdf = data[:1024].lstrip().startswith(b"%PDF") or ext == "pdf"
    try:
        doc = fitz.open(stream=data, filetype="pdf" if is_pdf else (ext or None))
    except Exception as exc:
        raise EditError("This file could not be opened. Is it a valid PDF?") from exc
    encrypted = bool(doc.needs_pass)
    if doc.needs_pass:
        if not password or not doc.authenticate(password):
            raise NeedsPassword()
    if not doc.is_pdf:
        doc = fitz.open("pdf", doc.convert_to_pdf())
    if doc.page_count == 0:
        raise EditError("This document has no pages.")
    return doc, encrypted


class DocSession:
    def __init__(self, data: bytes, filename: str, path: str | None = None, password: str | None = None):
        self.id = uuid.uuid4().hex
        self.filename = _clean_filename(filename)
        self.path = path
        self.assets: dict[str, dict] = {}
        self.memory = textedit.FontMemory()
        self.last_access = time.time()
        self._counter = 0
        self.version = 0
        doc, self.was_encrypted = _open_any(data, filename, password)
        repaired = False
        for pno in range(doc.page_count):
            try:
                repaired |= normalize_rotation(doc, pno)
            except Exception:
                continue
        try:  # text layers written without a /ToUnicode map extract as U+FFFD
            self.recovered_fonts = tounicode.repair(doc)
        except Exception:
            self.recovered_fonts = []
        pdf = doc.tobytes(garbage=1, encryption=fitz.PDF_ENCRYPT_NONE)
        ids = tuple(self._new_id() for _ in range(doc.page_count))
        revs = tuple(self._next_rev() for _ in range(doc.page_count))
        self.states = [State(pdf, ids, revs, {}, "Open")]
        self.index = 0
        self.doc = fitz.open("pdf", pdf)
        self.saved_index = 0

    # ------------------------------------------------------------------ state
    def _new_id(self) -> str:
        return uuid.uuid4().hex[:10]

    def _next_rev(self) -> int:
        self._counter += 1
        return self._counter

    @property
    def state(self) -> State:
        return self.states[self.index]

    def memory_bytes(self) -> int:
        """Roughly what this session is holding: every distinct PDF it remembers.

        Undo states share a buffer when a step changed nothing about the file, so
        count each buffer once.  The live fitz document is a copy of the current
        one, hence the doubling of that.
        """
        seen, total = set(), 0
        for st in self.states:
            if id(st.pdf) not in seen:
                seen.add(id(st.pdf))
                total += len(st.pdf)
        total += len(self.state.pdf)  # the open document alongside its state
        for asset in self.assets.values():
            data = asset.get("data")
            if isinstance(data, (bytes, bytearray)):
                total += len(data)
        return total

    def _push(self, state: State) -> None:
        del self.states[self.index + 1:]
        self.states.append(state)
        self.index = len(self.states) - 1
        self.version += 1
        # trim history (keep the newest states within count and memory budgets)
        while len(self.states) > MAX_HISTORY:
            self.states.pop(0)
            self.index -= 1
            self.saved_index -= 1
        total, seen, keep_from = 0, set(), 0
        for i in range(len(self.states) - 1, -1, -1):
            pdf = self.states[i].pdf
            if id(pdf) not in seen:
                seen.add(id(pdf))
                total += len(pdf)
            if total > config.MAX_HISTORY_BYTES and i < self.index:
                keep_from = i + 1
                break
        if keep_from:
            del self.states[:keep_from]
            self.index -= keep_from
            self.saved_index -= keep_from

    def _reload(self) -> None:
        self.doc = fitz.open("pdf", self.state.pdf)

    def commit_pdf(self, label: str, changed_pages: set[int] | None = None, page_ids=None, page_revs=None, objects=None) -> None:
        """Serialise the (already modified) live document as a new undo state."""
        st = self.state
        pdf = self.doc.tobytes(garbage=1)
        ids = tuple(page_ids) if page_ids is not None else st.page_ids
        if page_revs is not None:
            revs = tuple(page_revs)
        else:
            revs = tuple(self._next_rev() if (changed_pages is None or i in changed_pages) else r for i, r in enumerate(st.page_revs))
        if len(ids) != self.doc.page_count or len(revs) != self.doc.page_count:
            raise RuntimeError("page bookkeeping mismatch")
        objs = st.objects if objects is None else objects
        objs = {pid: lst for pid, lst in objs.items() if lst and pid in ids}
        self._push(State(pdf, ids, revs, objs, label))
        self.doc = fitz.open("pdf", pdf)

    def run_pdf_op(self, label: str, fn, **commit_kwargs):
        """Run ``fn(doc)`` on the live document; roll back if it fails."""
        try:
            result = fn(self.doc)
        except BaseException:
            self._reload()
            raise
        extra = result.pop("_commit", {}) if isinstance(result, dict) else {}
        kwargs = {**commit_kwargs, **extra}
        self.commit_pdf(label, **kwargs)
        return result

    def undo(self) -> bool:
        if self.index == 0:
            return False
        prev_pdf = self.state.pdf
        self.index -= 1
        self.version += 1
        if self.state.pdf is not prev_pdf:
            self._reload()
        return True

    def redo(self) -> bool:
        if self.index >= len(self.states) - 1:
            return False
        prev_pdf = self.state.pdf
        self.index += 1
        self.version += 1
        if self.state.pdf is not prev_pdf:
            self._reload()
        return True

    def page_index(self, page_id: str) -> int:
        try:
            return self.state.page_ids.index(page_id)
        except ValueError:
            raise EditError("That page no longer exists.") from None

    # ------------------------------------------------------------------ info
    def info(self) -> dict:
        st = self.state
        pages = []
        for i, (pid, rev) in enumerate(zip(st.page_ids, st.page_revs)):
            r = self.doc[i].rect
            pages.append({"id": pid, "w": round(r.width, 3), "h": round(r.height, 3), "rev": rev})
        meta = self.doc.metadata or {}
        return {
            "id": self.id,
            "filename": self.filename,
            "hasPath": bool(self.path),
            "version": self.version,
            "pages": pages,
            "objects": st.objects,
            "canUndo": self.index > 0,
            "canRedo": self.index < len(self.states) - 1,
            "undoLabel": st.label if self.index > 0 else "",
            "redoLabel": self.states[self.index + 1].label if self.index < len(self.states) - 1 else "",
            "dirty": self.index != self.saved_index,
            "metadata": {k: meta.get(k) or "" for k in ("title", "author", "subject", "keywords", "creator", "producer")},
            "hasForms": bool(self.doc.is_form_pdf),
            "wasEncrypted": self.was_encrypted,
        }

    # ------------------------------------------------------------------ text
    def page_text(self, page_id: str) -> dict:
        pno = self.page_index(page_id)
        return textedit.PageText(self.doc[pno]).payload()

    def edit_text(self, page_id: str, edits: list[dict]) -> dict:
        pno = self.page_index(page_id)
        items = [textedit.LineEdit(e["lineId"], e.get("origText", ""), e.get("text", ""), e.get("style"),
                                   float(e.get("dx") or 0), float(e.get("dy") or 0), e.get("bbox")) for e in edits]
        return self.run_pdf_op("Edit text", lambda d: textedit.edit_lines(d, pno, items, self.memory), changed_pages={pno})

    def edit_paragraph(self, page_id: str, body: dict) -> dict:
        pno = self.page_index(page_id)
        return self.run_pdf_op("Edit paragraph", lambda d: textedit.edit_paragraph(
            d, pno, int(body.get("paraId", -1)), body.get("lineIds", []), body.get("origText", ""), body.get("text", ""),
            body.get("style"), self.memory, float(body.get("dx") or 0), float(body.get("dy") or 0)), changed_pages={pno})

    def delete_text(self, page_id: str, items: list[dict]) -> dict:
        pno = self.page_index(page_id)
        norm = [{"line_id": i["lineId"], "orig_text": i.get("origText"), "bbox": i.get("bbox")} for i in items]
        return self.run_pdf_op("Delete text", lambda d: textedit.delete_lines(d, pno, norm, self.memory), changed_pages={pno})

    def scan_font(self, page_id: str, line_id: str | None, para_id: str | None) -> dict | None:
        """Recognised font of scanned text, so the editor can preview edits in it."""
        pno = self.page_index(page_id)
        page = self.doc[pno]
        pt = textedit.PageText(page)
        lines = []
        if para_id is not None and para_id.isdigit() and int(para_id) < len(pt.paragraphs):
            lines = pt.paragraphs[int(para_id)].lines
        elif line_id:
            lines = [l for l in pt.lines if l.id == line_id]
        if not lines:
            return None
        style = textedit.scan_style(page, pt, lines, self.memory)
        return style.payload(lines) if style else None

    def find_replace(self, body: dict) -> dict:
        pages = None
        if body.get("pages"):
            pages = tools.parse_page_spec(body["pages"], self.doc.page_count)
        holder: dict = {}

        def op(d):
            res = textedit.find_replace(d, body.get("find", ""), body.get("replace", ""), match_case=bool(body.get("matchCase")),
                                        whole_word=bool(body.get("wholeWord")), use_regex=bool(body.get("regex")),
                                        memory=self.memory, pages=pages)
            holder.update(res)
            if not res["replaced"]:
                raise _NoChange()
            res["_commit"] = {"changed_pages": set(res["pages"])}
            return res

        try:
            return self.run_pdf_op("Replace text", op)
        except _NoChange:
            return {"replaced": 0, "pages": [], "skipped": holder.get("skipped", 0)}

    def search(self, query: str, match_case: bool = False) -> list[dict]:
        hits = []
        if not query:
            return hits
        for pno, pid in enumerate(self.state.page_ids):
            page = self.doc[pno]
            rects = page.search_for(query)
            if match_case and rects:
                text_rects = []
                for r in rects:
                    found = page.get_textbox(r + (-0.5, -0.5, 0.5, 0.5))
                    if query in found.replace("\n", " "):
                        text_rects.append(r)
                rects = text_rects
            for r in rects:
                hits.append({"pageId": pid, "page": pno, "rect": [round(v, 2) for v in r]})
            if len(hits) > 5000:
                break
        return hits

    # ------------------------------------------------------------------ objects
    def objects_op(self, ops: list[dict]) -> dict:
        if not ops:
            return {}
        st = self.state
        objects = {pid: list(lst) for pid, lst in st.objects.items()}
        label = "Edit object"
        for op in ops:
            kind = op.get("op")
            pid = op.get("pageId")
            if pid not in st.page_ids:
                raise EditError("That page no longer exists.")
            lst = objects.setdefault(pid, [])
            if kind == "add":
                obj = op.get("object") or {}
                if len(json.dumps(obj)) > MAX_OBJECT_JSON:
                    raise EditError("Object is too large.")
                if not obj.get("id"):
                    obj["id"] = self._new_id()
                for page_list in objects.values():
                    page_list[:] = [o for o in page_list if o.get("id") != obj["id"]]
                index = op.get("index")
                if isinstance(index, int) and 0 <= index <= len(lst):
                    lst.insert(index, obj)
                else:
                    lst.append(obj)
                label = f"Add {obj.get('type', 'object')}"
            elif kind == "update":
                obj = op.get("object") or {}
                for i, cur in enumerate(lst):
                    if cur.get("id") == obj.get("id"):
                        lst[i] = obj
                        break
                label = f"Change {obj.get('type', 'object')}"
            elif kind == "delete":
                ids = set(op.get("ids") or [op.get("id")])
                lst[:] = [o for o in lst if o.get("id") not in ids]
                label = "Delete"
            elif kind in ("front", "back"):
                ids = set(op.get("ids") or [op.get("id")])
                picked = [o for o in lst if o.get("id") in ids]
                rest = [o for o in lst if o.get("id") not in ids]
                lst[:] = rest + picked if kind == "front" else picked + rest
                label = "Arrange"
            elif kind == "move":
                to_pid = op.get("toPageId")
                if to_pid not in st.page_ids:
                    raise EditError("That page no longer exists.")
                obj = op.get("object") or {}
                lst[:] = [o for o in lst if o.get("id") != obj.get("id")]
                objects.setdefault(to_pid, []).append(obj)
                label = "Move object"
            else:
                raise EditError(f"Unknown object operation: {kind}")
        objects = {pid: lst for pid, lst in objects.items() if lst}
        self._push(State(st.pdf, st.page_ids, st.page_revs, objects, ops[-1].get("label") or label))
        return {}

    def add_asset(self, data: bytes, mime: str) -> dict:
        try:
            pix = fitz.Pixmap(data)
        except Exception as exc:
            raise EditError("Unsupported image. Use PNG or JPEG.") from exc
        asset_id = uuid.uuid4().hex
        self.assets[asset_id] = {"data": data, "mime": mime or "image/png", "w": pix.width, "h": pix.height}
        return {"asset": asset_id, "w": pix.width, "h": pix.height}

    def bake_pages(self, doc: fitz.Document, page_indices: list[int]) -> dict:
        """Burn overlay objects of the given pages into ``doc``; returns remaining objects."""
        st = self.state
        remaining = dict(st.objects)
        for pno in page_indices:
            pid = st.page_ids[pno]
            objs = st.objects.get(pid)
            if objs:
                objmod.bake_page(doc, pno, objs, self.assets, self.memory)
                remaining.pop(pid, None)
        return remaining

    # ------------------------------------------------------------------ pages
    def rotate_pages(self, page_ids: list[str], angle: int) -> dict:
        indices = sorted(self.page_index(p) for p in page_ids)

        def op(d):
            remaining = self.bake_pages(d, indices)
            for pno in indices:
                page = d[pno]
                page.set_rotation((page.rotation + angle) % 360)
                normalize_rotation(d, pno)
            return {"_commit": {"changed_pages": set(indices), "objects": remaining}}

        return self.run_pdf_op("Rotate pages", op)

    def delete_pages(self, page_ids: list[str]) -> dict:
        st = self.state
        indices = sorted({self.page_index(p) for p in page_ids})
        if len(indices) >= len(st.page_ids):
            raise EditError("A document must keep at least one page.")
        keep = [i for i in range(len(st.page_ids)) if i not in indices]

        def op(d):
            d.delete_pages(indices)
            return {"_commit": {"page_ids": [st.page_ids[i] for i in keep], "page_revs": [st.page_revs[i] for i in keep]}}

        return self.run_pdf_op("Delete pages", op)

    def reorder_pages(self, order: list[str]) -> dict:
        st = self.state
        if sorted(order) != sorted(st.page_ids):
            raise EditError("Page order is out of date. Please try again.")
        perm = [st.page_ids.index(pid) for pid in order]
        if perm == list(range(len(perm))):
            return {}

        def op(d):
            d.select(perm)
            return {"_commit": {"page_ids": [st.page_ids[i] for i in perm], "page_revs": [st.page_revs[i] for i in perm]}}

        return self.run_pdf_op("Reorder pages", op)

    def duplicate_pages(self, page_ids: list[str]) -> dict:
        st = self.state
        indices = sorted({self.page_index(p) for p in page_ids})
        ids, revs = list(st.page_ids), list(st.page_revs)
        objects = dict(st.objects)

        def op(d):
            for offset, pno in enumerate(indices):
                src = pno + offset
                d.fullcopy_page(src, src + 1)
                new_id = self._new_id()
                ids.insert(src + 1, new_id)
                revs.insert(src + 1, self._next_rev())
                if st.objects.get(ids[src]):
                    objects[new_id] = [dict(copy.deepcopy(o), id=self._new_id()) for o in st.objects[ids[src]]]
            return {"_commit": {"page_ids": ids, "page_revs": revs, "objects": objects}}

        return self.run_pdf_op("Duplicate pages", op)

    def insert_blank(self, at: int, width: float | None = None, height: float | None = None) -> dict:
        st = self.state
        at = max(0, min(int(at), len(st.page_ids)))
        ref = self.doc[min(at, len(st.page_ids) - 1)].rect
        w, h = float(width or ref.width), float(height or ref.height)

        def op(d):
            d.new_page(at, width=w, height=h)
            ids, revs = list(st.page_ids), list(st.page_revs)
            ids.insert(at, self._new_id())
            revs.insert(at, self._next_rev())
            return {"_commit": {"page_ids": ids, "page_revs": revs}}

        return self.run_pdf_op("Insert page", op)

    def insert_pdf(self, data: bytes, filename: str, at: int, password: str | None = None) -> dict:
        st = self.state
        at = max(0, min(int(at), len(st.page_ids)))
        other, _ = _open_any(data, filename, password)
        for pno in range(other.page_count):
            try:
                normalize_rotation(other, pno)
            except Exception:
                pass
        other = fitz.open("pdf", other.tobytes())
        count = other.page_count

        def op(d):
            d.insert_pdf(other, start_at=at)
            ids, revs = list(st.page_ids), list(st.page_revs)
            ids[at:at] = [self._new_id() for _ in range(count)]
            revs[at:at] = [self._next_rev() for _ in range(count)]
            return {"inserted": count, "_commit": {"page_ids": ids, "page_revs": revs}}

        return self.run_pdf_op("Insert pages", op)

    def insert_images(self, images: list[bytes], at: int, page_size: str) -> dict:
        st = self.state
        at = max(0, min(int(at), len(st.page_ids)))

        def op(d):
            n = tools.insert_image_pages(d, images, at, page_size)
            ids, revs = list(st.page_ids), list(st.page_revs)
            ids[at:at] = [self._new_id() for _ in range(n)]
            revs[at:at] = [self._next_rev() for _ in range(n)]
            return {"inserted": n, "_commit": {"page_ids": ids, "page_revs": revs}}

        return self.run_pdf_op("Insert images", op)

    def crop_pages(self, page_ids: list[str], margins: dict) -> dict:
        indices = sorted({self.page_index(p) for p in page_ids})
        top, right = float(margins.get("top") or 0), float(margins.get("right") or 0)
        bottom, left = float(margins.get("bottom") or 0), float(margins.get("left") or 0)

        def op(d):
            remaining = self.bake_pages(d, indices)
            for pno in indices:
                page = d[pno]
                r = page.rect
                new = fitz.Rect(r.x0 + left, r.y0 + top, r.x1 - right, r.y1 - bottom)
                if new.width < 36 or new.height < 36:
                    raise EditError("Those margins leave too little of the page.")
                cb = page.cropbox
                page.set_cropbox(fitz.Rect(cb.x0 + left, cb.y0 + top, cb.x0 + left + new.width, cb.y0 + top + new.height))
            return {"_commit": {"changed_pages": set(indices), "objects": remaining}}

        return self.run_pdf_op("Crop pages", op)

    # ------------------------------------------------------------------ forms
    def form_fields(self, page_id: str) -> list[dict]:
        pno = self.page_index(page_id)
        out = []
        for w in self.doc[pno].widgets() or []:
            if w.field_type in (fitz.PDF_WIDGET_TYPE_SIGNATURE, fitz.PDF_WIDGET_TYPE_BUTTON):
                continue
            item = {
                "xref": w.xref,
                "name": w.field_name,
                "type": w.field_type_string.lower(),
                "rect": [round(v, 2) for v in w.rect],
                "value": w.field_value if not isinstance(w.field_value, bool) else ("Yes" if w.field_value else "Off"),
                "readonly": bool(w.field_flags & 1),
                "multiline": bool(w.field_flags & (1 << 12)),
                "fontsize": w.text_fontsize or 0,
                "maxlen": w.text_maxlen or 0,
            }
            if w.field_type in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
                try:
                    item["onState"] = w.on_state()
                except Exception:
                    item["onState"] = "Yes"
            if w.field_type in (fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX):
                item["options"] = [c if isinstance(c, str) else c[-1] for c in (w.choice_values or [])]
            out.append(item)
        return out

    def form_set(self, page_id: str, xref: int, value) -> dict:
        pno = self.page_index(page_id)

        def op(d):
            page = d[pno]
            target = None
            for w in page.widgets() or []:
                if w.xref == xref:
                    target = w
                    break
            if target is None:
                raise EditError("Form field not found.")
            if target.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON:
                on = value not in (False, "Off", "", None)
                for w in page.widgets() or []:
                    if w.field_name == target.field_name and w.xref != target.xref and on:
                        w.field_value = False
                        w.update()
                target.field_value = True if on else False
            elif target.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                target.field_value = value not in (False, "Off", "", None)
            else:
                target.field_value = "" if value is None else str(value)
            target.update()
            return {}

        return self.run_pdf_op("Fill form", op, changed_pages={pno})

    # ------------------------------------------------------------------ tools
    def watermark(self, opts: dict) -> dict:
        pages = tools.parse_page_spec(opts.get("pages"), self.doc.page_count)
        return self.run_pdf_op("Watermark", lambda d: tools.add_watermark(d, opts, pages) or {}, changed_pages=set(pages))

    def text_stamp(self, opts: dict) -> dict:
        pages = tools.parse_page_spec(opts.get("pages"), self.doc.page_count)
        stem = os.path.splitext(self.filename)[0]
        return self.run_pdf_op("Page numbers", lambda d: tools.add_text_stamp(d, opts, pages, stem) or {}, changed_pages=set(pages))

    def set_metadata(self, meta: dict) -> dict:
        def op(d):
            current = d.metadata or {}
            new = {k: current.get(k) or "" for k in ("title", "author", "subject", "keywords", "creator", "producer")}
            for k in new:
                if k in meta and meta[k] is not None:
                    new[k] = str(meta[k])
            d.set_metadata(new)
            return {}

        return self.run_pdf_op("Document properties", op, changed_pages=set())

    def ocr(self, opts: dict) -> dict:
        pages = tools.parse_page_spec(opts.get("pages"), self.doc.page_count)
        if config.MAX_OCR_PAGES and len(pages) > config.MAX_OCR_PAGES:
            # the server works on one request at a time: a hundred-page scan would
            # hold everyone else up for as long as it took
            raise EditError(f"Recognising text is limited to {config.MAX_OCR_PAGES} pages at a time here. "
                            f"Choose a page range.")
        holder = {}

        def op(d):
            res = tools.ocr_pages(d, pages, opts.get("language") or "eng", int(opts.get("dpi") or 300), not opts.get("force"))
            holder.update(res)
            if not res["ocr"]:
                raise _NoChange()
            res["_commit"] = {"changed_pages": set(res["ocr"])}
            return res

        try:
            return self.run_pdf_op("Recognize text", op)
        except _NoChange:
            return holder

    # ------------------------------------------------------------------ output
    def baked_document(self, flatten: bool = False) -> fitz.Document:
        doc = fitz.open("pdf", self.state.pdf)
        self.bake_pages(doc, list(range(doc.page_count)))
        if flatten:
            try:
                doc.bake(annots=True, widgets=True)
            except Exception:
                pass
        return doc

    def export_pdf(self, opts: dict) -> bytes:
        doc = self.baked_document(bool(opts.get("flatten")))
        compress = opts.get("compress") or "none"
        try:
            doc.subset_fonts()
        except Exception:
            pass
        if compress in ("balanced", "strong"):
            settings = {"balanced": (200, 150, 75), "strong": (120, 96, 55)}[compress]
            try:
                doc.rewrite_images(dpi_threshold=settings[0], dpi_target=settings[1], quality=settings[2])
            except Exception:
                pass
        kwargs = dict(garbage=4 if compress != "none" else 3, deflate=True, deflate_images=True, deflate_fonts=True,
                      clean=compress == "strong", use_objstms=1 if compress != "none" else 0)
        password = opts.get("password") or ""
        if password:
            perms = opts.get("permissions") or {}
            flags = fitz.PDF_PERM_ACCESSIBILITY
            if perms.get("print", True):
                flags |= fitz.PDF_PERM_PRINT | fitz.PDF_PERM_PRINT_HQ
            if perms.get("copy", True):
                flags |= fitz.PDF_PERM_COPY
            if perms.get("modify", True):
                flags |= fitz.PDF_PERM_MODIFY | fitz.PDF_PERM_ASSEMBLE
            if perms.get("annotate", True):
                flags |= fitz.PDF_PERM_ANNOTATE | fitz.PDF_PERM_FORM
            kwargs.update(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=password,
                          owner_pw=opts.get("ownerPassword") or password + "-owner-" + uuid.uuid4().hex[:8], permissions=flags)
        return doc.tobytes(**kwargs)

    def save_to_path(self, opts: dict) -> dict:
        if not self.path:
            raise EditError("This document was not opened from a file on disk.")
        data = self.export_pdf(opts)
        directory = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".pdfeditor-", suffix=".pdf", dir=directory)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        self.saved_index = self.index
        return {"saved": self.path, "size": len(data)}

    def mark_saved(self) -> None:
        self.saved_index = self.index

    def page_render(self, page_id: str, scale: float) -> bytes:
        pno = self.page_index(page_id)
        scale = max(0.05, min(8.0, scale))
        if config.MAX_RENDER_PIXELS:  # one pixmap must not be the whole machine
            page = self.doc[pno]
            pixels = (page.rect.width * scale) * (page.rect.height * scale)
            if pixels > config.MAX_RENDER_PIXELS:
                scale *= (config.MAX_RENDER_PIXELS / pixels) ** 0.5
        pix = self.doc[pno].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, annots=True)
        return pix.tobytes("png")

    def font_file(self, page_id: str, name: str) -> bytes | None:
        pno = self.page_index(page_id)
        return textedit.embedded_font_for_preview(self.doc, pno, name, self.memory)


class _NoChange(Exception):
    pass
