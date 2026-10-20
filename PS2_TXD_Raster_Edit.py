"""Validated PS2 native indexed TXD raster edits.

PAL4/PAL8 changes are supported through a same-RenderWare-version PS2 native
raster donor (which supplies authentic GIF/GIFtag/GS fields). No undocumented
GS descriptors are synthesized. Only the target texture's raster chunk is
changed; its original name, mask, filtering and extension chunks are kept.

Non-8888 palette pixel formats, direct-color rasters and mipmap rebuilds still
require game-specific serializer support and are deliberately refused.
"""
from __future__ import annotations
from pathlib import Path
from PIL import Image
import tempfile, struct
from texture_formats import TXDDocument, TIM2Document, chunks, png_to_tim2, pack4, clut8_index, rgba_to_ps2_alpha
from TM2_Replacement_Bridge import replace_txd_from_tm2

PIXEL_FORMAT_CHOICES = (
    'RASTER_1555', 'RASTER_565', 'RASTER_4444', 'RASTER_LUM',
    'RASTER_8888', 'RASTER_888', 'RASTER_555', 'RASTER_LUM_ALPHA',
)
RASTER_MODES = ('Original', 'Raw Raster', 'Palettized')
PALETTE_CHOICES = ('PAL4', 'PAL8')


def validate_ps2_raster_settings(current_bpp: int, *, mode: str,
                                 palette: str, pixel_format: str,
                                 generate_mipmaps: bool = False) -> int:
    """Return requested bpp or refuse a not-yet-supported native serialization.

    This function is side-effect free and may be run before displaying any
    donor-file chooser. A UI selection is not taken to imply codec support.
    """
    if current_bpp not in (4, 8):
        raise ValueError('The selected TXD is not a supported indexed PS2 native raster.')
    if mode not in RASTER_MODES:
        raise ValueError(f'Unknown raster mode: {mode}')
    if pixel_format not in PIXEL_FORMAT_CHOICES:
        raise ValueError(f'Unknown pixel format: {pixel_format}')
    if generate_mipmaps:
        raise ValueError('Generating mip levels requires a new PS2 native mip-chain serializer. No file bytes were changed.')
    if mode == 'Raw Raster':
        raise ValueError('Raw Raster requires a direct-color PS2 native raster serializer. The indexed TXD structure cannot safely be re-tagged as raw.')
    if mode == 'Original':
        return current_bpp  # Preserve original native format and all pixel metadata.
    if pixel_format != 'RASTER_8888':
        raise ValueError(f'{pixel_format} is available in the format selector, but no PS2 native CLUT serializer for this format has been validated. Only RASTER_8888 (32-bit RGBA CLUT) is supported for writes.')
    if palette not in PALETTE_CHOICES:
        raise ValueError('Choose PAL4 or PAL8.')
    return int(palette[-1])


def _native_nodes(doc: TXDDocument):
    b = doc.data
    outer = chunks(b, 0, len(b))
    if len(outer) != 1 or outer[0][0] != 0x16:
        raise ValueError('Not a RenderWare texture dictionary.')
    root = outer[0]
    all_children = chunks(b, root[2], root[2] + root[3])
    natives = [node for node in all_children if node[0] == 0x15]
    if len(natives) != len(doc.textures):
        raise ValueError('The TXD contains unrecognized native textures; rebuilding is unsafe.')
    return root, natives


def donor_candidates(target: TXDDocument, idx: int, donor: TXDDocument, bpp: int):
    """List safe donor native-image indices with matching dimensions/version."""
    target_root, target_native = _native_nodes(target)
    donor_root, donor_native = _native_nodes(donor)
    if target_root[4] != donor_root[4]:
        return []  # Do not mix RenderWare game-version serializers.
    tgt = target.textures[idx]
    node = target_native[idx]
    target_sub = chunks(target.data, node[2], node[2]+node[3])
    if len(target_sub) < 4 or target_sub[3][0] != 1:
        raise ValueError('Selected texture has no recognized native raster.')
    found = []
    for j, t in enumerate(donor.textures):
        if (t.width, t.height, t.bpp) != (tgt.width, tgt.height, bpp):
            continue
        n = donor_native[j]
        sub = chunks(donor.data, n[2], n[2]+n[3])
        if len(sub) < 4 or sub[3][0] != 1:
            continue
        if n[4] != node[4] or sub[3][4] != target_sub[3][4]:
            continue
        # A native PS2 raster has an independent image chunk whose header
        # advertises its actual platform. Keep the platform and revision.
        target_r = target_sub[3]
        donor_r = sub[3]
        first = chunks(target.data, target_r[2], target_r[2]+target_r[3])[0]
        second = chunks(donor.data, donor_r[2], donor_r[2]+donor_r[3])[0]
        if first[0] != 1 or second[0] != 1 or first[3] < 64 or second[3] < 64:
            continue
        # Pixel format flags deliberately differ between PAL4 and PAL8.
        # Confirm instead that both native texture wrappers advertise PS2.
        if target.data[target_sub[0][2]:target_sub[0][2]+4] != b'PS2\0':
            continue
        if donor.data[sub[0][2]:sub[0][2]+4] != b'PS2\0':
            continue
        found.append(j)
    return found


def _transplant_native_raster(target: TXDDocument, idx: int,
                              donor: TXDDocument, donor_idx: int,
                              desired_bpp: int) -> TXDDocument:
    old_root, old_nodes = _native_nodes(target)
    _, donor_nodes = _native_nodes(donor)
    orig_t = target.textures[idx]
    if (orig_t.width, orig_t.height) != (donor.textures[donor_idx].width, donor.textures[donor_idx].height):
        raise ValueError('Donor dimensions do not match the selected native raster.')
    if donor.textures[donor_idx].bpp != desired_bpp:
        raise ValueError('Donor indexed format does not match requested PAL depth.')
    if donor_idx not in donor_candidates(target, idx, donor, desired_bpp):
        raise ValueError('Donor is not a compatible same-version PS2 native raster template.')
    source_node = old_nodes[idx]
    donor_node = donor_nodes[donor_idx]
    old_children = chunks(target.data, source_node[2], source_node[2]+source_node[3])
    donor_children = chunks(donor.data, donor_node[2], donor_node[2]+donor_node[3])
    old_r = old_children[3]
    new_r = donor_children[3]
    # Preserve target name, mask, platform wrapper, filter flags and extension.
    new_raster = donor.data[new_r[1]:new_r[2]+new_r[3]]
    native_blob = bytearray(target.data[source_node[1]:source_node[2]+source_node[3]])
    rel_start = old_r[1]-source_node[1]
    rel_end = (old_r[2]+old_r[3])-source_node[1]
    native_blob[rel_start:rel_end] = new_raster
    struct.pack_into('<I', native_blob, 4, len(native_blob)-12)
    result = bytearray(target.data)
    result[source_node[1]:source_node[2]+source_node[3]] = native_blob
    struct.pack_into('<I', result, 4, len(result)-12)
    reparsed = TXDDocument(bytes(result))
    if len(reparsed.textures) != len(target.textures):
        raise ValueError('Raster conversion changed dictionary texture count.')
    changed = reparsed.textures[idx]
    if (changed.name, changed.width, changed.height, changed.bpp) != (orig_t.name, orig_t.width, orig_t.height, desired_bpp):
        raise ValueError('Converted raster failed native dictionary reparse.')
    for j,(before,after) in enumerate(zip(target.textures,reparsed.textures)):
        if j == idx: continue
        if (before.name, before.width, before.height, before.bpp) != (after.name, after.width, after.height, after.bpp):
            raise ValueError('Another texture metadata entry changed during reserialization.')
        for left,right,length in ((before.pixel_offset,after.pixel_offset,before.pixel_size),
                                  (before.palette_offset,after.palette_offset,before.palette_size)):
            if target.data[left:left+length] != reparsed.data[right:right+length]:
                raise ValueError('Another texture payload changed during reserialization.')
    return reparsed




def _encode_rgba_tm2_for_native_image(image: Image.Image, bpp: int) -> bytes:
    """Build TIM2 through the verified template header, retaining RGBA alpha.

    The generic PNG encoder reduces RGBA to RGB before quantization. Native
    HUD icons often share RGB colors but intentionally use different alpha;
    color-key quantization would flatten their visibility. Preserve distinct
    RGBA entries exactly when the source has <=16/256 colors.
    """
    rgba=image.convert('RGBA')
    colors=16 if bpp==4 else 256
    ordered=list(dict.fromkeys(rgba.getdata()))
    if len(ordered) <= colors:
        palette=list(ordered)
        lookup={color:i for i,color in enumerate(ordered)}
        indices=bytes(lookup[p] for p in rgba.getdata())
    else:
        indexed=rgba.quantize(colors=colors, method=Image.Quantize.FASTOCTREE,
                              dither=Image.Dither.NONE)
        indices=indexed.tobytes()
        color_data=indexed.convert('RGBA')
        palette=[(0,0,0,0)]*colors
        assigned=set()
        for idx,px in zip(indices,color_data.getdata()):
            if idx not in assigned:
                palette[idx]=px
                assigned.add(idx)
    palette=palette+[(0,0,0,0)]*(colors-len(palette))
    with tempfile.TemporaryDirectory(prefix='gta_native_tm2_enc_') as tmp:
        png=Path(tmp)/'image.png';rgba.save(png)
        output=bytearray(png_to_tim2(png,bpp=bpp,width=rgba.width,height=rgba.height,swizzled=False))
    doc=TIM2Document(output)
    t=doc.texture
    encoded_pixels=pack4(indices) if bpp==4 else indices
    encoded_clut=bytearray(colors*4)
    for logical,(r,g,b,a) in enumerate(palette):
        stored=clut8_index(logical) if bpp==8 else logical
        encoded_clut[stored*4:stored*4+4]=bytes((r,g,b,rgba_to_ps2_alpha(a)))
    assert len(encoded_pixels)==t.pixel_size and len(encoded_clut)==t.palette_size
    output[t.pixel_offset:t.pixel_offset+t.pixel_size]=encoded_pixels
    output[t.palette_offset:t.palette_offset+t.palette_size]=encoded_clut
    TIM2Document(output)
    return bytes(output)


def convert_txd_palette_depth(document: TXDDocument, texture_index: int,
                              new_bpp: int, donor: TXDDocument,
                              donor_index: int, *, swizzled: bool = True) -> TXDDocument:
    """Convert one texture and return a validated, separately-owned document.

    Uses a native raster from the same RenderWare version/game as a structural
    template; RGB and alpha colors are requantized to a new indexed CLUT.
    """
    if new_bpp not in (4,8):
        raise ValueError('Supported native palette depths are PAL4 and PAL8.')
    original_texture = document.textures[texture_index]
    if original_texture.bpp == new_bpp:
        return TXDDocument(document.data)
    preview = original_texture.image(document.data, swizzled=swizzled)
    converted = _transplant_native_raster(document, texture_index, donor, donor_index, new_bpp)
    tm2 = _encode_rgba_tm2_for_native_image(preview, new_bpp)
    replace_txd_from_tm2(converted, texture_index, tm2,
                         source_swizzled=False, target_swizzled=swizzled,
                         source_psp_alpha=False)
    if converted.textures[texture_index].bpp != new_bpp:
        raise ValueError('Raster conversion did not retain its new palette depth.')
    return converted
