"""Complete four-game DTZ index audit and double-pass preview/offset verification.

Optional supplied originals: GTA LCS PS2 GAME.DTZ, GTA LCS PSP GAME.DTZ,
GTA VCS PS2 GAME.DTZ, GTA VCS PSP GAME.DTZ. No game files are modified.
"""
from __future__ import annotations
import os, zlib, hashlib, json, struct, tempfile, collections
from pathlib import Path
from PIL import Image, ImageDraw
from texture_formats import DTZDocument, chk_named_extent, pack4, unpack4, swizzle8, deswizzle8
from GATG_Indexed_Texture import psp_swizzle, psp_unswizzle
from DTZ_Section_Index import inspect_indexed_sections

NAMES = ('GTA LCS PS2 GAME.DTZ','GTA LCS PSP GAME.DTZ','GTA VCS PS2 GAME.DTZ','GTA VCS PSP GAME.DTZ')
EXPECTED = {
 'GTA LCS PS2 GAME.DTZ': {'hud':60,'generic':21,'particle':117},
 'GTA LCS PSP GAME.DTZ': {'hud':60,'generic':20,'particle':117},
 'GTA VCS PS2 GAME.DTZ': {'hud':68,'generic':53,'particle':131},
 'GTA VCS PSP GAME.DTZ': {'hud':72,'generic':53,'particle':131},
}
MENU = [('head',0x40,8),('doubleclef',0x1440,8),('kjah',0x2840,4),('rise',0x3080,4),
        ('lips106',0x38C0,4),('radiodelmundo',0x4100,4),('msx',0x4940,8),
        ('flashback',0x5D40,4),('libertyjam',0x6580,8),('libertyfree',0x7980,4)]


def _image_sanity(doc, grp, tex, index):
    sec = next(x for x in doc.sections if x['name']==grp)
    blob=sec['raw']
    assert tex.width>0 and tex.height>0 and tex.bpp in (4,8), (grp,tex.name)
    assert 0<=tex.pixel_offset and tex.pixel_offset+tex.pixel_size<=tex.palette_offset
    assert tex.palette_offset+tex.palette_size<=len(blob)
    assert tex.palette_size == (16 if tex.bpp==4 else 256)*4
    if tex.owner!='indexed_psp':
        assert tex.pixel_size==tex.width*tex.height*tex.bpp//8
        stored=blob[tex.pixel_offset:tex.pixel_offset+tex.pixel_size]
        linear=unpack4(stored) if tex.bpp==4 else stored
        if tex.swizzled:
            unsw=deswizzle8(linear,tex.width,tex.height)
            assert swizzle8(unsw,tex.width,tex.height)==linear
        else:
            assert len(linear)==tex.width*tex.height
    else:
        if tex.palette_offset>tex.pixel_offset+tex.pixel_size:
            assert 2<=((tex.format_word>>8)&255)<=8, (grp,tex.name,'unexpected mipmap range')
        assert tex.pixel_size==tex.row_pitch*tex.height
        stored=blob[tex.pixel_offset:tex.pixel_offset+tex.pixel_size]
        assert psp_swizzle(psp_unswizzle(stored,tex.row_pitch,tex.height),tex.row_pitch,tex.height)==stored
    image=doc.preview(index)
    assert image.mode=='RGBA' and image.size==(tex.width,tex.height)
    return image


def contact_sheet(records, out: Path, title: str, columns=8):
    if not records: return None
    cellw=156;cellh=159;rows=(len(records)+columns-1)//columns
    img=Image.new('RGB',(cellw*columns,39+cellh*rows),'#161c2b')
    d=ImageDraw.Draw(img)
    d.text((8,8),title,fill='white')
    for i, (name, preview) in enumerate(records):
        x=(i%columns)*cellw;y=39+(i//columns)*cellh
        thumb=preview.copy()
        thumb.thumbnail((142,125),Image.Resampling.NEAREST)
        layer=Image.new('RGBA',thumb.size,'#26283b');layer.alpha_composite(thumb)
        img.paste(layer.convert('RGB'),(x+(cellw-thumb.width)//2,y+2))
        label=name if len(name)<=24 else name[:21]+'…'
        d.text((x+3,y+132),label,fill='#f6f7fc')
    out.parent.mkdir(parents=True,exist_ok=True)
    img.save(out, optimize=True)
    return str(out)


def run(root='/mnt/data', outdir=None):
    root=Path(root)
    reports={}
    for fname in NAMES:
        p=root/fname
        assert p.is_file(),f'Missing required game reference: {p}'
        before=p.read_bytes();before_sha=hashlib.sha256(before).hexdigest()
        doc=DTZDocument(before)
        second=DTZDocument(before)
        assert doc.data==zlib.decompress(before)
        assert [(g,t.name,t.pixel_offset,t.palette_offset,t.bpp) for g,t in doc.entries] == [
            (g,t.name,t.pixel_offset,t.palette_offset,t.bpp) for g,t in second.entries]
        raw=inspect_indexed_sections(doc.data)
        observed={k:len(v) for k,v in raw.items()}
        assert observed==EXPECTED[fname],(fname,observed,EXPECTED[fname])
        # Verify every index was read from the name slot of the linked node.
        for group, records in raw.items():
            used=[]
            for row in records:
                node=row['record_offset']
                assert doc.data[node+0x20:node+0x54].split(b'\0',1)[0].decode('ascii')==row['name']
                assert 0x100<row['asset_pointer']<len(doc.data)
                used.append(node)
            assert len(used)==len(set(used)),(fname,group,'duplicate node offset')
        decoded={(g,t.name,t.pixel_offset,t.palette_offset) for g,t in doc.entries}
        for group,rows in doc.indexed_sections.items():
            for row in rows:
                assert not any(g==group and name==row['name'] for g,name,_,_ in decoded), ('catalog duplicates decoded', fname,group,row['name'])
        if fname.startswith('GTA LCS PS2'):
            menu=[t for group,t in doc.entries if group=='menu']
            assert [(t.name,t.pixel_offset,t.bpp) for t in menu]==MENU
            fonts=[t for group,t in doc.entries if group=='fonts']
            assert [(t.name,t.pixel_offset) for t in fonts]==[('font1',0x40),('font2',0x8080)]
        if fname.startswith('GTA VCS'):
            radar=[(g,t) for g,t in doc.entries if g.startswith('XET@')]
            assert len(radar)==64 and len({t.name for _,t in radar})==64
            assert {t.name for _,t in radar}=={f'radar{i:02d}' for i in range(64)}
        groups=collections.defaultdict(list); occupancy=collections.defaultdict(list)
        manifest=[]; brightness=[]
        for i,(grp,tex) in enumerate(doc.entries):
            im=_image_sanity(doc,grp,tex,i)
            # A second decode from independently initialized metadata must
            # be pixel-identical, detecting accidental mutable/shared state.
            assert im.tobytes()==second.preview(i).tobytes(),(fname,grp,tex.name)
            scope='GATG' if tex.owner.startswith('indexed_') and not grp.startswith('XET@') else grp
            occupancy[scope].append((tex.pixel_offset,tex.palette_offset+tex.palette_size,tex.name))
            displaygroup='radar' if grp.startswith('XET@') else grp
            groups[displaygroup].append((tex.name,im))
            rgba=im.tobytes(); unique=len({rgba[j:j+4] for j in range(0,len(rgba),4)})
            if unique==1:brightness.append((grp,tex.name))
            manifest.append({'section':displaygroup,'name':tex.name,'width':tex.width,'height':tex.height,
                             'bpp':tex.bpp,'pixel_offset':tex.pixel_offset,'pixel_size':tex.pixel_size,
                             'palette_offset':tex.palette_offset,'palette_size':tex.palette_size,
                             'storage':tex.owner,'pixel_sha256':hashlib.sha256(im.tobytes()).hexdigest(),
                             'unique_colors':unique})
        overlaps=[]
        for scope,rows in occupancy.items():
            rows.sort()
            for a,b in zip(rows,rows[1:]):
                if b[0]<a[1]: overlaps.append((scope,a,b))
        assert not overlaps, (fname,overlaps[:3])
        assert hashlib.sha256(p.read_bytes()).hexdigest()==before_sha,'Original file changed'
        # Save an unmodified copy, reparse all rows from the newly compressed
        # GATG container, and verify a second image decode is identical.
        with tempfile.TemporaryDirectory(prefix='dtz_audit_') as tmp:
            target=Path(tmp)/fname
            doc.save(target)
            rescan=DTZDocument(target.read_bytes())
            assert [(g,t.name,t.pixel_offset,t.pixel_size,t.palette_offset,t.palette_size) for g,t in doc.entries] == [(g,t.name,t.pixel_offset,t.pixel_size,t.palette_offset,t.palette_size) for g,t in rescan.entries]
            assert {g:[(r['name'],r['record_offset']) for r in rows] for g,rows in doc.indexed_sections.items()} == {g:[(r['name'],r['record_offset']) for r in rows] for g,rows in rescan.indexed_sections.items()}
            for i,(g,t) in enumerate(doc.entries):
                assert doc.preview(i).tobytes()==rescan.preview(i).tobytes(),(fname,g,t.name,'recompress preview changed')
        report={'original_sha256':before_sha,'decompressed_bytes':len(doc.data),'sections_indexed':observed,
                'decoded_count':len(doc.entries),'catalog_only_count':sum(map(len,doc.indexed_sections.values())),
                'decoded_by_section':{g:len(v) for g,v in groups.items()},
                'fully_transparent_or_single_rgba_color':brightness,
                'unparsed_section_status':{'fonts':'LCS PSP/VCS font layout not fully decoded; no safe TM2 replacement for these records'},
                'entries':manifest,
                'index_only':{g:[{'name':x['name'],'node':x['record_offset'],'layout':x['node_layout']} for x in rows]
                              for g,rows in doc.indexed_sections.items()}}
        reports[fname]=report
        if outdir:
            outbase=Path(outdir)
            prefix=fname.replace(' GTA','').replace(' GAME.DTZ','').replace(' ','_')
            for grp,values in groups.items():
                if grp=='radar':values=sorted(values,key=lambda x:x[0].lower())
                pagesize=64
                for start in range(0,len(values),pagesize):
                    contact_sheet(values[start:start+pagesize],outbase/f'{prefix}_{grp}_{start//pagesize+1}.png',f'{fname} — {grp}: {start+1}–{min(len(values),start+pagesize)}')
        print(fname, 'decoded',len(doc.entries),'indexed-only',report['catalog_only_count'],'groups',report['decoded_by_section'], 'monocolor',len(brightness),'overlaps',len(overlaps))
    # Independent cross-platform check: VCS PS2 and PSP have completely
    # different XET headers/storage orders but the same 64 map RGB rasters.
    ps2=DTZDocument((root/'GTA VCS PS2 GAME.DTZ').read_bytes())
    psp=DTZDocument((root/'GTA VCS PSP GAME.DTZ').read_bytes())
    a={t.name:ps2.preview(i).tobytes() for i,(g,t) in enumerate(ps2.entries) if g.startswith('XET@')}
    b={t.name:psp.preview(i).tobytes() for i,(g,t) in enumerate(psp.entries) if g.startswith('XET@')}
    assert set(a)==set(b)
    for name in a:
        assert a[name][0::4]==b[name][0::4] and a[name][1::4]==b[name][1::4] and a[name][2::4]==b[name][2::4], name+' cross-platform radar RGB mismatch'
    print('Cross-platform check: all 64 VCS radar tiles match PS2 and PSP RGB pixel-for-pixel.')
    if outdir:
        dst=Path(outdir)/'DTZ_FOUR_GAME_AUDIT.json'
        dst.write_text(json.dumps(reports,indent=2),encoding='utf-8')
    return reports

if __name__=='__main__':
    run(os.environ.get('GTA_DTZ_REFERENCE_DIR','/mnt/data'),os.environ.get('GTA_DTZ_AUDIT_OUTPUT','/mnt/data/audit_v0324/contact_sheets'))
