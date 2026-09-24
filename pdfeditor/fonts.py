"""System font discovery and PDF-font -> writable-font resolution."""

from __future__ import annotations

import os
import re
import struct
import sys
import threading
from dataclasses import dataclass, field

import pymupdf as fitz

from .fontutil import build_sfnt

_HOME = os.path.expanduser("~")

if sys.platform == "darwin":
    FONT_DIRS = [
        "/System/Library/Fonts",
        "/System/Library/Fonts/Supplemental",
        "/Library/Fonts",
        os.path.join(_HOME, "Library/Fonts"),
    ]
elif sys.platform.startswith("win"):
    _windir = os.environ.get("WINDIR", r"C:\Windows")
    FONT_DIRS = [
        os.path.join(_windir, "Fonts"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
    ]
else:
    FONT_DIRS = [
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        os.path.join(_HOME, ".fonts"),
        os.path.join(_HOME, ".local/share/fonts"),
    ]

# Families shown first in the font picker (only those actually installed are listed).
COMMON_FAMILIES = [
    "Arial", "Helvetica", "Helvetica Neue", "Times New Roman", "Times", "Georgia",
    "Verdana", "Tahoma", "Trebuchet MS", "Calibri", "Cambria", "Segoe UI", "Roboto",
    "Open Sans", "Lato", "Inter", "Avenir Next", "Futura", "Gill Sans", "Optima",
    "Palatino", "Baskerville", "Didot", "Garamond", "Courier New", "Courier", "Menlo",
    "American Typewriter", "Comic Sans MS", "Impact",
]

# Built-in PDF base-14 fonts (always available, Latin-1 only).
BASE14 = {
    ("sans", False, False): "helv", ("sans", True, False): "hebo",
    ("sans", False, True): "heit", ("sans", True, True): "hebi",
    ("serif", False, False): "tiro", ("serif", True, False): "tibo",
    ("serif", False, True): "tiit", ("serif", True, True): "tibi",
    ("mono", False, False): "cour", ("mono", True, False): "cobo",
    ("mono", False, True): "coit", ("mono", True, True): "cobi",
}

GENERIC_FAMILIES = {
    "sans": ["Arial", "Helvetica", "Helvetica Neue", "Verdana", "Liberation Sans", "Arimo", "DejaVu Sans"],
    "serif": ["Times New Roman", "Times", "Georgia", "Palatino", "Liberation Serif", "Tinos", "DejaVu Serif"],
    "mono": ["Courier New", "Courier", "Menlo", "Monaco", "Liberation Mono", "Cousine", "DejaVu Sans Mono"],
}

UNICODE_FALLBACK_FAMILIES = ["Arial Unicode MS", "Arial", "Helvetica Neue", "Apple Symbols", "Segoe UI", "DejaVu Sans"]

# Metric-compatible / look-alike substitutes for common fonts that are often missing.
SUBSTITUTES = {
    "calibri": ["Carlito", "Helvetica Neue", "Arial"],
    "cambria": ["Caladea", "Georgia", "Times New Roman"],
    "segoeui": ["Helvetica Neue", "Arial"],
    "garamond": ["EB Garamond", "Palatino", "Times New Roman"],
    "bookantiqua": ["Palatino", "Georgia"],
    "centurygothic": ["Futura", "Avenir Next", "Arial"],
    "consolas": ["Menlo", "Courier New"],
    "lucidasans": ["Lucida Grande", "Helvetica Neue", "Arial"],
    "myriadpro": ["Helvetica Neue", "Arial"],
    "minionpro": ["Palatino", "Times New Roman"],
    "helveticaneue": ["Helvetica Neue", "Helvetica", "Arial"],
    "roboto": ["Helvetica Neue", "Arial"],
    "opensans": ["Helvetica Neue", "Arial"],
    "sourcesanspro": ["Helvetica Neue", "Arial"],
    "dejavusans": ["Verdana", "Arial"],
    "liberationsans": ["Arial", "Helvetica"],
    "liberationserif": ["Times New Roman", "Times"],
    "nimbussans": ["Helvetica", "Arial"],
    "nimbusroman": ["Times", "Times New Roman"],
    "cmr": ["Times New Roman", "Georgia"],
    "lmroman": ["Times New Roman", "Georgia"],
    # the other direction, for a machine that has the metric-compatible clones rather
    # than the fonts themselves - which is every Linux server
    "arial": ["Liberation Sans", "Arimo", "Nimbus Sans", "DejaVu Sans"],
    "arialmt": ["Liberation Sans", "Arimo", "Nimbus Sans"],
    "helvetica": ["Nimbus Sans", "Liberation Sans", "Arimo", "DejaVu Sans"],
    "timesnewroman": ["Liberation Serif", "Tinos", "Nimbus Roman", "DejaVu Serif"],
    "times": ["Nimbus Roman", "Liberation Serif", "Tinos"],
    "couriernew": ["Liberation Mono", "Cousine", "Nimbus Mono PS", "DejaVu Sans Mono"],
    "courier": ["Nimbus Mono PS", "Liberation Mono", "Cousine"],
    "verdana": ["DejaVu Sans", "Liberation Sans"],
    "tahoma": ["DejaVu Sans", "Liberation Sans"],
    "georgia": ["Gelasio", "Tinos", "Liberation Serif"],
    "palatino": ["P052", "URW Palladio L", "Liberation Serif"],
    "bookman": ["URW Bookman", "P052"],
}


def squash(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


@dataclass
class Face:
    path: str
    offset: int  # offset of the sfnt table directory (non-zero inside .ttc collections)
    family: str
    subfamily: str
    weight: int
    italic: bool

    @property
    def bold(self) -> bool:
        return self.weight >= 600


@dataclass
class Family:
    name: str
    faces: list[Face] = field(default_factory=list)

    def pick(self, bold: bool, italic: bool) -> Face | None:
        if not self.faces:
            return None
        target = 700 if bold else 400

        def score(face: Face) -> float:
            s = abs(face.weight - target)
            if face.italic != italic:
                s += 1000
            if "condensed" in face.subfamily.lower() or "narrow" in face.subfamily.lower():
                s += 300
            return s

        return min(self.faces, key=score)

    def has_style(self, bold: bool, italic: bool) -> bool:
        face = self.pick(bold, italic)
        return bool(face and face.bold == bold and face.italic == italic)


def _read_exact(f, offset: int, length: int) -> bytes:
    f.seek(offset)
    data = f.read(length)
    if len(data) != length:
        raise ValueError("truncated font file")
    return data


def _directory(f, offset: int) -> tuple[int, dict[bytes, tuple[int, int]]]:
    version, num = struct.unpack(">IH", _read_exact(f, offset, 6))
    if num > 200:
        raise ValueError("implausible table count")
    raw = _read_exact(f, offset + 12, 16 * num)
    tables = {}
    for i in range(num):
        tag, _, t_off, t_len = struct.unpack_from(">4sIII", raw, i * 16)
        tables[tag] = (t_off, t_len)
    return version, tables


def _decode_name(platform: int, data: bytes) -> str:
    if platform in (0, 3):
        return data.decode("utf-16-be", "replace")
    return data.decode("mac_roman", "replace")


def _names(name_table: bytes) -> dict[int, str]:
    _, count, str_off = struct.unpack_from(">HHH", name_table, 0)
    best: dict[int, tuple[int, str]] = {}
    for i in range(count):
        pid, eid, lid, nid, length, off = struct.unpack_from(">HHHHHH", name_table, 6 + 12 * i)
        if nid not in (1, 2, 4, 6, 16, 17):
            continue
        start = str_off + off
        raw = name_table[start:start + length]
        if pid == 3 and lid == 0x409:
            rank = 0
        elif pid == 3:
            rank = 2
        elif pid == 1 and lid == 0:
            rank = 1
        elif pid == 0:
            rank = 3
        else:
            continue
        text = _decode_name(pid, raw).strip("\0 ")
        if text and (nid not in best or rank < best[nid][0]):
            best[nid] = (rank, text)
    return {k: v[1] for k, v in best.items()}


def _read_face(f, path: str, offset: int) -> Face | None:
    version, tables = _directory(f, offset)
    if version not in (0x00010000, 0x4F54544F, 0x74727565):
        return None
    if b"name" not in tables or (b"glyf" not in tables and b"CFF " not in tables):
        return None  # bitmap-only / AAT-only fonts are not writable
    n_off, n_len = tables[b"name"]
    names = _names(_read_exact(f, n_off, min(n_len, 1 << 20)))
    family = names.get(16) or names.get(1)
    subfamily = names.get(17) or names.get(2) or "Regular"
    if not family or family.startswith(".") or "emoji" in family.lower():
        return None
    weight, italic = 400, False
    if b"OS/2" in tables:
        o_off, o_len = tables[b"OS/2"]
        if o_len >= 64:
            os2 = _read_exact(f, o_off, 64)
            fs_type = struct.unpack_from(">H", os2, 8)[0]
            if fs_type & 0x0002:
                return None  # "restricted license": the font may not be embedded in documents
            weight = struct.unpack_from(">H", os2, 4)[0] or 400
            fs_sel = struct.unpack_from(">H", os2, 62)[0]
            italic = bool(fs_sel & 1)
            if fs_sel & 0x20 and weight < 600:
                weight = 700
    if b"head" in tables:
        h_off, h_len = tables[b"head"]
        if h_len >= 46:
            mac_style = struct.unpack_from(">H", _read_exact(f, h_off, 46), 44)[0]
            italic = italic or bool(mac_style & 2)
            if mac_style & 1 and weight < 600:
                weight = 700
    sub = subfamily.lower()
    if "italic" in sub or "oblique" in sub:
        italic = True
    if weight < 600 and re.search(r"\b(bold|black|heavy)\b", sub):
        weight = 700
    return Face(path=path, offset=offset, family=family, subfamily=subfamily, weight=weight, italic=italic)


class SystemFonts:
    """Lazily scans installed fonts; thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._families: dict[str, Family] | None = None
        self._loaded: dict[tuple[str, int], fitz.Font | None] = {}

    def start_background_scan(self) -> None:
        threading.Thread(target=lambda: self.families, daemon=True).start()

    @property
    def families(self) -> dict[str, Family]:
        with self._lock:
            if self._families is None:
                self._families = self._scan()
            return self._families

    def _scan(self) -> dict[str, Family]:
        families: dict[str, Family] = {}
        seen: set[str] = set()
        for base in FONT_DIRS:
            if not base or not os.path.isdir(base):
                continue
            for root, _dirs, files in os.walk(base):
                for fn in files:
                    ext = fn.lower().rsplit(".", 1)[-1]
                    if ext not in ("ttf", "otf", "ttc"):
                        continue
                    path = os.path.join(root, fn)
                    real = os.path.realpath(path)
                    if real in seen:
                        continue
                    seen.add(real)
                    try:
                        with open(path, "rb") as f:
                            tag = f.read(4)
                            if tag == b"ttcf":
                                f.seek(8)
                                count = struct.unpack(">I", f.read(4))[0]
                                offsets = struct.unpack(f">{min(count, 64)}I", f.read(4 * min(count, 64)))
                            else:
                                offsets = (0,)
                            for off in offsets:
                                face = _read_face(f, path, off)
                                if face:
                                    key = squash(face.family)
                                    families.setdefault(key, Family(face.family)).faces.append(face)
                    except Exception:
                        continue
        return families

    # ----------------------------------------------------------------- lookup
    def family(self, name: str) -> Family | None:
        return self.families.get(squash(name))

    def ui_list(self) -> list[dict]:
        fams = self.families
        common = []
        for name in COMMON_FAMILIES:
            fam = fams.get(squash(name))
            if fam and fam.pick(False, False):
                common.append(fam.name)
        others = sorted(
            (f.name for k, f in fams.items() if f.name not in common and f.pick(False, False)),
            key=str.lower,
        )
        return [{"family": n, "common": True} for n in common] + [{"family": n, "common": False} for n in others]

    def load(self, face: Face) -> fitz.Font | None:
        key = (face.path, face.offset)
        with self._lock:
            if key in self._loaded:
                return self._loaded[key]
        font = None
        try:
            if face.offset == 0 and not face.path.lower().endswith(".ttc"):
                font = fitz.Font(fontfile=face.path)
            else:
                font = fitz.Font(fontbuffer=self._extract_face(face))
            if not can_embed(font):
                font = None
        except Exception:
            font = None
        with self._lock:
            self._loaded[key] = font
        return font

    @staticmethod
    def _extract_face(face: Face) -> bytes:
        with open(face.path, "rb") as f:
            version, tables = _directory(f, face.offset)
            data = {tag: _read_exact(f, off, ln) for tag, (off, ln) in tables.items()}
        return build_sfnt(version, data)

    def font_for(self, family: str, bold: bool, italic: bool) -> tuple[fitz.Font, bool, bool] | None:
        """Returns (font, needs_fake_bold, needs_fake_italic)."""
        fam = self.family(family)
        if not fam:
            return None
        face = fam.pick(bold, italic)
        if not face:
            return None
        font = self.load(face)
        if not font:
            return None
        return font, bold and not face.bold, italic and not face.italic


def can_embed(font: fitz.Font) -> bool:
    """True if MuPDF can write this font into a PDF.

    Fonts whose licence forbids embedding (the OS/2 fsType bits) are refused with
    "substitute font creation is not implemented yet", so ask before choosing one.
    """
    try:
        if not font.is_writable or font.flags.get("never-embed"):
            return False
    except Exception:
        return False
    return _renders(font)


def _renders(font: fitz.Font) -> bool:
    """True if text in this font survives being written into a PDF and rendered."""
    try:
        doc = fitz.open()
        page = doc.new_page(width=60, height=30)
        tw = fitz.TextWriter(page.rect)
        tw.append((5, 20), "Ag", font=font, fontsize=12)
        tw.write_text(page)
        page.get_pixmap(dpi=36)
        return True
    except Exception:
        return False


SYSTEM_FONTS = SystemFonts()

_BASE14_CACHE: dict[str, fitz.Font] = {}


def base14(kind: str, bold: bool, italic: bool) -> fitz.Font:
    code = BASE14[(kind, bold, italic)]
    if code not in _BASE14_CACHE:
        _BASE14_CACHE[code] = fitz.Font(code)
    return _BASE14_CACHE[code]


_STYLE_RE = re.compile(
    r"(bold|black|heavy|semibold|demibold|extrabold|ultrabold|medium|light|thin|regular|roman|book|"
    r"italic|oblique|it|condensed|narrow|mt|psmt|ps|std|pro|lt|w\d|\d+)$",
    re.I,
)


def parse_pdf_font_name(name: str) -> dict:
    """Split a PDF BaseFont name into family candidates and style hints."""
    raw = name or ""
    if re.match(r"^[A-Z]{6}\+", raw):
        raw = raw[7:]
    lowered = raw.lower()
    bold = bool(re.search(r"bold|black|heavy|semibold|demi", lowered))
    italic = bool(re.search(r"italic|oblique|inclined|slanted|kursiv", lowered))
    base = re.split(r"[-,]", raw, maxsplit=1)[0]
    # split trailing style words glued to the family ("ArialBold", "Calibri Light")
    candidates: list[str] = []
    for text in (raw, base):
        key = squash(text)
        while key and key not in candidates:
            candidates.append(key)
            stripped = _STYLE_RE.sub("", key)
            if stripped == key:
                break
            key = stripped
    return {"candidates": candidates, "bold": bold, "italic": italic, "display": base.strip() or raw}


def css_family_for(name: str, serif: bool, mono: bool) -> str:
    """Best-effort CSS family name for previewing a PDF font in the browser."""
    info = parse_pdf_font_name(name)
    fams = SYSTEM_FONTS.families
    for cand in info["candidates"]:
        fam = fams.get(cand)
        if fam:
            return fam.name
    for cand in info["candidates"]:
        for sub in SUBSTITUTES.get(cand, []):
            fam = fams.get(squash(sub))
            if fam:
                return fam.name
    generic = "mono" if mono else "serif" if serif else "sans"
    for fam_name in GENERIC_FAMILIES[generic]:
        if squash(fam_name) in fams:
            return fams[squash(fam_name)].name
    return {"mono": "monospace", "serif": "serif", "sans": "sans-serif"}[generic]
