"""Regression tests for VCS PS2 compressed menu and font XET indexing.

Set GTA_VCS_PS2_DTZ_REFERENCE to an original GAME.DTZ for real archive tests.
Never changes the original input.
"""
from __future__ import annotations
import os
import struct
import tempfile
import zlib
from pathlib import Path
from PIL import Image
from texture_formats import DTZDocument
from VCS_XET_Textures import read_vcs_xet, read_named_xet_textures
from DTZ_CHK_Relocation import _gatg_tables


def test_original(path):
    path = Path(path)
    original = path.read_bytes()
    gatg = zlib.decompress(original)
    assert gatg[:4] == b'GATG'
    for name, field, sentinel, size in (
        ('menu', 0xE0, b'radio_4\0', 33),
        ('fonts', 0xFC, b'fancytext\0', 4),
    ):
        source = read_vcs_xet(gatg, field, expected_name=sentinel)
        assert source is not None, name
        at, clen, raw = source
        assert at == struct.unpack_from('<I', gatg, field)[0]
        assert zlib.decompress(gatg[at:at+clen]) == raw
        parsed, unsupported = read_named_xet_textures(raw, group=name)
        assert len(parsed) == size and not unsupported, (name, len(parsed), unsupported)
        assert len({t.name for t in parsed}) == size
        for tex in parsed:
            assert tex.pixel_size == tex.width * tex.height * tex.bpp // 8
            assert tex.palette_size == (16 if tex.bpp == 4 else 256) * 4
            assert tex.pixel_offset + tex.pixel_size == tex.palette_offset
            assert tex.palette_offset + tex.palette_size <= len(raw)
            pic = tex.image(raw)
            assert pic.mode == 'RGBA' and pic.size == (tex.width, tex.height)
    doc = DTZDocument(original)
    groups = {}
    for i, (section, tex) in enumerate(doc.entries):
        groups.setdefault(section if not section.startswith('XET@') else 'radar', []).append((i,tex))
    assert len(groups['menu']) == 33
    assert len(groups['fonts']) == 4
    assert len(groups['radar']) == 64
    assert len(groups['hud']) == 68
    assert len(groups['generic']) == 53
    assert len(groups['particle']) == 131
    assert len(doc.entries) == 353
    menu = {t.name: (i,t) for i,t in groups['menu']}
    fonts = {t.name: (i,t) for i,t in groups['fonts']}
    assert {'radio_1','radio_4','radio_9','frontend_background','gta3_pause_map'} <= menu.keys()
    assert set(fonts) == {'fancytext', 'pagertext', 'gametext', 'bigtext'}
    assert (menu['radio_4'][1].pixel_offset, menu['radio_4'][1].width,
            menu['radio_4'][1].height, menu['radio_4'][1].bpp) == (0x8E5F0,64,64,8)
    assert (fonts['gametext'][1].width,fonts['gametext'][1].height,fonts['gametext'][1].bpp)==(256,256,8)
    _gatg_tables(doc.data)
    with tempfile.TemporaryDirectory(prefix='vcs_dtz_xet_') as tdir:
        dest = Path(tdir)/'unchanged_edit.DTZ'
        doc.save(dest)
        assert DTZDocument(dest.read_bytes()).entries == doc.entries
        assert path.read_bytes() == original
        # Replacing a menu raster must not modify the fonts raw XET.
        i,tex=menu['radio_4']
        image = doc.preview(i)
        image.putpixel((0,0),image.getpixel((1,0)))
        replacement = Path(tdir)/'radio4.png'
        image.save(replacement)
        prior_fonts=next(s for s in doc.sections if s['name']=='fonts')['raw']
        prior_direct=doc.direct_entries[:]
        before_metadata=[(g,t.name,t.width,t.height,t.bpp) for g,t in doc.entries]
        doc.replace(i,replacement)
        after_metadata=[(g,t.name,t.width,t.height,t.bpp) for g,t in doc.entries]
        assert before_metadata == after_metadata
        assert next(s for s in doc.sections if s['name']=='fonts')['raw'] == prior_fonts
        assert len(doc.direct_entries) == len(prior_direct)
        _gatg_tables(doc.data)
        dest = Path(tdir)/'edited.DTZ'
        doc.save(dest)
        again=DTZDocument(dest.read_bytes())
        assert [(g,t.name) for g,t in again.entries] == [(g,t.name) for g,t in doc.entries]
        assert again.preview(i).size == (64,64)
        assert path.read_bytes() == original
    print('VCS PS2 XET: 33 menu + 4 fonts + 64 radar; previews, replacement, save/reopen passed')


def test_header_rejection():
    fake = bytearray(1024)
    fake[:4]=b'GATG'
    struct.pack_into('<I', fake, 0xE0, 0x3FF)
    assert read_vcs_xet(bytes(fake),0xE0,expected_name=b'radio_4\0') is None
    struct.pack_into('<I', fake, 0xE0, 0x200)
    fake[0x200:0x205] = b'nope!'
    assert read_vcs_xet(bytes(fake),0xE0,expected_name=b'radio_4\0') is None
    assert read_named_xet_textures(b'xet\0',group='menu') == ([],[])


def run_self_test():
    test_header_rejection()
    ref=os.environ.get('GTA_VCS_PS2_DTZ_REFERENCE')
    if ref:
        test_original(ref)
    else:
        print('VCS PS2 reference not provided; structural-negative tests only')


if __name__ == '__main__':
    run_self_test()
