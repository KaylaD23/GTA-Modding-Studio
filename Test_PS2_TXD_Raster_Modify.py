"""Native TXD raster palette options and serialized conversion regression tests.

Optional user-owned reference TXDs are only read, never modified or bundled.
"""
from __future__ import annotations
import sys, os, tempfile, hashlib
from pathlib import Path
from texture_formats import TXDDocument
from PS2_TXD_Raster_Edit import (
    validate_ps2_raster_settings, donor_candidates, convert_txd_palette_depth,
    PIXEL_FORMAT_CHOICES, PALETTE_CHOICES,
)
from texture_actions import rename_txd_texture


def expect_raises(fn, text=None):
    try: fn()
    except ValueError as exc:
        if text and text not in str(exc): raise AssertionError(f'Expected {text}, got {exc}')
        return
    raise AssertionError('Unsafe operation was accepted')


def run_self_test():
    assert PALETTE_CHOICES == ('PAL4','PAL8')
    assert all(v in PIXEL_FORMAT_CHOICES for v in ('RASTER_1555','RASTER_565','RASTER_4444','RASTER_LUM','RASTER_8888','RASTER_888','RASTER_555','RASTER_LUM_ALPHA'))
    assert validate_ps2_raster_settings(4, mode='Original', palette='PAL8',pixel_format='RASTER_1555')==4
    assert validate_ps2_raster_settings(4, mode='Palettized',palette='PAL4',pixel_format='RASTER_8888')==4
    assert validate_ps2_raster_settings(4, mode='Palettized',palette='PAL8',pixel_format='RASTER_8888')==8
    assert validate_ps2_raster_settings(8, mode='Palettized',palette='PAL4',pixel_format='RASTER_8888')==4
    for fmt in PIXEL_FORMAT_CHOICES:
        if fmt != 'RASTER_8888':
            expect_raises(lambda fmt=fmt: validate_ps2_raster_settings(4,mode='Palettized',palette='PAL4',pixel_format=fmt), 'has been validated')
    expect_raises(lambda:validate_ps2_raster_settings(4,mode='Raw Raster',palette='PAL4',pixel_format='RASTER_8888'), 'requires')
    expect_raises(lambda:validate_ps2_raster_settings(4,mode='Palettized',palette='PAL4',pixel_format='RASTER_8888',generate_mipmaps=True),'mip')
    directory=Path(os.environ.get('GTA_PS2_TXD_REFERENCE_DIR','/mnt/data'))
    vice=directory/'GTA VICE CITY PS2 FRONTEN2.TXD'
    sa=directory/'GTA SA PS2 FRONTEN1.TXD'
    if not vice.exists() or not sa.exists():
        print('Skipping reference-image checks: supply GTA_PS2_TXD_REFERENCE_DIR. Mode and guard tests passed.')
        return
    unchanged={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (vice,sa)}
    base=TXDDocument(vice.read_bytes())
    assert len(base.textures)==25
    examples=[('espantoso', 8),('vcpr',4)]
    with tempfile.TemporaryDirectory(prefix='gta_ps2_palette_regression_') as temp:
        for name,dest_bpp in examples:
            index=next(i for i,t in enumerate(base.textures) if t.name==name)
            matches=donor_candidates(base,index,base,dest_bpp)
            assert matches,(name,dest_bpp)
            converted=convert_txd_palette_depth(base,index,dest_bpp,base,matches[0])
            assert converted.textures[index].bpp==dest_bpp
            assert converted.textures[index].name==name
            assert len(converted.textures)==len(base.textures)
            rename_txd_texture(converted,index,name+'_x')
            assert converted.textures[index].name==name+'_x'
            out=Path(temp)/(name+'_edited.txd')
            converted.save(out)
            reread=TXDDocument(out.read_bytes())
            assert reread.textures[index].name==name+'_x'
            assert reread.textures[index].bpp==dest_bpp
            assert reread.textures[index].image(reread.data).size==(64,64)
            print(name,'converted to PAL'+str(dest_bpp),'saved size',len(out.read_bytes()),'reread verified')
        # A different game RenderWare version cannot be a donor.
        sa_doc=TXDDocument(sa.read_bytes())
        assert not donor_candidates(sa_doc,0,base,8), 'Cross-version donor was incorrectly accepted'
    for p,digest in unchanged.items():
        assert hashlib.sha256(p.read_bytes()).hexdigest()==digest, f'Modified source: {p}'
    print('PS2 TXD property and real native PAL4/PAL8 conversion tests passed; source files unchanged.')


if __name__=='__main__': run_self_test()
