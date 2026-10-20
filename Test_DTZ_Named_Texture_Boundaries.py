"""Actual-file regression for GATG CHK named image pointer indexing.

Original DTZs are never written. Run with the four original reference files in
an input folder; default /mnt/data for development, or set GTA_DTZ_REFERENCE_DIR.
"""
from pathlib import Path
import hashlib, os, tempfile, zlib
from PIL import Image, ImageChops
from texture_formats import DTZDocument, png_to_tim2, chk_named_extent
from TM2_Replacement_Bridge import replace_dtz_from_tm2

MENU_EXPECTED = {
    'head': (0x40, 8),
    'doubleclef': (0x1440, 8),
    'kjah': (0x2840, 4),
    'rise': (0x3080, 4),
    'lips106': (0x38C0, 4),
    'radiodelmundo': (0x4100, 4),
    'msx': (0x4940, 8),
    'flashback': (0x5D40, 4),
    'libertyjam': (0x6580, 8),
    'libertyfree': (0x7980, 4),
}


def run(root):
    # Fully synthetic pointer-direction test runs on every installation.
    for bpp in (4, 8):
        pixel=64*64*bpp//8
        palette=(16 if bpp==4 else 256)*4
        raw=bytearray(0x5000)
        import struct
        struct.pack_into('<I',raw,0x400-8,0x40)
        struct.pack_into('<I',raw,0x400,0x40+pixel+palette)
        assert chk_named_extent(raw,0x400,64,64,bpp)==(0x40,pixel,0x40+pixel,palette)
        struct.pack_into('<I',raw,0x400,0x40+pixel+palette+16)
        try:
            chk_named_extent(raw,0x400,64,64,bpp)
        except ValueError: pass
        else: raise AssertionError('Malformed CHK extent was accepted')
    root = Path(root)
    lcs = root / 'GTA LCS PS2 GAME.DTZ'
    if not lcs.is_file():
        print(f'Synthetic CHK named boundary regression passed; optional original GAME.DTZ not present in {root}.')
        return
    original = lcs.read_bytes()
    h = hashlib.sha256(original).hexdigest()
    doc = DTZDocument(original)
    textures = {t.name:(i,t) for i,(group,t) in enumerate(doc.entries) if group=='menu'}
    assert set(textures)==set(MENU_EXPECTED), (set(textures),set(MENU_EXPECTED))
    for name,(off,bpp) in MENU_EXPECTED.items():
        i,t=textures[name]
        assert (t.pixel_offset,t.bpp)==(off,bpp), (name,hex(t.pixel_offset),t.bpp)
        assert t.palette_offset==off+(64*64*bpp//8)
        assert doc.preview(i).size==(64,64)
    fonts={t.name:t for group,t in doc.entries if group=='fonts'}
    assert {n:tex.pixel_offset for n,tex in fonts.items()}=={'font1':0x40,'font2':0x8080}
    # Verify current and previous boundaries differ: the old parser incorrectly
    # used 0x1440 for 'head' (the 'doubleclef' image).
    assert textures['head'][1].pixel_offset==0x40
    assert textures['doubleclef'][1].pixel_offset==0x1440

    with tempfile.TemporaryDirectory(prefix='dtz_index_test_') as td:
        td=Path(td)
        for name,bpp,color in [('head',8,(200,22,100,255)),('kjah',4,(28,200,144,255))]:
            doc=DTZDocument(original)
            index=next(i for i,(g,t) in enumerate(doc.entries) if g=='menu' and t.name==name)
            source=doc.preview(index)
            prev={t.name:doc.preview(i).tobytes() for i,(g,t) in enumerate(doc.entries) if g=='menu'}
            png=td/f'{name}.png'
            Image.new('RGBA',(64,64),color).save(png)
            tm2=png_to_tim2(png,bpp=bpp)
            section=next(s for s in doc.sections if s['name']=='menu')
            before=section['raw']
            replace_dtz_from_tm2(doc,index,tm2)
            after=next(s for s in doc.sections if s['name']=='menu')['raw']
            assert len(after)==len(before)
            tex=doc.entries[index][1]
            ok=set(range(tex.pixel_offset,tex.pixel_offset+tex.pixel_size)) | set(range(tex.palette_offset,tex.palette_offset+tex.palette_size))
            changes=[i for i,(a,b) in enumerate(zip(before,after)) if a!=b]
            assert changes, name+' did not modify any bytes'
            assert all(i in ok for i in changes), name+' changed unrelated bytes'
            for i,(g,t) in enumerate(doc.entries):
                if g=='menu' and t.name!=name:
                    assert doc.preview(i).tobytes()==prev[t.name], (name,t.name)
            target=td/f'{name}_edited.DTZ'
            doc.save(target)
            reread=DTZDocument(target.read_bytes())
            assert [ (g,t.name,t.pixel_offset) for g,t in doc.entries ] == [ (g,t.name,t.pixel_offset) for g,t in reread.entries ]
            assert reread.preview(index).tobytes()==doc.preview(index).tobytes()
            print(f'  {name} replacement: {len(changes)} CHK bytes changed in correct slot; 9 other radio icons preserved')
    # Other GAME.DTZ variants must not regress after the indexing fix.
    expected={
        'GTA LCS PSP GAME.DTZ':113,
        'GTA VCS PS2 GAME.DTZ':64,
        'GTA VCS PSP GAME.DTZ':97,
    }
    for filename,n in expected.items():
        p=root/filename
        if p.is_file():
            other=DTZDocument(p.read_bytes())
            assert len(other.entries)==n,(filename,len(other.entries),n)
    assert hashlib.sha256(lcs.read_bytes()).hexdigest()==h, 'Original reference changed!'
    print('DTZ named image indexing regression passed: exact named extents, previews, TM2 writes, save/reopen, immutable originals.')


if __name__=='__main__':
    run(os.environ.get('GTA_DTZ_REFERENCE_DIR','/mnt/data'))
