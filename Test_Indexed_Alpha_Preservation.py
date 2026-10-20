"""RGBA/CLUT regressions for GTA Stories menu icons and DTZ texture replacement.

Run: python Test_Indexed_Alpha_Preservation.py [--reference-dtz GAME.DTZ]
"""
from __future__ import annotations
import argparse
import tempfile
from pathlib import Path
from PIL import Image

from RGBA_Indexed_Quantizer import quantize_rgba
from TIM2_Converter import convert_png_to_tim2
from texture_formats import TIM2Document, DTZDocument, png_to_tim2
from TM2_Replacement_Bridge import replace_dtz_from_tm2


def test_same_rgb_different_opacity():
    # Previous RGB-only quantization coalesced all these into ONE CLUT entry,
    # destroying transparent and translucent boundaries around icons.
    image = Image.new('RGBA', (8, 1))
    original = [(0, 210, 225, a) for a in (0, 32, 64, 96, 128, 176, 224, 255)]
    image.putdata(original)
    indices, palette = quantize_rgba(image, 16, dither=False)
    reconstructed = [palette[i] for i in indices]
    assert reconstructed == [(0, 0, 0, 0)] + original[1:], reconstructed
    assert len(set(indices)) == 8


def test_transparent_background_is_not_opaque():
    image = Image.new('RGBA', (64, 64), (9, 40, 71, 0))
    for y in range(8, 56):
        for x in range(8, 56):
            # Competing alpha values share identical RGB. This models an icon
            # edge with a transparent background and a partially covered rim.
            opacity = (0 if x < 10 else 64 if x == 10 else
                       140 if x == 11 else 255)
            image.putpixel((x, y), (25, 198, 224, opacity))
    with tempfile.TemporaryDirectory(prefix='gta_alpha_') as folder:
        src = Path(folder)/'icon.png'
        dest = Path(folder)/'icon.tm2'
        image.save(src)
        convert_png_to_tim2(src, dest, image_format='4bit')
        tm2 = TIM2Document(dest.read_bytes())
        # The new PNG→TM2 converter must maintain different alpha values at
        # the same RGB, including one fully transparent palette entry.
        pal = tm2.texture.palette(tm2.data)
        assert sorted(set(a for _r,_g,_b,a in pal)) == [0, 32, 70, 128], set(a for _,_,_,a in pal)
        pixels = tm2.texture.image(tm2.data)
        assert pixels.getpixel((0, 0))[3] == 0
        assert pixels.getpixel((10, 20))[3] == 64
        assert pixels.getpixel((11, 20))[3] == 140
        assert pixels.getpixel((12, 20))[3] == 255


def test_integrated_png_to_tim2_keeps_alpha():
    # The radio/texture workbench also offers a separate native TIM2 path.
    # That path must not silently re-introduce the old RGB-only quantizer.
    with tempfile.TemporaryDirectory(prefix='gta_alpha_native_') as folder:
        original=Image.new('RGBA',(64,64),(0,0,0,0))
        for y in range(12,52):
            for x in range(12,52):
                original.putpixel((x,y),(10,210,225,128 if x==12 else 255))
        src=Path(folder)/'native.png';original.save(src)
        tm2=TIM2Document(png_to_tim2(src,bpp=4))
        decoded=tm2.image()
        assert decoded.getpixel((0,0))[3] == 0
        assert decoded.getpixel((12,24))[3] == 128
        assert decoded.getpixel((15,24))[3] == 255


def test_overflow_transparency():
    # Overflow PAL4's 16 entry budget: transparent background must STILL
    # receive a dedicated exact-zero-alpha index.
    pixels = [(i*11 % 256, i*27 % 256, i*41 % 256, 255) for i in range(24)]
    pixels += [(70, 80, 90, 0)] * 8
    img = Image.new('RGBA', (len(pixels), 1));img.putdata(pixels)
    indices, palette = quantize_rgba(img, 16)
    assert all(palette[indices[i]][3] == 0 for i in range(24, len(pixels)))
    assert all(palette[indices[i]][3] != 0 for i in range(24))


def test_reference_dtz_image_roundtrip(path):
    # Preserve actual PS2 menu pixels and CLUT while regenerating a PAL4 TM2
    # and forcing a nested CHK rebuild. The archive is never edited in-place.
    original=DTZDocument(Path(path).read_bytes())
    selected=next(i for i,(section,t) in enumerate(original.entries)
                  if section=='menu' and t.name=='kjah')
    original_image=original.preview(selected)
    with tempfile.TemporaryDirectory(prefix='gta_alpha_ref_') as folder:
        src=Path(folder)/'kjah.png'; dest=Path(folder)/'kjah.tm2'
        original_image.save(src)
        convert_png_to_tim2(src,dest,image_format='4bit')
        replace_dtz_from_tm2(original,selected,dest)
        final_image=original.preview(selected)
        assert original_image.tobytes()==final_image.tobytes(), 'Image changed after PNG → TM2 → DTZ'
        with (Path(folder)/'rebuilt.dtz').open('wb') as f:f.write(original.compressed)
        reopened=DTZDocument((Path(folder)/'rebuilt.dtz').read_bytes())
        assert reopened.preview(selected).tobytes()==original_image.tobytes()


def main():
    p=argparse.ArgumentParser();p.add_argument('--reference-dtz',type=Path)
    args=p.parse_args()
    test_same_rgb_different_opacity()
    test_transparent_background_is_not_opaque()
    test_overflow_transparency()
    test_integrated_png_to_tim2_keeps_alpha()
    print('RGBA quantizer, transparency and TIM2 regressions passed')
    if args.reference_dtz:
        test_reference_dtz_image_roundtrip(args.reference_dtz)
        print('Original LCS PS2 kjah PNG→TM2→DTZ save/reopen round-trip passed')


if __name__ == '__main__': main()
