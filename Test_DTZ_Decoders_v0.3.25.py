"""Independent four-game DTZ decoder and TM2 edit integration tests.

Uses original user-supplied game archives supplied at runtime, never edits them.
Validates every indexed HUD/generic/particle resource, native storage codecs,
menu/font/radar coverage, cross-platform pixel comparisons and edit/reparse.
"""
from __future__ import annotations
from pathlib import Path
import collections, hashlib, json, tempfile, zlib
from PIL import Image
from texture_formats import DTZDocument, png_to_tim2, unpack4, pack4, deswizzle8, swizzle8
from GATG_Indexed_Texture import psp_swizzle, psp_unswizzle
from DTZ_Section_Index import inspect_indexed_sections
from TM2_Replacement_Bridge import replace_dtz_from_tm2

FILES = ('GTA LCS PS2 GAME.DTZ','GTA LCS PSP GAME.DTZ',
         'GTA VCS PS2 GAME.DTZ','GTA VCS PSP GAME.DTZ')
COUNTS = (210,197,316,320)


def audit(root='/mnt/data'):
    root=Path(root); docs={}; manifest={}
    for filename, expected in zip(FILES,COUNTS):
        original=(root/filename).read_bytes(); sha=hashlib.sha256(original).hexdigest()
        doc=DTZDocument(original); docs[filename]=doc
        assert len(doc.entries)==expected,(filename,len(doc.entries),expected)
        records=inspect_indexed_sections(doc.data)
        actual=collections.Counter(s for s,t in doc.entries)
        for section, rows in records.items():
            # Every previously index-only resource must resolve to a distinct
            # named raster record; duplicated names are distinguished by offsets.
            represented=collections.Counter(t.name for group,t in doc.entries if group==section)
            indexed=collections.Counter(r['name'] for r in rows)
            assert represented==indexed,(filename,section,(represented-indexed),(indexed-represented))
        assert sum(map(len,doc.indexed_sections.values()))==0,(filename,doc.indexed_sections)
        rows=[];regions=collections.defaultdict(list)
        for index,(section,tex) in enumerate(doc.entries):
            owner=next(x for x in doc.sections if x['name']==section)
            raw=owner['raw'];pixel_start=tex.pixel_offset;pixel_end=pixel_start+tex.pixel_size
            pal_start=tex.palette_offset;pal_end=pal_start+tex.palette_size
            assert 0 <= pixel_start < pixel_end <= pal_start < pal_end <= len(raw)
            assert tex.palette_size==(16 if tex.bpp==4 else 256)*4
            pic=doc.preview(index)
            assert pic.size==(tex.width,tex.height) and pic.mode=='RGBA'
            pixels=raw[pixel_start:pixel_end]
            if tex.owner=='indexed_psp':
                assert psp_swizzle(psp_unswizzle(pixels,tex.row_pitch,tex.height),tex.row_pitch,tex.height)==pixels
            elif tex.swizzled:
                expanded=unpack4(pixels) if tex.bpp==4 else pixels
                back=swizzle8(deswizzle8(expanded,tex.width,tex.height),tex.width,tex.height)
                if tex.bpp==4:back=pack4(back)
                assert back==pixels
            else:
                assert len(pixels)==tex.width*tex.height*tex.bpp//8
            scope='GATG' if owner.get('direct') else section
            regions[scope].append((pixel_start,pal_end,tex.name))
            rows.append({'section':section if not section.startswith('XET@') else 'radar',
                         'name':tex.name,'dimensions':[tex.width,tex.height],
                         'bpp':tex.bpp,'storage':tex.owner,'swizzled':tex.swizzled,
                         'pixel_offset':pixel_start,'palette_offset':pal_start,
                         'mip_byte_count':pal_start-pixel_end})
        for scope,rr in regions.items():
            rr.sort()
            for first,second in zip(rr,rr[1:]):
                assert first[1] <= second[0],(filename,scope,'overlapping images',first,second)
        assert hashlib.sha256((root/filename).read_bytes()).hexdigest()==sha
        manifest[filename]={'count':len(rows),'indexed_only':0,'sections':dict(actual),'textures':rows,
                            'reference_sha256':sha}
        print('PASS',filename,'decoded',len(rows),'unresolved indexed',0,flush=True)
    # Independent source versions and encoding modes, but the game's common
    # artwork in many same-sized HUD/vehicle slots must match exactly in RGB.
    for game,required in (('LCS',110),('VCS',220)):
        ps2=docs[f'GTA {game} PS2 GAME.DTZ'];psp=docs[f'GTA {game} PSP GAME.DTZ']
        x={(g,t.name):i for i,(g,t) in enumerate(ps2.entries) if g in ('hud','generic','particle')}
        y={(g,t.name):i for i,(g,t) in enumerate(psp.entries) if g in ('hud','generic','particle')}
        exact=0; compared=0
        for key in sorted(set(x)&set(y)):
            a=ps2.entries[x[key]][1];b=psp.entries[y[key]][1]
            if (a.width,a.height)!=(b.width,b.height):continue
            ac=ps2.preview(x[key]).tobytes();bc=psp.preview(y[key]).tobytes()
            compared+=1
            if ac[0::4]==bc[0::4] and ac[1::4]==bc[1::4] and ac[2::4]==bc[2::4]: exact+=1
        assert exact>=required,(game,compared,exact,required)
        manifest[f'{game}_cross_platform']={'same_dimension_names':compared,'exact_RGB_matches':exact}
        print('PASS',game,'RGB-identical PS2/PSP textures',exact,'/',compared,flush=True)
    # 64 VCS radar tiles have independently parsed compressed XET records.
    for machine in ('PS2','PSP'):
        d=docs[f'GTA VCS {machine} GAME.DTZ']
        named=[t.name for sec,t in d.entries if sec.startswith('XET@')]
        assert sorted(named)==sorted(f'radar{i:02d}' for i in range(64))
    # Test replacing a direct native raster using the actual PNG->TM2 codec,
    # and verify the reindexed output plus all other image bytes are untouched.
    import os
    if os.environ.get('GTA_DTZ_RUN_TM2_INTEGRATION') != '1':
        print('Base decoder/index/cross-platform audit passed. Set GTA_DTZ_RUN_TM2_INTEGRATION=1 to test all TM2 writes.',flush=True)
        return manifest
    replacement_cases={
        'GTA LCS PS2 GAME.DTZ': [('hud','radar_diaz'),('generic','wheel_lighttruck_q')],
        'GTA LCS PSP GAME.DTZ': [('generic','wheel_saloon_q'),('hud','radar_diaz')],
        'GTA VCS PS2 GAME.DTZ': [('hud','phone'),('generic','wheel_saloon_q')],
        'GTA VCS PSP GAME.DTZ': [('hud','radar_centre'),('generic','wheel_saloon_q')],
    }
    with tempfile.TemporaryDirectory(prefix='gta_dtz_codec_test_') as temp:
        folder=Path(temp)
        for filename, cases in replacement_cases.items():
            for section,name in cases:
                print('START TM2 replacement',filename,section,name,flush=True)
                old=docs[filename]; raw=old.compressed; d=DTZDocument(raw)
                i=next(k for k,(s,t) in enumerate(d.entries) if s==section and t.name==name)
                t=d.entries[i][1]
                pic=Image.new('RGBA',(t.width,t.height),(20,160,225,255))
                png=folder/'test.png';pic.save(png)
                tm2=png_to_tim2(png,bpp=t.bpp,width=t.width,height=t.height)
                untouched=d.data
                meta=[(grp,t.name,t.pixel_offset,t.pixel_size,t.palette_offset,t.palette_size) for grp,t in d.entries]
                mip=untouched[t.pixel_offset+t.pixel_size:t.palette_offset]
                replace_dtz_from_tm2(d,i,tm2)
                reread=DTZDocument(zlib.compress(d.data,9))
                assert [(grp,t.name,t.pixel_offset,t.pixel_size,t.palette_offset,t.palette_size) for grp,t in reread.entries]==meta
                # A compatible replacement changes ONLY base pixels and CLUT,
                # deliberately preserving any original native mipmaps.
                diff=[k for k,(u,v) in enumerate(zip(untouched,d.data)) if u!=v]
                permitted=lambda k:(t.pixel_offset<=k<t.pixel_offset+t.pixel_size) or (t.palette_offset<=k<t.palette_offset+t.palette_size)
                assert all(permitted(k) for k in diff),(filename,name,'unexpected bytes changed',diff[:10])
                assert mip==d.data[t.pixel_offset+t.pixel_size:t.palette_offset]
                assert hashlib.sha256((root/filename).read_bytes()).hexdigest()==manifest[filename]['reference_sha256']
                print('PASS native TM2 replacement',filename,section,name,'changed',len(diff),'bytes',flush=True)
    return manifest

if __name__=='__main__':
    import sys
    root=sys.argv[1] if len(sys.argv)>1 else '/mnt/data'
    m=audit(root)
    path=Path(root)/'DTZ_Decoder_Audit_v0.3.25.json'
    path.write_text(json.dumps(m,indent=2),encoding='utf-8')
    print('Complete:',path)
