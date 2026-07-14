"""Project-agnostic Unreal content-path helpers.

UEFN project assets usually live below ``/{ProjectName}/`` rather than
``/Game/``.  Detection stays here so capture and UI code never need a
project-specific mount point.
"""

from __future__ import annotations

import os
import re


class ContentPathError(ValueError):
    pass


_INTERNAL_ROOTS = {"engine", "fortnitegame", "memory", "script", "temp", "transient"}


def normalize_content_path(value: str) -> str:
    """Normalize an Unreal package folder and reject object/traversal paths."""
    value = str(value or "").strip().replace("\\", "/")
    if not value.startswith("/"):
        raise ContentPathError("Content paths must start with '/'.")
    parts = [part for part in value.split("/") if part]
    if not parts:
        raise ContentPathError("A content root is required.")
    if any(part in {".", ".."} for part in parts):
        raise ContentPathError("Content paths cannot contain '.' or '..'.")
    if any("." in part or ":" in part or "'" in part or '"' in part for part in parts):
        raise ContentPathError("Use a package folder, not an asset/object path.")
    return "/" + "/".join(parts)


def content_root_from_reference(value: str) -> str | None:
    """Extract the first non-engine mount point from an Unreal object reference."""
    candidates = re.findall(r"/([^/'\".:\s]+)(?=/|$)", str(value or ""))
    for candidate in candidates:
        if candidate.lower() not in _INTERNAL_ROOTS:
            return "/" + candidate
    return None


def detect_project_content_root(unreal_module=None) -> str:
    """Detect the active project mount point, falling back to standard UE ``/Game``."""
    if unreal_module is None:
        try:
            import unreal as unreal_module
        except ImportError:
            return "/Game"

    try:
        world = unreal_module.EditorLevelLibrary.get_editor_world()
        outer = world.get_outermost() if world is not None else None
        for obj in (outer, world):
            if obj is None:
                continue
            for getter_name in ("get_path_name", "get_name"):
                getter = getattr(obj, getter_name, None)
                if getter:
                    root = content_root_from_reference(getter())
                    if root:
                        return root
    except Exception:
        pass

    try:
        for asset in unreal_module.EditorUtilityLibrary.get_selected_assets():
            root = content_root_from_reference(asset.get_path_name())
            if root:
                return root
    except Exception:
        pass

    try:
        project_file = str(unreal_module.Paths.get_project_file_path())
        project_name = os.path.splitext(os.path.basename(project_file))[0]
        if project_name and project_name.lower() not in _INTERNAL_ROOTS:
            return "/" + project_name
    except Exception:
        pass
    return "/Game"


def default_import_path(unreal_module=None) -> str:
    return detect_project_content_root(unreal_module) + "/Textures/GeneratedThumbnails"


def validate_destination_path(destination: str, content_root: str) -> str:
    destination = normalize_content_path(destination)
    content_root = normalize_content_path(content_root)
    if destination != content_root and not destination.startswith(content_root + "/"):
        raise ContentPathError(
            "Texture destination must be under the active project root %s/." % content_root
        )
    return destination
