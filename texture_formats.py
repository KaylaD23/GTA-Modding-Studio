"""PS2 RenderWare TXD, PS2 TIM2, and GTA Stories GATG/CHK texture inspection.

Conservative writes: only pre-identified, fixed-size texture/palette payloads are
replaced. Originals are never modified; all container offsets and lengths remain
unchanged. Explicitly reject unsupported/swizzle/size combinations.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from PIL import Image
import io, re, struct, zlib, hashlib, os, tempfile


def u32(b, o): return struct.unpack_from('<I', b, o)[0]
def u16(b, o): return struct.unpack_from('<H', b, o)[0]
def chunks(data, start, end):
    out=[]; pos=start
    while pos+12 <= end:
        typ,size,ver=struct.unpack_from('<III',data,pos)
        nxt=pos+12+size
        if nxt>end or nxt<=pos: raise ValueError(f'Invalid RenderWare chunk at 0x{pos:X}')
        out.append((typ,pos,pos+12,size,ver));pos=nxt
    if pos!=end: raise ValueError('RenderWare chunk contains unaccounted bytes')
    return out


def swizzle_index(x,y,logw):
    yb1=(y>>1)&1;yb2=(y>>2)&1;xb3=(x>>3)&1
    x2=x ^ ((yb1 ^ yb2)<<2)
    nx=(x2&7) | ((x2>>1)&~7)
    ny=(y&1) | ((y>>1)&~1)
    return yb1 | (xb3<<1) | (nx<<2) | (ny<<(logw+1))


def deswizzle8(data,w,h):
    if w<16 or w&(w-1) or h%4: raise ValueError('PSMT8 swizzle requires power-of-two width >= 16, height divisible by 4')
    out=bytearray(w*h); logw=w.bit_length()-1; mask=(1<<(logw+2))-1
    if len(data)!=w*h:raise ValueError('8-bit pixel length mismatch')
    for y in range(h):
        band=(y//4*4)*w
        for x in range(w):out[y*w+x]=data[band+(swizzle_index(x,y,logw)&mask)]
    return bytes(out)


def swizzle8(data,w,h):
    if len(data)!=w*h:raise ValueError('8-bit pixel length mismatch')
    out=bytearray(w*h);logw=w.bit_length()-1;mask=(1<<(logw+2))-1
    for y in range(h):
        band=(y//4*4)*w
        for x in range(w):out[band+(swizzle_index(x,y,logw)&mask)]=data[y*w+x]
    return bytes(out)


def unpack4(data):
    out=bytearray(len(data)*2)
    for i,c in enumerate(data): out[2*i]=c&15;out[2*i+1]=(c>>4)&15
    return bytes(out)

def pack4(data):
    if len(data)%2:raise ValueError('Odd 4bpp pixel count')
    return bytes(data[i]|(data[i+1]<<4) for i in range(0,len(data),2))


def rgba_to_ps2_alpha(a): return min(128,(int(a)+1)//2)
def ps2_to_rgba_alpha(a): return min(255,int(a)*2)


def declut_8_palette(clut):
    # GS CSM1 palette shuffle: swap CLUT groups 8..15 and 16..23 in each 32.
    if len(clut)!=256:raise ValueError('Expected 256 palette entries')
    return [clut[(i&~31)|((i&31)^24) if (i&31) in range(8,24) else i] for i in range(256)]


def clut8_index(i):
    return (i&~31)|((i&31)^24) if 8 <= (i&31) <24 else i


def decode_paletted_pixels(texels,palette,w,h,bpp,swizzled=True,clut_swapped=False):
    if bpp not in (4,8): raise ValueError('Only indexed 4/8 bpp image decoding is implemented')
    raw=(unpack4(texels) if bpp==4 else bytes(texels))
    if swizzled:
        # PS2 4bpp texture storage is commonly a nibble-packed representation
        # of an 8bpp upload swizzle. The app exposes a layout toggle when games
        # store unswizzled indices instead.
        raw=deswizzle8(raw,w,h)
    if len(raw)!=w*h:raise ValueError('Texel count mismatch')
    pal=[tuple(x) for x in palette]
    if clut_swapped and bpp==8:pal=declut_8_palette(pal)
    rgba=bytearray(w*h*4)
    for i,v in enumerate(raw):
        r,g,b,a=pal[v]
        rgba[i*4:i*4+4]=bytes((r,g,b,ps2_to_rgba_alpha(a)))
    return Image.frombytes('RGBA',(w,h),bytes(rgba))


def encode_with_existing_palette(img,palette,w,h,bpp,swizzled=True,clut_swapped=False):
    """Fixed palette replacement: closest existing RGBA color, preserved GS CLUT."""
    if img.size!=(w,h):raise ValueError(f'PNG must be exactly {w}×{h}, received {img.size}')
    if bpp not in (4,8):raise ValueError('Indexed replacement supports 4bpp or 8bpp only')
    pal=[tuple(v) for v in palette]
    if clut_swapped and bpp==8:pal=declut_8_palette(pal)
    src=img.convert('RGBA').tobytes(); out=bytearray(w*h)
    cache={}
    for i in range(w*h):
        rgba=src[i*4:i*4+4]
        key=tuple((rgba[0]//4,rgba[1]//4,rgba[2]//4,rgba[3]//8))
        if key not in cache:
            a=rgba[3]//2
            cache[key]=min(range(len(pal)),key=lambda k:(pal[k][0]-rgba[0])**2+(pal[k][1]-rgba[1])**2+(pal[k][2]-rgba[2])**2 + 2*(pal[k][3]-a)**2)
        out[i]=cache[key]
    raw=swizzle8(bytes(out),w,h) if swizzled else bytes(out)
    return pack4(raw) if bpp==4 else raw


def palette_from_png(img, colors=16):
    """Alpha-aware CLUT quantization shared with the dedicated TIM2 converter."""
    from RGBA_Indexed_Quantizer import quantize_rgba
    indices, full_rgba = quantize_rgba(img, colors, dither=False)
    palette = [(r, g, b, rgba_to_ps2_alpha(a)) for r, g, b, a in full_rgba]
    return indices, palette


@dataclass
class Texture:
    name:str
    width:int
    height:int
    bpp:int
    pixel_offset:int
    pixel_size:int
    palette_offset:int
    palette_size:int
    owner:str
    swizzled:bool=True
    clut_swapped:bool=False
    format_word:int=0
    note:str=''
    row_pitch:int=0
    platform:str=''

    def palette(self,data):
        count=16 if self.bpp==4 else 256
        if self.palette_size!=count*4:raise ValueError('Unsupported palette size')
        raw=data[self.palette_offset:self.palette_offset+self.palette_size]
        if len(raw)!=self.palette_size:raise ValueError('Truncated palette')
        return [tuple(raw[i*4:i*4+4]) for i in range(count)]

    def image(self,data,swizzled=None):
        if self.owner == 'indexed_psp':
            from GATG_Indexed_Texture import psp_index_image
            return psp_index_image(data,self,swizzled=True if swizzled is None else swizzled)
        return decode_paletted_pixels(data[self.pixel_offset:self.pixel_offset+self.pixel_size], self.palette(data), self.width,self.height,self.bpp, self.swizzled if swizzled is None else swizzled,self.clut_swapped)

    def replace_pixels(self,data,png_path,swizzled=None):
        img=Image.open(png_path).convert('RGBA');img.load()
        if img.size != (self.width,self.height):
            raise ValueError(f'Image must be exactly {self.width}x{self.height}')
        if self.owner=='indexed_psp':
            # PSP CLUTs use full-range alpha, and its native tiled row pitch is
            # NOT the PS2 GS raster swizzle.  Preserve the source CLUT and any
            # mipmap bytes; only update the base level's encoded texels.
            from GATG_Indexed_Texture import psp_swizzle
            pal=self.palette(data); indices=[]; cache={}
            for color in img.getdata():
                if color not in cache:
                    cache[color]=min(range(len(pal)), key=lambda j: sum((int(x)-int(y))**2 for x,y in zip(color,pal[j])))
                indices.append(cache[color])
            pitch=self.row_pitch;linear=bytearray(pitch*self.height)
            for y in range(self.height):
                line=bytes(indices[y*self.width:(y+1)*self.width])
                if self.bpp==4:line=pack4(line)
                linear[y*pitch:y*pitch+len(line)]=line
            raw=psp_swizzle(bytes(linear),pitch,self.height)
        else:
            raw=encode_with_existing_palette(img,self.palette(data),self.width,self.height,self.bpp,
                self.swizzled if swizzled is None else swizzled,self.clut_swapped)
        if len(raw)!=self.pixel_size:raise ValueError('Pixel size mismatch; refusing unsafe write')
        out=bytearray(data);out[self.pixel_offset:self.pixel_offset+len(raw)]=raw
        return bytes(out)


class TXDDocument:
    def __init__(self,data):
        self.original=bytes(data);self.data=bytes(data);self.textures=[]
        outer=chunks(self.data,0,len(self.data))
        if len(outer)!=1 or outer[0][0]!=0x16:raise ValueError('Not a single RenderWare texture dictionary')
        top=outer[0]
        for typ,off,payload,length,version in chunks(self.data,top[2],top[2]+top[3]):
            if typ!=0x15:continue
            subs=chunks(self.data,payload,payload+length)
            if len(subs)<4 or [s[0] for s in subs[:4]]!=[1,2,2,1]:continue
            name=self.data[subs[1][2]:subs[1][2]+subs[1][3]].split(b'\0')[0].decode('ascii','replace')
            r=subs[3];wrap=r[2]
            nt,sp,spl,slen,_=chunks(self.data,wrap,wrap+r[3])[0]
            if nt!=1 or slen<64:continue
            w,h,bpp=struct.unpack_from('<III',self.data,spl)
            if bpp not in (4,8) or w<16 or h<4 or w*h>4_194_304:continue
            gif=wrap+12+12+slen
            try:
                def tag(p):
                    lo,hi=struct.unpack_from('<QQ',self.data,p)
                    return (lo&0x7fff,(lo>>58)&3)
                n,f=tag(gif)
                if n!=3 or f!=0:continue
                gif2=gif+16+n*16;n2,f2=tag(gif2)
                pixel=gif2+16; psize=n2*16
                gif3=pixel+psize;n3,f3=tag(gif3)
                gif4=gif3+16+n3*16;n4,f4=tag(gif4)
                pal=gif4+16; palsize=n4*16
                if f2!=2 or f4!=2 or psize!=w*h*bpp//8 or palsize not in ((16 if bpp==4 else 256)*4, 96 if bpp==4 else 1024):continue
                if pal+palsize>payload+length:continue
                self.textures.append(Texture(name,w,h,bpp,pixel,psize,pal,(16 if bpp==4 else 256)*4,'txd',True,bpp==8,note=f'RenderWare 0x15 at 0x{off:X}'))
            except (struct.error,IndexError):continue
        if not self.textures:raise ValueError('No supported PS2 indexed 4/8bpp native textures found. Unsupported platform/format.')

    def replace(self,i,path,swizzled=None):
        tex=self.textures[i];self.data=tex.replace_pixels(self.data,path,swizzled)

    def save(self,path):
        if Path(path).resolve()==getattr(self,'source_path',Path('__no_source__')).resolve():raise ValueError('Save a separate copy, not the original TXD')
        Path(path).write_bytes(self.data)
        check=TXDDocument(Path(path).read_bytes())
        if len(check.textures)!=len(self.textures):raise ValueError('TXD output texture count changed')


class TIM2Document:
    """Single-picture 4/8bpp PS2 TIM2 with RGBA32 CLUT, 16-byte alignment."""
    def __init__(self,data):
        self.data=bytes(data)
        b=self.data
        if b[:4]!=b'TIM2' or len(b)<64:raise ValueError('Not a TIM2 file')
        if u16(b,6)!=1:raise ValueError('Only single-picture TIM2 is currently supported')
        total,clut_size,image_size=struct.unpack_from('<III',b,16)
        header_size=u16(b,28); clut_type=b[34]; img_type=b[35];w=u16(b,36);h=u16(b,38)
        self.width=w;self.height=h;self.bpp={4:4,5:8}.get(img_type,0)
        if not self.bpp or clut_type not in (0,3):raise ValueError('Only TIM2 indexed 4/8bpp with 32-bit CLUT supported')
        if header_size!=48 or total!=header_size+clut_size+image_size or len(b)!=16+total:
            raise ValueError('Unsupported TIM2 header layout/extra picture data')
        if image_size!=w*h*self.bpp//8 or clut_size!=4*(16 if self.bpp==4 else 256):raise ValueError(f'TIM2 payload dimensions/palette size mismatch: pixel={image_size} expected={w*h*self.bpp//8}; clut={clut_size} expected={4*(16 if self.bpp==4 else 256)} bpp={self.bpp} dims={w}x{h}')
        self.texture=Texture('TIM2',w,h,self.bpp,16+header_size,image_size,16+header_size+image_size,clut_size,'tim2',False,self.bpp==8)

    def image(self,swizzled=False):return self.texture.image(self.data,swizzled=swizzled)


def png_to_tim2(source,game='GTA LCS PS2',bpp=4,width=None,height=None,swizzled=False):
    img=Image.open(source).convert('RGBA')
    w=width or img.width;h=height or img.height
    if w<8 or h<8 or w>1024 or h>1024 or (w&(w-1)) or(h&(h-1)):
        raise ValueError('TIM2 dimensions must be powers of two between 8 and 1024')
    if bpp not in (4,8):raise ValueError('Select 4 or 8 bpp')
    if img.size!=(w,h):img=img.resize((w,h),Image.Resampling.LANCZOS)
    pixels,palette=palette_from_png(img,16 if bpp==4 else 256)
    if swizzled: pixels=swizzle8(pixels,w,h)
    if bpp==4:pixels=pack4(pixels)
    # Stored in CSM1 order for 8-bit palette; 4-bit is linear.
    palbuf=bytearray(len(palette)*4)
    for i,c in enumerate(palette):
        dest=clut8_index(i) if bpp==8 else i
        palbuf[dest*4:dest*4+4]=bytes(c)
    total=48+len(pixels)+len(palbuf)
    out=bytearray(16+total);out[:4]=b'TIM2'
    out[4]=4;out[5]=0;struct.pack_into('<H',out,6,1)
    struct.pack_into('<III',out,16,total,len(palbuf),len(pixels))
    struct.pack_into('<H',out,28,48);struct.pack_into('<H',out,30,16 if bpp==4 else 256)
    out[34]=3;out[35]=4 if bpp==4 else 5
    struct.pack_into('<HH',out,36,w,h)
    # GS TEX0: PSMT4=0x14, PSMT8=0x13; TBW minimum one 64px unit
    psm=0x14 if bpp==4 else 0x13
    tex0=(max(1,w//64)<<14)|(psm<<20)|((w.bit_length()-1)<<26)|((h.bit_length()-1)<<30)
    struct.pack_into('<Q',out,40,tex0)
    out[64:64+len(pixels)]=pixels
    out[64+len(pixels):]=palbuf
    # Re-read in decoder before returning; game compatibility still needs testing.
    TIM2Document(out)
    return bytes(out)


def png_to_tim2_template(source, template, *, swizzled=False):
    """Replace image and CLUT in a supported original .tm2 without touching its metadata."""
    original=Path(template).read_bytes() if isinstance(template,(str,Path)) else bytes(template)
    doc=TIM2Document(original)
    image=Image.open(source).convert('RGBA') if isinstance(source,(str,Path)) else source.convert('RGBA')
    if image.size!=(doc.width,doc.height):
        raise ValueError(f'Template is {doc.width}×{doc.height}; replacement PNG must have exactly that size')
    indices, palette=palette_from_png(image,16 if doc.bpp==4 else 256)
    if swizzled:indices=swizzle8(indices,doc.width,doc.height)
    pixels=pack4(indices) if doc.bpp==4 else indices
    clut=bytearray(len(palette)*4)
    for i,color in enumerate(palette):
        dest=clut8_index(i) if doc.bpp==8 else i
        clut[4*dest:4*(dest+1)]=bytes(color)
    out=bytearray(original)
    t=doc.texture
    if len(pixels)!=t.pixel_size or len(clut)!=t.palette_size:
        raise ValueError('Replacement changes the original TIM2 payload length')
    out[t.pixel_offset:t.pixel_offset+t.pixel_size]=pixels
    out[t.palette_offset:t.palette_offset+t.palette_size]=clut
    verified=TIM2Document(out)
    if verified.width!=doc.width or verified.height!=doc.height or verified.bpp!=doc.bpp:
        raise ValueError('Template write-back validation failed')
    return bytes(out)


def chk_named_extent(raw: bytes, table_ptr: int, width: int, height: int, bpp: int):
    """Resolve the CHK raster attached to a named index node.

    The linked node's table_ptr references the END offset of its own raster.
    The preceding 32-bit word holds the BEGIN offset. Format is at -4:
        [previous raster end / current raster begin] [format] [current raster end]
    A conventional packed raster holds pixel bytes, then 16/256 RGBA CLUT
    entries. Require an exact allocation match, never approximate.
    """
    if table_ptr < 8 or table_ptr + 4 > len(raw):
        raise ValueError('CHK named index pointer is out of range')
    begin = u32(raw, table_ptr - 8)
    end = u32(raw, table_ptr)
    pixel_size = width * height * bpp // 8
    palette_size = (16 if bpp == 4 else 256) * 4
    if begin < 0x40 or end > len(raw) or end - begin != pixel_size + palette_size:
        raise ValueError(f'CHK named extent mismatch at 0x{table_ptr:X}: '
                         f'0x{begin:X}..0x{end:X}, expected 0x{pixel_size + palette_size:X}')
    return begin, pixel_size, begin + pixel_size, palette_size


class DTZDocument:
    """GATG file, LCS PS2 menu/fonts CHKs and VCS PS2 menu/font XETs.

    Texture writes preserve decoded allocations. Oversized CHK/XET streams
    with a verified two-table GATG relocation index can be moved without
    shifting existing assets; every relocated pointer-site is rebased.
    """
    def __init__(self,data):
        self.compressed=bytes(data)
        try:self.data=zlib.decompress(self.compressed)
        except zlib.error as exc:raise ValueError(f'Unsupported DTZ compression: {exc}')
        if self.data[:4]!=b'GATG':raise ValueError('Decompressed file is not GATG')
        self.entries=[];self.sections=[]
        from DTZ_Section_Index import inspect_indexed_sections
        self.indexed_sections=inspect_indexed_sections(self.data)
        for label,pointer_off in [('menu',0xd4),('fonts',0xdc)]:
            pos=u32(self.data,pointer_off)
            if not (0x100 < pos < len(self.data)-100):continue
            decompressor=zlib.decompressobj()
            try:
                raw=decompressor.decompress(self.data[pos:]);raw+=decompressor.flush()
            except zlib.error:continue
            consumed=len(self.data)-pos-len(decompressor.unused_data)
            if not decompressor.eof or raw[:4]!=b'xet\0' or not (0<consumed<1_000_000):continue
            # First 13 named records appear as 0x54-byte doubly linked nodes in
            # the CHK metadata region immediately before the relocation list.
            records=[]
            start=raw.find(b'head\0')
            if start>0:start-=8
            # Discover all node candidates using self-referential next pointers.
            for p in range(0,len(raw)-0x54,4):
                prev,nxt=struct.unpack_from('<II',raw,p)
                if nxt!=p+0x54 or (prev!=0 and prev>len(raw)):continue
                name_bytes=raw[p+8:p+72]
                name=name_bytes.split(b'\0')[0]
                if not (2<=len(name)<=48) or not re.fullmatch(rb'[A-Za-z0-9_]+',name):continue
                if u32(raw,p+72)!=1:continue
                table_ptr=u32(raw,p+76)
                if table_ptr>=len(raw)-8:continue
                records.append((p,name.decode('ascii'),table_ptr))
            # Some head/tail nodes have non-+0x54 links; extend based on node
            # name+table-pointer signature in the continuous metadata area.
            if records:
                lo=min(r[0] for r in records);hi=max(r[0] for r in records)
                for p in range(max(0,lo-0x108),min(len(raw)-0x54,hi+0x108)+1,0x54):
                    n=raw[p+8:p+72].split(b'\0')[0]
                    if 2<=len(n)<=48 and re.fullmatch(rb'[A-Za-z0-9_]+',n) and u32(raw,p+72)==1:
                        t=u32(raw,p+76)
                        if t<len(raw)-8:records.append((p,n.decode('ascii'),t))
            entries=[];seen=set()
            if label=='menu':
                for node,name,tbl in sorted(records):
                    if node in seen:continue
                    seen.add(node)
                    if not (8<=tbl<=len(raw)-4):continue
                    fmt=u32(raw,tbl-4)
                    bpp=(fmt>>12)&0xf
                    if bpp not in (4,8) or (fmt&0xF)!=6:continue
                    # Name node points at the NEXT boundary, not at its own
                    # pixel start. Using raw[tbl] shifts every name one image
                    # forward (head becomes doubleclef, the last can be garbage).
                    try:
                        pix,psize,pal,palsize=chk_named_extent(raw,tbl,64,64,bpp)
                    except ValueError:
                        continue
                    entries.append(Texture(name,64,64,bpp,pix,psize,pal,palsize,
                        'dtz',True,bpp==8,fmt,
                        note=f'{label}.chk node 0x{node:X}; verified named boundary 0x{pix:X}..0x{u32(raw,tbl):X}; palette preserved'))
            elif label=='fonts':
                # The indexed font atlases use the SAME end-pointer scheme.
                # font1 and font2 are consecutive 256x256 PAL4 allocations.
                # font3 uses a distinct descriptor and remains read-only.
                for name in ('font1','font2'):
                    match=re.search(re.escape(name.encode('ascii'))+rb'\x00',raw)
                    if not match or match.start()<8:continue
                    node=match.start()-8
                    if u32(raw,node+72)!=1:continue
                    tbl=u32(raw,node+76)
                    if not (8<=tbl<=len(raw)-4) or u32(raw,tbl-4)!=0x01104208:continue
                    try:
                        pix,psize,pal,palsize=chk_named_extent(raw,tbl,256,256,4)
                    except ValueError:
                        continue
                    entries.append(Texture(name,256,256,4,pix,psize,pal,palsize,
                         'dtz',True,False,0x01104208,
                         note=f'LCS PS2 fonts.chk verified {name} atlas; named boundary 0x{pix:X}..0x{u32(raw,tbl):X}'))
            self.sections.append({'name':label,'offset':pos,'compressed_size':consumed,'raw':raw,'entries':entries})
            for tex in entries:self.entries.append((label,tex))

        # VCS PS2 stores named frontend/menu and font textures in two
        # independent XET zlib streams. These are not at the LCS CHK header
        # fields (0xD4/0xDC); VCS links them at 0xE0/0xFC instead.
        # Match the XET magic AND the expected name to avoid accidentally
        # misidentifying a different game's header or an arbitrary XET.
        from VCS_XET_Textures import read_vcs_xet, read_named_xet_textures
        for label, field, signature in (
                ('menu', 0xE0, b'radio_4\0'),
                ('fonts', 0xFC, b'fancytext\0')):
            if any(s['name'] == label for s in self.sections):
                continue
            hit = read_vcs_xet(self.data, field, expected_name=signature)
            if hit is None:
                continue
            pos, consumed, raw = hit
            textures, unknown = read_named_xet_textures(raw, group=label)
            if not textures:
                continue
            self.sections.append({'name': label, 'offset': pos,
                                  'compressed_size': consumed, 'raw': raw,
                                  'entries': textures, 'pointer_field': field,
                                  'unknown_records': unknown,
                                  'source_format': 'VCS XET'})
            for tex in textures:
                self.entries.append((label, tex))

        # VCS PS2/PSP radar map tiles are 66 individual compressed XETs.
        # Two distinct picture layouts are present in each version. In the PS2
        # archive the raster offset is explicitly at XET+0x58 (0x60 or 0xC0).
        # PSP has 0x20FC/0x2100 XETs, with native swizzled pixels at 0xA0/0x40.
        # Names may be in a prefix metadata record or at the tail of the XET;
        # only valid radar## records with fixed payload spans are accepted.
        if not any(s['name'].startswith('XET@') for s in self.sections):
            found={}
            # Only accept radar XET streams addressed by GATG relocation
            # records. Stale superseded compressed streams deliberately remain
            # in the archive after a size-increasing edit; blind zlib signature
            # scanning would otherwise expose them as duplicate textures.
            active_xet_refs = None
            if len(self.data) >= 0x100:
                rev, declared, _, rel_pos, rel_count = struct.unpack_from('<IIIII', self.data, 4)
                if (rev == 1 and declared == len(self.data) and
                        rel_pos >= 0x100 and rel_pos % 4 == 0 and
                        rel_count > 0 and rel_pos + rel_count * 4 == len(self.data)):
                    active_xet_refs = {}
                    for (location,) in struct.iter_unpack('<I', memoryview(self.data)[rel_pos:]):
                        if location % 4 == 0 and 0 < location <= rel_pos - 4:
                            value = u32(self.data, location)
                            if (0x100 <= value < rel_pos and
                                    self.data[value:value+2] in (b'\x78\xda',b'\x78\x9c',b'\x78\x01')):
                                active_xet_refs.setdefault(value, []).append(location)
            for sig in (b'\x78\xda', b'\x78\x9c', b'\x78\x01'):
                cursor=0
                while True:
                    pos=self.data.find(sig,cursor)
                    if pos<0:break
                    cursor=pos+1
                    if pos in found:continue
                    if active_xet_refs is not None and len(active_xet_refs.get(pos, ())) != 1:continue
                    try:
                        dec=zlib.decompressobj()
                        raw=dec.decompress(self.data[pos:],8_000_000)
                        if not dec.eof or raw[:4]!=b'xet\0':continue
                        clen=len(self.data)-pos-len(dec.unused_data)
                        if not (20<clen<=1000000):continue
                    except zlib.error:continue
                    length=len(raw)
                    if length==8476:
                        # PS2 XET: named record before or after the raster.
                        pixel=u32(raw,0x58)
                        if pixel not in (0x60,0xC0):continue
                        metas=raw[0x60:0xC0] if pixel==0xC0 else raw[0x20A0:]
                        owner='dtz';swz=False;pitch=0;platform='PS2'
                    elif length in (8444,8448):
                        # PSP XET stores a 16-byte raster descriptor either
                        # before or after its pixel/CLUT data, depending on
                        # whether the tile is preceded by a name record.
                        # Decode its *actual* pointer instead of assuming 0x40.
                        dpat=struct.pack('<II',0x07070040,0x4500C104)
                        possible=[];at=0
                        while True:
                            hit=raw.find(dpat,at)
                            if hit<4:break
                            at=hit+1
                            pixel=u32(raw,hit-4);pal=pixel+8192
                            if pixel not in (0x40,0x50,0x90,0xA0):continue
                            if pal+64>len(raw):continue
                            if not (hit+8<=pixel or hit-4>=pal+64):continue
                            possible.append(pixel)
                        if len(possible)!=1:continue
                        pixel=possible[0]
                        metas=raw[:pixel]+raw[pal+64:]
                        owner='indexed_psp';swz=True;pitch=64;platform='PSP'
                    else:continue
                    match=re.search(rb'radar[0-9]{2}\x00',metas,re.IGNORECASE)
                    if not match:continue
                    name=match.group()[:-1].decode('ascii')
                    pal=pixel+8192
                    if pal+64>len(raw):continue
                    tex=Texture(name,128,128,4,pixel,8192,pal,64,owner,swz,False,0,
                         note=f'VCS {platform} radar XET at GATG 0x{pos:X} (strict compressed slot)',
                         row_pitch=pitch,platform=platform)
                    # Naming by the pointer *location*, not the compressed
                    # stream location, keeps the section ID stable when a
                    # larger stream is relocated to the GATG tail.
                    ref_field = (active_xet_refs[pos][0] if active_xet_refs is not None
                                 else pos)
                    found[pos]={'name':f'XET@{ref_field:06X}','offset':pos,'compressed_size':clen,
                                'raw':raw,'entries':[tex], 'pointer_field':ref_field}
            for pos,sect in sorted(found.items(), key=lambda item: (item[1]['pointer_field'], item[0])):
                self.sections.append(sect)
                self.entries.append((sect['name'],sect['entries'][0]))

        # Decode the *direct* GATG raster descriptors for HUD / particle /
        # generic resources, as opposed to CHK streams or raw index names.
        # The native PSP row-pitch/tile layout and PS2 GS indexed descriptors
        # are validated before exposing these entries as writable textures.
        from GATG_Indexed_Texture import indexed_gatg_textures
        self.direct_entries=indexed_gatg_textures(self.data,self.indexed_sections)
        if self.direct_entries:
            by_section={}
            for label,tex in self.direct_entries:
                by_section.setdefault(label,[]).append(tex)
                self.entries.append((label,tex))
            for label,entries in by_section.items():
                self.sections.append({'name':label,'offset':0,'compressed_size':0,
                                      'raw':self.data,'entries':entries,'direct':True})
            # Do not list successfully decoded entries a second time as
            # 'index only'. Retain genuinely unknown formats in that catalog.
            done={(label,t.name) for label,t in self.direct_entries}
            self.indexed_sections={label:[rec for rec in items if (label,rec['name']) not in done]
                                   for label,items in self.indexed_sections.items()}

    def preview(self,i,swizzled=None):
        label,tex=self.entries[i]
        section=next(s for s in self.sections if s['name']==label)
        return tex.image(section['raw'],swizzled)

    def replace(self,i,png_path,swizzled=None):
        label,tex=self.entries[i]
        section=next(s for s in self.sections if s['name']==label)
        if section.get('direct'):
            # Direct GATG rasters are not separate compressed CHK slots.
            # Reparse transactionally and never reinterpret a user preview
            # toggle as the actual on-disc storage format.
            updated=tex.replace_pixels(self.data,png_path,tex.swizzled)
            reread=DTZDocument(zlib.compress(updated,9))
            current=[(g,t.name,t.pixel_offset,t.pixel_size,t.palette_offset,t.palette_size) for g,t in self.entries]
            revised=[(g,t.name,t.pixel_offset,t.pixel_size,t.palette_offset,t.palette_size) for g,t in reread.entries]
            if current!=revised: raise ValueError('Direct GATG index changed after replacement')
            self.__dict__.update(reread.__dict__)
            return
        updated=tex.replace_pixels(section['raw'],png_path,tex.swizzled)
        from DTZ_CHK_Relocation import pack_chk, replace_chk_stream
        packed = pack_chk(updated, capacity=section['compressed_size'])
        rebuilt, _ = replace_chk_stream(self.data, section, packed)
        reread = DTZDocument(zlib.compress(rebuilt, 9))
        old_entries = [(g,t.name,t.width,t.height,t.bpp,t.pixel_offset,t.palette_offset) for g,t in self.entries]
        new_entries = [(g,t.name,t.width,t.height,t.bpp,t.pixel_offset,t.palette_offset) for g,t in reread.entries]
        if old_entries != new_entries:
            raise ValueError('DTZ texture index changed after PNG replacement')
        if next(s for s in reread.sections if s['name']==label)['raw'] != updated:
            raise ValueError('CHK failed to round-trip after PNG replacement')
        self.__dict__.update(reread.__dict__)

    def save(self,path):
        # Protect users who open an archive produced by v0.3.35/v0.3.36:
        # those builds inserted CHK streams between the two GATG pointer
        # tables, silently breaking the second table's coverage of the first.
        from DTZ_CHK_Relocation import _gatg_tables
        _gatg_tables(self.data)
        packed=zlib.compress(self.data,9)
        if zlib.decompress(packed)!=self.data:raise ValueError('DTZ verification failed')
        Path(path).write_bytes(packed)
        if zlib.decompress(Path(path).read_bytes())!=self.data:raise ValueError('DTZ write-back verification failed')
