"""GATG CHK/XET replacement with validated TWO-TABLE relocation.

GATG v1 stores TWO adjacent tables at the archive tail:

  +0x0C: first table start (array of absolute GATG offsets)
  +0x10: second table start (locations of pointer fields)
  +0x14: count of second-table records
  +0x18: duplicate first table start

The second table indexes every pointer field in the first table. Previous
Studio builds incorrectly inserted enlarged CHK streams BETWEEN the tables,
invalidating the required adjacency. This writer inserts them BEFORE BOTH
lists, moves the lists together, and rebases second-list entries that point
at the moved first-list fields. Original asset addresses remain unchanged.

This is validated against supplied original LCS/VCS PS2/PSP GAME.DTZ layouts.
In-game support still requires independent PCSX2/PPSSPP verification.
"""
from __future__ import annotations
import struct
import zlib

GATG_HEADER_SIZE = 0x100
POINTER_FIELDS = {'menu': (0xD4, 0xE0), 'fonts': (0xDC, 0xFC)}
MAX_CHK_COMPRESSED = 8_000_000


def pack_chk(raw: bytes, capacity: int | None = None) -> bytes:
    """Compress a CHK and optionally try extra lossless methods to fit a slot.

    Uses standard zlib by default; optional python-zopfli is tried only when
    ordinary compression cannot meet the existing compressed-slot capacity.
    """
    raw = bytes(raw)
    candidate = []
    for strategy in (zlib.Z_DEFAULT_STRATEGY, zlib.Z_FILTERED,
                     zlib.Z_RLE, zlib.Z_FIXED, zlib.Z_HUFFMAN_ONLY):
        for memlevel in (9, 8):
            encoder = zlib.compressobj(level=9, method=zlib.DEFLATED,
                                       wbits=zlib.MAX_WBITS, memLevel=memlevel,
                                       strategy=strategy)
            candidate.append(encoder.compress(raw) + encoder.flush())
    result = min(candidate, key=len)
    if capacity is not None and len(result) > capacity:
        # Optional third-party DEFLATE compressor; lack of this library is not
        # fatal and must not turn into an installation dependency.
        try:
            import zopfli.zlib as zopfli_zlib
        except ImportError:
            pass
        else:
            better = zopfli_zlib.compress(raw, numiterations=12)
            if len(better) < len(result):
                result = better
    if zlib.decompress(result) != raw:
        raise ValueError('CHK encoder round trip failed')
    return result


def _gatg_tables(data: bytes) -> tuple[int, int, int, tuple[int, ...]]:
    """Read/validate both original GATG offset tables and their relationship."""
    if len(data) < GATG_HEADER_SIZE or data[:4] != b'GATG':
        raise ValueError('Expected decompressed GATG archive')
    version, declared_length, first, second, count, first_copy = struct.unpack_from('<6I', data, 4)
    if version != 1 or declared_length != len(data):
        raise ValueError('GATG version or decompressed archive length is invalid')
    if not (GATG_HEADER_SIZE < first < second < len(data) and
            first % 4 == 0 and second % 4 == 0 and first_copy == first):
        raise ValueError('GATG dual offset-table header is invalid')
    if not (0 < count <= (len(data)-second)//4 and second + count * 4 == len(data)):
        raise ValueError('GATG second relocation table does not cover the archive tail')
    sites = struct.unpack_from(f'<{count}I', data, second)
    if any(site % 4 or not 0 < site < second for site in sites):
        raise ValueError('GATG relocation table points outside the pointer-field data')
    # The second table MUST reference each and every 4-byte entry in the first
    # table once. Otherwise insertion could silently corrupt a foreign format.
    moved_sites = [site for site in sites if site >= first]
    if len(moved_sites) != (second-first)//4 or sorted(moved_sites) != list(range(first, second, 4)):
        raise ValueError('GATG first/second table cross-references are inconsistent')
    return first, second, count, sites


def replace_chk_stream(gatg: bytes, section: dict, packed: bytes) -> tuple[bytes, bool]:
    """Replace active CHK/XET, rebasing BOTH GATG offset tables if needed.

    Existing data is never mutated on failure. The returned Boolean reports
    structural growth (relocation), not an in-game compatibility guarantee.
    """
    data = bytes(gatg)
    packed = bytes(packed)
    if not 0 < len(packed) <= MAX_CHK_COMPRESSED:
        raise ValueError('Invalid or excessively large CHK compressed stream')
    first, second, count, sites = _gatg_tables(data)
    label = section['name']
    start = section['offset']
    capacity = section['compressed_size']
    if (not isinstance(start, int) or not isinstance(capacity, int)
            or not (GATG_HEADER_SIZE <= start < first)
            or not (0 < capacity <= first - start)):
        raise ValueError('CHK allocation is outside the original GATG asset region')
    source_decoder = zlib.decompressobj()
    try:
        original = source_decoder.decompress(data[start:first])
    except zlib.error as exc:
        raise ValueError(f'CHK source is not valid zlib: {exc}') from exc
    consumed = (first-start) - len(source_decoder.unused_data)
    if not source_decoder.eof or consumed != capacity or original != section['raw']:
        raise ValueError('CHK source stream boundaries or contents changed')

    if len(packed) <= capacity:
        out = bytearray(data)
        out[start:start+capacity] = packed.ljust(capacity, b'\0')
        return bytes(out), False

    if label not in POINTER_FIELDS and not label.startswith('XET@'):
        raise ValueError(f'{label} has no verified compressed relocation format')

    fields = [site for site in sites if struct.unpack_from('<I', data, site)[0] == start]
    if len(fields) != 1:
        raise ValueError(f'CHK needs exactly one indexed pointer; found {len(fields)}')
    field = fields[0]
    if label in POINTER_FIELDS:
        if field not in POINTER_FIELDS[label] or (section.get('pointer_field') is not None and field != section['pointer_field']):
            raise ValueError('CHK pointer is not at the verified header location')
    elif not (GATG_HEADER_SIZE <= field < first and
              not (start <= field < start+capacity)):
        raise ValueError('XET pointer field is outside the supported asset region')

    # Insert the new CHK BEFORE the first offset table, not between the first
    # and second tables. The first table's values are unchanged, but every
    # location *within* that first table is shifted; the second table indexes
    # these locations and therefore MUST be rebased for all moved sites.
    insert = packed + b'\0' * (-len(packed) % 4)
    delta = len(insert)
    if len(data)+delta > 0xFFFFFFFF or second+delta > 0xFFFFFFFF:
        raise ValueError('GATG relocated offsets exceed uint32 range')
    out = bytearray(data[:first] + insert + data[first:])
    struct.pack_into('<I', out, 0x08, len(out))
    struct.pack_into('<I', out, 0x0C, first+delta)
    struct.pack_into('<I', out, 0x10, second+delta)
    struct.pack_into('<I', out, 0x18, first+delta)
    struct.pack_into('<I', out, field, first)
    for i,site in enumerate(sites):
        new_site = site+delta if site >= first else site
        struct.pack_into('<I', out, second+delta+4*i, new_site)

    new_first,new_second,new_count,new_sites = _gatg_tables(bytes(out))
    if (new_first, new_second, new_count) != (first+delta, second+delta, count):
        raise ValueError('Relocated GATG metadata did not validate')
    if bytes(out[new_first:new_second]) != data[first:second]:
        raise ValueError('First relocation table was modified')
    if any(b != (a+delta if a >= first else a) for a,b in zip(sites,new_sites)):
        raise ValueError('Relocation field sites were not all rebased')
    return bytes(out), True
