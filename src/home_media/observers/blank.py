"""Blank / DRM-protected frame heuristics for Apple TV screenshots.

A near-black frame is treated as unobservable — never as proof that a
menu state failed. Classification must not invent UI evidence from blanks.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

PNG_SIG = b"\x89PNG\r\n\x1a\n"
# Mean luminance at or below this (0–255) → blank_or_protected.
DEFAULT_BLANK_MEAN = 8.0
# Sample every Nth pixel for speed on large captures.
DEFAULT_STRIDE = 4


@dataclass(frozen=True)
class BlankAssessment:
    blank_or_protected: bool
    mean_luminance: float | None
    width: int | None
    height: int | None
    reason: str


def is_png(data: bytes) -> bool:
    return len(data) >= 8 and data[:8] == PNG_SIG


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    if not is_png(data) or len(data) < 24:
        return None
    # IHDR is the first chunk: length(4) + type(4) + data(13) …
    length = struct.unpack(">I", data[8:12])[0]
    if data[12:16] != b"IHDR" or length < 8:
        return None
    width, height = struct.unpack(">II", data[16:24])
    if width <= 0 or height <= 0:
        return None
    return width, height


def _iter_png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    if not is_png(data):
        return []
    chunks: list[tuple[bytes, bytes]] = []
    offset = 8
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        ctype = data[offset + 4 : offset + 8]
        start = offset + 8
        end = start + length
        if end + 4 > len(data):
            break
        chunks.append((ctype, data[start:end]))
        offset = end + 4  # skip CRC
        if ctype == b"IEND":
            break
    return chunks


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _defilter_scanline(
    filter_type: int,
    row: bytes,
    prev: bytes,
    bpp: int,
) -> bytes:
    out = bytearray(len(row))
    for i, value in enumerate(row):
        left = out[i - bpp] if i >= bpp else 0
        up = prev[i] if prev else 0
        up_left = prev[i - bpp] if prev and i >= bpp else 0
        if filter_type == 0:
            out[i] = value
        elif filter_type == 1:
            out[i] = (value + left) & 0xFF
        elif filter_type == 2:
            out[i] = (value + up) & 0xFF
        elif filter_type == 3:
            out[i] = (value + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:
            out[i] = (value + _paeth(left, up, up_left)) & 0xFF
        else:
            raise ValueError(f"unsupported PNG filter {filter_type}")
    return bytes(out)


def _decode_rgba_png(data: bytes) -> tuple[int, int, bytes] | None:
    """Decode 8-bit RGB/RGBA PNG into packed RGBA bytes. Returns None if unsupported."""
    chunks = _iter_png_chunks(data)
    if not chunks or chunks[0][0] != b"IHDR":
        return None
    ihdr = chunks[0][1]
    if len(ihdr) < 13:
        return None
    width, height, bit_depth, color_type, compression, filt, interlace = struct.unpack(
        ">IIBBBBB", ihdr[:13]
    )
    if (
        bit_depth != 8
        or compression != 0
        or filt != 0
        or interlace != 0
        or color_type not in {2, 6}
    ):
        return None
    idat = b"".join(chunk for ctype, chunk in chunks if ctype == b"IDAT")
    if not idat:
        return None
    try:
        raw = zlib.decompress(idat)
    except zlib.error:
        return None
    channels = 3 if color_type == 2 else 4
    bpp = channels
    stride = width * bpp
    expected = height * (1 + stride)
    if len(raw) < expected or width <= 0 or height <= 0:
        return None
    rows: list[bytes] = []
    prev = b"\x00" * stride
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        row = raw[offset : offset + stride]
        offset += stride
        decoded = _defilter_scanline(filter_type, row, prev, bpp)
        prev = decoded
        if channels == 3:
            rgba = bytearray(width * 4)
            for i in range(width):
                base = i * 3
                out = i * 4
                rgba[out : out + 3] = decoded[base : base + 3]
                rgba[out + 3] = 255
            rows.append(bytes(rgba))
        else:
            rows.append(decoded)
    return width, height, b"".join(rows)


def assess_blank_or_protected(
    png_bytes: bytes | None,
    *,
    mean_threshold: float = DEFAULT_BLANK_MEAN,
    stride: int = DEFAULT_STRIDE,
) -> BlankAssessment:
    if not png_bytes:
        return BlankAssessment(
            blank_or_protected=True,
            mean_luminance=None,
            width=None,
            height=None,
            reason="empty_image",
        )
    if not is_png(png_bytes):
        return BlankAssessment(
            blank_or_protected=True,
            mean_luminance=None,
            width=None,
            height=None,
            reason="malformed_or_non_png",
        )
    dims = png_dimensions(png_bytes)
    decoded = _decode_rgba_png(png_bytes)
    if decoded is None:
        pillow = _assess_with_pillow(png_bytes, mean_threshold=mean_threshold, stride=stride)
        if pillow is not None:
            return pillow
        # Valid PNG signature but unsupported encoding — treat as unobservable.
        w, h = dims if dims else (None, None)
        return BlankAssessment(
            blank_or_protected=True,
            mean_luminance=None,
            width=w,
            height=h,
            reason="undecodable_png_treat_as_unobservable",
        )
    width, height, rgba = decoded
    if stride < 1:
        stride = 1
    total = 0.0
    count = 0
    pixel_count = width * height
    for idx in range(0, pixel_count, stride):
        base = idx * 4
        r, g, b = rgba[base], rgba[base + 1], rgba[base + 2]
        # Rec. 601 luminance
        total += 0.299 * r + 0.587 * g + 0.114 * b
        count += 1
    if count == 0:
        return BlankAssessment(
            blank_or_protected=True,
            mean_luminance=None,
            width=width,
            height=height,
            reason="zero_pixels",
        )
    mean = total / count
    blank = mean <= mean_threshold
    return BlankAssessment(
        blank_or_protected=blank,
        mean_luminance=mean,
        width=width,
        height=height,
        reason="near_black_frame" if blank else "visible_content",
    )


def _assess_with_pillow(
    png_bytes: bytes,
    *,
    mean_threshold: float,
    stride: int,
) -> BlankAssessment | None:
    try:
        from io import BytesIO

        from PIL import Image
    except Exception:  # noqa: BLE001 — optional dependency
        return None
    try:
        with Image.open(BytesIO(png_bytes)) as img:
            width, height = img.size
            gray = img.convert("L")
            if hasattr(gray, "get_flattened_data"):
                raw_seq = list(gray.get_flattened_data())
            else:
                raw_seq = list(gray.getdata())
            data: list[int] = []
            for v in raw_seq:
                if isinstance(v, (int, float)):
                    data.append(int(v))
                elif isinstance(v, tuple) and v:
                    data.append(int(v[0]))
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return BlankAssessment(
            blank_or_protected=True,
            mean_luminance=None,
            width=width,
            height=height,
            reason="zero_pixels",
        )
    step = max(1, stride)
    sample = data[::step]
    mean = float(sum(sample)) / float(len(sample))
    blank = mean <= mean_threshold
    return BlankAssessment(
        blank_or_protected=blank,
        mean_luminance=mean,
        width=width,
        height=height,
        reason="near_black_frame" if blank else "visible_content",
    )


def synthesize_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Write a minimal 8-bit RGB PNG (filter 0) for tests/fixtures."""
    r, g, b = rgb
    raw = bytearray()
    row = bytes([r, g, b]) * width
    for _ in range(height):
        raw.append(0)  # filter None
        raw.extend(row)
    compressed = zlib.compress(bytes(raw), level=9)

    def chunk(ctype: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + ctype
            + payload
            + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return PNG_SIG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
