"""Cross-platform synthetic tests for real Windows PSP tool smoke-test logic.

The external .EXEs are not launched in these tests. They are launched by
Verify_PSP_Tools.py on Windows when Install_Dependencies.bat is run.
"""
from __future__ import annotations

from pathlib import Path
import struct
import subprocess
import tempfile
from unittest import mock

import Audio_Preparation_Profiles as audio
import Verify_PSP_Tools as verify
from PS2_ISO_Patcher import _make_test_at3, _parse_at3_prefix


def _fake_exe(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'MZ' + b'0' * 700)


def _riff_plus(p: Path, *, format_subtype=audio.ATRAC3PLUS_SUBTYPE, data=None):
    # The fmt chunk is adapted from the original LCS/VCS PSP streams: 52 bytes.
    fmt = bytearray.fromhex('feff020044ac00001a2f0000300200002200000803000000bfaa23e958cb7144a119fffa01e4ce62010028450000000000000000')
    assert len(fmt) == 52
    fmt[24:40] = format_subtype
    data = data or bytes((i * 11 + 7) % 256 for i in range(576 * 20))
    def chunk(tag, payload):
        return tag + struct.pack('<I',len(payload)) + payload + (b'\0' if len(payload) % 2 else b'')
    body = b'WAVE' + chunk(b'fmt ',fmt) + chunk(b'fact',struct.pack('<II',33075,0)) + chunk(b'data',data)
    p.write_bytes(b'RIFF' + struct.pack('<I',len(body)) + body)


def run():
    with tempfile.TemporaryDirectory(prefix='gta_psp_smoke_test_') as folder:
        root = Path(folder)
        psp = root / 'Tools/pspdecrypt/pspdecrypt.exe'
        at3 = root / 'Tools/at3tool/at3tool.exe'
        _fake_exe(psp)
        _fake_exe(at3)
        (at3.parent/'msvcr71.dll').write_bytes(b'MZ')
        calls = []
        def success_runner(argv, **kw):
            calls.append(argv)
            if '--help' in argv:
                return subprocess.CompletedProcess(argv, 0, 'Usage: pspdecrypt [OPTION]... [FILE]\nCan also take PBP files', '')
            assert argv[1:4] == ['-e','-br','96']
            with __import__('wave').open(argv[4], 'rb') as wav:
                assert wav.getframerate() == 44100 and wav.getnchannels() == 2
                assert wav.getnframes() > 10000 and wav.getsampwidth() == 2
            _riff_plus(Path(argv[5]))
            return subprocess.CompletedProcess(argv, 0, 'Encoding 96 kbps', '')
        with mock.patch.object(verify, '_run', side_effect=success_runner):
            assert verify.run(root) == 0
            assert len(calls) == 2
        def bad_help(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, 'unrelated tool', '')
        with mock.patch.object(verify, '_run', side_effect=bad_help):
            try: verify.verify_pspdecrypt(psp)
            except verify.VerificationError as e: assert 'expected usage' in str(e)
            else: raise AssertionError('PSPDecrypt help failure was accepted')
        def pcm_at3(argv, **kw):
            _riff_plus(Path(argv[5]),format_subtype=bytes.fromhex('0100000000001000800000aa00389b71'))
            return subprocess.CompletedProcess(argv, 0, '', '')
        with mock.patch.object(verify, '_run', side_effect=pcm_at3):
            try: verify.verify_at3_encoder(at3)
            except verify.VerificationError as e: assert 'subtype' in str(e)
            else: raise AssertionError('PCM/WAVEFORMATEXTENSIBLE accepted as ATRAC3plus')
        def legacy_at3(argv, **kw):
            _riff_plus(Path(argv[5]),format_subtype=audio.ATRAC3_SUBTYPE)
            return subprocess.CompletedProcess(argv, 0, '', '')
        with mock.patch.object(verify, '_run', side_effect=legacy_at3):
            try: verify.verify_at3_encoder(at3)
            except verify.VerificationError as e: assert 'not ATRAC3plus' in str(e)
            else: raise AssertionError('Legacy ATRAC3 accepted as ATRAC3plus')
        (at3.parent/'msvcr71.dll').unlink()
        try: verify.verify_at3_encoder(at3)
        except verify.VerificationError as e: assert 'msvcr71.dll' in str(e)
        else: raise AssertionError('Missing DLL was accepted')
        # Independent ISO patcher guards against wrong codec headers.
        sample = _make_test_at3(0x2000,False)
        assert _parse_at3_prefix(sample[:0x800],len(sample),'test')['channels']==2
        mutant = bytearray(sample)
        i = mutant.index(audio.ATRAC3PLUS_SUBTYPE)
        mutant[i:i+16] = bytes.fromhex('0100000000001000800000aa00389b71')
        try: _parse_at3_prefix(bytes(mutant[:0x800]),len(mutant),'test')
        except ValueError as e: assert 'ATRAC3plus' in str(e)
        else: raise AssertionError('PSP ISO patcher accepted PCM as ATRAC3plus')
        # Real game GUID in both sample station files, with synthetic audio data.
        plus = root/'original_layout.at3'
        _riff_plus(plus)
        metadata = audio.inspect_at3(plus)
        assert metadata['codec']=='ATRAC3plus' and metadata['data_offset']==96
    print('PASS: PSP tool startup/encode smoke tests, failure handling, original GTA ATRAC3plus codec and ISO patcher guard.')


if __name__=='__main__': run()
