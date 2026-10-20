"""GATG indexed section catalog for all four GTA Stories PS2/PSP GAME.DTZs.

Discover group-member records independently of raster decoding. Different
Stories builds use both old type-2 and new native node layouts. A name being
indexed never implies that its pixels are understood or safely writable.
"""
from __future__ import annotations
import re
import struct

SECTION_NAMES = ('hud', 'generic', 'particle')
_NAME_RE = re.compile(rb'[A-Za-z_][A-Za-z0-9_]{1,51}\Z')


def u32(data: bytes, at: int) -> int:
    return struct.unpack_from('<I', data, at)[0]


def inspect_indexed_sections(data: bytes) -> dict[str, list[dict]]:
    """Enumerate name records validated by their parent section pointer.

    Index rows are stable across mixed raster formats.  They must remain
    inspection-only unless decoded separately by a format-specific parser.
    """
    if not data.startswith(b'GATG'):
        return {}
    sections = {}
    for section in SECTION_NAMES:
        marker = section.encode('ascii') + b'\0'
        cursor = 0
        group_ptr = None
        while True:
            idx = data.find(marker, cursor)
            if idx < 0:
                break
            cursor = idx + 1
            if idx < 8 or u32(data, idx - 4) != 1:
                continue
            candidate = u32(data, idx - 8)
            # Every supported directory starts with an 0x06 record marker.
            if 0x100 < candidate < len(data) - 0x54 and data[candidate] == 0x06:
                group_ptr = candidate
                break
        if group_ptr is None:
            continue
        needle = struct.pack('<I', group_ptr)
        result = []
        seen = set()
        cursor = 0
        while True:
            hit = data.find(needle, cursor)
            if hit < 0:
                break
            cursor = hit + 4
            node = hit - 0x14
            if node < 0 or node + 0x54 > len(data) or node in seen:
                continue
            name_bytes = data[node + 0x20:node + 0x54].split(b'\0', 1)[0]
            if not _NAME_RE.fullmatch(name_bytes):
                continue
            descriptor = u32(data, node + 0x10)
            if not (0x100 < descriptor < len(data) - 0x10):
                continue
            # Require both references surrounding the group reference to be
            # valid in-file links. This excludes fake matches in directories,
            # strings or raster payloads without requiring one raster format.
            left = u32(data, node + 0x18)
            right = u32(data, node + 0x1c)
            if not all(v == 0 or 0x100 < v < len(data) for v in (left, right)):
                continue
            seen.add(node)
            record_type = u32(data, node + 0x0c)
            layout = 'type2' if record_type == 2 else 'alternate native'
            result.append({
                'name': name_bytes.decode('ascii'),
                'record_offset': node,
                'asset_pointer': descriptor,
                'section': section,
                'record_type': record_type,
                'node_layout': layout,
                'format_status': 'indexed only; image format needs validation',
            })
        result.sort(key=lambda r: r['record_offset'])
        sections[section] = result
    return sections


def section_filter_items(texture_entries, raw_sections, *, selected='All sections'):
    if selected == 'All sections':
        return [('texture', i, grp, tex.name) for i, (grp, tex) in enumerate(texture_entries)] + [
            ('catalog', i, grp, rec['name']) for grp, items in raw_sections.items() for i, rec in enumerate(items)]
    return [('texture', i, grp, tex.name) for i, (grp, tex) in enumerate(texture_entries) if grp == selected] + [
        ('catalog', i, selected, rec['name']) for i, rec in enumerate(raw_sections.get(selected, []))]
