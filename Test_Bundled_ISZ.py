"""Distribution regression: ISZ input works without downloading isz-tool.py."""
from __future__ import annotations

import ast
import hashlib
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile

import Native_ISZ
import PS2_ISO_Patcher as patcher


def run_self_test() -> None:
    root = Path(__file__).resolve().parent
    script = root / "Tools" / "image_tools" / "isz-tool.py"
    license_path = root / "Tools" / "image_tools" / "LICENSE-isz-tool.txt"
    installer = (root / "Install_Image_Tools.ps1").read_text(encoding="utf-8-sig")
    assert script.is_file(), f"Missing bundled ISZ extractor: {script}"
    assert license_path.is_file(), f"Missing ISZ licence: {license_path}"
    content = script.read_text(encoding="utf-8")
    ast.parse(content, filename=str(script))
    assert "Copyright (C) 2012 Olivier Serres" in content
    assert "GNU GENERAL PUBLIC LICENSE" in license_path.read_text(encoding="utf-8")
    assert (root / "Tools" / "image_tools" / "LICENSE-MaxCSO.md").is_file()
    assert patcher._find_isz_tool() == script
    assert "Bundled ISZ decoder missing" in installer
    assert "raw.githubusercontent.com/oserres/isz-tool" not in installer, "ISZ setup must not download on install"
    # A licence must be SHIPPED in a redistributed release, but removing the
    # local text file must never prevent the user from using the installed tool.
    assert "Bundled ISZ GPL licence is missing" not in installer
    assert "Bundled MaxCSO licence is missing" not in installer
    assert "Join-Path $targetRoot 'LICENSE-isz-tool.txt'" not in installer
    assert "Join-Path $targetRoot 'LICENSE-MaxCSO.md'" not in installer

    help_result = subprocess.run([sys.executable, str(script), "--help"],
                                 capture_output=True, text=True, timeout=20)
    assert help_result.returncode == 0, help_result.stderr
    assert "isz2iso" in help_result.stdout

    # The app's native encoder and separately bundled third-party decoder
    # must interoperate directly after ZIP extraction, without installation.
    with tempfile.TemporaryDirectory(prefix="gta_studio_isz_bundled_") as tmp:
        tmp_path = Path(tmp)
        # Prove that the third-party decoder runs when the user's local copy
        # no longer contains the separate GPL text file.
        standalone = tmp_path / "image_tools" / "isz-tool.py"
        standalone.parent.mkdir()
        shutil.copyfile(script, standalone)
        assert not (standalone.parent / "LICENSE-isz-tool.txt").exists()
        help_without_licence = subprocess.run(
            [sys.executable, str(standalone), "--help"],
            capture_output=True, text=True, timeout=20,
        )
        assert help_without_licence.returncode == 0, help_without_licence.stderr
        source = tmp_path / "test.iso"
        encoded = tmp_path / "test.isz"
        decoded = tmp_path / "restored.iso"
        data = (b"GTA-STUDIO-ISZ-BUNDLE" * 500 + bytes(range(256)) * 8)
        data += bytes(-len(data) % 2048)
        source.write_bytes(data)
        original_sha = hashlib.sha256(data).hexdigest()
        Native_ISZ.encode_isz(source, encoded, block_size=2048)
        patcher._run_isz_decompress(encoded, decoded, lambda _: None)
        assert hashlib.sha256(decoded.read_bytes()).hexdigest() == original_sha
        decoded_no_licence = tmp_path / "no_licence_restored.iso"
        decode_without_licence = subprocess.run(
            [sys.executable, str(standalone), "isz2iso", str(encoded), str(decoded_no_licence)],
            capture_output=True, text=True, timeout=30,
        )
        assert decode_without_licence.returncode == 0, decode_without_licence.stderr
        assert hashlib.sha256(decoded_no_licence.read_bytes()).hexdigest() == original_sha
    print("ISZ distribution licence present; local licence optional; offline ISZ->ISO conversion: PASS")


if __name__ == "__main__":
    run_self_test()
