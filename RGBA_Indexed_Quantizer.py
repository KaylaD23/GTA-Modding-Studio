"""Transparency-safe RGBA quantization for indexed PlayStation textures.

Do not quantize RGB and subsequently average alpha by RGB index. Two pixels
with identical RGB but different alpha are different RGBA colours and must not
silently share an entry. In particular, anti-aliased icon edges must never
inherit opacity from the transparent background.
"""
from __future__ import annotations

from PIL import Image


def quantize_rgba(img_rgba: Image.Image, colors: int, dither: bool = False):
    """Return (linear palette indices, RGBA palette) for a 4/8bpp image.

    The palette is padded to the requested size. Fully transparent texels are
    normalized to one transparent colour before quantization, preventing their
    arbitrary hidden RGB values from consuming palette slots. Pillow's RGBA
    octree tracks alpha independently from RGB; unlike RGB-only quantization it
    can distinguish transparent black and opaque black.
    """
    if not 1 <= colors <= 256:
        raise ValueError('Indexed RGBA palette must contain 1..256 colours')
    rgba = img_rgba.convert('RGBA')
    data = bytearray(rgba.tobytes())
    for i in range(0, len(data), 4):
        if data[i+3] == 0:
            data[i:i+3] = b'\x00\x00\x00'
    # The original PS2/PSP assets often use fewer than 16 exact RGBA entries.
    # Octree quantization can merge near colours even when there is enough
    # space to preserve all of them. Prefer a lossless direct palette in that
    # case, retaining even close edge shades and distinct alpha values.
    unique = {}
    exact_indices = bytearray()
    fits = True
    for i in range(0, len(data), 4):
        pixel = tuple(data[i:i+4])
        index = unique.get(pixel)
        if index is None:
            if len(unique) == colors:
                fits = False
                break
            index = len(unique)
            unique[pixel] = index
        exact_indices.append(index)
    if fits:
        exact_palette = list(unique.keys())
        exact_palette.extend([(0, 0, 0, 0)] * (colors-len(exact_palette)))
        return bytes(exact_indices), exact_palette

    # When PAL4 has more than 16 visible shades, reserve an *exactly*
    # transparent entry rather than letting the octree blend the background
    # into partially opaque colour slots.
    transparent = any(data[i+3] == 0 for i in range(0, len(data), 4))
    if transparent and colors == 1:
        raise ValueError('A one-colour CLUT cannot preserve transparency and visible pixels')
    if transparent:
        visible_positions = [i for i in range(0, len(data), 4) if data[i+3] != 0]
        if not visible_positions:
            return bytes(len(data)//4), [(0, 0, 0, 0)] * colors
        count = len(visible_positions)
        visible_pixels = b''.join(data[i:i+4] for i in visible_positions)
        normal = Image.frombytes('RGBA', (count, 1), visible_pixels)
        reserved = 1
    else:
        normal = Image.frombytes('RGBA', rgba.size, bytes(data))
        reserved = 0
    indexed = normal.quantize(colors=colors-reserved, method=Image.Quantize.FASTOCTREE,
                              dither=Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE)
    mode, palette_raw = indexed.palette.getdata()
    if mode != 'RGBA':
        raise ValueError('Pillow discarded alpha during indexed RGBA quantization')
    palette = [tuple(palette_raw[i:i+4]) for i in range(0, min(len(palette_raw), (colors-reserved)*4), 4)]
    if reserved:
        compressed_indices = iter(indexed.tobytes())
        indices = bytearray(len(data)//4)
        for i in range(len(indices)):
            if data[i*4+3] != 0:
                indices[i] = next(compressed_indices)+1
        palette.insert(0, (0, 0, 0, 0))
    else:
        indices = indexed.tobytes()
    if len(palette) < colors:
        palette.extend([(0, 0, 0, 0)] * (colors-len(palette)))
    if len(indices) != rgba.width * rgba.height or max(indices, default=0) >= colors:
        raise ValueError('Quantized image contains invalid indexed pixels')
    return bytes(indices), palette
