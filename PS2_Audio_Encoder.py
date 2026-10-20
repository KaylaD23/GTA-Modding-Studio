#!/usr/bin/env python3
"""Small PS2 SPU ADPCM encoder used by SAAT PS2 Studio.

Source audio is decoded/resampled by FFmpeg into 16-bit stereo PCM. This module
encodes that PCM as interleaved PSX/SPU ADPCM frames for GTA SA PS2 radio stream
payloads (not the PC game's Ogg Vorbis format).
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import wave
import time

try:
    import numpy as np
except ImportError:  # dependency is installed by Install_Dependencies.bat
    np = None

try:
    from numba import njit
except ImportError:  # dependency is installed by Install_Dependencies.bat
    njit = None
from pathlib import Path
from typing import Callable, Optional

VAG_HEADER_SIZE = 0x30
VAG_MAGIC = b"VAGp"
FRAME_SAMPLES = 28
FRAME_BYTES = 16
# PSX SPU predictor coefficients, scaled by 64.
FILTERS = ((0, 0), (60, 0), (115, -52), (98, -55), (122, -60))
SUPPORTED_AUDIO_SUFFIXES = {
    ".wav", ".wave", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a",
    ".aac", ".wma", ".aiff", ".aif", ".ape", ".mp2", ".ac3", ".at3",
    ".at3p", ".caf", ".mp4", ".webm", ".mkv", ".mov",
}


class AudioEncodeError(RuntimeError):
    pass


def find_ffmpeg(preferred: str = "") -> Optional[str]:
    """Find FFmpeg from an explicit path, PATH, or the local installer's tools folder."""
    preferred = preferred.strip().strip('"')
    if preferred and (Path(preferred).is_file() or shutil.which(preferred)):
        return preferred

    # Install_Dependencies.bat installs FFmpeg locally so the app does not require a
    # machine-wide PATH change. Keep a few conventional layouts for portable installs.
    base = Path(__file__).resolve().parent
    candidates = (
        base / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe",
        base / "tools" / "ffmpeg" / "ffmpeg.exe",
        base / "ffmpeg" / "bin" / "ffmpeg.exe",
        base / "ffmpeg" / "ffmpeg.exe",
        base / "tools" / "ffmpeg" / "bin" / "ffmpeg",
        base / "ffmpeg" / "bin" / "ffmpeg",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def _round_div(n: int, d: int) -> int:
    """Round n/d to nearest integer, symmetrically for negative numbers."""
    if n >= 0:
        return (n + d // 2) // d
    return -((-n + d // 2) // d)


def _clip16(x: int) -> int:
    return -32768 if x < -32768 else 32767 if x > 32767 else x


def _encode_frame(samples: list[int], hist1: int, hist2: int, flag: int) -> tuple[bytes, int, int]:
    """Encode exactly 28 mono samples into one 16-byte PSX ADPCM frame."""
    if len(samples) != FRAME_SAMPLES:
        samples = samples + [0] * (FRAME_SAMPLES - len(samples))

    best = None
    # Test each predictor. Estimate the quantiser scale from residuals using the
    # source history, then score with reconstructed output and retained history.
    for filt, (c1, c2) in enumerate(FILTERS):
        residuals = []
        prev2, prev1 = hist2, hist1
        for x in samples:
            pred = (prev1 * c1 + prev2 * c2 + 32) >> 6
            residuals.append(x - pred)
            prev2, prev1 = prev1, x
        peak = max((abs(r) for r in residuals), default=0)
        # PSX ADPCM's decoded nibble scale is 1 << (12 - shift), 1..4096.
        need = max(1, (peak + 6) // 7)
        factor = 1
        while factor < need and factor < 4096:
            factor <<= 1
        factor = min(4096, factor)
        shift = 12 - (factor.bit_length() - 1)

        qvals = []
        recon_hist1, recon_hist2 = hist1, hist2
        error = 0
        for x in samples:
            pred = (recon_hist1 * c1 + recon_hist2 * c2 + 32) >> 6
            q = _round_div(x - pred, factor)
            q = -8 if q < -8 else 7 if q > 7 else q
            decoded = _clip16(pred + q * factor)
            error += (x - decoded) * (x - decoded)
            qvals.append(q)
            recon_hist2, recon_hist1 = recon_hist1, decoded
        candidate = (error, filt, shift, qvals, recon_hist1, recon_hist2)
        if best is None or candidate[0] < best[0]:
            best = candidate

    assert best is not None
    _, filt, shift, qvals, out_h1, out_h2 = best
    frame = bytearray(FRAME_BYTES)
    frame[0] = (filt << 4) | (shift & 0x0F)
    frame[1] = flag & 0xFF
    for i in range(0, FRAME_SAMPLES, 2):
        lo = qvals[i] & 0x0F
        hi = qvals[i + 1] & 0x0F
        frame[2 + i // 2] = lo | (hi << 4)
    return bytes(frame), out_h1, out_h2


def _encode_pcm_frames(left: list[int], right: list[int], histories: list[list[int]], first_frame: bool,
                       last_frame: bool = False) -> bytes:
    """Encode one chunk whose left/right sample arrays have the same length."""
    frame_count = (len(left) + FRAME_SAMPLES - 1) // FRAME_SAMPLES
    out = bytearray(frame_count * FRAME_BYTES * 2)
    out_pos = 0
    for frame_i in range(frame_count):
        a = left[frame_i * FRAME_SAMPLES:(frame_i + 1) * FRAME_SAMPLES]
        b = right[frame_i * FRAME_SAMPLES:(frame_i + 1) * FRAME_SAMPLES]
        # This stream format uses 0x02 for regular blocks and 0x06 for the
        # first block, matching the PS2 radio payload signature observed in the
        # supplied archive. Table length, not a VAGp end marker, ends a stream.
        at_start = first_frame and frame_i == 0
        at_end = last_frame and frame_i == frame_count - 1
        flag_l = 0x07 if at_start and at_end else 0x06 if at_start else 0x03 if at_end else 0x02
        # GTA SA's reference streams use a start/repeat marker (0x06), regular
        # frames (0x02), and an end/repeat marker (0x03) per mono channel.
        flag_r = flag_l
        f_l, histories[0][0], histories[0][1] = _encode_frame(a, histories[0][0], histories[0][1], flag_l)
        f_r, histories[1][0], histories[1][1] = _encode_frame(b, histories[1][0], histories[1][1], flag_r)
        out[out_pos:out_pos + FRAME_BYTES] = f_l
        out[out_pos + FRAME_BYTES:out_pos + 2 * FRAME_BYTES] = f_r
        out_pos += FRAME_BYTES * 2
    return bytes(out)


if njit is not None:
    @njit(cache=True)
    def _encode_channel_frame_numba(samples, group_index, channel, hist1, hist2,
                                    flag, out, out_offset, best_q, temp_q):
        """Encode one PSX ADPCM mono frame, searching all five predictor filters."""
        sample_frames = samples.size // 2
        frame_start = group_index * FRAME_SAMPLES
        best_error = 9223372036854775807
        best_filter = 0
        best_shift = 12
        best_hist1 = hist1
        best_hist2 = hist2

        for filt in range(5):
            if filt == 0:
                c1, c2 = 0, 0
            elif filt == 1:
                c1, c2 = 60, 0
            elif filt == 2:
                c1, c2 = 115, -52
            elif filt == 3:
                c1, c2 = 98, -55
            else:
                c1, c2 = 122, -60

            # Estimate quantizer shift using the original samples and the
            # reconstructed history inherited from the previous frame.
            peak = 0
            prev1, prev2 = hist1, hist2
            for j in range(FRAME_SAMPLES):
                pos = frame_start + j
                x = int(samples[pos * 2 + channel]) if pos < sample_frames else 0
                pred = (prev1 * c1 + prev2 * c2 + 32) >> 6
                residual = x - pred
                magnitude = -residual if residual < 0 else residual
                if magnitude > peak:
                    peak = magnitude
                prev2, prev1 = prev1, x

            need = max(1, (peak + 6) // 7)
            factor = 1
            shift = 12
            while factor < need and factor < 4096:
                factor <<= 1
                shift -= 1

            # Quantize/decode candidate and calculate its reconstruction error.
            recon1, recon2 = hist1, hist2
            error = 0
            for j in range(FRAME_SAMPLES):
                pos = frame_start + j
                x = int(samples[pos * 2 + channel]) if pos < sample_frames else 0
                pred = (recon1 * c1 + recon2 * c2 + 32) >> 6
                diff = x - pred
                if diff >= 0:
                    q = (diff + factor // 2) // factor
                else:
                    q = -((-diff + factor // 2) // factor)
                if q < -8:
                    q = -8
                elif q > 7:
                    q = 7
                decoded = pred + q * factor
                if decoded < -32768:
                    decoded = -32768
                elif decoded > 32767:
                    decoded = 32767
                delta = x - decoded
                error += delta * delta
                temp_q[j] = q
                recon2, recon1 = recon1, decoded

            if error < best_error:
                best_error = error
                best_filter = filt
                best_shift = shift
                best_hist1 = recon1
                best_hist2 = recon2
                for j in range(FRAME_SAMPLES):
                    best_q[j] = temp_q[j]

        out[out_offset] = (best_filter << 4) | (best_shift & 0x0F)
        out[out_offset + 1] = flag & 0xFF
        for j in range(0, FRAME_SAMPLES, 2):
            lo = best_q[j] & 0x0F
            hi = best_q[j + 1] & 0x0F
            out[out_offset + 2 + j // 2] = lo | (hi << 4)
        return best_hist1, best_hist2

    @njit(cache=True)
    def _encode_pcm_chunk_numba(samples, histories, first_frame, last_frame):
        """Compiled high-quality encoder; preserves five-filter predictor search."""
        sample_frames = samples.size // 2
        group_count = (sample_frames + FRAME_SAMPLES - 1) // FRAME_SAMPLES
        out = np.zeros(group_count * FRAME_BYTES * 2, dtype=np.uint8)
        best_q = np.zeros(FRAME_SAMPLES, dtype=np.int8)
        temp_q = np.zeros(FRAME_SAMPLES, dtype=np.int8)
        lh1, lh2 = int(histories[0]), int(histories[1])
        rh1, rh2 = int(histories[2]), int(histories[3])
        for group in range(group_count):
            at_start = first_frame and group == 0
            at_end = last_frame and group == group_count - 1
            if at_start and at_end:
                flag = 0x07  # single-frame stream: start and end on the same frame
            elif at_start:
                flag = 0x06
            elif at_end:
                flag = 0x03
            else:
                flag = 0x02
            base = group * FRAME_BYTES * 2
            right_flag = flag
            lh1, lh2 = _encode_channel_frame_numba(
                samples, group, 0, lh1, lh2, flag, out, base, best_q, temp_q
            )
            rh1, rh2 = _encode_channel_frame_numba(
                samples, group, 1, rh1, rh2, right_flag, out, base + FRAME_BYTES, best_q, temp_q
            )
        histories[0], histories[1] = lh1, lh2
        histories[2], histories[3] = rh1, rh2
        return out, histories
else:
    _encode_pcm_chunk_numba = None

def encode_pcm_wav_to_ps2_payload(pcm_wav: Path, output_raw: Path, sample_rate: int = 24000,
                                  progress: Optional[Callable[[str], None]] = None,
                                  cancel_event=None) -> dict:
    """Fast-vector encode normalized stereo PCM WAV as interleaved PS2 SPU ADPCM."""
    if np is None or njit is None:
        raise AudioEncodeError(
            "NumPy and Numba are required for fast, quality-preserving batch encoding. "
            "Run Install_Dependencies.bat or install requirements.txt."
        )
    try:
        wf = wave.open(str(pcm_wav), "rb")
    except (wave.Error, OSError) as exc:
        raise AudioEncodeError(f"Could not read decoded PCM WAV: {exc}") from exc
    with wf:
        if wf.getnchannels() != 2 or wf.getsampwidth() != 2:
            raise AudioEncodeError("Internal PCM file must be 16-bit stereo WAV.")
        if wf.getframerate() != sample_rate:
            raise AudioEncodeError(f"Internal PCM rate is {wf.getframerate()} Hz, expected {sample_rate} Hz.")
        total_frames = wf.getnframes()
        if total_frames < 1:
            raise AudioEncodeError("The source audio contains no samples.")
        chunk_samples = FRAME_SAMPLES * 8192
        total_payload = 0
        done = 0
        last_report = -1
        histories = np.zeros(4, dtype=np.int64)
        output_raw.parent.mkdir(parents=True, exist_ok=True)
        with output_raw.open("wb") as out:
            while done < total_frames:
                if cancel_event is not None and cancel_event.is_set():
                    raise AudioEncodeError("Conversion cancelled by user.")
                want = min(chunk_samples, total_frames - done)
                if want < total_frames - done:
                    want = (want // FRAME_SAMPLES) * FRAME_SAMPLES
                raw = wf.readframes(want)
                if not raw:
                    break
                # WAV PCM is little-endian s16. Numba runs the original five-predictor
                # search in compiled code, avoiding the multi-hour pure-Python batch path.
                samples = np.frombuffer(raw, dtype="<i2")
                encoded, histories = _encode_pcm_chunk_numba(samples, histories, done == 0, done + (len(samples) // 2) >= total_frames)
                encoded_bytes = encoded.tobytes()
                out.write(encoded_bytes)
                total_payload += len(encoded_bytes)
                done += len(samples) // 2
                if progress:
                    pct = min(100, int(done * 100 / total_frames))
                    # Report only when the visible percent changes, not for every chunk.
                    if pct != last_report:
                        progress(f"Encoding {pct}%")
                        last_report = pct
        if total_payload == 0:
            raise AudioEncodeError("The encoder produced no ADPCM output.")
        return {
            "sample_rate": sample_rate,
            "channels": 2,
            "pcm_frames": total_frames,
            "payload_bytes": total_payload,
            "duration_seconds": total_frames / sample_rate,
            "encoder": "Numba-compiled PSX ADPCM (five-predictor search)",
        }

# GTA San Andreas PS2 type-4 radio streams have two synchronized stereo
# representations per physical 0x21000-byte chunk:
#   mini stream:  0x0800 bytes left + 0x0800 bytes right (750 Hz)
#   full stream:  0x10000 bytes left + 0x10000 bytes right (usually 24000 Hz)
# The mini stream occupies the first 0x1000 bytes of each chunk. The normal
# stream follows it. These are data sections, not arbitrary zero padding.
GTASA_CHUNK_SIZE = 0x21000
GTASA_MINI_CHANNEL_BLOCK = 0x0800
GTASA_FULL_CHANNEL_BLOCK = 0x10000
GTASA_MINI_RATE = 750


def _split_stereo_adpcm_frames(raw: bytes, label: str) -> tuple[bytes, bytes]:
    """Split the encoder's temporary L-frame/R-frame pairs into channel streams."""
    if not raw:
        raise AudioEncodeError(f"{label} ADPCM stream is empty.")
    if len(raw) % (FRAME_BYTES * 2):
        raise AudioEncodeError(
            f"{label} stereo ADPCM is {len(raw)} bytes; expected complete 32-byte L/R frame pairs."
        )
    left = bytearray(len(raw) // 2)
    right = bytearray(len(raw) // 2)
    out = 0
    for pos in range(0, len(raw), FRAME_BYTES * 2):
        left[out:out + FRAME_BYTES] = raw[pos:pos + FRAME_BYTES]
        right[out:out + FRAME_BYTES] = raw[pos + FRAME_BYTES:pos + FRAME_BYTES * 2]
        out += FRAME_BYTES
    return bytes(left), bytes(right)


def pack_gtasa_ps2_stream_payload(full_raw_path: Path, mini_raw_path: Path,
                                  output_packed_path: Path) -> dict:
    """Pack temporary stereo-frame ADPCM into GTA SA PS2 type-4 chunk layout.

    Both sources are temporary interleaved-frame encodes. This packs every
    0x21000 chunk as mini-L (0x800), mini-R (0x800), full-L (0x10000), full-R
    (0x10000). Short final blocks are padded with silence so chunk offsets stay
    valid. The PAK lookup size must use the physical packed length, not either
    unpadded temporary length.
    """
    full_left, full_right = _split_stereo_adpcm_frames(full_raw_path.read_bytes(), "Full-rate")
    mini_left, mini_right = _split_stereo_adpcm_frames(mini_raw_path.read_bytes(), "Mini")
    full_block = GTASA_FULL_CHANNEL_BLOCK
    mini_block = GTASA_MINI_CHANNEL_BLOCK
    chunk_count = max(
        (len(full_left) + full_block - 1) // full_block,
        (len(mini_left) + mini_block - 1) // mini_block,
        1,
    )
    # Do not silently drop mini-stream bytes if independently rounded source
    # lengths differ by a frame around a chunk boundary.
    if (len(full_left) > chunk_count * full_block or
            len(mini_left) > chunk_count * mini_block):
        raise AudioEncodeError("Full/mini stream lengths could not be aligned to GTA SA PS2 chunks.")
    output_packed_path.parent.mkdir(parents=True, exist_ok=True)
    with output_packed_path.open("wb") as out:
        for index in range(chunk_count):
            fs = index * full_block
            ms = index * mini_block
            fl = full_left[fs:fs + full_block].ljust(full_block, b"\x00")
            fr = full_right[fs:fs + full_block].ljust(full_block, b"\x00")
            ml = mini_left[ms:ms + mini_block].ljust(mini_block, b"\x00")
            mr = mini_right[ms:ms + mini_block].ljust(mini_block, b"\x00")
            out.write(ml)
            out.write(mr)
            out.write(fl)
            out.write(fr)
    packed_size = output_packed_path.stat().st_size
    expected = chunk_count * GTASA_CHUNK_SIZE
    if packed_size != expected or packed_size % GTASA_CHUNK_SIZE:
        raise AudioEncodeError(
            f"Packed GTA SA PS2 stream size is {packed_size}; expected {expected} bytes in complete 0x21000 chunks."
        )
    return {
        "full_raw_bytes": len(full_left) + len(full_right),
        "mini_raw_bytes": len(mini_left) + len(mini_right),
        "full_channel_bytes": len(full_left),
        "mini_channel_bytes": len(mini_left),
        "packed_bytes": packed_size,
        "chunk_count": chunk_count,
        "chunk_size": GTASA_CHUNK_SIZE,
        "full_sample_rate": None,
        "mini_sample_rate": GTASA_MINI_RATE,
        "layout": "mini L/R 0x800 each, then full L/R 0x10000 each",
    }


def update_gtasa_type4_stream_header(header: bytes, packed_meta: dict, full_rate: int) -> bytes:
    """Update per-channel audio byte counts in a GTA SA PS2 type-4 stream header.

    Header fields store the unpadded byte count for each channel in each stream
    variant. The 750 Hz mini-stream pair and full-rate pair are duplicated for
    left and right. Timing/loop fields elsewhere in the header are preserved.
    """
    if len(header) != 0x1F84:
        raise AudioEncodeError(f"GTA SA stream header must be 8068 bytes; got {len(header)}.")
    if struct.unpack_from("<I", header, 0x1F80)[0] != 4:
        raise AudioEncodeError(
            "This automatic dual-stream header update supports GTA SA PS2 type-4 records only."
        )
    # Convert the immutable input bytes to a mutable buffer before packing fields.
    updated = bytearray(header)
    mini_size = int(packed_meta["mini_channel_bytes"])
    full_size = int(packed_meta["full_channel_bytes"])
    for off in (0x1F40, 0x1F48):
        struct.pack_into("<I", updated, off, mini_size)
    for off in (0x1F44, 0x1F4C):
        struct.pack_into("<I", updated, off, GTASA_MINI_RATE)
    for off in (0x1F50, 0x1F58):
        struct.pack_into("<I", updated, off, full_size)
    for off in (0x1F54, 0x1F5C):
        struct.pack_into("<I", updated, off, int(full_rate))
    return bytes(updated)


def validate_gtasa_type4_stream(header: bytes, payload: bytes | Path,
                                 expected_full_rate: Optional[int] = None) -> dict:
    """Validate a GTA SA PS2 type-4 header and chunked dual-rate SPU-ADPCM payload.

    Validation is based on original GTA SA PS2 CH.PAK records supplied with the
    project: each mono stream length is stored in the header, duplicated for L/R;
    every stream starts with frame flag 0x06, continues with 0x02, and ends with
    0x03. The physical payload is arranged in 0x21000-byte chunks.
    """
    if len(header) != 0x1F84:
        raise AudioEncodeError(f"GTA SA type-4 header must be 8068 bytes; got {len(header)}.")
    if header[:4] != b"\xff\xff\xff\xff" or struct.unpack_from("<I", header, 0x1F80)[0] != 4:
        raise AudioEncodeError("The supplied header is not a recognized GTA SA PS2 type-4 stream header.")
    blob = payload.read_bytes() if isinstance(payload, Path) else bytes(payload)
    if not blob:
        raise AudioEncodeError("GTA SA type-4 payload is empty.")

    mini_l = struct.unpack_from("<I", header, 0x1F40)[0]
    mini_l_rate = struct.unpack_from("<I", header, 0x1F44)[0]
    mini_r = struct.unpack_from("<I", header, 0x1F48)[0]
    mini_r_rate = struct.unpack_from("<I", header, 0x1F4C)[0]
    full_l = struct.unpack_from("<I", header, 0x1F50)[0]
    full_l_rate = struct.unpack_from("<I", header, 0x1F54)[0]
    full_r = struct.unpack_from("<I", header, 0x1F58)[0]
    full_r_rate = struct.unpack_from("<I", header, 0x1F5C)[0]

    if not mini_l or not full_l:
        raise AudioEncodeError("The type-4 header declares an empty mini or full-rate channel stream.")
    if mini_l != mini_r or full_l != full_r:
        raise AudioEncodeError(
            f"L/R stream sizes differ (mini {mini_l}/{mini_r}, full {full_l}/{full_r}); "
            "the supported GTA SA PS2 type-4 records use matching channel lengths."
        )
    if any(size % FRAME_BYTES for size in (mini_l, mini_r, full_l, full_r)):
        raise AudioEncodeError("A declared channel stream length is not aligned to 16-byte SPU-ADPCM frames.")
    if mini_l_rate != GTASA_MINI_RATE or mini_r_rate != GTASA_MINI_RATE:
        raise AudioEncodeError(
            f"GTA SA PS2 mini-stream rates must both be {GTASA_MINI_RATE} Hz; got {mini_l_rate}/{mini_r_rate}."
        )
    if full_l_rate != full_r_rate or not 8000 <= full_l_rate <= 48000:
        raise AudioEncodeError(f"Invalid full-stream sample rates in header: {full_l_rate}/{full_r_rate}.")
    if expected_full_rate is not None and full_l_rate != int(expected_full_rate):
        raise AudioEncodeError(
            f"Encoded stream is {full_l_rate} Hz but the selected track header expects {expected_full_rate} Hz."
        )

    mini_chunks = (mini_l + GTASA_MINI_CHANNEL_BLOCK - 1) // GTASA_MINI_CHANNEL_BLOCK
    full_chunks = (full_l + GTASA_FULL_CHANNEL_BLOCK - 1) // GTASA_FULL_CHANNEL_BLOCK
    chunk_count = max(mini_chunks, full_chunks, 1)
    expected_physical = chunk_count * GTASA_CHUNK_SIZE
    if len(blob) != expected_physical:
        raise AudioEncodeError(
            f"Payload is {len(blob):,} bytes, but header lengths require {chunk_count} complete "
            f"0x{GTASA_CHUNK_SIZE:X}-byte chunks ({expected_physical:,} bytes)."
        )

    full_duration = (full_l // FRAME_BYTES) * FRAME_SAMPLES / full_l_rate
    mini_duration = (mini_l // FRAME_BYTES) * FRAME_SAMPLES / GTASA_MINI_RATE
    duration_delta = abs(full_duration - mini_duration)
    duration_tolerance = 2 * FRAME_SAMPLES / GTASA_MINI_RATE
    if duration_delta > duration_tolerance:
        raise AudioEncodeError(
            f"Mini/full stream durations disagree by {duration_delta:.3f}s "
            f"(mini {mini_duration:.3f}s, full {full_duration:.3f}s)."
        )

    channel_specs = (
        ("mini-left", mini_l, GTASA_MINI_CHANNEL_BLOCK, 0x0000),
        ("mini-right", mini_r, GTASA_MINI_CHANNEL_BLOCK, 0x0800),
        ("full-left", full_l, GTASA_FULL_CHANNEL_BLOCK, 0x1000),
        ("full-right", full_r, GTASA_FULL_CHANNEL_BLOCK, 0x11000),
    )
    frame_total = 0
    for label, size, block_size, block_offset in channel_specs:
        consumed = 0
        total_frames = size // FRAME_BYTES
        seen_frames = 0
        while consumed < size:
            chunk_index = consumed // block_size
            in_block = min(block_size, size - consumed)
            byte_offset = chunk_index * GTASA_CHUNK_SIZE + block_offset
            segment = blob[byte_offset:byte_offset + in_block]
            if len(segment) != in_block or len(segment) % FRAME_BYTES:
                raise AudioEncodeError(f"{label} data is truncated or not frame-aligned at chunk {chunk_index}.")
            for frame_offset in range(0, len(segment), FRAME_BYTES):
                filter_shift = segment[frame_offset]
                flag = segment[frame_offset + 1]
                predictor = filter_shift >> 4
                shift = filter_shift & 0x0F
                if predictor > 4 or shift > 12:
                    raise AudioEncodeError(
                        f"{label} has an invalid SPU-ADPCM predictor/shift at frame {seen_frames}: "
                        f"filter={predictor}, shift={shift}."
                    )
                is_first = seen_frames == 0
                is_last = seen_frames == total_frames - 1
                expected_flag = 0x07 if is_first and is_last else 0x06 if is_first else 0x03 if is_last else 0x02
                if flag != expected_flag:
                    raise AudioEncodeError(
                        f"{label} frame {seen_frames} has flag 0x{flag:02X}; expected 0x{expected_flag:02X}. "
                        "The GTA SA PS2 stream's start/end markers may be missing."
                    )
                seen_frames += 1
            consumed += in_block
        if seen_frames != total_frames:
            raise AudioEncodeError(f"{label} validation counted {seen_frames} frames, expected {total_frames}.")
        frame_total += seen_frames

    return {
        "valid": True,
        "chunk_count": chunk_count,
        "payload_bytes": len(blob),
        "mini_channel_bytes": mini_l,
        "full_channel_bytes": full_l,
        "mini_sample_rate": GTASA_MINI_RATE,
        "full_sample_rate": full_l_rate,
        "mini_duration_seconds": mini_duration,
        "full_duration_seconds": full_duration,
        "duration_difference_seconds": duration_delta,
        "validated_channel_frames": frame_total,
        "frame_markers": "0x06 start, 0x02 continuation, 0x03 end",
    }


def run_gtasa_stream_layout_self_test() -> None:
    """Quick synthetic regression test for chunk packing and mutable header edits."""
    import tempfile

    def make_stereo(channel_bytes: int, marker_left: int, marker_right: int) -> bytes:
        if channel_bytes % FRAME_BYTES:
            raise AssertionError("test channel length must align to ADPCM frames")
        out = bytearray()
        count = channel_bytes // FRAME_BYTES
        for i in range(count):
            flag = 0x06 if i == 0 else 0x03 if i == count - 1 else 0x02
            left = bytes([marker_left, flag]) + bytes(FRAME_BYTES - 2)
            right = bytes([marker_right, flag]) + bytes(FRAME_BYTES - 2)
            out.extend(left)
            out.extend(right)
        return bytes(out)

    with tempfile.TemporaryDirectory(prefix="saatps2_layout_test_") as td:
        root = Path(td)
        full = root / "full.bin"
        mini = root / "mini.bin"
        packed = root / "packed.bin"
        full.write_bytes(make_stereo(GTASA_FULL_CHANNEL_BLOCK, 0x12, 0x42))
        mini.write_bytes(make_stereo(GTASA_MINI_CHANNEL_BLOCK, 0x22, 0x32))
        meta = pack_gtasa_ps2_stream_payload(full, mini, packed)
        payload = packed.read_bytes()
        assert len(payload) == GTASA_CHUNK_SIZE and meta["chunk_count"] == 1
        # Check channel layout and independently initialized first-frame flags.
        expected = ((0x0000, 0x22), (0x0800, 0x32), (0x1000, 0x12), (0x11000, 0x42))
        for offset, marker in expected:
            assert payload[offset] == marker and payload[offset + 1] == 0x06
        header = bytearray(0x1F84)
        header[:4] = b"\xff\xff\xff\xff"
        struct.pack_into("<I", header, 0x1F80, 4)
        updated = update_gtasa_type4_stream_header(bytes(header), meta, 24000)
        assert struct.unpack_from("<II", updated, 0x1F40) == (GTASA_MINI_CHANNEL_BLOCK, GTASA_MINI_RATE)
        assert struct.unpack_from("<II", updated, 0x1F50) == (GTASA_FULL_CHANNEL_BLOCK, 24000)
        assert struct.unpack_from("<I", updated, 0x1F80)[0] == 4
        validation = validate_gtasa_type4_stream(updated, payload, expected_full_rate=24000)
        assert validation["valid"] and validation["chunk_count"] == 1
        assert validation["mini_channel_bytes"] == GTASA_MINI_CHANNEL_BLOCK
        assert validation["full_channel_bytes"] == GTASA_FULL_CHANNEL_BLOCK
        broken = bytearray(payload)
        broken[0x1000 + GTASA_FULL_CHANNEL_BLOCK - FRAME_BYTES + 1] = 0x02
        try:
            validate_gtasa_type4_stream(updated, bytes(broken), expected_full_rate=24000)
        except AudioEncodeError as exc:
            assert "expected 0x03" in str(exc)
        else:
            raise AssertionError("Validator accepted a full-stream without its final 0x03 marker.")


def sys_byteorder_big() -> bool:
    import sys
    return sys.byteorder == "big"


def convert_audio_to_ps2_payload(source: Path, output_raw: Path, ffmpeg: str,
                                 sample_rate: int = 24000,
                                 progress: Optional[Callable[[str], None]] = None,
                                 cancel_event=None) -> dict:
    """Decode/resample via FFmpeg, then quickly encode PS2 SPU ADPCM."""
    if not source.is_file():
        raise AudioEncodeError(f"Source audio does not exist: {source}")
    if source.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
        raise AudioEncodeError(f"Unsupported audio filename extension: {source.suffix or '(none)'}")
    exe = find_ffmpeg(ffmpeg)
    if not exe:
        raise AudioEncodeError("FFmpeg was not found. Install FFmpeg or set its path in Audio Preparation.")
    # 750 Hz is used by GTA SA PS2's low-quality mini-stream; normal full
    # streams use the rate read from their original metadata (commonly 24000 Hz).
    if not 500 <= sample_rate <= 48000:
        raise AudioEncodeError(f"Unsupported target sample rate: {sample_rate} Hz.")
    if np is None or njit is None:
        raise AudioEncodeError("NumPy and Numba are required for fast encoding. Run Install_Dependencies.bat first.")
    output_raw.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="saatps2_encode_") as td:
        pcm_path = Path(td) / "normalized_pcm.wav"
        cmd = [
            exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "2", "-ar", str(sample_rate),
            "-sample_fmt", "s16", "-c:a", "pcm_s16le", "-f", "wav", str(pcm_path),
        ]
        if progress:
            progress("Decoding/resampling with FFmpeg")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    raise AudioEncodeError("Conversion cancelled by user.")
                time.sleep(0.1)
            stderr = proc.stderr.read() if proc.stderr else ""
        finally:
            if proc.stderr:
                proc.stderr.close()
        if proc.returncode != 0 or not pcm_path.is_file():
            details = (stderr or "FFmpeg could not decode this source.")[-3000:]
            raise AudioEncodeError(f"FFmpeg decode/resample failed for {source.name}:\n{details}")
        meta = encode_pcm_wav_to_ps2_payload(pcm_path, output_raw, sample_rate, progress, cancel_event)
    meta["source"] = str(source)
    meta["encoded_file"] = str(output_raw)
    return meta

def wrap_vagp(payload_path: Path, output_vag: Path, sample_rate: int = 24000) -> None:
    """Create a VAGp wrapper around PSX ADPCM data for conventional VAG tracks."""
    payload = payload_path.read_bytes()
    if not payload or len(payload) % 16:
        raise AudioEncodeError("ADPCM payload size is not a multiple of 16 bytes.")
    header = bytearray(VAG_HEADER_SIZE)
    header[:4] = VAG_MAGIC
    struct.pack_into(">I", header, 0x04, 0x20)
    struct.pack_into(">I", header, 0x0C, len(payload))
    struct.pack_into(">I", header, 0x10, sample_rate)
    output_vag.write_bytes(bytes(header) + payload)
