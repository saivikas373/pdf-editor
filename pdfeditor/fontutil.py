"""Low-level helpers for fonts extracted from PDFs.

Subset fonts embedded in PDFs frequently have no usable Unicode ``cmap``
(glyphs are addressed by CID/GID only).  To re-use such a font when editing
text we rebuild a small ``cmap`` table from the character -> glyph mapping we
observe on the page, which lets MuPDF both draw the glyphs and emit a correct
ToUnicode map (so edited text stays searchable / copyable / re-editable).
"""

from __future__ import annotations

import struct

_HEADER = struct.Struct(">IHHHH")
_RECORD = struct.Struct(">4sIII")


def parse_sfnt(buf: bytes) -> tuple[int, dict[bytes, bytes]] | None:
    """Return (sfnt_version, {tag: table_bytes}) or None if not an sfnt font."""
    if not buf or len(buf) < 12:
        return None
    try:
        version, num_tables, _, _, _ = _HEADER.unpack_from(buf, 0)
    except struct.error:
        return None
    if version not in (0x00010000, 0x4F54544F, 0x74727565):  # 1.0, 'OTTO', 'true'
        return None
    tables: dict[bytes, bytes] = {}
    for i in range(num_tables):
        off = 12 + i * 16
        if off + 16 > len(buf):
            return None
        tag, _, t_off, t_len = _RECORD.unpack_from(buf, off)
        if t_off + t_len > len(buf):
            # tolerate truncated trailing padding
            t_len = max(0, len(buf) - t_off)
        tables[tag] = bytes(buf[t_off:t_off + t_len])
    return version, tables


def _checksum(data: bytes) -> int:
    pad = (-len(data)) % 4
    data = data + b"\0" * pad
    total = 0
    for (word,) in struct.iter_unpack(">I", data):
        total = (total + word) & 0xFFFFFFFF
    return total


def build_sfnt(version: int, tables: dict[bytes, bytes]) -> bytes:
    tags = sorted(tables)
    n = len(tags)
    entry_selector = max(0, n.bit_length() - 1)
    search_range = (1 << entry_selector) * 16
    range_shift = n * 16 - search_range
    header = _HEADER.pack(version, n, search_range, entry_selector, range_shift)
    offset = 12 + 16 * n
    records = []
    body = []
    for tag in tags:
        data = tables[tag]
        if tag == b"head" and len(data) >= 12:
            data = data[:8] + b"\0\0\0\0" + data[12:]  # checkSumAdjustment is recomputed below
            tables[tag] = data
        records.append(_RECORD.pack(tag, _checksum(data), offset, len(data)))
        padded = data + b"\0" * ((-len(data)) % 4)
        body.append(padded)
        offset += len(padded)
    font = bytearray(header + b"".join(records) + b"".join(body))
    if b"head" in tables:
        adjustment = (0xB1B0AFBA - _checksum(bytes(font))) & 0xFFFFFFFF
        # locate head table offset
        for i, tag in enumerate(tags):
            if tag == b"head":
                _, _, h_off, h_len = _RECORD.unpack_from(font, 12 + i * 16)
                if h_len >= 12:
                    struct.pack_into(">I", font, h_off + 8, adjustment)
                break
    return bytes(font)


def _cmap_format4(mapping: dict[int, int]) -> bytes:
    bmp = sorted((c, g) for c, g in mapping.items() if c < 0xFFFF and 0 < g < 0xFFFF)
    segments: list[list[int]] = []  # [start, end, delta]
    for c, g in bmp:
        delta = (g - c) & 0xFFFF
        if segments and segments[-1][1] == c - 1 and segments[-1][2] == delta:
            segments[-1][1] = c
        else:
            segments.append([c, c, delta])
    segments = segments[:8000]
    segments.append([0xFFFF, 0xFFFF, 1])
    seg_count = len(segments)
    entry_selector = max(0, seg_count.bit_length() - 1)
    search_range = 2 * (1 << entry_selector)
    range_shift = 2 * seg_count - search_range
    length = 16 + 8 * seg_count
    out = struct.pack(">HHHHHHH", 4, length, 0, seg_count * 2, search_range, entry_selector, range_shift)
    out += b"".join(struct.pack(">H", s[1]) for s in segments)
    out += b"\0\0"
    out += b"".join(struct.pack(">H", s[0]) for s in segments)
    out += b"".join(struct.pack(">H", s[2]) for s in segments)
    out += b"\0\0" * seg_count
    return out


def _cmap_format12(mapping: dict[int, int]) -> bytes:
    groups: list[list[int]] = []  # [start, end, start_gid]
    for c, g in sorted(mapping.items()):
        if g <= 0:
            continue
        if groups and groups[-1][1] == c - 1 and groups[-1][2] + (c - groups[-1][0]) == g:
            groups[-1][1] = c
        else:
            groups.append([c, c, g])
    out = struct.pack(">HHIII", 12, 0, 16 + 12 * len(groups), 0, len(groups))
    out += b"".join(struct.pack(">III", *grp) for grp in groups)
    return out


def make_cmap(mapping: dict[int, int]) -> bytes:
    f4 = _cmap_format4(mapping)
    need12 = any(c >= 0xFFFF for c in mapping)
    subtables = [(0, 3, f4), (3, 1, f4)]
    if need12:
        f12 = _cmap_format12(mapping)
        subtables = [(0, 3, f4), (0, 4, f12), (3, 1, f4), (3, 10, f12)]
    header = struct.pack(">HH", 0, len(subtables))
    offset = 4 + 8 * len(subtables)
    records = b""
    blobs = b""
    placed: dict[int, int] = {}
    for platform, encoding, blob in subtables:
        key = id(blob)
        if key not in placed:
            placed[key] = offset + len(blobs)
            blobs += blob
        records += struct.pack(">HHI", platform, encoding, placed[key])
    return header + records + blobs


def patch_cmap(buf: bytes, mapping: dict[int, int]) -> bytes | None:
    """Return a copy of an sfnt font whose cmap is exactly ``mapping``."""
    parsed = parse_sfnt(buf)
    if not parsed or not mapping:
        return None
    version, tables = parsed
    if b"CFF " not in tables and b"glyf" not in tables and b"CFF2" not in tables:
        return None
    tables = dict(tables)
    tables[b"cmap"] = make_cmap(mapping)
    return build_sfnt(version, tables)


class GlyphTable:
    """Answers "does glyph N have an outline?" for TrueType (glyf/loca) fonts."""

    def __init__(self, buf: bytes):
        self.loca: list[int] | None = None
        parsed = parse_sfnt(buf)
        if not parsed:
            return
        _, tables = parsed
        head, loca = tables.get(b"head"), tables.get(b"loca")
        if not head or not loca or len(head) < 54:
            return
        long_format = struct.unpack_from(">h", head, 50)[0] == 1
        if long_format:
            self.loca = [v for (v,) in struct.iter_unpack(">I", loca[: len(loca) // 4 * 4])]
        else:
            self.loca = [v * 2 for (v,) in struct.iter_unpack(">H", loca[: len(loca) // 2 * 2])]

    def has_outline(self, gid: int) -> bool | None:
        if self.loca is None:
            return None  # unknown (e.g. CFF based font)
        if gid < 0 or gid + 1 >= len(self.loca):
            return False
        return self.loca[gid + 1] > self.loca[gid]
