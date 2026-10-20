"""Reference regression tests for the reconstructed GATG indexed raster layouts.
No assets are included; set GTA_TEXTURE_REFERENCE_DIR to user-owned references.
"""
from __future__ import annotations
import os, hashlib, tempfile, zlib
from pathlib import Path
from texture_formats import DTZDocument, TIM2Document
from TM2_Replacement_Bridge import replace_dtz_from_tm2
from GATG_Indexed_Texture import psp_swizzle,psp_unswizzle


def test(root):
    import random
    rng=random.Random(1344)
    for pitch,height in ((16,16),(32,32),(64,128)):
        data=rng.randbytes(pitch*height)
        assert psp_unswizzle(psp_swizzle(data,pitch,height),pitch,height)==data
    refs=list(Path(root).glob('*.DTZ'))
    assert len(refs)>=4,'Supply original LCS/VCS PS2 and PSP GAME.DTZ files.'
    outputs={}
    for path in refs:
        original=path.read_bytes(); original_hash=hashlib.sha256(original).hexdigest()
        doc=DTZDocument(original)
        native=[(i,g,t) for i,(g,t) in enumerate(doc.entries) if t.owner.startswith('indexed')]
        # Validate no decoded native extent exceeds original GATG data.
        for i,group,t in native:
            assert t.pixel_offset>=0 and t.pixel_offset+t.pixel_size==t.palette_offset
            assert t.palette_offset+t.palette_size<=len(doc.data)
            assert doc.preview(i,True).size==(t.width,t.height)
        outputs[path.name]={'native':len(native),'total':len(doc.entries),
                            'still_unknown':sum(map(len,doc.indexed_sections.values()))}
        if not native:continue
        tm2=None;index=None
        for tm2path in Path(root).glob('*.tm2'):
            try: candidate=TIM2Document(tm2path.read_bytes())
            except ValueError:continue
            matches=[j for j,g,t in native if (t.width,t.height,t.bpp)==(candidate.width,candidate.height,candidate.bpp)]
            if matches:
                tm2=tm2path;index=matches[0];break
        if tm2 is None:
            continue
        before=doc.data;group,target=doc.entries[index]
        replace_dtz_from_tm2(doc,index,tm2)
        changed=[j for j,(a,b) in enumerate(zip(before,doc.data)) if a!=b]
        valid=set(range(target.pixel_offset,target.pixel_offset+target.pixel_size)) | set(range(target.palette_offset,target.palette_offset+target.palette_size))
        assert all(j in valid for j in changed),'Changed unrelated GATG bytes'
        assert len(before)==len(doc.data)
        with tempfile.TemporaryDirectory() as td:
            dst=Path(td)/'edited.DTZ';doc.save(dst)
            result=DTZDocument(dst.read_bytes())
            assert [(s,t.name) for s,t in result.entries]==[(s,t.name) for s,t in doc.entries]
        assert hashlib.sha256(path.read_bytes()).hexdigest()==original_hash
        outputs[path.name]['replaced']=(group,target.name,str(tm2.name))
    print('Original GATG reference validations:',outputs)
    assert 'GTA LCS PS2 GAME.DTZ' in outputs
    assert outputs['GTA LCS PS2 GAME.DTZ']['native']>=145
    assert outputs['GTA LCS PSP GAME.DTZ']['native']>=110
    assert outputs['GTA VCS PSP GAME.DTZ']['native']>=95
    assert outputs['GTA LCS PS2 GAME.DTZ']['total']>=164
    assert outputs['GTA VCS PS2 GAME.DTZ']['total']==64
    assert outputs['GTA VCS PSP GAME.DTZ']['total']==97
    # Every original VCS radar tile has a validated XET image/CLUT slot.
    for platform in ('GTA VCS PS2 GAME.DTZ','GTA VCS PSP GAME.DTZ'):
        d=DTZDocument((Path(root)/platform).read_bytes())
        tiles=[t.name for section,t in d.entries if section.startswith('XET@')]
        assert len(tiles)==64 and len(set(tiles))==64
        assert all(name.startswith('radar') for name in tiles)
    return outputs

if __name__=='__main__':
    test(os.environ.get('GTA_TEXTURE_REFERENCE_DIR','/mnt/data/research_gta'))
