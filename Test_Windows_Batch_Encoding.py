"""Prevent UTF-8 BOMs and malformed first lines in Windows .bat/.cmd files."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def test_batch_scripts():
    scripts = sorted([*ROOT.rglob('*.bat'), *ROOT.rglob('*.cmd')])
    assert scripts, 'No Windows batch scripts found'
    for path in scripts:
        data = path.read_bytes()
        assert not data.startswith((b'\xef\xbb\xbf', b'\xff\xfe', b'\xfe\xff')), f'BOM in {path}'
        assert data.startswith(b'@echo off\r\n'), f'Invalid first line or line endings: {path}'
        assert b'\n' not in data.replace(b'\r\n', b''), f'Non-Windows newline in {path}'
        data.decode('ascii')
    installer = (ROOT / 'Install_Dependencies.bat').read_bytes()
    assert b'REQUIRED INSTALLATION STEPS COMPLETED' in installer
    assert b'Local FFmpeg: tools\\ffmpeg\\bin' not in installer
    # Do not pass a quoted %~dp0 path as a Python argv value: the trailing
    # Windows backslash can escape the closing quote, adding a literal ".
    # Verify_PSP_Tools.py derives its root from __file__ by default.
    assert b'%PYTHON_CMD% "%~dp0Verify_PSP_Tools.py"' in installer
    assert b'--root "%~dp0"' not in installer
    assert b'--root' not in installer
    print(f'PASS: {len(scripts)} batch files are BOM-free ASCII with CRLF and installer reports truthful FFmpeg location.')

if __name__ == '__main__':
    test_batch_scripts()
