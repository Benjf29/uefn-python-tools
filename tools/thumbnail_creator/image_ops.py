from __future__ import annotations

import math
import os
from dataclasses import replace
from typing import Any

from .models import AdjustState
from .pillow_support import activate_vendor_path

activate_vendor_path()
from PIL import Image, ImageChops, ImageEnhance, ImageFilter  # noqa: E402


class ImageError(RuntimeError):
    pass


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _linear_to_srgb(value: float) -> float:
    value = _clamp01(value)
    if value <= 0.0031308:
        return 12.92 * value
    return 1.055 * math.pow(value, 1.0 / 2.4) - 0.055


def pixels_to_image(
    pixels: Any,
    output_size: int,
    supersample: int,
    color_pixels: Any | None = None,
) -> tuple[Image.Image, dict]:
    """Convert UEFN HDR pixels into a straight-alpha Pillow image.

    ``pixels`` always supplies inverse opacity. Studio captures may supply a
    separate tone-curved RGB buffer because Unreal's final-color capture
    sources do not preserve alpha.
    """
    output_size = int(output_size)
    supersample = int(supersample)
    capture_size = output_size * supersample
    expected = capture_size * capture_size
    if pixels is None or len(pixels) != expected:
        actual = 0 if pixels is None else len(pixels)
        raise ImageError("Render Target returned %d pixels; expected %d." % (actual, expected))
    if color_pixels is not None and len(color_pixels) != expected:
        raise ImageError(
            "Tone-curved Render Target returned %d pixels; expected %d."
            % (len(color_pixels), expected)
        )

    rgba = bytearray(output_size * output_size * 4)
    sample_count = supersample * supersample
    alpha_nonzero = 0
    alpha_fractional = 0

    for y in range(output_size):
        source_y = y * supersample
        for x in range(output_size):
            source_x = x * supersample
            red = green = blue = alpha_sum = 0.0
            for sy in range(supersample):
                row = (source_y + sy) * capture_size + source_x
                for sx in range(supersample):
                    pixel = pixels[row + sx]
                    color_pixel = (
                        color_pixels[row + sx]
                        if color_pixels is not None
                        else pixel
                    )
                    alpha = _clamp01(1.0 - float(pixel.a))
                    alpha_sum += alpha
                    red += max(0.0, float(color_pixel.r))
                    green += max(0.0, float(color_pixel.g))
                    blue += max(0.0, float(color_pixel.b))

            alpha = alpha_sum / sample_count
            if alpha > 1.0e-6:
                red = (red / sample_count) / alpha
                green = (green / sample_count) / alpha
                blue = (blue / sample_count) / alpha
            else:
                red = green = blue = 0.0

            index = (y * output_size + x) * 4
            rgba[index] = int(_linear_to_srgb(red) * 255.0 + 0.5)
            rgba[index + 1] = int(_linear_to_srgb(green) * 255.0 + 0.5)
            rgba[index + 2] = int(_linear_to_srgb(blue) * 255.0 + 0.5)
            alpha_byte = int(alpha * 255.0 + 0.5)
            rgba[index + 3] = alpha_byte
            if alpha_byte:
                alpha_nonzero += 1
            if 0 < alpha_byte < 255:
                alpha_fractional += 1

    return Image.frombytes("RGBA", (output_size, output_size), bytes(rgba)), {
        "alpha_nonzero": alpha_nonzero,
        "alpha_fractional": alpha_fractional,
    }


def visible_alpha_bounds(pixels: Any, capture_size: int, threshold: float = 0.01):
    min_x = min_y = capture_size
    max_x = max_y = -1
    for index, pixel in enumerate(pixels or []):
        if 1.0 - float(pixel.a) <= threshold:
            continue
        x = index % capture_size
        y = index // capture_size
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x)
        max_y = max(max_y, y)
    if max_x < min_x or max_y < min_y:
        return None
    return min_x, min_y, max_x, max_y


def color_visible_alpha_bounds(pixels: Any, capture_size: int, threshold: int = 2):
    min_x = min_y = capture_size
    max_x = max_y = -1
    for index, pixel in enumerate(pixels or []):
        if 255 - int(pixel.a) <= threshold:
            continue
        x = index % capture_size
        y = index // capture_size
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x)
        max_y = max(max_y, y)
    if max_x < min_x or max_y < min_y:
        return None
    return min_x, min_y, max_x, max_y


def sampled_luminance_stats(
    alpha_pixels: Any,
    color_pixels: Any,
    *,
    raw: bool,
    max_samples: int = 65536,
) -> dict[str, float | int]:
    """Measure visible tone-curved RGB without scanning a full-size export."""
    if alpha_pixels is None or color_pixels is None:
        raise ImageError("Highlight analysis requires alpha and color pixels.")
    if len(alpha_pixels) != len(color_pixels):
        raise ImageError("Highlight alpha/color buffers have different sizes.")
    count = len(alpha_pixels)
    if not count:
        return {
            "samples": 0,
            "p50": 0.0,
            "p90": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "white_fraction": 0.0,
        }
    step = max(1, int(math.ceil(count / max(1, int(max_samples)))))
    luminance = []
    for index in range(0, count, step):
        alpha_pixel = alpha_pixels[index]
        color_pixel = color_pixels[index]
        if raw:
            alpha = _clamp01(1.0 - float(alpha_pixel.a))
            if alpha <= 0.01:
                continue
            red = _linear_to_srgb(max(0.0, float(color_pixel.r)) / alpha) * 255.0
            green = _linear_to_srgb(max(0.0, float(color_pixel.g)) / alpha) * 255.0
            blue = _linear_to_srgb(max(0.0, float(color_pixel.b)) / alpha) * 255.0
        else:
            if 255 - int(alpha_pixel.a) <= 2:
                continue
            red = float(color_pixel.r)
            green = float(color_pixel.g)
            blue = float(color_pixel.b)
        luminance.append(0.2126 * red + 0.7152 * green + 0.0722 * blue)
    if not luminance:
        return {
            "samples": 0,
            "p50": 0.0,
            "p90": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "white_fraction": 0.0,
        }
    luminance.sort()

    def percentile(fraction: float) -> float:
        index = int((len(luminance) - 1) * fraction)
        return round(float(luminance[index]), 3)

    return {
        "samples": len(luminance),
        "p50": percentile(0.50),
        "p90": percentile(0.90),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "white_fraction": round(
            sum(value >= 250.0 for value in luminance) / len(luminance), 6
        ),
    }


def colors_to_preview_image(
    pixels: Any,
    size: int,
    color_pixels: Any | None = None,
) -> tuple[Image.Image, dict]:
    """Fast 8-bit conversion used only by the dirty-only interactive preview."""
    expected = int(size) * int(size)
    if pixels is None or len(pixels) != expected:
        raise ImageError("Preview read returned an invalid pixel count.")
    if color_pixels is not None and len(color_pixels) != expected:
        raise ImageError("Preview color read returned an invalid pixel count.")
    rgba = bytearray(expected * 4)
    nonzero = fractional = 0
    for index, pixel in enumerate(pixels):
        color_pixel = color_pixels[index] if color_pixels is not None else pixel
        target = index * 4
        alpha = 255 - int(pixel.a)
        rgba[target] = int(color_pixel.r)
        rgba[target + 1] = int(color_pixel.g)
        rgba[target + 2] = int(color_pixel.b)
        rgba[target + 3] = alpha
        if alpha:
            nonzero += 1
        if 0 < alpha < 255:
            fractional += 1
    return Image.frombytes("RGBA", (int(size), int(size)), bytes(rgba)), {
        "alpha_nonzero": nonzero,
        "alpha_fractional": fractional,
    }


def scaled_preview_adjustments(
    state: AdjustState,
    preview_size: int,
    output_size: int,
) -> AdjustState:
    """Return preview-only adjustments with pixel effects scaled to export size."""
    width = max(0, int(state.outline_width))
    if not width:
        return replace(state, outline_width=0)
    scale = max(1, int(preview_size)) / max(1, int(output_size))
    return replace(state, outline_width=max(1, int(round(width * scale))))


def alpha_bounds_in_frame(bounds, capture_size: int, padding: float = 0.01) -> bool:
    if not bounds:
        return False
    inset = max(1, int(capture_size * padding))
    min_x, min_y, max_x, max_y = bounds
    return min_x >= inset and min_y >= inset and max_x < capture_size - inset and max_y < capture_size - inset


def apply_adjustments(image: Image.Image, state: AdjustState) -> Image.Image:
    image = image.convert("RGBA")
    alpha = image.getchannel("A")
    rgb = image.convert("RGB")

    if abs(float(state.hue)) > 0.001:
        hsv = rgb.convert("HSV")
        h, s, v = hsv.split()
        shift = int((float(state.hue) % 360.0) * 255.0 / 360.0)
        h = h.point(lambda value: (value + shift) % 256)
        rgb = Image.merge("HSV", (h, s, v)).convert("RGB")
    if abs(float(state.saturation) - 1.0) > 0.001:
        rgb = ImageEnhance.Color(rgb).enhance(max(0.0, float(state.saturation)))
    brightness = max(0.0, float(state.brightness)) * math.pow(2.0, float(state.exposure))
    if abs(brightness - 1.0) > 0.001:
        rgb = ImageEnhance.Brightness(rgb).enhance(brightness)
    if abs(float(state.contrast) - 1.0) > 0.001:
        rgb = ImageEnhance.Contrast(rgb).enhance(max(0.0, float(state.contrast)))

    adjusted = Image.merge("RGBA", (*rgb.split(), alpha))
    width = max(0, int(state.outline_width))
    if width:
        kernel = width * 2 + 1
        expanded = alpha.filter(ImageFilter.MaxFilter(kernel))
        ring = ImageChops.subtract(expanded, alpha)
        color = tuple(max(0, min(255, int(v))) for v in state.outline_color)
        ring = ring.point(lambda value: int(value * color[3] / 255.0))
        outline = Image.new("RGBA", image.size, color[:3] + (0,))
        outline.putalpha(ring)
        adjusted = Image.alpha_composite(outline, adjusted)
    return adjusted


def composite_background(image: Image.Image, color: tuple[int, int, int, int]) -> Image.Image:
    background = Image.new("RGBA", image.size, tuple(int(v) for v in color))
    return Image.alpha_composite(background, image)


def save_png(
    image: Image.Image,
    path: str,
    *,
    transparent: bool = True,
    background_color: tuple[int, int, int, int] = (32, 32, 32, 255),
) -> str:
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    output = image if transparent else composite_background(image, background_color)
    output.save(path, "PNG", optimize=False, compress_level=6)
    if not os.path.isfile(path):
        raise ImageError("Pillow returned without creating the PNG.")
    return path
