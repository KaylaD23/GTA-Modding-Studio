"""Native GTA VCS Stories menu/font compressed-XET resource discovery.

The VCS header holds independent xet containers at 0xE0 (menu) and 0xFC
(fonts), unlike the LCS CHK streams at 0xD4/0xDC. Named records point to
native GS raster descriptors; all decoded offsets are relative to the XET,
not to the outer GATG. This module deliberately requires the original linked
node, matching dimensions/format and non-overlapping pixel/CLUT spans.
"""
from __future__ import annotations

import bisect
import re
import struct
import zlib

_NAMED = re.compile(rb'[A-Za-z_][A-Za-z0-9_]{1,51}\Z')
_MAX_XET = 16_000_000


def _word(data: bytes, pos: int) -> int:
    return struct.unpack_from('<I', data, pos)[0]


def read_vcs_xet(gatg: bytes, header_field: int, *, expected_name: bytes):
    """Return (offset, compressed length, decompressed bytes), if recognised."""
    if gatg[:4] != b'GATG' or header_field + 4 > len(gatg):
        return None
    offset = _word(gatg, header_field)
    if not (0x100 <= offset < len(gatg)-2) or gatg[offset:offset+2] not in (b'\x78\x01', b'\x78\x9c', b'\x78\xda'):
        return None
    try:
        decomp = zlib.decompressobj()
        raw = decomp.decompress(gatg[offset:], _MAX_XET + 1)
        if len(raw) > _MAX_XET or not decomp.eof or raw[:4] != b'xet\0' or expected_name not in raw:
            return None
        consumed = len(gatg) - offset - len(decomp.unused_data)
        if not (20 < consumed < 2_000_000):
            return None
        return offset, consumed, raw
    except zlib.error:
        return None


def read_named_xet_textures(raw: bytes, *, group: str):
    """Decode supported named VCS PS2 XET images; return (textures, unsupported).

    A valid name record has a descriptor pointer 16 bytes before its name,
    followed by the native 0x20 link marker. The descriptor describes image
    bytes, width, format and GS CLUT. Allocation boundaries are checked against
    the next known raster pointer, never guessed from neighbouring text data.
    """
    from texture_formats import Texture
    candidates = []
    unsupported = []
    for name_at in range(0x30, len(raw)-0x50, 0x10):
        name = raw[name_at:name_at+52].split(b'\0', 1)[0]
        if not _NAMED.fullmatch(name):
            continue
        descriptor = _word(raw, name_at-0x10)
        if _word(raw, name_at-0x0c) != 0x20 or not (0x20 <= descriptor <= len(raw)-16):
            continue
        v0, flags, pixel, fmt = struct.unpack_from('<4I', raw, descriptor)
        if v0 != 0 or flags >> 16 not in (0x25, 0x45) or pixel >= len(raw):
            continue
        label = name.decode('ascii')
        code = (fmt >> 8) & 0xff
        depth = 4 if code in (0x40, 0x41, 0x42) else 8 if code in (0x80, 0x81, 0x82) else None
        exponent = fmt & 15
        if depth is None or not 3 <= exponent <= 10:
            unsupported.append({'name': label, 'reason': 'unknown GS format', 'descriptor': descriptor})
            continue
        width = 1 << exponent
        if flags & 0xffff != width * depth // 8:
            unsupported.append({'name': label, 'reason': 'descriptor row-pitch mismatch', 'descriptor': descriptor})
            continue
        candidates.append((label, descriptor, pixel, fmt, width, depth))

    pointers = sorted({v[2] for v in candidates})
    valid = []
    used_spans = []
    for name, descriptor, pixel, fmt, width, depth in candidates:
        following = bisect.bisect_right(pointers, pixel)
        next_pixel = pointers[following] if following < len(pointers) else len(raw)
        code = (fmt >> 4) & 15
        palette_size = (16 if depth == 4 else 256) * 4
        choices = []
        for exp in range(3, 11):
            if ((exp - 4) % 4) * 4 != code:
                continue
            height = 1 << exp
            pixel_size = width * height * depth // 8
            end = pixel + pixel_size + palette_size
            if pixel < 0x40 or end > next_pixel or end > len(raw):
                continue
            gap = next_pixel - end
            # A section can contain unrelated padding, but pixels and the
            # CLUT must be complete before the next texture begins.
            choices.append((gap, abs(exp - (width.bit_length()-1)), height, pixel_size))
        if not choices:
            unsupported.append({'name': name, 'reason': 'unsupported size/allocation', 'descriptor': descriptor})
            continue
        _, _, height, pixel_size = min(choices)
        span = (pixel, pixel + pixel_size + palette_size)
        if any(a < span[1] and span[0] < b for a,b in used_spans):
            unsupported.append({'name': name, 'reason': 'overlapping raster', 'descriptor': descriptor})
            continue
        used_spans.append(span)
        tex = Texture(name, width, height, depth, pixel, pixel_size,
                      pixel+pixel_size, palette_size, 'dtz', False, depth == 8, fmt,
                      note=f'{group} XET: node descriptor 0x{descriptor:X}, pixel 0x{pixel:X}; in-game compatibility not verified',
                      platform='PS2')
        valid.append(tex)
    return valid, unsupported
