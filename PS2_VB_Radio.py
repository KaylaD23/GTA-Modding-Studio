"""GTA III/VC/LCS/VCS PS2 headless stereo PSX ADPCM radio VB streams.

Unlike GTA SA PAK payloads, these raw VBs contain 0x2000 bytes of one
channel followed by 0x2000 of the other (interleave 0x2000), repeating.
They contain no sample-rate header. Sample rates must come from the selected
station/game, not from the source WAV or the file extension.
"""
from __future__ import annotations

from pathlib import Path


CHANNEL_BLOCK = 0x2000
STEREO_BLOCK = 2 * CHANNEL_BLOCK
ADPCM_FRAME_BYTES = 16
PCM_SAMPLES_PER_FRAME = 28
PAIRS_PER_BLOCK = CHANNEL_BLOCK // ADPCM_FRAME_BYTES


def required_vb_size(pcm_sample_frames: int) -> int:
    """VB byte count, including final per-channel 0x2000 padding."""
    if pcm_sample_frames <= 0:
        raise ValueError('PCM has no samples.')
    mono_adpcm_frames = (pcm_sample_frames + PCM_SAMPLES_PER_FRAME - 1) // PCM_SAMPLES_PER_FRAME
    return ((mono_adpcm_frames + PAIRS_PER_BLOCK - 1) // PAIRS_PER_BLOCK) * STEREO_BLOCK


def estimated_vb_duration(size_bytes: int, sample_rate: int) -> float:
    """Duration in seconds inferred from file size; final sector may be padding."""
    if not 8000 <= sample_rate <= 48000:
        raise ValueError('Invalid PS2 VB playback sample rate.')
    if size_bytes <= 0 or size_bytes % STEREO_BLOCK:
        raise ValueError('GTA PS2 radio VB must be a multiple of 0x4000 bytes.')
    return (size_bytes // (ADPCM_FRAME_BYTES * 2)) * PCM_SAMPLES_PER_FRAME / sample_rate


def repack_stereo_frames(raw_path: Path, vb_path: Path) -> dict:
    """Stream an existing L/R/L/R... PSX ADPCM stream into GTA PS2 radio VB layout.

    The shared GTA SA encoder emits alternating 16-byte frames, which are
    *not* a playable GTA III/VC/LCS/VCS radio VB until repacked. Pad each
    channel equally to complete 0x2000-byte blocks, clear per-frame loop
    flags and provide the silent first frame used by original game assets.
    This is independent of the sample-rate setting.
    """
    raw_path, vb_path = Path(raw_path), Path(vb_path)
    if raw_path.resolve() == vb_path.resolve():
        raise ValueError('VB packing input and output paths must differ.')
    size = raw_path.stat().st_size
    if size == 0 or size % (ADPCM_FRAME_BYTES * 2):
        raise ValueError('Input ADPCM is not complete stereo 16-byte frame pairs.')
    vb_path.parent.mkdir(parents=True, exist_ok=True)
    stereo_pairs = size // 32
    written = 0
    identical_channel_blocks = 0
    total_channel_blocks = 0
    with raw_path.open('rb') as inp, vb_path.open('wb') as out:
        pair_index = 0
        while pair_index < stereo_pairs:
            count = min(PAIRS_PER_BLOCK, stereo_pairs - pair_index)
            data = inp.read(count * 32)
            if len(data) != count * 32:
                raise ValueError('ADPCM truncated while packing channel blocks.')
            left = bytearray(CHANNEL_BLOCK)
            right = bytearray(CHANNEL_BLOCK)
            for i in range(count):
                a = bytearray(data[i * 32:i * 32 + 16])
                b = bytearray(data[i * 32 + 16:i * 32 + 32])
                # The originals use 0x00, not GTA SA's 0x02/0x06/0x03 flags.
                a[1] = 0
                b[1] = 0
                if pair_index == 0 and i == 0:
                    a[:] = bytes(16)
                    b[:] = bytes(16)
                left[i * 16:i * 16 + 16] = a
                right[i * 16:i * 16 + 16] = b
            if left == right:
                identical_channel_blocks += 1
            total_channel_blocks += 1
            out.write(left)
            out.write(right)
            written += STEREO_BLOCK
            pair_index += count
    if written != required_vb_size(stereo_pairs * PCM_SAMPLES_PER_FRAME):
        raise AssertionError('VB packer produced an inconsistent size.')
    return {
        'encoded_stereo_pairs': stereo_pairs,
        'raw_bytes': size,
        'vb_bytes': written,
        'channel_interleave_bytes': CHANNEL_BLOCK,
        'padding_bytes': written - size,
        'first_frame_silent_per_channel': True,
        'loop_flags_cleared': True,
        # Objective layout information, not a claim about the input's original
        # channel count: intentional dual-mono and coincidentally identical
        # stereo both have the same channel bytes.
        'matching_channel_block_pairs': identical_channel_blocks,
        'total_channel_block_pairs': total_channel_blocks,
        'encoded_channels_identical': identical_channel_blocks == total_channel_blocks,
    }


def inspect_vb(path: Path, sample_rate: int, *, scan_all: bool = True) -> dict:
    """Check VB framing/layout and compute size-derived duration.

    No VB header exists to independently prove the sample rate; this verifies
    the structure only. The chosen rate must match the specific station.
    """
    path = Path(path)
    size = path.stat().st_size
    duration = estimated_vb_duration(size, sample_rate)
    checked = 0
    with path.open('rb') as stream:
        while True:
            block = stream.read(STEREO_BLOCK)
            if not block:
                break
            if len(block) != STEREO_BLOCK:
                raise ValueError('Truncated VB interleave block.')
            if checked == 0 and (block[:16] != bytes(16) or block[CHANNEL_BLOCK:CHANNEL_BLOCK + 16] != bytes(16)):
                raise ValueError('VB does not have a silent first frame in BOTH channels.')
            for i in range(0, len(block), 16):
                predictor_shift = block[i]
                if predictor_shift >> 4 > 4 or predictor_shift & 15 > 12:
                    raise ValueError(f'Invalid PSX ADPCM predictor/shift at offset 0x{checked * STEREO_BLOCK + i:X}.')
                if block[i + 1] != 0:
                    raise ValueError(f'Nonzero VB loop flag at offset 0x{checked * STEREO_BLOCK + i + 1:X}.')
            checked += 1
            if not scan_all and checked >= 4:
                break
    return {'size_bytes': size, 'sample_rate': sample_rate, 'channels': 2,
            'interleave_bytes': CHANNEL_BLOCK, 'estimated_seconds': duration,
            'checked_blocks': checked,
            'duration_note': 'Size-derived; includes at most one padded 0x4000-byte block; raw VB has no rate header.'}
