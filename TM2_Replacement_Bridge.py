"""Pixel+CLUT replacement from an encoded TM2 into existing PS2 TXD/DTZ slots.

The TM2 itself is *not* a native RenderWare or XET/CHK record. Decode its
indexed pixels/CLUT, translate their PS2 storage order if required, and write
only the target's existing pixel/CLUT allocation. Do not create/resize records.
"""
from __future__ import annotations

from pathlib import Path
import zlib
from texture_formats import TIM2Document, TXDDocument, DTZDocument
from texture_formats import unpack4, pack4, swizzle8, deswizzle8, clut8_index
from DTZ_CHK_Relocation import pack_chk, replace_chk_stream


def tm2_payload_for_target(source: bytes | bytearray | str | Path, target, *,
                           source_swizzled: bool = False,
                           target_swizzled: bool | None = None,
                           source_psp_alpha: bool | None = None) -> tuple[bytes, bytes]:
    """Return native texel+CLUT byte arrays for an existing supported texture.

    Strictly require matching dimensions and indexed bit depth. A mismatched
    TM2 cannot be safely inserted without a game-specific raster rebuild.
    """
    if isinstance(source, (str, Path)):
        source = Path(source).read_bytes()
    tm2 = TIM2Document(bytes(source))
    if (tm2.width, tm2.height) != (target.width, target.height):
        raise ValueError(
            f'TM2 image is {tm2.width}×{tm2.height}, but the selected texture is '
            f'{target.width}×{target.height}. Convert your PNG using the correct '
            'dimensions or the matching original TM2 template.')
    if tm2.bpp != target.bpp:
        raise ValueError(
            f'TM2 image is PAL{tm2.bpp}, but the selected texture is PAL{target.bpp}. '
            f'Rebuild the TM2 with PAL{target.bpp} using the PNG → TIM2 converter. '
            'The existing native texture raster format cannot be changed by this operation.')
    src = tm2.texture
    dest_swz = target.swizzled if target_swizzled is None else bool(target_swizzled)
    logical = unpack4(tm2.data[src.pixel_offset:src.pixel_offset+src.pixel_size]) if tm2.bpp == 4 else tm2.data[src.pixel_offset:src.pixel_offset+src.pixel_size]
    if source_swizzled:
        logical = deswizzle8(logical, tm2.width, tm2.height)
    if target.owner == 'indexed_psp':
        from GATG_Indexed_Texture import psp_swizzle
        # Logical indexed source becomes row-pitched PSP raster bytes.
        pitch=target.row_pitch
        linear=bytearray(pitch*target.height)
        for y in range(target.height):
            row=logical[y*target.width:(y+1)*target.width]
            if target.bpp==4: row=pack4(row)
            linear[y*pitch:y*pitch+len(row)]=row
        target_pixels=psp_swizzle(bytes(linear),pitch,target.height)
    else:
        target_pixels = swizzle8(logical, target.width, target.height) if dest_swz else bytes(logical)
    if target.bpp == 4 and target.owner != 'indexed_psp':
        target_pixels = pack4(target_pixels)
    if len(target_pixels) != target.pixel_size:
        raise ValueError('Decoded TM2 pixels do not match the target native texture allocation.')
    # Make CLUT values *logical* before rearranging them for the target raster.
    raw_palette = src.palette(tm2.data)
    count = 16 if target.bpp == 4 else 256
    if len(raw_palette) != count:
        raise ValueError('TM2 palette has an incompatible number of colors.')
    logical_clut = [raw_palette[clut8_index(i)] if tm2.bpp == 8 else raw_palette[i] for i in range(count)]
    if source_psp_alpha is None:
        source_psp_alpha = any(a > 128 for r,g,b,a in logical_clut)
    output_clut = bytearray(count * 4)
    for i,(r,g,b,a) in enumerate(logical_clut):
        # PSP TM2 assets can use full 0..255 alpha, but native PS2 RenderWare
        # and GS CLUT alpha is normally 0..128. Preserve PS2 CLUT values.
        if target.owner == 'indexed_psp':
            # PSP palette entries use full-range alpha rather than PS2 GS
            # 0..128 alpha. Promote incoming PS2 alpha values if needed.
            a = a if source_psp_alpha else min(255,a*2)
        else:
            if source_psp_alpha:
                a = (a * 128 + 127) // 255
            a = min(128,a)
        j = clut8_index(i) if target.clut_swapped and target.bpp == 8 else i
        output_clut[j*4:j*4+4] = bytes((r,g,b,a))
    if len(output_clut) != target.palette_size:
        raise ValueError('TM2 palette does not fit the existing native CLUT allocation.')
    return bytes(target_pixels), bytes(output_clut)



def _verify_visual_roundtrip(source, target, restored_image, *, source_swizzled=False,
                             source_psp_alpha=None):
    """Check *decoded RGBA pixels*, not merely file size or texture indexes.

    A structurally valid GATG/CHK may contain visually scrambled pixels. Decode
    the incoming TM2 as logical pixels and ensure that native write/reparse
    preserves that image exactly (including transparency and palette ordering).
    """
    from PIL import Image
    raw = Path(source).read_bytes() if isinstance(source, (str, Path)) else bytes(source)
    tm2 = TIM2Document(raw)
    indices = (unpack4(tm2.data[tm2.texture.pixel_offset:tm2.texture.pixel_offset+tm2.texture.pixel_size])
               if tm2.bpp == 4 else
               tm2.data[tm2.texture.pixel_offset:tm2.texture.pixel_offset+tm2.texture.pixel_size])
    if source_swizzled:
        indices = deswizzle8(indices, tm2.width, tm2.height)
    palette = tm2.texture.palette(tm2.data)
    if tm2.bpp == 8:
        palette = [palette[clut8_index(i)] for i in range(256)]
    psp_alpha = (any(a > 128 for _, _, _, a in palette)
                 if source_psp_alpha is None else source_psp_alpha)
    new_palette = []
    for r, g, b, a in palette:
        if target.owner == 'indexed_psp':
            alpha = a if psp_alpha else min(255, a*2)
        else:
            alpha = (a*128+127)//255 if psp_alpha else min(128, a)
            alpha = min(255, alpha*2)
        new_palette.append((r, g, b, alpha))
    expected = Image.frombytes('RGBA', (tm2.width, tm2.height),
                               bytes(c for index in indices for c in new_palette[index]))
    if expected.size != restored_image.size or expected.tobytes() != restored_image.tobytes():
        different = sum(a != b for a, b in zip(expected.getdata(), restored_image.getdata()))
        raise ValueError('TM2 native texture replacement changes decoded image pixels '
                         f'({different} of {tm2.width*tm2.height} pixels differ). '
                         'Swizzle, CLUT or alpha layout may be incorrect; refusing to commit.')


def _patched_native_blob(data: bytes, target, pixels: bytes, palette: bytes) -> bytes:
    out = bytearray(data)
    po = target.pixel_offset
    co = target.palette_offset
    if (po < 0 or co < 0 or po+len(pixels)>len(out) or co+len(palette)>len(out)):
        raise ValueError('Native texture pixel/CLUT offsets exceed their containing section.')
    if len(pixels) != target.pixel_size or len(palette) != target.palette_size:
        raise ValueError('A replacement does not fit the existing native texture byte allocations.')
    out[po:po+len(pixels)] = pixels
    out[co:co+len(palette)] = palette
    return bytes(out)


def replace_txd_from_tm2(document: TXDDocument, index: int, source, *,
                         source_swizzled: bool = False, target_swizzled: bool | None = None,
                         source_psp_alpha: bool | None = None) -> None:
    t = document.textures[index]
    pixels,clut = tm2_payload_for_target(source,t,
                       source_swizzled=source_swizzled,
                       target_swizzled=target_swizzled,
                       source_psp_alpha=source_psp_alpha)
    modified = _patched_native_blob(document.data,t,pixels,clut)
    verified = TXDDocument(modified)
    if len(verified.textures)!=len(document.textures):
        raise ValueError('TXD native texture count changed after replacement.')
    changed = verified.textures[index]
    if (changed.name,changed.width,changed.height,changed.bpp)!=(t.name,t.width,t.height,t.bpp):
        raise ValueError('TXD native texture metadata changed after replacement.')
    _verify_visual_roundtrip(source, changed, changed.image(modified),
                             source_swizzled=source_swizzled, source_psp_alpha=source_psp_alpha)
    document.data = modified
    document.textures = verified.textures


def replace_dtz_from_tm2(document: DTZDocument, index: int, source, *,
                         source_swizzled: bool = False, target_swizzled: bool | None = None,
                         source_psp_alpha: bool | None = None) -> None:
    label,t = document.entries[index]
    section = next(s for s in document.sections if s['name']==label)
    if section.get('direct'):
        # The GATG index directly describes the raster. There is no nested
        # compressed CHK allocation to modify; update only texels and CLUT in
        # the decompressed GATG, then rebuild/reparse the outer DTZ copy.
        pixels,clut = tm2_payload_for_target(source,t,
                          source_swizzled=source_swizzled,
                          target_swizzled=(t.swizzled if t.owner=='indexed_psp' else target_swizzled),
                          source_psp_alpha=source_psp_alpha)
        modified = _patched_native_blob(document.data,t,pixels,clut)
        reread=DTZDocument(zlib.compress(modified,9))
        original_names=[(label,tx.name,tx.width,tx.height,tx.bpp) for label,tx in document.entries]
        revised_names=[(label,tx.name,tx.width,tx.height,tx.bpp) for label,tx in reread.entries]
        if original_names != revised_names:
            raise ValueError('GATG index changed after direct raster replacement; refusing commit.')
        if reread.data != modified:
            raise ValueError('GATG direct-raster round trip failed.')
        _verify_visual_roundtrip(source, t, reread.preview(index),
                                 source_swizzled=source_swizzled, source_psp_alpha=source_psp_alpha)
        document.__dict__.update(reread.__dict__)
        return
    pixels,clut = tm2_payload_for_target(source,t,
                       source_swizzled=source_swizzled,
                       target_swizzled=target_swizzled,
                       source_psp_alpha=source_psp_alpha)
    modified = _patched_native_blob(section['raw'],t,pixels,clut)
    packed = pack_chk(modified, capacity=section['compressed_size'])
    output, relocated = replace_chk_stream(document.data, section, packed)
    # Parse the rebuilt outer archive before committing. Relocation must not
    # rename, move or truncate ANY existing decoded texture allocation.
    try:
        reread = DTZDocument(zlib.compress(output, 9))
    except Exception as exc:
        raise ValueError(f'DTZ reparse failed after TM2 replacement: {exc}') from exc
    previous = [(g, tx.name, tx.width, tx.height, tx.bpp,
                 tx.pixel_offset, tx.pixel_size, tx.palette_offset, tx.palette_size)
                for g, tx in document.entries]
    current = [(g, tx.name, tx.width, tx.height, tx.bpp,
                tx.pixel_offset, tx.pixel_size, tx.palette_offset, tx.palette_size)
               for g, tx in reread.entries]
    if previous != current:
        raise ValueError('DTZ texture names or allocations changed unexpectedly after CHK rebuilding')
    rebuilt = next(s for s in reread.sections if s['name'] == label)
    if rebuilt['raw'] != modified or rebuilt['compressed_size'] != len(packed):
        raise ValueError('Relocated CHK did not decode to the required native texture bytes')
    _verify_visual_roundtrip(source, t, reread.preview(index),
                             source_swizzled=source_swizzled, source_psp_alpha=source_psp_alpha)
    # The previous CHK remains intact. Both relocation tables are shifted
    # together and the second table is rebased to cover the moved first table.
    if relocated:
        old_start = section['offset']
        old_end = old_start + section['compressed_size']
        if reread.data[old_start:old_end] != document.data[old_start:old_end]:
            raise ValueError('CHK relocation modified the old archive region')
    document.__dict__.update(reread.__dict__)
