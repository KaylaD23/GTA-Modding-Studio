"""Native ISZ regression tests with independent isz-tool interoperability checks."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import zlib

import Native_ISZ as isz
import PS2_ISO_Patcher as patcher


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_self_test():
    external_reader = patcher._find_isz_tool()
    with tempfile.TemporaryDirectory(prefix='studio_native_isz_') as tmp:
        root = Path(tmp)
        source = root / 'native.iso'
        rng = random.Random(2843)
        raw = (b'\0' * (2 * 2048) + bytes(rng.getrandbits(8) for _ in range(2048)) +
               (b'ABCD' * 512) + b'\0' * 2048 + bytes(rng.getrandbits(8) for _ in range(2048)) +
               b'\0' * 2048 + bytes(rng.getrandbits(8) for _ in range(2048)))
        source.write_bytes(raw)
        original = sha(source)
        dst = root / 'packed.isz'
        result = isz.encode_isz(source, dst, block_size=2048)
        assert result['stored_blocks'] > 0
        assert result['zlib_blocks'] > 0
        assert result['zero_blocks'] > 0
        assert isz.verify_isz(dst, expected_sha256=original)['compressed_crc_ok']
        assert patcher._probe_uncompressed_size(dst, '.isz', lambda _:None) == len(raw)
        assert sha(source) == original

        if external_reader:
            command = [sys.executable, str(external_reader)]
            for args in [('verify', str(dst)), ('verify', '--slow', str(dst)), ('isz2iso', str(dst), str(root / 'independent.iso'))]:
                proc = subprocess.run(command + list(args), capture_output=True, text=True)
                assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
            assert sha(root / 'independent.iso') == original
            print('Independent isz-tool compatibility: PASS')
        else:
            print('Independent isz-tool compatibility: SKIPPED (tool not installed)')

        try:
            isz.encode_isz(source, dst, block_size=2048)
            raise AssertionError('Output collision accepted')
        except FileExistsError:
            pass
        try:
            isz.encode_isz(source, source, block_size=2048)
            raise AssertionError('Source overwrite accepted')
        except ValueError:
            pass
        assert sha(source) == original

        tampered = root / 'tampered.isz'
        content = bytearray(dst.read_bytes())
        content[-1] ^= 7
        tampered.write_bytes(content)
        try:
            isz.verify_isz(tampered)
            raise AssertionError('Corrupt stream accepted')
        except ValueError:
            pass

        encrypted = root / 'encrypted.isz'
        content = bytearray(dst.read_bytes())
        content[16] = 2  # ISZ header encryption_type at offset 16
        encrypted.write_bytes(content)
        try:
            patcher._probe_uncompressed_size(encrypted, '.isz', lambda _:None)
            raise AssertionError('Encrypted ISZ accepted')
        except ValueError as exc:
            assert 'encrypt' in str(exc).lower()

        # Fully exercised GTA SA ISO patch -> native ISZ path.
        sa_source = root / 'sa_source.iso'
        patcher._build_test_iso(sa_source)
        sa_original = sha(sa_source)
        stage = root / 'sa_stage'
        (stage / 'AUDIO' / 'STREAMS').mkdir(parents=True)
        (stage / 'AUDIO' / 'CONFIG').mkdir(parents=True)
        pak = stage / 'AUDIO' / 'STREAMS' / 'TK.PAK'
        lookup = stage / 'AUDIO' / 'CONFIG' / 'TRAKLKUP.DAT'
        pak.write_bytes(b'NEW-PAK-' * 400)
        lookup.write_bytes(b'NEW-LOOKUP' * 20)
        manifest = {'tool':'GTA Modding Studio v0.3.8', 'purpose':'Prepared files for GTA San Andreas PS2 ISO patching; staging does not modify an ISO.', 'archive': {'filename':'TK.PAK','archive_id':'0x0F'}, 'files':[
            {'game_disc_path':'AUDIO/STREAMS/TK.PAK','staged_path':str(pak),'size_bytes':pak.stat().st_size,'sha256':sha(pak)},
            {'game_disc_path':'AUDIO/CONFIG/TRAKLKUP.DAT','staged_path':str(lookup),'size_bytes':lookup.stat().st_size,'sha256':sha(lookup)}
        ]}
        (stage / 'ISO_PATCH_MANIFEST.json').write_text(json.dumps(manifest))
        sa_out = root / 'sa_patched.isz'
        outcome = patcher.patch_sa_ps2_radio_image(sa_source,sa_out,stage)
        assert outcome['output_format']=='.isz' and outcome['report_path'].exists()
        assert outcome['files'] and sha(sa_source)==sa_original
        iso_copy = root / 'sa_patched.iso'
        if external_reader:
            patcher._run_isz_decompress(sa_out,iso_copy, lambda _: None)
            parsed = patcher.Iso9660(iso_copy)
            parsed.scan()
            assert any(record.path.upper() == 'AUDIO/STREAMS/TK.PAK' for record in parsed.records)

        # GTA LCS PSP direct radio stream -> native ISZ with cache patching.
        lcs_iso = root / 'lcs_original.iso'
        patcher._build_direct_profile_test_iso(lcs_iso,'gta_lcs_psp')
        lcs_source_hash=sha(lcs_iso)
        stream = root / 'KJAH.AT3'
        stream.write_bytes(patcher._make_test_at3(0x3000,True,seed=0x73))
        lcs_stage=root / 'lcs_stage'
        patcher.prepare_game_radio_staging(stream,lcs_stage,'gta_lcs_psp')
        lcs_out=root / 'lcs_patched.isz'
        outcome=patcher.patch_game_radio_image(lcs_iso,lcs_out,lcs_stage,'gta_lcs_psp')
        assert outcome['output_format']=='.isz' and outcome['report_path'].is_file()
        assert sha(lcs_iso)==lcs_source_hash
        assert isz.inspect_isz(lcs_out)['bytes']>=lcs_iso.stat().st_size
        if external_reader:
            patcher._run_isz_decompress(lcs_out, root/'lcs_final.iso',lambda _:None)
            image = patcher.Iso9660(root/'lcs_final.iso')
            image.scan()
            record = [r for r in image.records if r.path.upper()=='PSP_GAME/USRDIR/AUDIO/MUSIC/KJAH.AT3']
            assert len(record)==1
            with (root/'lcs_final.iso').open('rb') as f:
                f.seek(record[0].data_extent * patcher.SECTOR_SIZE)
                assert f.read(record[0].size)==stream.read_bytes()
        print('Native ISZ regression tests passed: codec, tampering, source preservation, SA PS2 + LCS PSP integration.')


if __name__ == '__main__':
    run_self_test()
