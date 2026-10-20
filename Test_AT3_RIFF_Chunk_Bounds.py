"""RIFF chunk traversal regressions: large metadata, duplicates and padding."""
import struct
import tempfile
from pathlib import Path
from Audio_Preparation_Profiles import inspect_at3
import importlib.util
_spec = importlib.util.spec_from_file_location('at3validation', Path(__file__).with_name('Test_AT3_Validation_v0.3.26.py'))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
make_wave = _mod.make_wave


def chunk(tag, data):
    return tag + struct.pack('<I', len(data)) + data + (b'\0' if len(data) & 1 else b'')


def check():
    atrac = bytes.fromhex('7002000000001000800000aa00389b71')
    original = make_wave(atrac)
    body = original[12:]
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'sample.at3'
        def put(data):
            p.write_bytes(data)
        def riff(chunks):
            return b'RIFF' + struct.pack('<I', 4 + len(chunks)) + b'WAVE' + chunks
        put(riff(chunk(b'JUNK', bytes(70001)) + body))
        assert inspect_at3(p)['samples'] == 1000
        put(riff(body + chunk(b'fmt ', original[20:60])))
        try: inspect_at3(p)
        except ValueError as e: assert 'Duplicate' in str(e)
        else: raise AssertionError('duplicate fmt accepted')
        put(riff(body + b'X'))
        try: inspect_at3(p)
        except ValueError as e: assert 'trailing' in str(e)
        else: raise AssertionError('trailing RIFF garbage accepted')
    print('AT3 RIFF bounds regression passed')

if __name__ == '__main__': check()
