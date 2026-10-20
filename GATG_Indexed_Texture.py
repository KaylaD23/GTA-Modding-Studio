"""Validated direct GATG HUD/particle/generic indexed raster parser.

These GTA Stories records are outside the zlib CHK menu streams. PS2 descriptors
are 8-byte pointer/format records; PSP descriptors are 16-byte records carrying
pointer, pitch, two size exponents and indexed bit depth. Direct bytes remain
at their fixed offsets in the decompressed GATG image. No pointer relocation.
"""
from __future__ import annotations
import struct
from PIL import Image


def psp_unswizzle(raw: bytes, pitch: int, height: int) -> bytes:
    if pitch % 16 or height % 8 or len(raw) != pitch * height:
        raise ValueError('Unsupported PSP swizzle allocation')
    dest = bytearray(len(raw)); blocks_x=pitch//16
    for y in range(height):
        for x in range(pitch):
            source = ((y//8)*blocks_x + x//16)*128 + (y%8)*16 + x%16
            dest[y*pitch+x] = raw[source]
    return bytes(dest)


def psp_swizzle(raw: bytes, pitch: int, height: int) -> bytes:
    if pitch % 16 or height % 8 or len(raw) != pitch*height:
        raise ValueError('Unsupported PSP swizzle allocation')
    dest=bytearray(len(raw));blocks_x=pitch//16
    for y in range(height):
        for x in range(pitch):
            target=((y//8)*blocks_x+x//16)*128 + (y%8)*16 + x%16
            dest[target]=raw[y*pitch+x]
    return bytes(dest)


def psp_index_image(data:bytes, texture, *, swizzled=True) -> Image.Image:
    from texture_formats import unpack4
    bpp=texture.bpp;w=texture.width;h=texture.height;pitch=texture.row_pitch
    raw=data[texture.pixel_offset:texture.pixel_offset+texture.pixel_size]
    pixels=psp_unswizzle(raw,pitch,h) if swizzled else raw
    indices=[]
    for y in range(h):
        row=pixels[y*pitch:(y+1)*pitch]
        if bpp==4: indices.extend(unpack4(row)[:w])
        else: indices.extend(row[:w])
    palette=texture.palette(data)
    rgba=bytes(c for i in indices for c in palette[i]);img=Image.frombytes('RGBA',(w,h),rgba)
    return img


def indexed_gatg_textures(data: bytes, records: dict) -> list[tuple[str, object]]:
    """Parse game-specific GATG 4/8bpp indexed descriptors by validated sizes.

    The linked resource node `record_type` is *not* a raster type: earlier
    releases discarded most real textures by checking for node type 2 only.
    Descriptor format and bounds are the validation source instead.

    PSP: [0, pixel_pointer, row_pitch|w_exp<<16|h_exp<<24, format]
    LCS PS2: [pixel_pointer, GS_format] (possibly followed by next descriptor)
    VCS PS2: [0, native_header_flags, pixel_pointer, GS_format].

    PS2 size codes are cyclic for dimensions >=256.  Infer the exact height
    using the next raster pointer when possible, not only the packed nibble.
    """
    from texture_formats import Texture
    import bisect
    potential=[]
    for section, items in records.items():
        for row in items:
            address=row['asset_pointer']; name=row['name']
            if address < 0x100 or address+16>len(data): continue
            v=struct.unpack_from('<4I',data,address)
            # PSP and LCS PS2 have opposite pointer positions: PSP pointer is
            # the second 32-bit word, while PS2 starts with a live file offset.
            if v[0]==0 and v[1] >= 0x100 and ((v[2]>>16)&255) in range(3,11) \
               and ((v[2]>>24)&255) in range(3,11) and (v[3]&255) in (4,8):
                ptr=v[1];geo=v[2];fmt=v[3]
                we=(geo>>16)&255; he=(geo>>24)&255
                width=1<<we;height=1<<he; pitch=geo&65535; bpp=fmt&255
                ps=pitch*height;pal=(16 if bpp==4 else 256)*4
                if pitch%16 or height%8 or pitch<(width*bpp+7)//8 or \
                   ptr<0x100 or ptr+ps+pal>len(data):continue
                potential.append((section,row,'PSP',ptr,fmt,width,height,bpp,ps,pal,pitch))
                continue
            # LCS PS2 references the first pair.  VCS PS2's descriptor has
            # two control words first and the pointer+format at offsets +8/+12.
            if v[0] >= 0x100:
                ptr,fmt=v[0],v[1]
                ps2_is_swizzled=True
            elif v[0]==0 and v[2]>=0x100:
                ptr,fmt=v[2],v[3]
                ps2_is_swizzled=False
            else:
                continue
            if ptr < 0x100 or ptr>=len(data):continue
            pcode=(fmt>>8)&255
            bpp={0x40:4,0x41:4,0x42:4,0x80:8,0x81:8}.get(pcode)
            exp=fmt&15
            dimcode=fmt&255
            hy=(dimcode>>4)&15
            if bpp is None or not (3<=exp<=10) or hy not in (0,4,8,12):continue
            width=1<<exp
            potential.append((section,row,'PS2-swz' if ps2_is_swizzled else 'PS2-linear',ptr,fmt,width,0,bpp,0,(16 if bpp==4 else 256)*4,0))

    # Determine the distance to the next distinct indexed image.  In native
    # GATG these are generally contiguous pixel+CLUT blocks, but there may be
    # unindexed effects/data between named textures.  Never exceed a known
    # following image's starting offset.
    pointers=sorted({v[3] for v in potential})
    results=[]
    for section,row,platform,ptr,fmt,width,height,bpp,ps,pal,pitch in potential:
        at=bisect.bisect_right(pointers,ptr)
        next_ptr=pointers[at] if at<len(pointers) else len(data)
        if platform.startswith('PS2'):
            exp=fmt&15; code=(fmt>>4)&15
            # y exponent modulo 4 is encoded in bits 4..7.  0xC3 is an 8x8
            # image, whereas 0xC7 is 128x128.  0x08 is 256x256, not 256x16.
            heights=[1<<h for h in range(3,11) if ((h-4)%4)*4==code]
            viable=[]
            for candidate_h in heights:
                size=width*candidate_h*bpp//8
                end=ptr+size+pal
                if end>len(data) or end>next_ptr:continue
                gap=next_ptr-end
                viable.append((gap,abs((candidate_h.bit_length()-1)-exp),candidate_h,size))
            if not viable:continue
            _,_,height,ps=min(viable)
            # A 16-byte padding between images is normal; >64 indicates the
            # source has unindexed bytes. Keep it but do not infer a neighbour.
        if ptr+ps+pal>len(data) or ptr+ps+pal>next_ptr:continue
        if width>1024 or height>1024 or width*height>1024*1024:continue
        if platform=='PSP':
            # GTA LCS PSP vehicle/generic rasters with format byte 05/06/07
            # contain PSP mip levels BETWEEN the base level and CLUT.  The
            # palette is the final 16/256 RGBA entries before the next raster,
            # not immediately after the base pixel allocation.
            miplevels=(fmt >> 8) & 255
            paloffset=(next_ptr-pal) if 2 <= miplevels <= 8 else ptr+ps
            if paloffset < ptr+ps or paloffset+pal > next_ptr: continue
            tex=Texture(row['name'],width,height,bpp,ptr,ps,paloffset,pal,
                 'indexed_psp',True,False,fmt,
                 note=f'{section}: PSP native raster at 0x{row["asset_pointer"]:X}; node 0x{row["record_offset"]:X}',
                 row_pitch=pitch,platform='PSP')
        else:
            tex=Texture(row['name'],width,height,bpp,ptr,ps,ptr+ps,pal,
                 'indexed_ps2',bool(fmt & 0x01000000) and width>=16, bpp==8,fmt,
                 note=f'{section}: PS2 native raster at 0x{row["asset_pointer"]:X}; node 0x{row["record_offset"]:X}',
                 platform='PS2')
        results.append((section,tex))
    return results
