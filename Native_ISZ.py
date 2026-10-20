"""Native, unencrypted ISZ v1 writer and streaming round-trip checker.

Based on the publicly published EZB Systems ISZ format specification, 2006.
Creates a single-part image with zlib, stored, or zero chunks; no external
converter is invoked to *create* the ISZ.  This is not an encryption codec.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import struct
import tempfile
import zlib
from typing import Callable

SECTOR_SIZE = 2048
HEADER = struct.Struct('<4sBBIHIBqIIBbIIIBIIII')  # 64-byte ISZ v1 header
SIGNATURE = b'IsZ!'
XOR_KEY = bytes((0xB6, 0x8C, 0xA5, 0xDE))
CHUNK_ZEROS, CHUNK_RAW, CHUNK_ZLIB, CHUNK_BZIP2 = range(4)
DEFAULT_BLOCK_SIZE = 1024 * 1024
MAX_PTR_SIZE = 0x3FFFFF


def _xor_table(table: bytes) -> bytes:
    return bytes(value ^ XOR_KEY[index & 3] for index, value in enumerate(table))


def _header(fields: tuple) -> bytes:
    return HEADER.pack(*fields)


def inspect_isz(source: Path) -> dict:
    """Inspect a single-segment ISZ without decompressing it."""
    source = Path(source)
    with source.open('rb') as handle:
        header = handle.read(HEADER.size)
    if len(header) != HEADER.size:
        raise ValueError('Truncated ISZ header.')
    fields = HEADER.unpack(header)
    signature, header_size, version, serial, sector_size, sectors, encryption, segment_size, blocks, block_size, ptr_len, seg_num, ptr_offs, seg_offs, data_offs, reserved, checksum1, size1, unknown2, checksum2 = fields
    if signature != SIGNATURE or version != 1 or header_size != 64:
        raise ValueError('This is not a supported ISZ v1 image with a 64-byte header.')
    if encryption:
        raise ValueError(f'This ISZ uses encryption/password mode {encryption}; encrypted ISZ is not supported.')
    if sector_size != SECTOR_SIZE or not sectors:
        raise ValueError('ISZ image has an unsupported sector size or no sectors.')
    return {'sectors': sectors, 'bytes': sectors * sector_size, 'blocks': blocks, 'block_size': block_size,
            'pointer_length': ptr_len, 'ptr_offs': ptr_offs, 'seg_offs': seg_offs,
            'data_offs': data_offs, 'segment_number': seg_num, 'encrypted': False,
            'compressed_crc': checksum2, 'uncompressed_crc': checksum1}


def encode_isz(iso_path: Path, output_path: Path, *, block_size: int = DEFAULT_BLOCK_SIZE,
               overwrite: bool = False, progress: Callable[[str], None] | None = None) -> dict:
    """Compress a sector-aligned ISO into a single-file ISZ v1, atomically.

    Any existing output is left untouched if encoding or verification fails.
    The caller should call verify_isz(...) before publishing the result when
    extra protection is needed; this routine verifies the output itself too.
    """
    iso_path, output_path = Path(iso_path).resolve(), Path(output_path).resolve()
    if not iso_path.is_file() or iso_path == output_path:
        raise ValueError('Choose an existing source ISO and a different ISZ output path.')
    if output_path.suffix.lower() != '.isz':
        raise ValueError('ISZ output must have a .isz extension.')
    size = iso_path.stat().st_size
    if size == 0 or size % SECTOR_SIZE:
        raise ValueError('ISZ encoder requires a non-empty ISO with 2048-byte sector alignment.')
    if size // SECTOR_SIZE > 0xFFFFFFFF:
        raise ValueError('ISO exceeds the 32-bit sector count supported by ISZ v1.')
    if block_size < SECTOR_SIZE or block_size % SECTOR_SIZE or block_size > MAX_PTR_SIZE:
        raise ValueError('ISZ chunk size must be a 2048-byte multiple between 2048 and 4194303.')
    if output_path.exists() and not overwrite:
        raise FileExistsError(f'Output already exists: {output_path}')
    nblocks = (size + block_size - 1) // block_size
    if nblocks > 0xFFFFFFFF or HEADER.size + nblocks * 3 > 0xFFFFFFFF:
        raise ValueError('ISZ image requires a chunk table that exceeds v1 format limits.')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    original_crc = 0
    compressed_crc = 0
    pointer_entries = bytearray()
    count_zlib = count_raw = count_zero = 0
    tmp = None
    try:
        with tempfile.NamedTemporaryFile('w+b', prefix='.gta_isz_', suffix='.tmp', dir=output_path.parent, delete=False) as dest:
            tmp = Path(dest.name)
            table_offset = HEADER.size
            data_offset = table_offset + nblocks * 3
            dest.write(b'\x00' * HEADER.size)
            # Allocate table area on disk without holding it in memory first.
            dest.seek(data_offset)
            with iso_path.open('rb') as original:
                for idx in range(nblocks):
                    raw = original.read(block_size)
                    if not raw:
                        raise IOError('ISO became shorter while compressing.')
                    digest.update(raw)
                    original_crc = zlib.crc32(raw, original_crc)
                    if not any(raw):
                        flag, stored = CHUNK_ZEROS, b''
                        length_field = len(raw)
                        count_zero += 1
                    else:
                        compressed = zlib.compress(raw, level=6)
                        if len(compressed) < len(raw):
                            flag, stored = CHUNK_ZLIB, compressed
                            count_zlib += 1
                        else:
                            flag, stored = CHUNK_RAW, raw
                            count_raw += 1
                        length_field = len(stored)
                        compressed_crc = zlib.crc32(stored, compressed_crc)
                        dest.write(stored)
                    entry = (flag << 22) | length_field
                    pointer_entries.extend(entry.to_bytes(3, 'little'))
                    if progress and (idx % max(1, nblocks // 20) == 0 or idx + 1 == nblocks):
                        progress(f'ISZ compression: {idx + 1:,}/{nblocks:,} chunks')
                if original.read(1):
                    raise IOError('ISO grew during compression; no output published.')
            # ISZ stores one's-complement CRC32s for the decompressed byte stream
            # and for the concatenated non-zero stored chunks.
            original_crc = (~original_crc) & 0xFFFFFFFF
            compressed_crc = (~compressed_crc) & 0xFFFFFFFF
            fields = (SIGNATURE, 64, 1, original_crc, SECTOR_SIZE, size // SECTOR_SIZE,
                      0, 0, nblocks, block_size, 3, 0, table_offset, 0, data_offset, 0,
                      original_crc, min(size, 0xFFFFFFFF), 0, compressed_crc)
            dest.seek(0)
            dest.write(_header(fields))
            dest.seek(table_offset)
            dest.write(_xor_table(pointer_entries))
            dest.flush()
            os.fsync(dest.fileno())
        # Self-verification decodes every block independently before publishing.
        verified = verify_isz(tmp, expected_sha256=digest.hexdigest(), progress=progress)
        if output_path.exists() and not overwrite:
            raise FileExistsError(f'Output appeared during compression: {output_path}')
        os.replace(tmp, output_path)
        tmp = None
        return {'output': output_path, 'bytes': output_path.stat().st_size, 'iso_bytes': size,
                'sha256': digest.hexdigest(), 'blocks': nblocks, 'zlib_blocks': count_zlib,
                'stored_blocks': count_raw, 'zero_blocks': count_zero, 'verified': verified}
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def verify_isz(path: Path, *, expected_sha256: str | None = None,
               progress: Callable[[str], None] | None = None) -> dict:
    """Verify a single-segment ISZ and hash its restored ISO stream in memory.

    This also rejects unsupported segmented/encrypted/bzip2 input rather than
    guessing its boundaries. Input *decoder* for general ISZ images remains
    isz-tool.py; this verifier is deliberately scoped to single-part v1.
    """
    path = Path(path)
    details = inspect_isz(path)
    if details['seg_offs'] or details['segment_number'] or details['pointer_length'] != 3:
        raise ValueError('ISZ verification supports single-part images with 3-byte chunk entries.')
    blocks, block_size = details['blocks'], details['block_size']
    if (not blocks or block_size < SECTOR_SIZE or block_size > MAX_PTR_SIZE
            or block_size % SECTOR_SIZE
            or blocks != (details['bytes'] + block_size - 1) // block_size):
        raise ValueError('Invalid ISZ block count, block size, or image length.')
    table_size = blocks * 3
    file_size = path.stat().st_size
    if details['ptr_offs'] < HEADER.size or details['ptr_offs'] + table_size > file_size:
        raise ValueError('Invalid ISZ chunk table bounds.')
    if details['data_offs'] < details['ptr_offs'] + table_size or details['data_offs'] > file_size:
        raise ValueError('Invalid ISZ data offset.')
    with path.open('rb') as handle:
        handle.seek(details['ptr_offs'])
        table = _xor_table(handle.read(table_size))
        handle.seek(details['data_offs'])
        checksum1 = checksum2 = restored_bytes = 0
        digest = hashlib.sha256()
        for idx in range(blocks):
            value = int.from_bytes(table[idx * 3:idx * 3 + 3], 'little')
            mode, length = value >> 22, value & MAX_PTR_SIZE
            expected_len = min(block_size, details['bytes'] - restored_bytes)
            if expected_len <= 0:
                raise ValueError('ISZ has more chunks than its declared uncompressed length.')
            if mode == CHUNK_ZEROS:
                if length != expected_len:
                    raise ValueError('ISZ zero-chunk length mismatch.')
                raw = b'\x00' * length
            else:
                if length == 0 or length > MAX_PTR_SIZE:
                    raise ValueError('Invalid ISZ stored chunk length.')
                data = handle.read(length)
                if len(data) != length:
                    raise ValueError('Truncated ISZ stored chunk.')
                checksum2 = zlib.crc32(data, checksum2)
                if mode == CHUNK_RAW:
                    raw = data
                elif mode == CHUNK_ZLIB:
                    decompress = zlib.decompressobj()
                    raw = decompress.decompress(data, expected_len + 1)
                    if not decompress.eof or decompress.unconsumed_tail or decompress.unused_data:
                        raise ValueError('Invalid or oversized ISZ zlib stream.')
                else:
                    raise ValueError('This ISZ verifier does not implement bzip2 chunks.')
            if len(raw) != expected_len:
                raise ValueError('ISZ chunk expands to an unexpected size.')
            checksum1 = zlib.crc32(raw, checksum1)
            digest.update(raw)
            restored_bytes += len(raw)
            if progress and (idx % max(1, blocks // 20) == 0 or idx + 1 == blocks):
                progress(f'ISZ verification: {idx + 1:,}/{blocks:,} chunks')
        if restored_bytes != details['bytes']:
            raise ValueError('ISZ decoded byte count differs from ISO length.')
        if handle.read(1):
            raise ValueError('ISZ contains unexpected trailing data.')
        if ((~checksum1) & 0xFFFFFFFF) != details['uncompressed_crc']:
            raise ValueError('ISZ uncompressed CRC mismatch.')
        if ((~checksum2) & 0xFFFFFFFF) != details['compressed_crc']:
            raise ValueError('ISZ compressed CRC mismatch.')
        calculated = digest.hexdigest()
        if expected_sha256 is not None and calculated != expected_sha256:
            raise ValueError('ISZ full-stream SHA-256 does not match original ISO.')
        return {'sha256': calculated, 'bytes': restored_bytes, 'blocks': blocks,
                'compressed_crc_ok': True, 'uncompressed_crc_ok': True}
