from __future__ import annotations

from typing import Iterable

import unreal

from .models import CaptureSource, SourceKind


SUPPORTED_CLASS_NAMES = {
    "StaticMesh": SourceKind.STATIC_MESH,
    "SkeletalMesh": SourceKind.SKELETAL_MESH,
    "Blueprint": SourceKind.BLUEPRINT,
    "BlueprintGeneratedClass": SourceKind.BLUEPRINT,
    "NiagaraSystem": SourceKind.NIAGARA,
}


def source_from_asset(asset) -> CaptureSource | None:
    if asset is None:
        return None
    class_name = asset.get_class().get_name()
    kind = SUPPORTED_CLASS_NAMES.get(class_name)
    if kind is None:
        if isinstance(asset, unreal.StaticMesh):
            kind = SourceKind.STATIC_MESH
        elif isinstance(asset, unreal.SkeletalMesh):
            kind = SourceKind.SKELETAL_MESH
        elif isinstance(asset, unreal.NiagaraSystem):
            kind = SourceKind.NIAGARA
        elif hasattr(asset, "generated_class"):
            kind = SourceKind.BLUEPRINT
    if kind is None:
        return None
    return CaptureSource(kind=kind, paths=[asset.get_path_name()], display_name=asset.get_name())


def selected_asset_sources() -> list[CaptureSource]:
    sources = []
    for asset in unreal.EditorUtilityLibrary.get_selected_assets():
        source = source_from_asset(asset)
        if source:
            sources.append(source)
    return sources


def selected_actor_source() -> CaptureSource | None:
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = list(actor_sub.get_selected_level_actors()) if actor_sub else []
    if not actors:
        return None
    return CaptureSource(
        kind=SourceKind.ACTORS,
        paths=[actor.get_path_name() for actor in actors],
        display_name="%d selected actors" % len(actors),
    )


def selected_folder_sources(recursive: bool = True) -> tuple[list[CaptureSource], list[str]]:
    folders = []
    getter = getattr(unreal.EditorUtilityLibrary, "get_selected_folder_paths", None)
    if getter:
        try:
            folders = [str(path) for path in getter()]
        except Exception:
            folders = []
    if not folders:
        getter = getattr(unreal.EditorUtilityLibrary, "get_selected_path_view_folder_paths", None)
        if getter:
            try:
                folders = [str(path) for path in getter()]
            except Exception:
                folders = []

    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    sources: list[CaptureSource] = []
    ignored: list[str] = []
    seen: set[str] = set()
    for folder in folders:
        for data in registry.get_assets_by_path(folder, recursive=recursive):
            package = str(data.package_name)
            if package in seen:
                continue
            seen.add(package)
            asset = unreal.EditorAssetLibrary.load_asset(package)
            source = source_from_asset(asset)
            if source:
                sources.append(source)
            else:
                ignored.append("%s (%s)" % (package, str(data.asset_class_path.asset_name)))
    return sources, ignored


def supported_sources_from_assets(assets: Iterable) -> tuple[list[CaptureSource], list[str]]:
    sources = []
    ignored = []
    for asset in assets:
        source = source_from_asset(asset)
        if source:
            sources.append(source)
        elif asset is not None:
            ignored.append("%s (%s)" % (asset.get_path_name(), asset.get_class().get_name()))
    return sources, ignored



