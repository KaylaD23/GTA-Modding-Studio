"""Windows smoke tests for bundled PSPDecrypt and ATRAC3plus at3tool.

Use only synthetic PCM audio. No copyrighted or encrypted game file is
required, read, or modified. A healthy CLI/encoder is not evidence of
successful game EBOOT decryption or in-game radio playback.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import wave

from Audio_Preparation_Profiles import inspect_at3

ROOT = Path(__file__).resolve().parent
# Hashes recorded when the user originally supplied these executables.
KNOWN_PSPDECRYPT_SHA256 = '6eea3ad4003718e36fb8655f74f30101553d97fe95f01ebe09c1d4f66741e756'
KNOWN_AT3TOOL_SHA256 = '823199113c59ffcd3fc3e1855dbb88a9cd44d29e82e7a7cfc42e8c92f2a61341'


class VerificationError(RuntimeError):
    """A PSP tool is missing or did not pass the minimum runtime checks."""


def _check_present(path: Path, label: str) -> None:
    if not path.is_file():
        raise VerificationError(f'{label} is missing: {path}. Re-extract the release ZIP.')
    if path.stat().st_size < 512:
        raise VerificationError(f'{label} is unexpectedly small: {path}')
    with path.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise VerificationError(f'{label} is not a Windows PE executable: {path}')


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _run(argv: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, errors='replace', timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise VerificationError(f'{Path(argv[0]).name} timed out after {timeout}s.') from exc
    except OSError as exc:
        raise VerificationError(f'Unable to start {Path(argv[0]).name}: {exc}. '
                                'Check Windows compatibility and required DLLs.') from exc


def _output_excerpt(process: subprocess.CompletedProcess[str], limit: int = 900) -> str:
    text = ((process.stdout or '') + '\n' + (process.stderr or '')).strip()
    return text[-limit:] if text else '(no output)'


def verify_pspdecrypt(path: Path) -> str:
    """Exercise help-mode startup; does not attempt real game decryption."""
    _check_present(path, 'PSPDecrypt')
    proc = _run([str(path), '--help'], timeout=15)
    message = (proc.stdout or '') + '\n' + (proc.stderr or '')
    # The supplied tool prints a distinctive usage header and PSP/PBP options.
    # Do not mistake a generic shell/error message for successful startup.
    if proc.returncode not in (0, 1) or 'Usage:' not in message or 'PBP' not in message:
        raise VerificationError('PSPDecrypt --help did not return expected usage text '
                                f'(exit {proc.returncode}): {_output_excerpt(proc)}')
    checksum = _sha256(path)
    note = 'original user-supplied SHA-256 matched' if checksum == KNOWN_PSPDECRYPT_SHA256 else 'binary differs from original user-supplied SHA-256'
    return ('PSPDecrypt executable STARTUP VERIFIED (--help); ' + note + '. '
            'Real EBOOT decryption NOT tested (requires a permitted sample EBOOT).')


def _create_pcm_test_signal(wav_path: Path) -> None:
    """Write a small, stereo 44.1 kHz/16-bit tone with no FFmpeg dependency."""
    rate = 44100
    frames = int(rate * 0.75)
    # Standard-library only; keep the sample tiny and deterministic.
    with wave.open(str(wav_path), 'wb') as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        block = bytearray()
        for i in range(frames):
            left = int(11000 * math.sin(2 * math.pi * 440 * i / rate))
            right = int(11000 * math.sin(2 * math.pi * 660 * i / rate))
            block.extend(struct.pack('<hh', left, right))
        wav.writeframes(block)


def verify_at3_encoder(path: Path) -> str:
    """Actually encode PCM and validate container, codec, metadata and payload."""
    _check_present(path, 'at3tool')
    dll = path.parent / 'msvcr71.dll'
    if not dll.is_file():
        raise VerificationError('at3tool requires the bundled msvcr71.dll beside the EXE: ' + str(dll))
    with tempfile.TemporaryDirectory(prefix='gta_studio_psp_smoke_') as directory:
        wav_path = Path(directory) / 'input.wav'
        at3_path = Path(directory) / 'output.at3'
        _create_pcm_test_signal(wav_path)
        proc = _run([str(path), '-e', '-br', '96', str(wav_path), str(at3_path)], timeout=60)
        if proc.returncode != 0 or not at3_path.is_file():
            raise VerificationError('at3tool failed to encode synthetic audio '
                                    f'(exit {proc.returncode}): {_output_excerpt(proc)}')
        if at3_path.stat().st_size < 200:
            raise VerificationError('at3tool returned an unexpectedly small output file.')
        try:
            result = inspect_at3(at3_path)
        except (ValueError, OSError) as exc:
            raise VerificationError(f'at3tool output failed RIFF/ATRAC inspection: {exc}') from exc
        if result['codec'] != 'ATRAC3plus':
            raise VerificationError('at3tool produced ' + result['codec'] + ', not ATRAC3plus.')
        if result['channels'] != 2 or result['sample_rate'] != 44100:
            raise VerificationError('at3tool output was not 44.1 kHz stereo.')
        if result['samples'] < 1000 or result['data_size'] < 128:
            raise VerificationError('at3tool output has insufficient sample/data length.')
        with at3_path.open('rb') as stream:
            stream.seek(result['data_offset'])
            first_payload = stream.read(min(result['data_size'], 512))
        if not first_payload or not any(first_payload):
            raise VerificationError('at3tool output contains no nonzero encoded audio data at its beginning.')
        checksum = _sha256(path)
        note = 'original user-supplied SHA-256 matched' if checksum == KNOWN_AT3TOOL_SHA256 else 'binary differs from original user-supplied SHA-256'
        return (f'ATRAC3plus ENCODING VERIFIED (96 kbps request, {result["sample_rate"]} Hz, '
                f'{result["channels"]} channels, {result["data_size"]} payload bytes); '
                + note + '. In-game LCS/VCS compatibility NOT verified.')


def run(root: Path) -> int:
    results = []
    checks = [
        ('PSPDecrypt', verify_pspdecrypt, root / 'Tools' / 'pspdecrypt' / 'pspdecrypt.exe'),
        ('ATRAC3plus', verify_at3_encoder, root / 'Tools' / 'at3tool' / 'at3tool.exe'),
    ]
    for name, fn, path in checks:
        try:
            print('[PSP tool] ' + fn(path), flush=True)
            results.append(True)
        except (VerificationError, ValueError) as exc:
            print(f'[PSP tool] FAILED [{name}]: {exc}', file=sys.stderr, flush=True)
            results.append(False)
    if all(results):
        print('PSP TOOL SMOKE TESTS PASSED: CLI startup and synthetic ATRAC3plus encoding.')
        return 0
    print('PSP TOOL SMOKE TESTS FAILED: at least one bundled tool is not usable.', file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description='Test PSPDecrypt startup and encode a synthetic ATRAC3plus sample.')
    parser.add_argument('--root', type=Path, default=ROOT, help='Extracted GTA Modding Studio directory.')
    args = parser.parse_args()
    if os.name != 'nt':
        print('Windows executable smoke tests require Windows (no bundled EXE was run).', file=sys.stderr)
        return 2
    return run(args.root.resolve())


if __name__ == '__main__':
    sys.exit(main())
