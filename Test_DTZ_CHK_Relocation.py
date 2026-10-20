"""GATG dual-table relocation tests, with optional original GAME.DTZ fixtures.

Set GTA_DTZ_REFERENCE_DIR to the directory with four original-game archives.
"""
from __future__ import annotations
import os
import random
import struct
import unittest
import zlib
from pathlib import Path

from DTZ_CHK_Relocation import _gatg_tables, pack_chk, replace_chk_stream


def fixture(label='menu', *, duplicate=False):
    raw=b'xet\0'+b'\0'*512
    compressed=zlib.compress(raw,9)
    first=0x300
    # Four first-table fields; each is a valid pointer value into asset data.
    first_values=(0x160, 0x188, 0x1e0, 0x220)
    second=first+4*len(first_values)
    pointer=0xd4 if label=='menu' else 0x200
    sites=[pointer] + ([0x204] if duplicate else []) + list(range(first,second,4))
    data=bytearray(second+len(sites)*4)
    data[:4]=b'GATG'
    struct.pack_into('<6I',data,4,1,len(data),first,second,len(sites),first)
    data[0x120:0x120+len(compressed)]=compressed
    for site in sites[:1+(1 if duplicate else 0)]:
        struct.pack_into('<I',data,site,0x120)
    struct.pack_into('<4I',data,first,*first_values)
    for i,site in enumerate(sites):
        struct.pack_into('<I',data,second+i*4,site)
    return bytes(data), {'name':label,'offset':0x120,
                          'compressed_size':len(compressed),'raw':raw},pointer


class RelocationTests(unittest.TestCase):
    def test_growth_rebases_both_tables_and_moved_field_sites(self):
        for label in ('menu','XET@000200'):
            with self.subTest(label=label):
                src,section,pointer=fixture(label)
                first,second,count,sites=_gatg_tables(src)
                packed=pack_chk(b'xet\0'+random.Random(801).randbytes(9000))
                out,moved=replace_chk_stream(src,section,packed)
                self.assertTrue(moved)
                new_first,new_second,new_count,new_sites=_gatg_tables(out)
                delta=new_first-first
                self.assertEqual(new_second,second+delta)
                self.assertEqual(new_count,count)
                self.assertEqual(len(out),len(src)+delta)
                self.assertEqual(out[new_first:new_second],src[first:second])
                self.assertEqual(new_sites,tuple(site+delta if site>=first else site for site in sites))
                self.assertEqual(struct.unpack_from('<I',out,pointer)[0],first)
                self.assertEqual(struct.unpack_from('<I',out,0x18)[0],new_first)
                self.assertEqual(zlib.decompress(out[first:first+len(packed)]),b'xet\0'+random.Random(801).randbytes(9000))
                self.assertEqual(out[section['offset']:section['offset']+section['compressed_size']],
                                 src[section['offset']:section['offset']+section['compressed_size']])
                self.assertEqual(out[:8],src[:8])

    def test_in_place_replacement_preserves_archive_header_and_both_tables(self):
        src,section,_=fixture()
        packed=pack_chk(section['raw'])
        out,moved=replace_chk_stream(src,section,packed)
        self.assertFalse(moved)
        self.assertEqual(len(out),len(src))
        self.assertEqual(out[0:0x100],src[0:0x100])
        self.assertEqual(out[0x300:],src[0x300:])

    def test_duplicate_reference_rejected(self):
        src,section,_=fixture('XET@000200',duplicate=True)
        with self.assertRaisesRegex(ValueError,'exactly one indexed pointer'):
            replace_chk_stream(src,section,pack_chk(random.Random(7).randbytes(5000)))

    def test_corrupt_first_table_and_bad_pointer_rejected(self):
        src,section,_=fixture()
        packed=pack_chk(random.Random(7).randbytes(5000))
        for index,broken in enumerate((0x0c,0x10,0x18)):
            bad=bytearray(src)
            struct.pack_into('<I',bad,broken,0x2fc)
            with self.subTest(field=hex(broken)),self.assertRaises(ValueError):
                replace_chk_stream(bad,section,packed)
        bad=bytearray(src)
        # Remove one relocation-site reference into the first table.
        struct.pack_into('<I',bad,0x310+4,0x200)
        with self.assertRaises(ValueError):
            replace_chk_stream(bad,section,packed)
        bad=bytearray(src); bad[0x120]^=0xff
        with self.assertRaises(ValueError):
            replace_chk_stream(bad,section,packed)

    def test_old_broken_between_tables_relocation_rejected(self):
        from texture_formats import DTZDocument
        import tempfile
        src,section,site=fixture()
        first,second,count,sites=_gatg_tables(src)
        packed=pack_chk(b'xet\0'+random.Random(802).randbytes(5000))
        insert=packed+b'\0'*(-len(packed)%4)
        bad=bytearray(src[:second]+insert+src[second:])
        struct.pack_into('<I',bad,8,len(bad))
        struct.pack_into('<I',bad,0x10,second+len(insert))
        struct.pack_into('<I',bad,site,second)
        with self.assertRaisesRegex(ValueError,'cross-references'):
            _gatg_tables(bytes(bad))
        doc=DTZDocument(zlib.compress(bad,9))
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError,'cross-references'):
                doc.save(Path(td)/'corrupt.DTZ')

    def test_unknown_layout_rejected(self):
        src,section,_=fixture()
        with self.assertRaisesRegex(ValueError,'no verified'):
            replace_chk_stream(src,dict(section,name='unknown'),pack_chk(random.Random(1).randbytes(5000)))

    def test_two_consecutive_original_game_growth_replacements(self):
        root=os.environ.get('GTA_DTZ_REFERENCE_DIR')
        if not root:self.skipTest('GTA_DTZ_REFERENCE_DIR not configured')
        from texture_formats import DTZDocument,png_to_tim2
        from TM2_Replacement_Bridge import replace_dtz_from_tm2
        from PIL import Image
        import io
        fixtures={
            'GTA LCS PS2 GAME.DTZ':('kjah','font1'),
            'GTA VCS PS2 GAME.DTZ':('radar00','radar01'),
            'GTA VCS PSP GAME.DTZ':('radar00','radar01'),
        }
        for filename,names in fixtures.items():
            with self.subTest(game=filename):
                d=DTZDocument((Path(root)/filename).read_bytes())
                original={ (g,t.name):d.preview(i).tobytes()
                           for i,(g,t) in enumerate(d.entries)
                           if t.name not in names }
                before_names=[(g,t.name,t.width,t.height,t.bpp) for g,t in d.entries]
                for k,name in enumerate(names):
                    idx=next(i for i,(_,t) in enumerate(d.entries) if t.name==name)
                    tex=d.entries[idx][1]
                    rng=random.Random(1101+k)
                    cols=[((x*19+k)%256,(x*47)%256,(x*73)%256,255) for x in range(16)]
                    image=Image.new('RGBA',(tex.width,tex.height))
                    image.putdata([cols[rng.randrange(16)] for _ in range(tex.width*tex.height)])
                    input_png=io.BytesIO();image.save(input_png,format='PNG');input_png.seek(0)
                    replace_dtz_from_tm2(d,idx,png_to_tim2(input_png,bpp=tex.bpp))
                    _gatg_tables(d.data)
                    self.assertEqual([(g,t.name,t.width,t.height,t.bpp) for g,t in d.entries],before_names)
                for i,(g,t) in enumerate(d.entries):
                    key=(g,t.name)
                    if key in original:
                        self.assertEqual(d.preview(i).tobytes(),original[key],key)
                # Full DTZ write/reopen and checks of both independently edited sections.
                check=DTZDocument(zlib.compress(d.data,9))
                self.assertEqual(check.data,d.data)
                for i,(g,t) in enumerate(check.entries):
                    self.assertEqual(check.preview(i).tobytes(),d.preview(i).tobytes())

    def test_original_game_refs_verify_both_tables_and_all_unrelated_textures(self):
        root=os.environ.get('GTA_DTZ_REFERENCE_DIR')
        if not root:self.skipTest('GTA_DTZ_REFERENCE_DIR not configured')
        from texture_formats import DTZDocument,png_to_tim2
        from TM2_Replacement_Bridge import replace_dtz_from_tm2
        from PIL import Image
        import io,tempfile
        fixtures={
            'GTA LCS PS2 GAME.DTZ':('kjah',),
            'GTA VCS PS2 GAME.DTZ':('radar00',),
            'GTA VCS PSP GAME.DTZ':('radar00',),
            'GTA LCS PSP GAME.DTZ':('arrow_02',),
        }
        for filename,names in fixtures.items():
            with self.subTest(game=filename):
                d=DTZDocument((Path(root)/filename).read_bytes())
                a,b,c,sites=_gatg_tables(d.data)
                before=[(grp,t.name,t.width,t.height,t.bpp) for grp,t in d.entries]
                for name in names:
                    index=next(i for i,(_,t) in enumerate(d.entries) if t.name==name)
                    grp,target=d.entries[index]
                    # Preserve all unrelated textures byte-for-byte.
                    unaffected={ (g,tex.name):d.preview(j).tobytes() for j,(g,tex) in enumerate(d.entries) if j!=index }
                    before_data=d.data
                    rng=random.Random(912)
                    palette=[(x*11%256,x*31%256,x*83%256,255) for x in range(16)]
                    img=Image.new('RGBA',(target.width,target.height))
                    img.putdata([palette[rng.randrange(16)] for _ in range(target.width*target.height)])
                    buf=io.BytesIO();img.save(buf,format='PNG');buf.seek(0)
                    replace_dtz_from_tm2(d,index,png_to_tim2(buf,bpp=target.bpp))
                    self.assertEqual([(g,t.name,t.width,t.height,t.bpp) for g,t in d.entries], before)
                    a2,b2,c2,s2=_gatg_tables(d.data)
                    self.assertEqual(c,c2)
                    self.assertEqual(d.data[a2:b2],before_data[a:b])
                    delta=a2-a
                    self.assertEqual(s2,tuple(v+delta if v>=a else v for v in sites))
                    for j,(g,tex) in enumerate(d.entries):
                        if j!=index:
                            self.assertEqual(d.preview(j).tobytes(),unaffected[(g,tex.name)],(filename,g,tex.name))
                    with tempfile.TemporaryDirectory() as temp:
                        output=Path(temp)/'edited.DTZ';d.save(output)
                        recovered=DTZDocument(output.read_bytes())
                        self.assertEqual(recovered.data,d.data)


if __name__=='__main__': unittest.main(verbosity=2)
