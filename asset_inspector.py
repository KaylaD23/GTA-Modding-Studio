"""RenderWare binary chunk helpers used by GTA Modding Studio's native TXD tab."""
import struct

RW_NAMES = {
    0x01:'Struct',0x02:'String',0x03:'Extension',0x05:'Camera',0x06:'Texture',
    0x07:'Material',0x08:'Material List',0x09:'Atomic Section',0x0E:'World',
    0x0F:'Clump',0x14:'Geometry List',0x15:'Native Texture',0x16:'Texture Dictionary',
    0x18:'Raster',0x1A:'Image',0x1B:'Skin',0x1C:'Animation',0x1F:'User Data'
}

def parse_rw_chunks(data: bytes, start: int=0, end: int|None=None, depth: int=0):
    """Bounds-checked RenderWare chunk listing. Does not guess texture payloads."""
    if end is None: end=len(data)
    pos=start; out=[]
    while pos+12<=end:
        typ,length,version=struct.unpack_from('<III',data,pos)
        payload=pos+12; chunk_end=payload+length
        if chunk_end>end or chunk_end<payload:
            out.append((pos,depth,f'Invalid/truncated chunk 0x{typ:02X}',length,f'declared end 0x{chunk_end:X} exceeds container 0x{end:X}')); break
        name=RW_NAMES.get(typ,f'Chunk 0x{typ:02X}')
        out.append((pos,depth,name,length,f'version=0x{version:08X}; payload=0x{payload:X}'))
        if typ in (0x16,0x15) and depth<12:
            child=payload
            while child+12<=chunk_end:
                ctype,clen,cver=struct.unpack_from('<III',data,child); cend=child+12+clen
                if cend>chunk_end or cend<=child:
                    out.append((child,depth+1,'Invalid/truncated child',clen,f'parent end=0x{chunk_end:X}')); break
                out.append((child,depth+1,RW_NAMES.get(ctype,f'Chunk 0x{ctype:02X}'),clen,f'version=0x{cver:08X}'))
                child=cend
            if child<chunk_end and chunk_end-child<12:
                out.append((child,depth+1,'Trailing bytes',chunk_end-child,'not a full chunk header'))
        pos=chunk_end
    if pos<end: out.append((pos,depth,'Trailing bytes',end-pos,'not a full 12-byte RenderWare header'))
    return out
