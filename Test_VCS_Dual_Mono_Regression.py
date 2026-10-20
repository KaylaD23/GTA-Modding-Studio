"""Tests 32 kHz VCS stereo 0x2000 interleave and mono duplication in this build.

Run: python -m unittest -v Test_VCS_Dual_Mono_Regression
Source WAVs are synthetic and contain no game media. These are offline
encoding/layout checks, not game playback validation.
"""
from __future__ import annotations
import math
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path

import Audio_Preparation_Profiles as profiles
import PS2_VB_Radio as radio


def make_wav(path: Path, channels: int, duration: float = 0.7, rate: int = 44100):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(round(duration * rate)):
            left = int(13000 * math.sin(2 * math.pi * 440 * i / rate))
            if channels == 1:
                frames.extend(struct.pack('<h', left))
            else:
                right = int(10000 * math.sin(2 * math.pi * 880 * i / rate))
                frames.extend(struct.pack('<hh', left, right))
        w.writeframes(frames)


class TestVcsMonoDuplication(unittest.TestCase):
    def test_vcs_rate_interleave_and_station_exceptions(self):
        p = profiles.PROFILES['GTA VCS PS2 VB']
        self.assertEqual((p['rate'], p['channels'], p['interleave']), (32000, 2, 0x2000))
        self.assertEqual(profiles.ps2_station_rate('GTA VCS PS2 VB', 'VCPR.VB'), 32000)
        self.assertEqual(profiles.ps2_station_rate('GTA VC PS2 VB', 'VCPR.VB'), 16000)
        self.assertEqual(profiles.ps2_station_rate('GTA III PS2 VB', 'CHAT.VB'), 16000)
        self.assertEqual(profiles.ps2_station_rate('GTA LCS PS2 VB', 'KJAH.VB'), 32000)

    def test_repack_independent_stereo_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            raw = folder / 'raw.adpcm'; output = folder / 'radio.VB'
            left = b'\x0c\0' + bytes([0x11]) * 14
            right = b'\x0c\0' + bytes([0x33]) * 14
            raw.write_bytes((left + right) * (radio.PAIRS_PER_BLOCK + 1))
            info = radio.repack_stereo_frames(raw, output)
            data = output.read_bytes()
            self.assertEqual(len(data), radio.STEREO_BLOCK * 2)
            self.assertFalse(info['encoded_channels_identical'])
            self.assertEqual(info['matching_channel_block_pairs'], 0)
            self.assertEqual(info['total_channel_block_pairs'], 2)
            for n in range(0, len(data), radio.STEREO_BLOCK):
                self.assertEqual(data[n:n+16], bytes(16) if n == 0 else left)
                self.assertEqual(data[n+radio.CHANNEL_BLOCK:n+radio.CHANNEL_BLOCK+16], bytes(16) if n == 0 else right)
            self.assertEqual(data[radio.STEREO_BLOCK + 16:radio.STEREO_BLOCK + 32], bytes(16))

    def test_repack_explicit_dual_mono_and_padding(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            raw = folder / 'raw.adpcm'; output = folder / 'radio.VB'
            f = b'\x0c\x06' + bytes([0x55]) * 14
            raw.write_bytes((f + f) * (radio.PAIRS_PER_BLOCK + 3))
            info = radio.repack_stereo_frames(raw, output)
            self.assertTrue(info['encoded_channels_identical'])
            self.assertEqual(info['matching_channel_block_pairs'], info['total_channel_block_pairs'])
            data = output.read_bytes()
            self.assertEqual(len(data), radio.STEREO_BLOCK * 2)
            for offset in range(0, len(data), radio.STEREO_BLOCK):
                self.assertEqual(data[offset:offset + radio.CHANNEL_BLOCK],
                                 data[offset + radio.CHANNEL_BLOCK:offset + radio.STEREO_BLOCK])
            self.assertEqual(data[:16], bytes(16))
            self.assertEqual(data[radio.CHANNEL_BLOCK:radio.CHANNEL_BLOCK+16], bytes(16))
            self.assertEqual(data[radio.STEREO_BLOCK + 3*16:radio.STEREO_BLOCK + radio.CHANNEL_BLOCK],
                             bytes(radio.CHANNEL_BLOCK - 3*16))
            self.assertEqual(data[radio.STEREO_BLOCK + radio.CHANNEL_BLOCK + 3*16:],
                             bytes(radio.CHANNEL_BLOCK - 3*16))
            self.assertTrue(all(data[n+1] == 0 for n in range(0, len(data), 16)))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required for actual conversion')
    def test_mono_source_keeps_duplicated_lr_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            wav = folder / 'source.wav'; vb = folder / 'VCPR.VB'
            make_wav(wav, 1, 0.9)
            meta = profiles.convert(wav, vb, 'GTA VCS PS2 VB', shutil.which('ffmpeg'), station_name='VCPR.VB')
            data = vb.read_bytes()
            self.assertEqual(meta['sample_rate'], 32000)
            self.assertEqual(meta['channel_interleave_bytes'], 0x2000)
            self.assertTrue(meta['encoded_channels_identical'])
            self.assertTrue(meta['vb_layout']['encoded_channels_identical'])
            self.assertEqual(data[:16], bytes(16))
            self.assertEqual(data[0x2000:0x2010], bytes(16))
            self.assertNotEqual(data[32:0x2000], bytes(0x2000 - 32))
            self.assertEqual(len(data) % 0x4000, 0)
            for off in range(0, len(data), 0x4000):
                self.assertEqual(data[off:off+0x2000], data[off+0x2000:off+0x4000])
            self.assertLess(meta['vb_estimated_duration_seconds'] - meta['source_duration_seconds'], 0.45)
            self.assertGreaterEqual(meta['vb_estimated_duration_seconds'] - meta['source_duration_seconds'], 0)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required for actual conversion')
    def test_true_stereo_channels_not_accidentally_duplicated(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            wav = folder / 'source_stereo.wav'; vb = folder / 'VCPR.VB'
            make_wav(wav, 2, 0.9)
            meta = profiles.convert(wav, vb, 'GTA VCS PS2 VB', shutil.which('ffmpeg'), station_name='VCPR.VB')
            data = vb.read_bytes()
            self.assertFalse(meta['encoded_channels_identical'])
            self.assertLess(meta['vb_layout']['matching_channel_block_pairs'], meta['vb_layout']['total_channel_block_pairs'])
            self.assertTrue(any(data[off:off+0x2000] != data[off+0x2000:off+0x4000] for off in range(0, len(data), 0x4000)))
            self.assertEqual(len(data) % 0x4000, 0)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required for actual conversion')
    def test_failed_conversion_preserves_existing_target(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            target = folder / 'VCPR.VB'; target.write_bytes(b'ORIGINAL OUTPUT')
            with self.assertRaises(Exception):
                profiles.convert(folder/'missing.wav', target, 'GTA VCS PS2 VB', shutil.which('ffmpeg'), station_name='VCPR')
            self.assertEqual(target.read_bytes(), b'ORIGINAL OUTPUT')
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ['VCPR.VB'])

    def test_long_duration_required_size(self):
        source_samples = 442_559_232
        sample_rate = 44_100
        frames = round(source_samples * 32_000 / sample_rate)
        self.assertEqual(radio.required_vb_size(frames), 367_017_984)
        duration = radio.estimated_vb_duration(367_017_984, 32_000)
        self.assertLess(abs(duration - source_samples / sample_rate), 0.45)


if __name__ == '__main__':
    unittest.main(verbosity=2)
