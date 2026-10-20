"""Offline format regression tests. Synthetic fixtures contain no game assets.

Set GTA_TEXTURE_REFERENCE_DIR to a directory of user-owned .TXD/.DTZ/.TM2 files
for optional reference-archive round-trip checks without bundling those files.
"""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
from PIL import Image, ImageChops
from texture_formats import (
    TIM2Document, TXDDocument, DTZDocument, png_to_tim2, png_to_tim2_template,
    swizzle8, deswizzle8, u16,
)


def run_self_test():
    with tempfile.TemporaryDirectory(prefix='gta_textures_test_') as root_str:
        root=Path(root_str)
        for w,h in [(64,64),(128,128)]:
            raw=bytes((x*7+y*5)&255 for y in range(h) for x in range(w))
            assert deswizzle8(swizzle8(raw,w,h),w,h)==raw
        for bpp in (4,8):
            image=Image.new('RGBA',(64,64),(0,0,0,0))
            for y in range(64):
                for x in range(64):
                    image.putpixel((x,y),((x*15)%256,(y*17)%256,100,255 if x%2 else 120))
            png=root/f'input_{bpp}.png';image.save(png)
            tm2=png_to_tim2(png,bpp=bpp)
            assert u16(tm2,30)==(16 if bpp==4 else 256)
            decoded=TIM2Document(tm2)
            assert decoded.width==64 and decoded.height==64 and decoded.bpp==bpp
            out=png_to_tim2_template(png,tm2)
            verify=TIM2Document(out)
            assert len(out)==len(tm2) and verify.width==64 and verify.bpp==bpp
            assert out[:64]==tm2[:64], 'Template conversion changed metadata'
            try:
                wrong=Image.new('RGBA',(128,128));wrong.save(root/'wrong.png')
                png_to_tim2_template(root/'wrong.png',tm2)
            except ValueError: pass
            else: raise AssertionError('Template accepted wrong dimensions')
        if os.environ.get('GTA_TEXTURE_REFERENCE_DIR'):
            directory=Path(os.environ['GTA_TEXTURE_REFERENCE_DIR'])
            observed={'txd':0,'dtz':0,'tm2':0}
            for source in directory.iterdir():
                ext=source.suffix.lower()
                if ext not in ('.txd','.dtz','.tm2'):continue
                original_bytes=source.read_bytes()
                if ext=='.tm2':
                    doc=TIM2Document(original_bytes)
                    png=root/'original.png';doc.image().save(png)
                    new=png_to_tim2_template(png,source)
                    assert len(new)==len(original_bytes)
                    observed['tm2']+=1
                    continue
                doc=TXDDocument(original_bytes) if ext=='.txd' else DTZDocument(original_bytes)
                if ext=='.txd':
                    textures=doc.textures
                    original=textures[0].image(doc.data)
                else:
                    textures=doc.entries
                    if not textures:continue
                    original=doc.preview(0)
                png=root/'reference.png';original.save(png)
                doc.replace(0,png)
                modified=textures[0].image(doc.data) if ext=='.txd' else doc.preview(0)
                assert ImageChops.difference(original,modified).getbbox() is None
                dst=root/('result'+ext);doc.save(dst)
                reread=TXDDocument(dst.read_bytes()) if ext=='.txd' else DTZDocument(dst.read_bytes())
                assert len(textures)==len(reread.textures if ext=='.txd' else reread.entries)
                assert source.read_bytes()==original_bytes, 'Source was mutated'
                observed[ext.lstrip('.')]+=1
            assert observed['txd']>=1 and observed['dtz']>=1 and observed['tm2']>=1,observed
            print('Reference archive round-trips passed:',observed)
    print('Texture codec self-tests passed: 4/8bpp TIM2, palette counts, PS2 swizzle inversion, template preservation and input guards.')


if __name__=='__main__': run_self_test()
