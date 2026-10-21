"""Native PS2 ADPCM volume adjustment for GTA radio assets.

Decodes and *re-encodes* each existing ADPCM frame; NEVER scales the
compressed nibbles. Maintains frame count/size, channel interleave, flags and
GTA SA dual-rate chunk layout. Input remains untouched; caller selects a
separate output path (or stages before replacing extracted track working files).

PSP ATRAC3plus and unidentified SA PAK entries are intentionally unsupported.
"""
from __future__ import annotations

import math
import os
import struct
import tempfile
from pathlib import Path

import PS2_Audio_Encoder as audio
import PS2_VB_Radio as vb

try:
    import numpy as np
    from numba import njit
except ImportError:
    np = None
    njit = None

FILTERS = ((0, 0), (60, 0), (115, -52), (98, -55), (122, -60))


def validated_gain(gain_db: float) -> float:
    """Conservative intentional amplification range; no silent limiting."""
    value = float(gain_db)
    if not math.isfinite(value) or not 0.0 < value <= 12.0:
        raise ValueError('Volume increase must be greater than 0 and at most +12 dB.')
    return value


if njit is not None:
    @njit(cache=True)
    def _boost_channel(src, factor, dec1, dec2, enc1, enc2, flag_mode):
        """Process successive mono 16-byte frames with continuous histories.

        Decode histories follow INPUT ADPCM; encoder histories follow OUTPUT.
        flag_mode 0: must be GTA VB flag 0; mode 1: preserve SA flags.
        """
        out = np.empty(src.size, dtype=np.uint8)
        pcm = np.empty(56, dtype=np.int16)
        best_q = np.empty(28, dtype=np.int64)
        temp_q = np.empty(28, dtype=np.int64)
        clipped = 0
        for offset in range(0, src.size, 16):
            header = int(src[offset])
            predictor = header >> 4
            shift = header & 15
            flag = int(src[offset + 1])
            if predictor > 4 or shift > 12:
                raise ValueError('Invalid PSX ADPCM predictor or shift.')
            if flag_mode == 0 and flag != 0:
                raise ValueError('VB unexpectedly contains a nonzero loop flag.')
            if predictor == 0:
                c1, c2 = 0, 0
            elif predictor == 1:
                c1, c2 = 60, 0
            elif predictor == 2:
                c1, c2 = 115, -52
            elif predictor == 3:
                c1, c2 = 98, -55
            else:
                c1, c2 = 122, -60
            scale = 1 << (12 - shift)
            for i in range(28):
                byte = int(src[offset + 2 + i // 2])
                nibble = (byte & 15) if i % 2 == 0 else (byte >> 4)
                if nibble >= 8:
                    nibble -= 16
                predicted = (int(dec1) * c1 + int(dec2) * c2 + 32) >> 6
                decoded = int(predicted + nibble * scale)
                if decoded > 32767:
                    decoded = 32767
                elif decoded < -32768:
                    decoded = -32768
                dec2, dec1 = dec1, decoded
                boosted = int(round(decoded * factor))
                if boosted > 32767:
                    boosted = 32767
                    clipped += 1
                elif boosted < -32768:
                    boosted = -32768
                    clipped += 1
                pcm[2 * i] = boosted
                pcm[2 * i + 1] = boosted
            enc1, enc2 = audio._encode_channel_frame_numba(
                pcm, 0, 0, enc1, enc2, flag, out, offset, best_q, temp_q
            )
        return out, dec1, dec2, enc1, enc2, clipped


def _require_encoder():
    if np is None or njit is None or audio._encode_channel_frame_numba is None:
        raise RuntimeError('NumPy and Numba are required to safely boost PS2 ADPCM. Run Install_Dependencies.bat.')


def _channel(segment: bytes, factor: float, state: tuple, flag_mode: int) -> tuple[bytes, tuple, int]:
    if not segment:
        return b'', state, 0
    if len(segment) % 16:
        raise ValueError('ADPCM channel contains a partial frame.')
    out, d1, d2, e1, e2, clipped = _boost_channel(
        np.frombuffer(segment, dtype=np.uint8), factor, *state, flag_mode)
    return out.tobytes(), (int(d1), int(d2), int(e1), int(e2)), int(clipped)


def _safe_paths(src: Path, dst: Path):
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        raise FileNotFoundError(src)
    if src.resolve() == dst.resolve():
        raise ValueError('Volume boost requires a separate output file; source is never overwritten.')
    if dst.exists():
        raise FileExistsError(f'Output already exists: {dst}')
    dst.parent.mkdir(parents=True, exist_ok=True)
    return src, dst


def _commit_staged(dst: Path, write):
    """Do not publish any partially re-encoded asset on failure."""
    with tempfile.TemporaryDirectory(prefix='.gta_db_', dir=str(dst.parent)) as folder:
        staged = Path(folder) / 'boosted.tmp'
        result = write(staged)
        # A concurrent writer should not be silently overwritten either.
        if dst.exists():
            raise FileExistsError(f'Output appeared during volume processing: {dst}')
        os.replace(staged, dst)
        return result


def boost_vb(source: Path, output: Path, gain_db: float, *, progress=None) -> dict:
    """Re-encode an already prepared III/VC/LCS/VCS PS2 VB, same physical size."""
    gain_db = validated_gain(gain_db)
    _require_encoder()
    source, output = _safe_paths(source, output)
    size = source.stat().st_size
    if not size or size % vb.STEREO_BLOCK:
        raise ValueError('Expected complete 0x4000-byte interleaved VB blocks.')
    factor = 10.0 ** (gain_db / 20.0)

    def write(tmp):
        states = [(0, 0, 0, 0), (0, 0, 0, 0)]
        clipped = 0
        read_size = 0
        with source.open('rb') as inp, tmp.open('wb') as out:
            while True:
                block = inp.read(vb.STEREO_BLOCK)
                if not block:
                    break
                if len(block) != vb.STEREO_BLOCK:
                    raise ValueError('Truncated VB block.')
                if read_size == 0 and (block[:16] != bytes(16) or block[vb.CHANNEL_BLOCK:vb.CHANNEL_BLOCK+16] != bytes(16)):
                    raise ValueError('This VB lacks the required first silent frame in both channels.')
                left, states[0], clipped_l = _channel(block[:vb.CHANNEL_BLOCK], factor, states[0], 0)
                right, states[1], clipped_r = _channel(block[vb.CHANNEL_BLOCK:], factor, states[1], 0)
                clipped += clipped_l + clipped_r
                if read_size == 0:
                    # PS2 Stories VBs expect 16 literal zero bytes as the
                    # first frame of each channel, not merely decoded silence.
                    left = bytes(16) + left[16:]
                    right = bytes(16) + right[16:]
                out.write(left)
                out.write(right)
                read_size += len(block)
                if progress:
                    progress(read_size / size)
        if tmp.stat().st_size != size:
            raise ValueError('Boosted VB changed physical length.')
        # Preserve the silent first-frame convention from the source.
        with tmp.open('rb') as f:
            if f.read(16) != bytes(16):
                raise ValueError('The VB silent first frame was altered.')
            f.seek(vb.CHANNEL_BLOCK)
            if f.read(16) != bytes(16):
                raise ValueError('The VB right-channel silent first frame was altered.')
        return {'input':str(source),'output':str(output),'gain_db':gain_db,
                'bytes':size,'clipped_pcm_samples':clipped,'format':'PS2 VB 0x2000 stereo interleave'}

    return _commit_staged(output, write)


def boost_gtasa_type4(source: Path, header: bytes, output: Path, gain_db: float, *, progress=None) -> dict:
    """Boost an EXTRACTED SA PS2 type-4 entry.bin, preserving all 4 ADPCM streams.

    Requires matching entry.hdr; no PAK/lookup metadata is changed because
    every encoded frame remains the same length and in the same position.
    """
    gain_db = validated_gain(gain_db)
    _require_encoder()
    source, output = _safe_paths(source, output)
    if len(header) != 0x1F84 or header[:4] != b'\xff'*4 or struct.unpack_from('<I',header,0x1F80)[0] != 4:
        raise ValueError('Only GTA SA PS2 type-4 headers are supported.')
    mini_l,mini_r,full_l,full_r = (struct.unpack_from('<I',header,o)[0] for o in (0x1F40,0x1F48,0x1F50,0x1F58))
    if mini_l != mini_r or full_l != full_r or min(mini_l,full_l) <= 0:
        raise ValueError('Unsupported or invalid GTA SA channel-size metadata.')
    if mini_l % 16 or full_l % 16:
        raise ValueError('GTA SA channel size must be whole ADPCM frames.')
    mini_blocks = (mini_l + audio.GTASA_MINI_CHANNEL_BLOCK - 1) // audio.GTASA_MINI_CHANNEL_BLOCK
    full_blocks = (full_l + audio.GTASA_FULL_CHANNEL_BLOCK - 1) // audio.GTASA_FULL_CHANNEL_BLOCK
    chunks = max(mini_blocks,full_blocks)
    if source.stat().st_size != chunks * audio.GTASA_CHUNK_SIZE:
        raise ValueError('The type-4 physical chunk length disagrees with the header.')
    for off in (0x1F44,0x1F4C):
        if struct.unpack_from('<I',header,off)[0] != audio.GTASA_MINI_RATE:
            raise ValueError('Invalid GTA SA mini stream sample-rate metadata.')
    factor = 10.0 ** (gain_db / 20.0)
    specs = ((0x0000,audio.GTASA_MINI_CHANNEL_BLOCK,mini_l),
             (0x0800,audio.GTASA_MINI_CHANNEL_BLOCK,mini_l),
             (0x1000,audio.GTASA_FULL_CHANNEL_BLOCK,full_l),
             (0x11000,audio.GTASA_FULL_CHANNEL_BLOCK,full_l))

    def write(tmp):
        states = [(0,0,0,0)] * 4
        clipped = 0
        with source.open('rb') as inp, tmp.open('wb') as out:
            for ci in range(chunks):
                block = bytearray(inp.read(audio.GTASA_CHUNK_SIZE))
                if len(block) != audio.GTASA_CHUNK_SIZE:
                    raise ValueError(f'Truncated type-4 payload chunk {ci}.')
                for si,(off,capacity,total) in enumerate(specs):
                    valid = max(0,min(capacity,total-ci*capacity))
                    if not valid:
                        continue
                    segment, states[si], nclip = _channel(bytes(block[off:off+valid]),factor,states[si],1)
                    # Do not touch padding; headers describe actual end position.
                    block[off:off+valid] = segment
                    clipped += nclip
                out.write(block)
                if progress:
                    progress((ci+1)/chunks)
        if tmp.stat().st_size != source.stat().st_size:
            raise ValueError('GTA SA volume change altered PAK entry length.')
        # Checks all frames, flags, rate metadata, chunk lengths and duration.
        checked = audio.validate_gtasa_type4_stream(header,tmp)
        return {'input':str(source),'output':str(output),'gain_db':gain_db,
                'bytes':source.stat().st_size,'clipped_pcm_samples':clipped,
                'format':'GTA SA PS2 type-4 (full + mini, both channels)',
                'validated_chunks':checked['chunk_count']}

    return _commit_staged(output, write)
