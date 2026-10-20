"""PNG/image to Sony TIM2 (.TM2) converter for GTA PS2/PSP modding.

The GTA Stories preset follows the commonly used 4-bit indexed image + 32-bit
CLUT + CSM1 settings and 16-byte picture-block alignment. The optional template
mode reads an existing one-picture TIM2 file and preserves its image type,
CLUT type, dimensions, and GS register values. It intentionally refuses
mipmapped or multi-picture templates rather than silently destroying metadata.

Pillow is the only third-party dependency. This module writes TIM2 directly;
it does not edit RenderWare TXD dictionaries or GAME.DTZ archives.
"""
from __future__ import annotations

import math
import struct
import tempfile
from pathlib import Path
from typing import Any

try:
    from PIL import Image
except ImportError:  # Friendly UI-friendly import failure
    Image = None

TIM2_MAGIC = b"TIM2"
TIM2_VERSION = 0x04
PICTURE_HEADER_SIZE = 48

# TIM2 imageType/bppType values used by common readers.
IMAGE_RGBA32 = 0x03
IMAGE_RGBA16 = 0x01
IMAGE_RGB24 = 0x02
IMAGE_INDEXED8 = 0x05
IMAGE_INDEXED4 = 0x04

CLUT_NONE = 0x00
CLUT_RGBA5551 = 0x01
CLUT_RGB888 = 0x02
CLUT_RGBA8888 = 0x03

PSM_CT32 = 0x00
PSM_CT24 = 0x01
PSM_CT16 = 0x02
PSM_T8 = 0x13
PSM_T4 = 0x14

FORMAT_TO_IMAGE_TYPE = {
    "32bit": IMAGE_RGBA32,
    "24bit": IMAGE_RGB24,
    "16bit": IMAGE_RGBA16,
    "8bit": IMAGE_INDEXED8,
    "4bit": IMAGE_INDEXED4,
}
IMAGE_TYPE_TO_FORMAT = {
    IMAGE_RGBA32: "32bit",
    IMAGE_RGB24: "24bit",
    IMAGE_RGBA16: "16bit",
    IMAGE_INDEXED8: "8bit",
    IMAGE_INDEXED4: "4bit",
}
FORMAT_LABELS = {
    "4-bit Indexed (16 colors)": "4bit",
    "8-bit Indexed (256 colors)": "8bit",
    "16-bit RGBA5551": "16bit",
    "24-bit RGB888": "24bit",
    "32-bit RGBA8888": "32bit",
}


def _require_pillow() -> None:
    if Image is None:
        raise RuntimeError("Pillow is not installed. Run Install_Dependencies.bat or: python -m pip install Pillow")


def _align_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


def _is_power_of_two(value: int) -> bool:
    return value > 0 and (value & (value - 1)) == 0


def _next_power_of_two(value: int) -> int:
    return 1 if value <= 1 else 1 << (value - 1).bit_length()


def _previous_power_of_two(value: int) -> int:
    return 1 if value <= 1 else 1 << (value.bit_length() - 1)


def _ps2_alpha(alpha: int) -> int:
    # GS/TIM2 RGBA32 assets conventionally use 0x80 for fully opaque alpha.
    return max(0, min(128, round(alpha * 128 / 255)))


def _pack_rgba5551(r: int, g: int, b: int, a: int) -> int:
    r5 = max(0, min(31, round(r * 31 / 255)))
    g5 = max(0, min(31, round(g * 31 / 255)))
    b5 = max(0, min(31, round(b * 31 / 255)))
    a1 = 1 if a >= 128 else 0
    # GS PSMCT16 bit order in memory: A1:B5:G5:R5.
    return (a1 << 15) | (b5 << 10) | (g5 << 5) | r5


def _format_from_image_type(image_type: int) -> str:
    if image_type not in IMAGE_TYPE_TO_FORMAT:
        raise ValueError(f"TIM2 image type 0x{image_type:02X} is not supported by this converter.")
    return IMAGE_TYPE_TO_FORMAT[image_type]


def inspect_tim2(path: str | Path) -> dict[str, Any]:
    """Read and validate the main TIM2 and first picture header."""
    p = Path(path)
    data = p.read_bytes()
    if len(data) < 16 or data[:4] != TIM2_MAGIC:
        raise ValueError(f"{p.name} is not a TIM2 file (missing TIM2 magic/header).")
    version, file_format, picture_count = struct.unpack_from("<BBH", data, 4)
    if picture_count < 1:
        raise ValueError("TIM2 file declares no pictures.")
    if len(data) < 16 + PICTURE_HEADER_SIZE:
        raise ValueError("TIM2 picture header is truncated.")
    off = 16
    total_size, clut_size, image_size = struct.unpack_from("<III", data, off)
    header_size, clut_colors = struct.unpack_from("<HH", data, off + 12)
    picture_format = data[off + 16]
    mipmap_count = data[off + 17]
    clut_type = data[off + 18]
    image_type = data[off + 19]
    width, height = struct.unpack_from("<HH", data, off + 20)
    gs_tex0, gs_tex1 = struct.unpack_from(">QQ", data, off + 24)
    gs_regs, gs_clut = struct.unpack_from(">II", data, off + 40)
    if total_size < PICTURE_HEADER_SIZE:
        raise ValueError("TIM2 picture's total size is smaller than its header.")
    if off + total_size > len(data):
        raise ValueError("TIM2 picture block extends beyond end of file.")
    if header_size < PICTURE_HEADER_SIZE or header_size > total_size:
        raise ValueError(f"Invalid TIM2 picture header size: {header_size}.")
    if width < 1 or height < 1:
        raise ValueError("TIM2 width and height must be non-zero.")
    if image_size + clut_size + header_size > total_size:
        raise ValueError("TIM2 image/CLUT data exceeds the picture block size.")
    fmt = IMAGE_TYPE_TO_FORMAT.get(image_type, f"unknown 0x{image_type:02X}")
    return {
        "path": p,
        "file_size": len(data),
        "version": version,
        "file_format": file_format,
        "picture_count": picture_count,
        "total_size": total_size,
        "clut_size": clut_size,
        "image_size": image_size,
        "header_size": header_size,
        "clut_colors": clut_colors,
        "picture_format": picture_format,
        "mipmap_count": mipmap_count,
        "clut_type": clut_type,
        "image_type": image_type,
        "image_format": fmt,
        "width": width,
        "height": height,
        "gs_tex0": gs_tex0,
        "gs_tex1": gs_tex1,
        "gs_regs": gs_regs,
        "gs_clut": gs_clut,
        "picture_offset": off,
        "raw": data,
    }


def _template_settings(template_path: str | Path) -> dict[str, Any]:
    info = inspect_tim2(template_path)
    if info["picture_count"] != 1:
        raise ValueError("Reference TIM2 must contain exactly one picture. Multi-picture templates are not safely rewritten yet.")
    if info["mipmap_count"] > 1:
        raise ValueError("Reference TIM2 contains mipmaps. This version refuses to remove/rebuild its mipmap chain silently.")
    if info["header_size"] != PICTURE_HEADER_SIZE:
        raise ValueError(f"Reference TIM2 uses a non-standard {info['header_size']}-byte picture header; only 48-byte headers are supported.")
    if info["image_format"].startswith("unknown"):
        raise ValueError("Reference TIM2's image format is not supported by this converter.")
    if info["image_format"] in ("4bit", "8bit") and info["clut_size"] == 0:
        raise ValueError("Reference TIM2 uses an indexed image but has no embedded CLUT; external/shared palette support is not implemented.")
    raw_end = PICTURE_HEADER_SIZE + info["image_size"] + info["clut_size"]
    a16 = _align_up(raw_end, 16)
    a128 = _align_up(raw_end, 128)
    if info["total_size"] == a16:
        alignment = 16
    elif info["total_size"] == a128:
        alignment = 128
    else:
        # Preserve the block's existing byte size whenever possible. If its
        # original padding does not match a common alignment, report an error.
        raise ValueError("Reference TIM2's block size is not consistent with 16- or 128-byte alignment; use the GTA Stories or Generic PS2 preset instead.")
    return {**info, "alignment": alignment}


def _quantize_rgb_preserve_alpha(img_rgba, colors: int, dither: bool):
    # Preserve both RGB and per-pixel alpha when selecting CLUT entries.
    # The older RGB-only path averaged alpha per palette entry, causing
    # opaque/translucent edge pixels and transparent background pixels to share
    # a colour and producing dark speckled halos on PAL4 icons.
    from RGBA_Indexed_Quantizer import quantize_rgba
    return quantize_rgba(img_rgba, colors, dither)


def _clut8_csm1_order(palette: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Reorder an 8-bit CSM1 palette by swapping stripes 1 and 2 per 32 entries."""
    if len(palette) != 256:
        return palette
    out = list(palette)
    for i, color in enumerate(palette):
        block = i // 32
        inner = i % 32
        stripe = inner // 8
        pos = inner % 8
        new_stripe = (0, 2, 1, 3)[stripe]
        out[block * 32 + new_stripe * 8 + pos] = color
    return out


def _encode_clut(palette: list[tuple[int, int, int, int]], clut_type: int,
                 image_format: str) -> bytes:
    if clut_type == CLUT_RGBA8888:
        # TIM2 CLUT type 3 is 32-bit RGBA; GS alpha is 0x80 at full opacity.
        out = bytearray()
        for r, g, b, a in palette:
            out.extend((r, g, b, _ps2_alpha(a)))
        return bytes(out)
    if clut_type == CLUT_RGB888:
        return bytes(channel for r, g, b, _a in palette for channel in (r, g, b))
    if clut_type == CLUT_RGBA5551:
        return b"".join(struct.pack("<H", _pack_rgba5551(r, g, b, a)) for r, g, b, a in palette)
    raise ValueError(f"Indexed TIM2 output requires a supported embedded CLUT (type 1 or 2); got 0x{clut_type:02X}.")


def _encode_pixels(img_rgba, image_format: str, clut_colors: int,
                   clut_type: int, dither: bool) -> tuple[bytes, bytes, int]:
    w, h = img_rgba.size
    if image_format in ("4bit", "8bit"):
        max_colors = 16 if image_format == "4bit" else 256
        color_count = min(max_colors, max(1, int(clut_colors)))
        indices, palette = _quantize_rgb_preserve_alpha(img_rgba, color_count, dither)
        if image_format == "8bit" and color_count == 256:
            palette = _clut8_csm1_order(palette)
        # Each scanline is independently byte-aligned; a final odd 4-bit pixel
        # receives a zero high nibble rather than shifting the next row.
        out = bytearray()
        if image_format == "4bit":
            for y in range(h):
                row = indices[y * w:(y + 1) * w]
                for x in range(0, w, 2):
                    lo = row[x] & 0x0F
                    hi = (row[x + 1] & 0x0F) if x + 1 < w else 0
                    out.append(lo | (hi << 4))
        else:
            out.extend(indices)
        clut = _encode_clut(palette, clut_type, image_format)
        return bytes(out), clut, color_count

    raw = img_rgba.convert("RGBA").tobytes()
    out = bytearray()
    if image_format == "32bit":
        for i in range(0, len(raw), 4):
            out.extend((raw[i], raw[i + 1], raw[i + 2], _ps2_alpha(raw[i + 3])))
        return bytes(out), b"", 0
    if image_format == "24bit":
        for i in range(0, len(raw), 4):
            out.extend((raw[i], raw[i + 1], raw[i + 2]))
        return bytes(out), b"", 0
    if image_format == "16bit":
        if dither:
            # Pillow's quantizer is reserved for indexed outputs; 5551 uses
            # nearest rounding at the moment, which avoids expensive hidden conversions.
            pass
        for i in range(0, len(raw), 4):
            out.extend(struct.pack("<H", _pack_rgba5551(raw[i], raw[i + 1], raw[i + 2], raw[i + 3])))
        return bytes(out), b"", 0
    raise ValueError(f"Unsupported TIM2 image format: {image_format}.")


def _calc_gs_tex0(width: int, height: int, image_format: str,
                  clut_type: int, has_clut: bool) -> int:
    if image_format == "32bit":
        psm, tbw = PSM_CT32, max(1, _align_up(width, 64) // 64)
    elif image_format == "24bit":
        psm, tbw = PSM_CT24, max(1, _align_up(width, 64) // 64)
    elif image_format == "16bit":
        psm, tbw = PSM_CT16, max(1, _align_up(width, 64) // 64)
    elif image_format == "8bit":
        psm, tbw = PSM_T8, max(1, _align_up(width, 128) // 64)
    else:
        psm, tbw = PSM_T4, max(1, _align_up(width, 256) // 64)
    tw = max(0, math.ceil(math.log2(max(1, width))))
    th = max(0, math.ceil(math.log2(max(1, height))))
    cpsm = PSM_CT16 if clut_type == CLUT_RGBA5551 else (PSM_CT24 if clut_type == CLUT_RGB888 else PSM_CT32)
    value = 0
    value |= (tbw & 0x3F) << 14
    value |= (psm & 0x3F) << 20
    value |= (tw & 0xF) << 26
    value |= (th & 0xF) << 30
    value |= (1 if image_format != "24bit" else 0) << 34  # TCC
    value |= 0 << 35  # TFX = MODULATE
    if has_clut:
        value |= (cpsm & 0xF) << 51
        value |= 0 << 55  # CSM1
        value |= 1 << 61  # CLD: load CLUT on first use
    return value & 0xFFFFFFFFFFFFFFFF


def _calc_gs_clut(has_clut: bool) -> int:
    # TEXCLUT register: CBW=1 when a CLUT is present; COU/COV remain zero.
    return 1 if has_clut else 0


def _resolve_resize(img, mode: str):
    if mode == "Keep source dimensions":
        return img
    w, h = img.size
    fn = _next_power_of_two if mode == "Resize up to powers of 2" else _previous_power_of_two
    nw, nh = fn(w), fn(h)
    if (nw, nh) == (w, h):
        return img
    return img.resize((nw, nh), Image.Resampling.LANCZOS)


def convert_png_to_tim2(input_path: str | Path, output_path: str | Path,
                        image_format: str = "4bit", alignment: int = 16,
                        resize_mode: str = "Keep source dimensions", dither: bool = False,
                        template_path: str | Path | None = None) -> dict[str, Any]:
    """Convert an image file to one-picture TIM2 and write output_path.

    image_format: 4bit, 8bit, 16bit, 24bit, 32bit
    alignment: 16 or 128 bytes for the picture block
    template_path: optional original one-picture .TM2 file to match
    """
    _require_pillow()
    src, dst = Path(input_path), Path(output_path)
    if not src.is_file():
        raise FileNotFoundError(f"Input image not found: {src}")
    if src.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".tif", ".tiff", ".webp", ".gif", ".ico", ".ppm", ".pgm", ".pbm"}:
        raise ValueError("Unsupported input image. Use PNG, JPG/JPEG, BMP, TGA, TIFF, WebP, GIF, ICO, PPM, PGM or PBM.")
    if alignment not in (16, 128):
        raise ValueError("Picture-block alignment must be 16 or 128 bytes.")
    if image_format not in FORMAT_TO_IMAGE_TYPE:
        raise ValueError(f"Unsupported image format {image_format!r}.")
    with Image.open(src) as opened:
        opened.load()
        img = opened.convert("RGBA")
    input_dimensions = img.size
    template = _template_settings(template_path) if template_path else None
    if template:
        w, h = template["width"], template["height"]
        if img.size != (w, h):
            img = img.resize((w, h), Image.Resampling.LANCZOS)
        image_format = template["image_format"]
        clut_type = template["clut_type"] if image_format in ("4bit", "8bit") else CLUT_NONE
        if image_format in ("4bit", "8bit"):
            clut_colors = template["clut_colors"] or (16 if image_format == "4bit" else 256)
            max_colors = 16 if image_format == "4bit" else 256
            if clut_colors < 1 or clut_colors > max_colors:
                raise ValueError(f"Reference TIM2 CLUT color count {clut_colors} is invalid for {image_format}.")
            if clut_type not in (CLUT_RGBA5551, CLUT_RGB888, CLUT_RGBA8888):
                raise ValueError(f"Reference TIM2 has an unsupported CLUT type 0x{clut_type:02X}.")
        else:
            clut_colors = 0
        alignment = template["alignment"]
        pixel_data, clut_data, actual_clut_colors = _encode_pixels(img, image_format, clut_colors, clut_type, dither)
        # Keep texture and GS register metadata from the source template. Since
        # dimensions/format are matched, those flags remain associated with the
        # original texture's format/layout.
        gs_tex0 = template["gs_tex0"]
        gs_tex1 = template["gs_tex1"]
        gs_regs = template["gs_regs"]
        gs_clut = template["gs_clut"]
        picture_format = template["picture_format"]
        image_type = template["image_type"]
        clut_type_out = clut_type
    else:
        img = _resolve_resize(img, resize_mode)
        if img.width < 1 or img.height < 1 or img.width > 4096 or img.height > 4096:
            raise ValueError("TIM2 texture dimensions must be between 1 and 4096 pixels per side.")
        if alignment not in (16, 128):
            raise ValueError("Picture-block alignment must be 16 or 128 bytes.")
        image_type = FORMAT_TO_IMAGE_TYPE[image_format]
        clut_type = CLUT_RGBA8888 if image_format in ("4bit", "8bit") else CLUT_NONE
        clut_colors = 16 if image_format == "4bit" else (256 if image_format == "8bit" else 0)
        pixel_data, clut_data, actual_clut_colors = _encode_pixels(img, image_format, clut_colors or 1, clut_type, dither)
        gs_tex0 = _calc_gs_tex0(img.width, img.height, image_format, clut_type, bool(clut_data))
        gs_tex1 = 0  # default nearest filtering; games may override at runtime
        gs_regs = 0
        gs_clut = _calc_gs_clut(bool(clut_data))
        picture_format = 0
        clut_type_out = clut_type

    w, h = img.size
    if w < 1 or h < 1 or w > 4096 or h > 4096:
        raise ValueError("TIM2 texture dimensions must be between 1 and 4096 pixels per side.")
    raw_size = PICTURE_HEADER_SIZE + len(pixel_data) + len(clut_data)
    total_size = _align_up(raw_size, alignment)
    header = bytearray(PICTURE_HEADER_SIZE)
    struct.pack_into("<III", header, 0, total_size, len(clut_data), len(pixel_data))
    struct.pack_into("<HH", header, 12, PICTURE_HEADER_SIZE, actual_clut_colors)
    # TIM2 v4: PictureFormat, MipmapCount, ClutType, ImageType.
    header[16] = picture_format & 0xFF
    header[17] = 1  # no mipmaps
    header[18] = clut_type_out & 0xFF
    header[19] = image_type & 0xFF
    struct.pack_into("<HH", header, 20, w, h)
    struct.pack_into(">QQII", header, 24, gs_tex0, gs_tex1, gs_regs, gs_clut)
    file_header = struct.pack("<4sBBH8s", TIM2_MAGIC, TIM2_VERSION, 0, 1, b"\x00" * 8)
    block = bytes(header) + pixel_data + clut_data
    block += b"\x00" * (total_size - len(block))
    payload = file_header + block
    # Validate the generated bytes before touching the destination.
    verify_tim2_bytes(payload)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(payload)
    return {
        "input": src,
        "output": dst,
        "input_dimensions": input_dimensions,
        "width": w,
        "height": h,
        "format": image_format,
        "clut_type": clut_type_out,
        "clut_colors": actual_clut_colors,
        "alignment": alignment,
        "file_size": len(payload),
        "image_size": len(pixel_data),
        "clut_size": len(clut_data),
        "used_template": bool(template),
        "template": Path(template_path) if template_path else None,
    }


def verify_tim2_bytes(data: bytes) -> dict[str, Any]:
    """Strict structural validation for the simple one-picture files we emit."""
    if len(data) < 16 + PICTURE_HEADER_SIZE or data[:4] != TIM2_MAGIC:
        raise ValueError("Output is missing the TIM2 magic or is too short.")
    version, file_format, picture_count = struct.unpack_from("<BBH", data, 4)
    if version != TIM2_VERSION:
        raise ValueError(f"Unexpected TIM2 version 0x{version:02X}.")
    if picture_count != 1:
        raise ValueError(f"Expected one TIM2 picture, found {picture_count}.")
    off = 16
    total_size, clut_size, image_size = struct.unpack_from("<III", data, off)
    header_size, clut_colors = struct.unpack_from("<HH", data, off + 12)
    picture_format = data[off + 16]
    mipmap_count = data[off + 17]
    clut_type = data[off + 18]
    image_type = data[off + 19]
    width, height = struct.unpack_from("<HH", data, off + 20)
    if header_size != PICTURE_HEADER_SIZE:
        raise ValueError(f"Expected 48-byte picture header, got {header_size}.")
    if mipmap_count != 1:
        raise ValueError("This converter emits exactly one mip level.")
    if width < 1 or height < 1:
        raise ValueError("Output texture dimensions are invalid.")
    if image_type not in IMAGE_TYPE_TO_FORMAT:
        raise ValueError(f"Output image type 0x{image_type:02X} is unsupported.")
    if total_size % 16 != 0:
        raise ValueError("Picture block is not 16-byte aligned.")
    if off + total_size != len(data):
        raise ValueError("File length does not match the single picture-block length.")
    if header_size + image_size + clut_size > total_size:
        raise ValueError("Image/CLUT data would overrun the picture block.")
    image_format = IMAGE_TYPE_TO_FORMAT[image_type]
    expected_image_size = {
        "32bit": width * height * 4,
        "24bit": width * height * 3,
        "16bit": width * height * 2,
        "8bit": width * height,
        "4bit": ((width + 1) // 2) * height,
    }[image_format]
    if image_size != expected_image_size:
        raise ValueError(f"Image data size is {image_size}; expected {expected_image_size} for {width}x{height} {image_format}.")
    if image_format in ("4bit", "8bit"):
        if clut_type not in (CLUT_RGBA5551, CLUT_RGB888, CLUT_RGBA8888):
            raise ValueError("Indexed TIM2 output has no supported CLUT type.")
        bytes_per_color = {CLUT_RGBA5551: 2, CLUT_RGB888: 3, CLUT_RGBA8888: 4}[clut_type]
        if clut_colors < 1 or clut_size != clut_colors * bytes_per_color:
            raise ValueError("Indexed TIM2 output has inconsistent CLUT color count/size.")
        if clut_colors > (16 if image_format == "4bit" else 256):
            raise ValueError("CLUT contains more colors than the image format can address.")
    elif clut_size != 0 or clut_colors != 0 or clut_type != CLUT_NONE:
        raise ValueError("Direct-color TIM2 output unexpectedly contains a CLUT.")
    return {
        "valid": True,
        "version": version,
        "file_format": file_format,
        "picture_count": picture_count,
        "total_size": total_size,
        "image_size": image_size,
        "clut_size": clut_size,
        "header_size": header_size,
        "clut_colors": clut_colors,
        "picture_format": picture_format,
        "mipmap_count": mipmap_count,
        "clut_type": clut_type,
        "image_type": image_type,
        "image_format": image_format,
        "width": width,
        "height": height,
        "file_size": len(data),
    }


def verify_tim2(path: str | Path) -> dict[str, Any]:
    return verify_tim2_bytes(Path(path).read_bytes())


def run_self_test() -> None:
    """Run format tests against generated PNGs without touching user files."""
    _require_pillow()
    with tempfile.TemporaryDirectory(prefix="gta_studio_tim2_test_") as td:
        root = Path(td)
        # Include alpha and an odd width to check row-by-row nibble packing.
        img = Image.new("RGBA", (17, 9), (0, 0, 0, 0))
        for y in range(img.height):
            for x in range(img.width):
                img.putpixel((x, y), ((x * 13) % 256, (y * 23) % 256, ((x + y) * 9) % 256, 255 if (x + y) % 3 else 0))
        png = root / "synthetic.png"
        img.save(png)
        for fmt in ("4bit", "8bit", "16bit", "24bit", "32bit"):
            out = root / f"test_{fmt}.tm2"
            result = convert_png_to_tim2(png, out, image_format=fmt, alignment=16)
            checked = verify_tim2(out)
            assert checked["valid"] and checked["image_format"] == fmt
            assert checked["width"] == 17 and checked["height"] == 9
            assert out.stat().st_size == result["file_size"]
        # A second profile check for 128-byte blocks.
        out = root / "test_128.tm2"
        convert_png_to_tim2(png, out, image_format="32bit", alignment=128)
        assert verify_tim2(out)["valid"]
        # Template pass: dimensions/format are retained.
        ref = root / "template.tm2"
        convert_png_to_tim2(png, ref, image_format="4bit", alignment=16)
        diff_sized = Image.new("RGBA", (31, 11), (255, 0, 64, 160))
        different = root / "different.png"
        diff_sized.save(different)
        out = root / "templated.tm2"
        converted = convert_png_to_tim2(different, out, template_path=ref)
        assert converted["used_template"] and converted["width"] == 17 and converted["height"] == 9
        assert verify_tim2(out)["image_format"] == "4bit"


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Convert images to Sony TIM2 (.TM2) for GTA modding.")
    parser.add_argument("input", help="source PNG or supported image")
    parser.add_argument("output", help="output .tm2 path")
    parser.add_argument("--format", choices=tuple(FORMAT_TO_IMAGE_TYPE), default="4bit")
    parser.add_argument("--alignment", type=int, choices=(16, 128), default=16)
    parser.add_argument("--resize", choices=("Keep source dimensions", "Resize up to powers of 2", "Resize down to powers of 2"), default="Keep source dimensions")
    parser.add_argument("--dither", action="store_true")
    parser.add_argument("--template", help="optional original one-picture .tm2 template")
    parser.add_argument("--verify", action="store_true", help="verify output after conversion")
    parser.add_argument("--self-test", action="store_true", help="run converter self-tests")
    args = parser.parse_args()
    if args.self_test:
        run_self_test()
        print("TIM2 converter self-tests passed.")
    else:
        result = convert_png_to_tim2(args.input, args.output, args.format, args.alignment, args.resize, args.dither, args.template)
        print(f"Wrote {result['output']} ({result['width']}x{result['height']}, {result['format']}, {result['file_size']:,} bytes)")
        if args.verify:
            print(verify_tim2(args.output))
