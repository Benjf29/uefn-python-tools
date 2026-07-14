from __future__ import annotations

import os
import re
from datetime import datetime


TOKENS = ("name", "parent", "index", "date", "preset", "size")


def safe_name(value: str, fallback: str = "Icon") -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "").strip("._")
    return value or fallback


def source_name(path: str) -> str:
    leaf = path.rsplit("/", 1)[-1].split(".", 1)[0]
    return safe_name(leaf)


def render_pattern(
    pattern: str,
    *,
    source_path: str,
    index: int,
    preset: str,
    size: int,
) -> str:
    normalized = source_path.replace("\\", "/").rstrip("/")
    name = source_name(normalized)
    parent = safe_name(normalized.rsplit("/", 2)[-2] if "/" in normalized else "")
    values = {
        "name": name,
        "parent": parent,
        "index": "%03d" % int(index),
        "date": datetime.now().strftime("%Y%m%d"),
        "preset": safe_name(preset, "Default"),
        "size": str(int(size)),
    }
    try:
        rendered = pattern.format(**values)
    except KeyError as exc:
        raise ValueError("Unknown naming token: %s" % exc) from exc
    return safe_name(rendered, name + "_icon")


def unique_png_path(directory: str, stem: str, reserved: set[str] | None = None) -> str:
    reserved = reserved if reserved is not None else set()
    base = os.path.abspath(os.path.join(directory, safe_name(stem) + ".png"))
    candidate = base
    suffix = 2
    while candidate.lower() in reserved:
        candidate = os.path.splitext(base)[0] + "_%d.png" % suffix
        suffix += 1
    reserved.add(candidate.lower())
    return candidate



