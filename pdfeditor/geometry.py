"""Page geometry helpers: rotation normalisation."""

from __future__ import annotations

import re

import pymupdf as fitz

_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _numbers(text: str) -> list[float]:
    return [float(v) for v in _NUM.findall(text or "")]


def _fmt(values) -> str:
    return "[" + " ".join(f"{v:.4f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v)) for v in values) + "]"


def _get(doc: fitz.Document, xref: int, key: str, inherit: bool = False) -> tuple[str, str]:
    kind, value = doc.xref_get_key(xref, key)
    cur = xref
    while inherit and kind == "null":
        pkind, pval = doc.xref_get_key(cur, "Parent")
        if pkind != "xref":
            break
        cur = int(pval.split()[0])
        kind, value = doc.xref_get_key(cur, key)
    if kind == "xref":
        ref = int(value.split()[0])
        return "array", doc.xref_object(ref, compressed=True)
    return kind, value


def _apply(m: tuple, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def _rect(m: tuple, box: list[float]) -> list[float]:
    x0, y0, x1, y1 = box
    pts = [_apply(m, x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def _points(m: tuple, values: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(0, len(values) - 1, 2):
        out.extend(_apply(m, values[i], values[i + 1]))
    return out


def _concat(m1: tuple, m2: tuple) -> tuple:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def _appearance_streams(doc: fitz.Document, annot_xref: int) -> list[int]:
    streams: list[int] = []
    for key in ("AP/N", "AP/R", "AP/D"):
        kind, value = doc.xref_get_key(annot_xref, key)
        if kind == "xref":
            ref = int(value.split()[0])
            if doc.xref_is_stream(ref):
                streams.append(ref)
            else:
                streams.extend(int(r) for r in re.findall(r"(\d+) 0 R", doc.xref_object(ref)))
        elif kind == "dict":
            streams.extend(int(r) for r in re.findall(r"(\d+) 0 R", value))
    return [s for s in streams if doc.xref_is_stream(s)]


def normalize_rotation(doc: fitz.Document, pno: int) -> bool:
    """Bake /Rotate into the page so that stored and displayed coordinates coincide.

    Content is wrapped with a transformation; annotations, links and form
    fields are moved (geometry *and* appearance streams) so the page looks
    exactly as before. Returns True if the page changed.
    """
    page = doc[pno]
    rot = page.rotation % 360
    if rot == 0:
        return False
    xref = page.xref
    kind, value = _get(doc, xref, "MediaBox", inherit=True)
    mb = _numbers(value)[:4] if kind != "null" else [0, 0, 612, 792]
    x0, y0, x1, y1 = min(mb[0], mb[2]), min(mb[1], mb[3]), max(mb[0], mb[2]), max(mb[1], mb[3])
    if rot == 90:
        m = (0, -1, 1, 0, -y0, x1)
    elif rot == 180:
        m = (-1, 0, 0, -1, x1, y1)
    else:  # 270
        m = (0, 1, -1, 0, y1, -x0)
    rotation_only = (m[0], m[1], m[2], m[3], 0, 0)

    boxes = {}
    for key in ("CropBox", "BleedBox", "TrimBox", "ArtBox"):
        k, v = _get(doc, xref, key, inherit=(key == "CropBox"))
        if k != "null":
            nums = _numbers(v)[:4]
            if len(nums) == 4:
                boxes[key] = _rect(m, [min(nums[0], nums[2]), min(nums[1], nums[3]), max(nums[0], nums[2]), max(nums[1], nums[3])])

    cmd = " ".join(f"{v:g}" for v in m) + " cm\n"
    fitz.TOOLS._insert_contents(page, cmd.encode(), False)
    new_mb = _rect(m, [x0, y0, x1, y1])
    doc.xref_set_key(xref, "MediaBox", _fmt(new_mb))
    for key, box in boxes.items():
        doc.xref_set_key(xref, key, _fmt(box))
    doc.xref_set_key(xref, "Rotate", "0")

    kind, value = doc.xref_get_key(xref, "Annots")
    annot_refs: list[int] = []
    if kind == "xref":
        annot_refs = [int(r) for r in re.findall(r"(\d+) 0 R", doc.xref_object(int(value.split()[0])))]
    elif kind == "array":
        annot_refs = [int(r) for r in re.findall(r"(\d+) 0 R", value)]
    for aref in annot_refs:
        try:
            _transform_annotation(doc, aref, m, rotation_only, rot)
        except Exception:
            continue
    return True


def _transform_annotation(doc: fitz.Document, aref: int, m: tuple, rotation_only: tuple, rot: int) -> None:
    kind, value = doc.xref_get_key(aref, "Rect")
    if kind in ("array", "xref"):
        nums = _numbers(value if kind == "array" else doc.xref_object(int(value.split()[0])))
        if len(nums) >= 4:
            doc.xref_set_key(aref, "Rect", _fmt(_rect(m, nums[:4])))
    for key in ("QuadPoints", "L", "Vertices", "CL"):
        kind, value = doc.xref_get_key(aref, key)
        if kind == "array":
            nums = _numbers(value)
            if nums:
                doc.xref_set_key(aref, key, _fmt(_points(m, nums)))
    kind, value = doc.xref_get_key(aref, "InkList")
    if kind == "array":
        strokes = re.findall(r"\[([^\[\]]*)\]", value)
        doc.xref_set_key(aref, "InkList", "[" + " ".join(_fmt(_points(m, _numbers(s))) for s in strokes) + "]")
    kind, value = doc.xref_get_key(aref, "RD")
    if kind == "array":
        l, b, r, t = (_numbers(value) + [0, 0, 0, 0])[:4]
        rd = {90: [b, r, t, l], 180: [r, t, l, b], 270: [t, l, b, r]}[rot]
        doc.xref_set_key(aref, "RD", _fmt(rd))
    kind, value = doc.xref_get_key(aref, "MK/R")
    if kind != "null":
        try:
            new_r = (int(float(value)) - rot) % 360
            doc.xref_set_key(aref, "MK/R", str(new_r))
        except ValueError:
            pass
    for sref in _appearance_streams(doc, aref):
        k, v = doc.xref_get_key(sref, "Matrix")
        current = tuple(_numbers(v)[:6]) if k == "array" and len(_numbers(v)) >= 6 else (1, 0, 0, 1, 0, 0)
        doc.xref_set_key(sref, "Matrix", _fmt(_concat(current, rotation_only)))
