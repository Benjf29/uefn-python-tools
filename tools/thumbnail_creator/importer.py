from __future__ import annotations

import os

import unreal

from .naming import safe_name
from .project_paths import (
    ContentPathError,
    detect_project_content_root,
    validate_destination_path,
)


class ImportError(RuntimeError):
    pass


def _set_texture_ui_properties(texture) -> None:
    settings = (
        ("lod_group", unreal.TextureGroup.TEXTUREGROUP_UI),
        ("srgb", True),
        ("compression_settings", unreal.TextureCompressionSettings.TC_DEFAULT),
        ("mip_gen_settings", unreal.TextureMipGenSettings.TMGS_FROM_TEXTURE_GROUP),
    )
    for name, value in settings:
        try:
            texture.set_editor_property(name, value)
        except Exception as exc:
            unreal.log_warning("[ThumbnailCreator] Texture property %s: %s" % (name, exc))


def import_or_reimport_texture(
    png_path: str,
    destination_path: str,
    asset_name: str | None = None,
) -> dict:
    """Import with replace_existing so the existing package/object keeps its references."""
    png_path = os.path.abspath(png_path)
    if not os.path.isfile(png_path):
        raise ImportError("PNG is inaccessible: %s" % png_path)
    content_root = detect_project_content_root(unreal)
    try:
        destination_path = validate_destination_path(destination_path, content_root)
    except ContentPathError as exc:
        raise ImportError(str(exc)) from exc
    asset_name = safe_name(asset_name or os.path.splitext(os.path.basename(png_path))[0])
    object_path = "%s/%s" % (destination_path, asset_name)
    asset_lib = unreal.EditorAssetLibrary
    asset_lib.make_directory(destination_path)
    before = asset_lib.load_asset(object_path)

    task = unreal.AssetImportTask()
    task.set_editor_property("automated", True)
    task.set_editor_property("filename", png_path)
    task.set_editor_property("destination_path", destination_path)
    task.set_editor_property("destination_name", asset_name)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("replace_existing_settings", False)
    task.set_editor_property("save", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

    texture = asset_lib.load_asset(object_path)
    if texture is None or not isinstance(texture, unreal.Texture2D):
        imported = [str(path) for path in task.get_objects()] if hasattr(task, "get_objects") else []
        raise ImportError("Texture import failed for %s (objects: %s)." % (object_path, imported))
    _set_texture_ui_properties(texture)
    texture.modify()
    asset_lib.save_loaded_asset(texture, False)
    after_path = texture.get_path_name()
    return {
        "texture_path": after_path,
        "asset_path": object_path,
        "reimported": before is not None,
        "same_object": bool(before is None or before == texture),
        "source_png": png_path,
        "content_root": content_root,
        "lod_group": str(texture.get_editor_property("lod_group")),
        "srgb": bool(texture.get_editor_property("srgb")),
        "compression": str(texture.get_editor_property("compression_settings")),
        "mips": str(texture.get_editor_property("mip_gen_settings")),
    }


