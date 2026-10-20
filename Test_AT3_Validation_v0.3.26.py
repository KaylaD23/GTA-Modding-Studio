"""Regression checks for ATRAC subtype validation in audio preparation."""
import struct
import tempfile
from pathlib import Path
from Audio_Preparation_Profiles import inspect_at3


def make_wave(subtype):
    fmt = (struct.pack('<HHIIHHH', 0xfffe, 2, 44100, 12000, 0x180, 16, 22)
           + struct.pack('<HI', 16, 3) + subtype)
    chunks = b''.join(tag + struct.pack('<I', len(data)) + data +
                      (b'\0' if len(data) % 2 else b'')
                      for tag, data in ((b'fmt ', fmt), (b'fact', struct.pack('<I', 1000)),
                                        (b'data', bytes(32))))
    return b'RIFF' + struct.pack('<I', 4 + len(chunks)) + b'WAVE' + chunks


def run():
    pcm = bytes.fromhex('0100000000001000800000aa00389b71')
    atrac = bytes.fromhex('7002000000001000800000aa00389b71')
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'test.at3'
        path.write_bytes(make_wave(pcm))
        try:
            inspect_at3(path)
        except ValueError as exc:
            assert 'subtype' in str(exc)
        else:
            raise AssertionError('Extensible PCM was incorrectly accepted as ATRAC')
        path.write_bytes(make_wave(atrac))
        assert inspect_at3(path)['samples'] == 1000
        path.write_bytes(make_wave(atrac)[:-1])
        try:
            inspect_at3(path)
        except ValueError as exc:
            assert 'RIFF size' in str(exc)
        else:
            raise AssertionError('Truncated RIFF was incorrectly accepted')
    print('AT3 codec and truncation regression checks passed')


if __name__ == '__main__':
    run()
