from __future__ import annotations

import json
import math
import os
import time
from contextlib import nullcontext
from dataclasses import dataclass

import unreal

from .image_ops import (
    alpha_bounds_in_frame,
    apply_adjustments,
    colors_to_preview_image,
    color_visible_alpha_bounds,
    pixels_to_image,
    sampled_luminance_stats,
    save_png,
    visible_alpha_bounds,
)
from .lighting import (
    STUDIO_HIGHLIGHT_MIN_SCALE,
    disabled_capture_show_flags,
    interpolate_studio_highlight_scale,
    isolate_lighting_channels,
    resolve_studio_lights,
    studio_highlights_need_protection,
)
from .models import (
    CameraState,
    CaptureRequest,
    CaptureResult,
    CaptureSource,
    LightingMode,
    SourceKind,
)
from .visual_bounds import (
    Bounds3D,
    bounds_from_center_extent,
    combine_projection_intervals,
    inflate_bounds,
    next_probe_frame,
    pixel_bounds_touch_edge,
    projection_intervals,
)


TEMP_Z = -500000.0
MAX_CAPTURE_SIZE = 4096
TEMP_PREFIX = "__ThumbnailCreator_"
STUDIO_EXPOSURE_BIAS = -4.5
VISUAL_BOUNDS_PROBE_SIZE = 256
VISUAL_BOUNDS_MAX_PASSES = 3
VISUAL_BOUNDS_INITIAL_RADIUS = 100.0


class CaptureError(RuntimeError):
    pass


@dataclass
class CapturedFrame:
    image: object
    result: CaptureResult


class CaptureSession:
    """Persistent editor capture stage. All owned objects are transient and reusable."""

    def __init__(self):
        self.actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        self.level_sub = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        self.world = unreal.EditorLevelLibrary.get_editor_world()
        if self.actor_sub is None or self.world is None:
            raise CaptureError("No editable UEFN world is open.")
        try:
            self.previous_selection = list(self.actor_sub.get_selected_level_actors())
        except Exception:
            self.previous_selection = []
        self.owned_source_actors: list = []
        self.source_actors: list = []
        self.capture_actor = None
        self.studio_lights: list = []
        self.render_target = None
        self.render_target_size = 0
        self.bounds_render_target = None
        self.source_key = ""
        self._prepared_source_signature = None
        self._actor_state_signature = None
        self.bounds_center = unreal.Vector(0.0, 0.0, TEMP_Z)
        self.bounds_extent = unreal.Vector(57.735, 57.735, 57.735)
        self.bounds_radius = 100.0
        self.coarse_bounds_center = self.bounds_center
        self.coarse_bounds_extent = self.bounds_extent
        self.coarse_bounds_radius = self.bounds_radius
        self._visual_bounds_dirty = True
        self._visual_bounds_method = "actor_fallback"
        self._visual_bounds_fallback = "not_probed"
        self._visual_bounds_probe_count = 0
        self._studio_highlight_scale_cache = {}
        self.viewport_override = None
        self.use_viewport_camera = False
        self.closed = False
        self._source_changed = False

    def _spawn_actor(self, actor_class, location=None, rotation=None):
        actor = self.actor_sub.spawn_actor_from_class(
            actor_class,
            location or unreal.Vector(0.0, 0.0, TEMP_Z),
            rotation or unreal.Rotator(),
            True,
        )
        if actor is None:
            raise CaptureError("UEFN could not spawn %s." % actor_class.get_name())
        return actor

    def _destroy_owned_sources(self):
        for actor in reversed(self.owned_source_actors):
            try:
                if actor is not None:
                    self.actor_sub.destroy_actor(actor)
            except Exception as exc:
                unreal.log_warning("[ThumbnailCreator] Source cleanup failed: %s" % exc)
        self.owned_source_actors = []
        self.source_actors = []

    def _find_actor(self, path: str):
        for actor in self.actor_sub.get_all_level_actors():
            if actor.get_path_name() == path or actor.get_name() == path or actor.get_actor_label() == path:
                return actor
        return None

    def _load_asset(self, path: str):
        asset = unreal.EditorAssetLibrary.load_asset(path)
        if asset is None:
            raise CaptureError("Asset not found: %s" % path)
        return asset

    @staticmethod
    def _vector_tuple(vector) -> tuple[float, float, float]:
        return float(vector.x), float(vector.y), float(vector.z)

    @staticmethod
    def _source_signature(source: CaptureSource):
        return source.key, round(float(source.niagara_time), 6)

    def _current_actor_state_signature(self):
        signature = []
        for actor in self.source_actors:
            try:
                location = actor.get_actor_location()
                rotation = actor.get_actor_rotation()
                scale = actor.get_actor_scale3d()
                center, extent = actor.get_actor_bounds(False, True)
                values = (
                    float(location.x), float(location.y), float(location.z),
                    float(rotation.pitch), float(rotation.yaw), float(rotation.roll),
                    float(scale.x), float(scale.y), float(scale.z),
                    float(center.x), float(center.y), float(center.z),
                    float(extent.x), float(extent.y), float(extent.z),
                )
                signature.append(
                    (actor.get_path_name(),) + tuple(round(value, 4) for value in values)
                )
            except Exception as exc:
                signature.append((str(actor), "unavailable", str(exc)))
        return tuple(signature)

    def _invalidate_visual_bounds(self, reason: str):
        self._visual_bounds_dirty = True
        self._visual_bounds_method = "actor_fallback"
        self._visual_bounds_fallback = str(reason or "invalidated")
        self._visual_bounds_probe_count = 0
        self.bounds_center = self.coarse_bounds_center
        self.bounds_extent = self.coarse_bounds_extent
        self.bounds_radius = self.coarse_bounds_radius

    def prepare_source(self, source: CaptureSource):
        if self.closed:
            raise CaptureError("The capture session is closed.")
        source_signature = self._source_signature(source)
        if source.key == self.source_key and self.source_actors:
            if source_signature == self._prepared_source_signature:
                actor_signature = self._current_actor_state_signature()
                if actor_signature != self._actor_state_signature:
                    self._update_bounds()
                    self._actor_state_signature = self._current_actor_state_signature()
                return
        self._destroy_owned_sources()
        self.source_key = source.key
        self._prepared_source_signature = source_signature
        self._source_changed = True
        stage = unreal.Vector(0.0, 0.0, TEMP_Z)

        if source.kind == SourceKind.WHOLE_VIEW:
            self.source_actors = []
            self._actor_state_signature = None
            self._visual_bounds_dirty = False
            self._visual_bounds_method = "viewport"
            self._visual_bounds_fallback = ""
            self.read_viewport_camera()
            return
        if not source.paths:
            raise CaptureError("The capture source is empty.")

        if source.kind == SourceKind.STATIC_MESH:
            asset = self._load_asset(source.paths[0])
            if not isinstance(asset, unreal.StaticMesh):
                raise CaptureError("The source is not a StaticMesh.")
            actor = self.actor_sub.spawn_actor_from_object(asset, stage, unreal.Rotator(), True)
            if actor is None:
                raise CaptureError("UEFN could not spawn the StaticMesh.")
            actor.set_actor_label(TEMP_PREFIX + "StaticMesh", False)
            self.source_actors = self.owned_source_actors = [actor]

        elif source.kind == SourceKind.SKELETAL_MESH:
            asset = self._load_asset(source.paths[0])
            if not isinstance(asset, unreal.SkeletalMesh):
                raise CaptureError("The source is not a SkeletalMesh.")
            actor = self._spawn_actor(unreal.SkeletalMeshActor, stage)
            actor.set_actor_label(TEMP_PREFIX + "SkeletalMesh", False)
            component = actor.skeletal_mesh_component
            component.set_skeletal_mesh_asset(asset)
            component.set_editor_property("pause_anims", True)
            self.source_actors = self.owned_source_actors = [actor]

        elif source.kind == SourceKind.BLUEPRINT:
            asset = self._load_asset(source.paths[0])
            class_name = asset.get_class().get_name()
            if class_name == "EntityPrefab":
                raise CaptureError("Standalone EntityPrefab assets cannot be instantiated by UEFN Python.")
            generated = asset.generated_class() if hasattr(asset, "generated_class") else asset
            try:
                actor = self._spawn_actor(generated, stage)
            except Exception as exc:
                raise CaptureError("The Blueprint/Class is not spawnable: %s" % exc) from exc
            actor.set_actor_label(TEMP_PREFIX + "Blueprint", False)
            self.source_actors = self.owned_source_actors = [actor]

        elif source.kind == SourceKind.ACTORS:
            actors = [self._find_actor(path) for path in source.paths]
            self.source_actors = [actor for actor in actors if actor is not None]
            if not self.source_actors:
                raise CaptureError("None of the selected actors still exists in the level.")

        elif source.kind == SourceKind.NIAGARA:
            asset = self._load_asset(source.paths[0])
            if not isinstance(asset, unreal.NiagaraSystem):
                raise CaptureError("The source is not a NiagaraSystem.")
            actor = self._spawn_actor(unreal.NiagaraActor, stage)
            actor.set_actor_label(TEMP_PREFIX + "Niagara", False)
            component = actor.get_component_by_class(unreal.NiagaraComponent)
            if component is None:
                raise CaptureError("The temporary NiagaraActor has no NiagaraComponent.")
            component.set_asset(asset, True)
            component.set_paused(False)
            component.activate(True)
            component.reinitialize_system()
            if source.niagara_time > 0.0:
                component.advance_simulation_by_time(float(source.niagara_time), 1.0 / 30.0)
            component.set_paused(True)
            self.source_actors = self.owned_source_actors = [actor]
        else:
            raise CaptureError("Unsupported source type: %s" % source.kind.value)

        self._update_bounds()
        self._actor_state_signature = self._current_actor_state_signature()

    def _update_bounds(self):
        if not self.source_actors:
            return
        min_x = min_y = min_z = float("inf")
        max_x = max_y = max_z = float("-inf")
        valid = 0
        for actor in self.source_actors:
            try:
                center, extent = actor.get_actor_bounds(False, True)
                values = (
                    float(center.x), float(center.y), float(center.z),
                    float(extent.x), float(extent.y), float(extent.z),
                )
                if not all(math.isfinite(value) for value in values):
                    continue
                if any(value < 0.0 for value in values[3:]):
                    continue
                min_x = min(min_x, float(center.x - extent.x))
                min_y = min(min_y, float(center.y - extent.y))
                min_z = min(min_z, float(center.z - extent.z))
                max_x = max(max_x, float(center.x + extent.x))
                max_y = max(max_y, float(center.y + extent.y))
                max_z = max(max_z, float(center.z + extent.z))
                valid += 1
            except Exception:
                pass
        fallback_reason = "source_bounds_changed"
        if valid:
            center = unreal.Vector(
                (min_x + max_x) * 0.5,
                (min_y + max_y) * 0.5,
                (min_z + max_z) * 0.5,
            )
            extent = unreal.Vector(
                (max_x - min_x) * 0.5,
                (max_y - min_y) * 0.5,
                (max_z - min_z) * 0.5,
            )
            radius = math.sqrt(
                float(extent.x) ** 2
                + float(extent.y) ** 2
                + float(extent.z) ** 2
            )
        else:
            radius = 0.0
        if radius <= 1.0e-4 or not math.isfinite(radius):
            locations = []
            for actor in self.source_actors:
                try:
                    locations.append(actor.get_actor_location())
                except Exception:
                    pass
            if not locations:
                raise CaptureError("The source has no usable bounds or location.")
            count = float(len(locations))
            center = unreal.Vector(
                sum(float(value.x) for value in locations) / count,
                sum(float(value.y) for value in locations) / count,
                sum(float(value.z) for value in locations) / count,
            )
            extent = unreal.Vector(
                VISUAL_BOUNDS_INITIAL_RADIUS,
                VISUAL_BOUNDS_INITIAL_RADIUS,
                VISUAL_BOUNDS_INITIAL_RADIUS,
            )
            radius = math.sqrt(3.0) * VISUAL_BOUNDS_INITIAL_RADIUS
            fallback_reason = "actor_bounds_invalid"
        self.coarse_bounds_center = center
        self.coarse_bounds_extent = extent
        self.coarse_bounds_radius = radius
        self._invalidate_visual_bounds(fallback_reason)
        self._source_changed = True

    def _ensure_capture_actor(self):
        if self.capture_actor is not None:
            return
        self.capture_actor = self._spawn_actor(unreal.SceneCapture2D)
        self.capture_actor.set_actor_label(TEMP_PREFIX + "Capture", False)
        component = self.capture_actor.capture_component2d
        component.capture_every_frame = False
        component.capture_on_movement = False
        component.always_persist_rendering_state = True
        component.capture_source = unreal.SceneCaptureSource.SCS_SCENE_COLOR_HDR
        self._set_capture_show_flags(False)

    def _set_capture_show_flags(self, studio: bool):
        if self.capture_actor is None:
            return
        self.capture_actor.capture_component2d.show_flag_settings = [
            unreal.EngineShowFlagsSetting(show_flag_name=name, enabled=False)
            for name in disabled_capture_show_flags(studio)
        ]

    def _configure_capture_exposure(self, studio: bool):
        component = self.capture_actor.capture_component2d
        settings = component.get_editor_property("post_process_settings")
        for override in (
            "override_auto_exposure_method",
            "override_auto_exposure_bias",
            "override_auto_exposure_apply_physical_camera_exposure",
        ):
            settings.set_editor_property(override, bool(studio))
        if studio:
            settings.set_editor_property(
                "auto_exposure_method", unreal.AutoExposureMethod.AEM_MANUAL
            )
            settings.set_editor_property(
                "auto_exposure_bias", STUDIO_EXPOSURE_BIAS
            )
            settings.set_editor_property(
                "auto_exposure_apply_physical_camera_exposure", False
            )
        component.set_editor_property("post_process_settings", settings)
        component.set_editor_property("post_process_blend_weight", 1.0)

    def _source_primitive_components(self):
        components = []
        seen = set()
        for actor in self.source_actors:
            try:
                actor_components = actor.get_components_by_class(unreal.PrimitiveComponent)
            except Exception:
                actor_components = []
            for component in actor_components:
                if component is None or id(component) in seen:
                    continue
                seen.add(id(component))
                if hasattr(component, "set_lighting_channels"):
                    components.append(component)
        if not components:
            raise CaptureError("The source has no primitive components for Studio lighting.")
        return components

    def _ensure_studio_lights(self):
        if len(self.studio_lights) == 3 and all(self.studio_lights):
            return
        self._destroy_studio_lights()
        created = []
        try:
            for role in ("Key", "Fill", "Rim"):
                actor = self._spawn_actor(unreal.RectLight)
                actor.set_actor_label(TEMP_PREFIX + "Light_" + role, False)
                component = actor.rect_light_component
                component.set_mobility(unreal.ComponentMobility.MOVABLE)
                component.set_editor_property(
                    "intensity_units", unreal.LightUnits.LUMENS
                )
                component.set_lighting_channels(False, False, True)
                component.set_indirect_lighting_intensity(0.0)
                component.set_affect_translucent_lighting(True)
                component.set_use_temperature(True)
                component.set_visibility(False, True)
                created.append(actor)
            self.studio_lights = created
        except Exception as exc:
            for actor in reversed(created):
                try:
                    self.actor_sub.destroy_actor(actor)
                except Exception:
                    pass
            self.studio_lights = []
            raise CaptureError("Studio lighting could not be created: %s" % exc) from exc

    def _destroy_studio_lights(self):
        for actor in reversed(self.studio_lights):
            try:
                if actor is not None:
                    self.actor_sub.destroy_actor(actor)
            except Exception as exc:
                unreal.log_warning("[ThumbnailCreator] Studio light cleanup failed: %s" % exc)
        self.studio_lights = []

    def _set_studio_lights_visible(self, visible: bool):
        errors = []
        for actor in self.studio_lights:
            try:
                actor.rect_light_component.set_visibility(bool(visible), True)
            except Exception as exc:
                errors.append(str(exc))
        if errors and visible:
            raise CaptureError(
                "Studio lights could not be activated: %s" % "; ".join(errors)
            )
        if errors:
            unreal.log_warning(
                "[ThumbnailCreator] Studio light hide failed: %s"
                % "; ".join(errors)
            )

    def _studio_highlight_cache_key(self, state, camera):
        return (
            self._prepared_source_signature,
            tuple(round(value, 3) for value in self._vector_tuple(self.bounds_center)),
            round(float(self.bounds_radius), 3),
            (
                round(float(camera.yaw), 1),
                round(float(camera.pitch), 1),
                round(float(camera.roll), 1),
                round(float(camera.dolly), 2),
                round(float(camera.fov), 1),
            ),
            json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":")),
        )

    def _remember_studio_highlight_scale(self, key, scale: float):
        if key not in self._studio_highlight_scale_cache:
            while len(self._studio_highlight_scale_cache) >= 32:
                self._studio_highlight_scale_cache.pop(
                    next(iter(self._studio_highlight_scale_cache))
                )
        self._studio_highlight_scale_cache[key] = max(
            STUDIO_HIGHLIGHT_MIN_SCALE, min(1.0, float(scale))
        )

    def _configure_studio_lights(
        self,
        state,
        camera_rotation,
        intensity_scale: float = 1.0,
    ):
        self._ensure_studio_lights()
        resolved = resolve_studio_lights(
            state,
            self.bounds_radius,
            intensity_scale=intensity_scale,
        )
        forward = unreal.MathLibrary.get_forward_vector(camera_rotation)
        toward_camera = forward * -1.0
        right = unreal.MathLibrary.get_right_vector(camera_rotation)
        up = unreal.MathLibrary.get_up_vector(camera_rotation)
        attenuation = max(1000.0, float(self.bounds_radius) * 10.0)
        try:
            for actor, spec in zip(self.studio_lights, resolved):
                toward, lateral, vertical = spec.position
                position = (
                    self.bounds_center
                    + toward_camera * toward
                    + right * lateral
                    + up * vertical
                )
                rotation = unreal.MathLibrary.find_look_at_rotation(
                    position, self.bounds_center
                )
                actor.set_actor_location(position, False, False)
                actor.set_actor_rotation(rotation, False)
                component = actor.rect_light_component
                component.set_intensity(float(spec.intensity))
                component.set_temperature(float(spec.temperature_kelvin))
                component.set_source_width(float(spec.size))
                component.set_source_height(float(spec.size))
                component.set_attenuation_radius(attenuation)
                component.set_cast_shadows(bool(spec.cast_shadows))
        except Exception as exc:
            raise CaptureError("Studio lighting configuration failed: %s" % exc) from exc
        return resolved

    def _ensure_render_target(self, size: int):
        if self.render_target is not None and self.render_target_size == size:
            return
        if self.render_target is not None:
            try:
                self.capture_actor.capture_component2d.texture_target = None
                unreal.RenderingLibrary.release_render_target2d(self.render_target)
            except Exception:
                pass
        self.render_target = unreal.RenderingLibrary.create_render_target2d(
            self.world,
            size,
            size,
            unreal.TextureRenderTargetFormat.RTF_RGBA16F,
            unreal.LinearColor(0.0, 0.0, 0.0, 1.0),
            False,
            False,
        )
        if self.render_target is None:
            raise CaptureError("UEFN could not allocate the HDR Render Target.")
        self.render_target_size = size

    def _ensure_bounds_render_target(self):
        if self.bounds_render_target is not None:
            return
        self.bounds_render_target = unreal.RenderingLibrary.create_render_target2d(
            self.world,
            VISUAL_BOUNDS_PROBE_SIZE,
            VISUAL_BOUNDS_PROBE_SIZE,
            unreal.TextureRenderTargetFormat.RTF_RGBA16F,
            unreal.LinearColor(0.0, 0.0, 0.0, 1.0),
            False,
            False,
        )
        if self.bounds_render_target is None:
            raise CaptureError("UEFN could not allocate the visual-bounds Render Target.")

    def _restore_current_show_only(self, component, render_mode):
        component.primitive_render_mode = render_mode
        component.clear_show_only_components()
        if render_mode == unreal.SceneCapturePrimitiveRenderMode.PRM_USE_SHOW_ONLY_LIST:
            for actor in self.source_actors:
                component.show_only_actor_components(actor, True)

    def _probe_visual_bounds_triplet(
        self,
        center: tuple[float, float, float],
        ortho_width: float,
    ):
        self._ensure_bounds_render_target()
        component = self.capture_actor.capture_component2d
        saved = {
            "texture_target": component.texture_target,
            "capture_source": component.capture_source,
            "projection_type": component.projection_type,
            "ortho_width": float(component.ortho_width),
            "fov_angle": float(component.fov_angle),
            "primitive_render_mode": component.primitive_render_mode,
            "camera_cut_this_frame": bool(component.camera_cut_this_frame),
            "location": self.capture_actor.get_actor_location(),
            "rotation": self.capture_actor.get_actor_rotation(),
        }
        projection_samples = []
        edge_touched = False
        captures = 0
        probe_center = unreal.Vector(*center)
        probe_distance = max(
            100.0,
            float(ortho_width) * 2.0,
            float(self.coarse_bounds_radius) * 2.0,
        )
        try:
            component.texture_target = self.bounds_render_target
            component.capture_source = unreal.SceneCaptureSource.SCS_SCENE_COLOR_HDR
            component.projection_type = unreal.CameraProjectionMode.ORTHOGRAPHIC
            component.ortho_width = float(ortho_width)
            component.primitive_render_mode = (
                unreal.SceneCapturePrimitiveRenderMode.PRM_USE_SHOW_ONLY_LIST
            )
            component.clear_show_only_components()
            for actor in self.source_actors:
                component.show_only_actor_components(actor, True)
            for axis in (
                unreal.Vector(1.0, 0.0, 0.0),
                unreal.Vector(0.0, 1.0, 0.0),
                unreal.Vector(0.0, 0.0, 1.0),
            ):
                location = probe_center + axis * probe_distance
                rotation = unreal.MathLibrary.find_look_at_rotation(
                    location, probe_center
                )
                self.capture_actor.set_actor_location(location, False, False)
                self.capture_actor.set_actor_rotation(rotation, False)
                component.camera_cut_this_frame = True
                component.capture_scene()
                captures += 1
                pixels = unreal.RenderingLibrary.read_render_target_raw(
                    self.world, self.bounds_render_target, False
                )
                pixel_bounds = visible_alpha_bounds(
                    pixels, VISUAL_BOUNDS_PROBE_SIZE
                )
                if pixel_bounds is None:
                    continue
                edge_touched = edge_touched or pixel_bounds_touch_edge(
                    pixel_bounds, VISUAL_BOUNDS_PROBE_SIZE
                )
                right = unreal.MathLibrary.get_right_vector(rotation)
                up = unreal.MathLibrary.get_up_vector(rotation)
                forward = unreal.MathLibrary.get_forward_vector(rotation)
                projection_samples.append(
                    projection_intervals(
                        pixel_bounds,
                        VISUAL_BOUNDS_PROBE_SIZE,
                        ortho_width,
                        center,
                        self._vector_tuple(right),
                        self._vector_tuple(up),
                        self._vector_tuple(forward),
                    )
                )
            return (
                combine_projection_intervals(projection_samples),
                edge_touched,
                captures,
                len(projection_samples),
            )
        finally:
            component.texture_target = saved["texture_target"]
            component.capture_source = saved["capture_source"]
            component.projection_type = saved["projection_type"]
            component.ortho_width = saved["ortho_width"]
            component.fov_angle = saved["fov_angle"]
            component.camera_cut_this_frame = saved["camera_cut_this_frame"]
            self.capture_actor.set_actor_location(saved["location"], False, False)
            self.capture_actor.set_actor_rotation(saved["rotation"], False)
            self._restore_current_show_only(
                component, saved["primitive_render_mode"]
            )

    def _use_coarse_bounds(self, reason: str, probe_count: int):
        self.bounds_center = self.coarse_bounds_center
        self.bounds_extent = self.coarse_bounds_extent
        self.bounds_radius = self.coarse_bounds_radius
        self._visual_bounds_method = "actor_fallback"
        self._visual_bounds_fallback = str(reason)
        self._visual_bounds_probe_count = int(probe_count)
        self._visual_bounds_dirty = False
        unreal.log_warning(
            "[ThumbnailCreator] Visual bounds fallback (%s)." % reason
        )

    def _ensure_visual_bounds(self):
        if not self._visual_bounds_dirty or not self.source_actors:
            return
        coarse = bounds_from_center_extent(
            self._vector_tuple(self.coarse_bounds_center),
            self._vector_tuple(self.coarse_bounds_extent),
        )
        center = coarse.center
        ortho_width = max(1.0, coarse.maximum_span * 1.10)
        candidate = None
        edge_touched = False
        probe_count = 0
        valid_views = 0
        final_width = ortho_width
        try:
            for pass_index in range(VISUAL_BOUNDS_MAX_PASSES):
                candidate, edge_touched, captures, valid_views = (
                    self._probe_visual_bounds_triplet(center, ortho_width)
                )
                probe_count += captures
                final_width = ortho_width
                if candidate is None:
                    self._use_coarse_bounds(
                        "insufficient_silhouette_views_%d" % valid_views,
                        probe_count,
                    )
                    return
                next_frame = next_probe_frame(
                    center,
                    ortho_width,
                    candidate,
                    edge_touched=edge_touched,
                    pass_index=pass_index,
                    maximum_passes=VISUAL_BOUNDS_MAX_PASSES,
                )
                if next_frame is None:
                    break
                center, ortho_width = next_frame
            if candidate is None:
                self._use_coarse_bounds("no_silhouette", probe_count)
                return
            if edge_touched:
                self._use_coarse_bounds(
                    "silhouette_touches_probe_edge", probe_count
                )
                return
            visual = inflate_bounds(
                candidate,
                absolute_padding=final_width / VISUAL_BOUNDS_PROBE_SIZE,
                fractional_padding=0.02,
            )
            if visual.radius <= 1.0e-4 or not math.isfinite(visual.radius):
                self._use_coarse_bounds("invalid_visual_bounds", probe_count)
                return
            self.bounds_center = unreal.Vector(*visual.center)
            self.bounds_extent = unreal.Vector(*visual.extent)
            self.bounds_radius = visual.radius
            self._visual_bounds_method = "orthographic_alpha"
            self._visual_bounds_fallback = ""
            self._visual_bounds_probe_count = probe_count
            self._visual_bounds_dirty = False
            unreal.log(
                "[ThumbnailCreator] Visual bounds resolved in %d probes "
                "(radius %.3f -> %.3f)."
                % (probe_count, self.coarse_bounds_radius, self.bounds_radius)
            )
        except Exception as exc:
            self._use_coarse_bounds("probe_error: %s" % exc, probe_count)

    def _camera_transform(self, camera: CameraState, source_kind: SourceKind, use_override: bool = True):
        if use_override and self.viewport_override and (source_kind == SourceKind.WHOLE_VIEW or self.use_viewport_camera):
            return self.viewport_override
        yaw = math.radians(float(camera.yaw))
        pitch = math.radians(float(camera.pitch))
        direction = unreal.Vector(
            math.cos(pitch) * math.cos(yaw),
            math.cos(pitch) * math.sin(yaw),
            math.sin(pitch),
        )
        half_fov = math.radians(float(camera.fov) * 0.5)
        distance = self.bounds_radius / max(0.02, math.sin(half_fov))
        distance *= float(camera.framing_margin) * max(0.02, float(camera.dolly))
        rough_location = self.bounds_center + direction * distance
        rough_rotation = unreal.MathLibrary.find_look_at_rotation(rough_location, self.bounds_center)
        right = unreal.MathLibrary.get_right_vector(rough_rotation)
        up = unreal.MathLibrary.get_up_vector(rough_rotation)
        target = self.bounds_center + right * (float(camera.pan_x) * self.bounds_radius) + up * (float(camera.pan_y) * self.bounds_radius)
        location = target + direction * distance
        rotation = unreal.MathLibrary.find_look_at_rotation(location, target)
        rotation.roll = float(camera.roll)
        return location, rotation, float(camera.fov)

    def _bounds_metadata(self):
        if self._visual_bounds_method == "viewport":
            return {
                "bounds_method": "viewport",
                "coarse_bounds": None,
                "visual_bounds": None,
                "visual_bounds_probe_count": 0,
                "visual_bounds_fallback": "",
            }

        def payload(center, extent, radius):
            return {
                "center": list(self._vector_tuple(center)),
                "extent": list(self._vector_tuple(extent)),
                "radius": float(radius),
            }

        return {
            "bounds_method": self._visual_bounds_method,
            "coarse_bounds": payload(
                self.coarse_bounds_center,
                self.coarse_bounds_extent,
                self.coarse_bounds_radius,
            ),
            "visual_bounds": payload(
                self.bounds_center,
                self.bounds_extent,
                self.bounds_radius,
            ),
            "visual_bounds_probe_count": self._visual_bounds_probe_count,
            "visual_bounds_fallback": self._visual_bounds_fallback,
        }

    def read_viewport_camera(self):
        if self.level_sub is None:
            return None
        try:
            key = self.level_sub.get_active_viewport_config_key()
            location, rotation = self.level_sub.get_level_viewport_camera_info(key)
            fov = float(self.level_sub.get_level_viewport_fov(key))
            self.viewport_override = (location, rotation, fov)
            return location, rotation, fov
        except Exception:
            return None

    def push_camera_to_viewport(self, camera: CameraState, source_kind: SourceKind):
        if self.level_sub is None:
            return False
        location, rotation, fov = self._camera_transform(camera, source_kind, use_override=False)
        key = self.level_sub.get_active_viewport_config_key()
        self.level_sub.set_level_viewport_camera_info(location, rotation, key)
        self.level_sub.set_level_viewport_fov(float(fov), key)
        self.viewport_override = (location, rotation, float(fov))
        return True

    def capture(self, request: CaptureRequest, png_path: str | None = None) -> CapturedFrame:
        started = time.perf_counter()
        self.prepare_source(request.source)
        output_size = int(request.export.output_size)
        supersample = int(request.export.supersample)
        capture_size = output_size * supersample
        if output_size < 32 or supersample < 1 or capture_size > MAX_CAPTURE_SIZE:
            raise CaptureError("Invalid output/supersampling size (%d px render)." % capture_size)
        if not 5.0 <= float(request.camera.fov) <= 120.0:
            raise CaptureError("FOV must be between 5 and 120 degrees.")

        studio = (
            request.source.kind != SourceKind.WHOLE_VIEW
            and request.lighting.mode == LightingMode.STUDIO
        )
        context = (
            isolate_lighting_channels(self._source_primitive_components())
            if studio
            else nullcontext(0)
        )
        try:
            with context:
                return self._capture_prepared(
                    request,
                    png_path,
                    started,
                    output_size,
                    supersample,
                    capture_size,
                    studio,
                )
        except CaptureError:
            raise
        except Exception as exc:
            if studio:
                raise CaptureError("Studio capture failed: %s" % exc) from exc
            raise

    def _capture_prepared(
        self,
        request,
        png_path,
        started,
        output_size,
        supersample,
        capture_size,
        studio,
    ):
        self._ensure_capture_actor()
        try:
            self._set_capture_show_flags(studio)
        except Exception as exc:
            if studio:
                raise CaptureError(
                    "Studio show-flag configuration failed: %s" % exc
                ) from exc
            raise
        try:
            self._configure_capture_exposure(studio)
        except Exception as exc:
            if studio:
                raise CaptureError(
                    "Studio exposure configuration failed: %s" % exc
                ) from exc
            raise
        if request.source.kind != SourceKind.WHOLE_VIEW:
            self._ensure_visual_bounds()
        self._ensure_render_target(capture_size)
        component = self.capture_actor.capture_component2d
        component.texture_target = self.render_target
        component.capture_source = unreal.SceneCaptureSource.SCS_SCENE_COLOR_HDR
        component.fov_angle = float(request.camera.fov)
        if request.source.kind == SourceKind.WHOLE_VIEW:
            self.read_viewport_camera()
            component.primitive_render_mode = unreal.SceneCapturePrimitiveRenderMode.PRM_RENDER_SCENE_PRIMITIVES
            component.clear_show_only_components()
        else:
            component.primitive_render_mode = unreal.SceneCapturePrimitiveRenderMode.PRM_USE_SHOW_ONLY_LIST
            component.clear_show_only_components()
            for actor in self.source_actors:
                component.show_only_actor_components(actor, True)

        location, rotation, fov = self._camera_transform(request.camera, request.source.kind)
        self.capture_actor.set_actor_location(location, False, False)
        self.capture_actor.set_actor_rotation(rotation, False)
        component.fov_angle = float(fov)
        fast_preview = bool(
            request.preview and request.preview_fast and supersample == 1
        )
        resolved_studio_lights = ()
        studio_highlight_cache_key = None
        studio_highlight_scale = 1.0
        studio_highlight_cache_hit = False
        studio_highlight_metadata = None
        if studio:
            studio_highlight_cache_key = self._studio_highlight_cache_key(
                request.lighting, request.camera
            )
            studio_highlight_cache_hit = (
                studio_highlight_cache_key in self._studio_highlight_scale_cache
            )
            studio_highlight_scale = self._studio_highlight_scale_cache.get(
                studio_highlight_cache_key, 1.0
            )
        try:
            if studio:
                resolved_studio_lights = self._configure_studio_lights(
                    request.lighting,
                    rotation,
                    intensity_scale=studio_highlight_scale,
                )
                self._set_studio_lights_visible(True)
            component.camera_cut_this_frame = True
            component.capture_scene()
            # Studio uses a second tone-curved color pass. SceneColor remains
            # the alpha source; fast previews read both buffers as 8-bit colors,
            # while refined previews and exports retain the raw HDR path.
            if fast_preview:
                pixels = unreal.RenderingLibrary.read_render_target(self.world, self.render_target, False)
                bounds = color_visible_alpha_bounds(pixels, capture_size)
            else:
                pixels = unreal.RenderingLibrary.read_render_target_raw(self.world, self.render_target, False)
                bounds = visible_alpha_bounds(pixels, capture_size)
            if request.source.kind != SourceKind.WHOLE_VIEW and bounds is None:
                raise CaptureError("The source produced no visible opacity. Check materials and bounds.")

            auto_fitted = False
            if (
                request.source.kind != SourceKind.WHOLE_VIEW
                and request.camera.auto_fit
                and bounds
                and (self._source_changed or not alpha_bounds_in_frame(bounds, capture_size))
            ):
                min_x, min_y, max_x, max_y = bounds
                span = max(max_x - min_x + 1, max_y - min_y + 1)
                target_span = capture_size / max(1.0, float(request.camera.framing_margin))
                scale = max(0.02, min(20.0, span / target_span))
                image_center = (capture_size - 1) * 0.5
                normalized_x = (((min_x + max_x) * 0.5) - image_center) / (capture_size * 0.5)
                normalized_y = (((min_y + max_y) * 0.5) - image_center) / (capture_size * 0.5)
                delta = location - self.bounds_center
                current_distance = math.sqrt(float(delta.x) ** 2 + float(delta.y) ** 2 + float(delta.z) ** 2)
                view_half_width = current_distance * math.tan(math.radians(float(fov) * 0.5))
                request.camera.pan_x += normalized_x * view_half_width / self.bounds_radius
                request.camera.pan_y -= normalized_y * view_half_width / self.bounds_radius
                request.camera.dolly = max(0.02, min(20.0, request.camera.dolly * scale))
                location, rotation, fov = self._camera_transform(request.camera, request.source.kind)
                self.capture_actor.set_actor_location(location, False, False)
                self.capture_actor.set_actor_rotation(rotation, False)
                if studio:
                    studio_highlight_cache_key = self._studio_highlight_cache_key(
                        request.lighting, request.camera
                    )
                    studio_highlight_cache_hit = (
                        studio_highlight_cache_key
                        in self._studio_highlight_scale_cache
                    )
                    studio_highlight_scale = (
                        self._studio_highlight_scale_cache.get(
                            studio_highlight_cache_key, 1.0
                        )
                    )
                    resolved_studio_lights = self._configure_studio_lights(
                        request.lighting,
                        rotation,
                        intensity_scale=studio_highlight_scale,
                    )
                component.camera_cut_this_frame = True
                component.capture_scene()
                if fast_preview:
                    pixels = unreal.RenderingLibrary.read_render_target(self.world, self.render_target, False)
                    bounds = color_visible_alpha_bounds(pixels, capture_size)
                else:
                    pixels = unreal.RenderingLibrary.read_render_target_raw(self.world, self.render_target, False)
                    bounds = visible_alpha_bounds(pixels, capture_size)
                auto_fitted = True

            color_pixels = None
            if studio:
                try:
                    component.capture_source = (
                        unreal.SceneCaptureSource.SCS_FINAL_TONE_CURVE_HDR
                    )
                    read_color = (
                        unreal.RenderingLibrary.read_render_target
                        if fast_preview
                        else unreal.RenderingLibrary.read_render_target_raw
                    )

                    def capture_tone_color():
                        component.camera_cut_this_frame = True
                        component.capture_scene()
                        return read_color(self.world, self.render_target, False)

                    color_pixels = capture_tone_color()
                    initial_highlights = sampled_luminance_stats(
                        pixels,
                        color_pixels,
                        raw=not fast_preview,
                    )
                    final_highlights = initial_highlights
                    pending = False
                    calibrated = False
                    if studio_highlights_need_protection(initial_highlights):
                        if fast_preview:
                            pending = True
                        elif (
                            not fast_preview
                            and studio_highlight_scale
                            > STUDIO_HIGHLIGHT_MIN_SCALE + 1.0e-4
                        ):
                            low_scale = STUDIO_HIGHLIGHT_MIN_SCALE
                            low_lights = self._configure_studio_lights(
                                request.lighting,
                                rotation,
                                intensity_scale=low_scale,
                            )
                            low_color_pixels = capture_tone_color()
                            low_highlights = sampled_luminance_stats(
                                pixels,
                                low_color_pixels,
                                raw=True,
                            )
                            chosen_scale = interpolate_studio_highlight_scale(
                                studio_highlight_scale,
                                initial_highlights["p95"],
                                low_scale,
                                low_highlights["p95"],
                            )
                            if abs(chosen_scale - low_scale) <= 0.01:
                                studio_highlight_scale = low_scale
                                resolved_studio_lights = low_lights
                                color_pixels = low_color_pixels
                                final_highlights = low_highlights
                            else:
                                studio_highlight_scale = chosen_scale
                                resolved_studio_lights = (
                                    self._configure_studio_lights(
                                        request.lighting,
                                        rotation,
                                        intensity_scale=chosen_scale,
                                    )
                                )
                                color_pixels = capture_tone_color()
                                final_highlights = sampled_luminance_stats(
                                    pixels,
                                    color_pixels,
                                    raw=True,
                                )
                                if (
                                    studio_highlights_need_protection(
                                        final_highlights
                                    )
                                    and not studio_highlights_need_protection(
                                        low_highlights
                                    )
                                ):
                                    studio_highlight_scale = low_scale
                                    resolved_studio_lights = low_lights
                                    color_pixels = low_color_pixels
                                    final_highlights = low_highlights
                            calibrated = True
                        elif not fast_preview:
                            calibrated = True
                    if studio_highlight_cache_key is not None and not pending:
                        self._remember_studio_highlight_scale(
                            studio_highlight_cache_key,
                            studio_highlight_scale,
                        )
                    studio_highlight_metadata = {
                        "scale": round(float(studio_highlight_scale), 6),
                        "cache_hit": bool(studio_highlight_cache_hit),
                        "calibrated": bool(calibrated),
                        "pending_refined": bool(pending),
                        "initial": initial_highlights,
                        "final": final_highlights,
                    }
                except Exception as exc:
                    raise CaptureError(
                        "Studio tone-curved color capture failed: %s" % exc
                    ) from exc
                finally:
                    component.capture_source = (
                        unreal.SceneCaptureSource.SCS_SCENE_COLOR_HDR
                    )

            if fast_preview:
                image, stats = colors_to_preview_image(
                    pixels,
                    output_size,
                    color_pixels=color_pixels,
                )
            else:
                image, stats = pixels_to_image(
                    pixels,
                    output_size,
                    supersample,
                    color_pixels=color_pixels,
                )
            image = apply_adjustments(image, request.adjust)
            if png_path:
                save_png(
                    image,
                    png_path,
                    transparent=request.export.transparent,
                    background_color=request.export.background_color,
                )
            self._source_changed = False
            effective_lighting = request.lighting.to_dict()
            effective_lighting["mode"] = (
                LightingMode.STUDIO.value if studio else LightingMode.WORLD.value
            )
            effective_lighting["effective_preset"] = (
                request.lighting.preset if studio else LightingMode.WORLD.value
            )
            if studio:
                effective_lighting["capture_exposure_bias"] = (
                    STUDIO_EXPOSURE_BIAS
                )
                effective_lighting["highlight_scale"] = round(
                    float(studio_highlight_scale), 6
                )
            result = CaptureResult(
                success=True,
                source_key=request.source.key,
                png_path=os.path.abspath(png_path) if png_path else "",
                output_size=output_size,
                capture_size=capture_size,
                elapsed_seconds=time.perf_counter() - started,
                alpha_nonzero=stats["alpha_nonzero"],
                alpha_fractional=stats["alpha_fractional"],
                metadata={
                    **self._bounds_metadata(),
                    "alpha_bounds": bounds,
                    "camera_location": str(location),
                    "camera_rotation": str(rotation),
                    "auto_fitted": auto_fitted,
                    "lighting": effective_lighting,
                    "studio_highlights": studio_highlight_metadata,
                    "resolved_studio_lights": [
                        {
                            "role": spec.role,
                            "position_relative": list(spec.position),
                            "intensity_lumens": spec.intensity,
                            "temperature_kelvin": spec.temperature_kelvin,
                            "size": spec.size,
                            "cast_shadows": spec.cast_shadows,
                        }
                        for spec in resolved_studio_lights
                    ],
                    "runtime_contract": "thumbnail_creator_lighting_v3",
                    "fitted_camera": {
                        "yaw": request.camera.yaw,
                        "pitch": request.camera.pitch,
                        "roll": request.camera.roll,
                        "pan_x": request.camera.pan_x,
                        "pan_y": request.camera.pan_y,
                        "dolly": request.camera.dolly,
                        "fov": request.camera.fov,
                        "framing_margin": request.camera.framing_margin,
                        "auto_fit": request.camera.auto_fit,
                    },
                },
            )
            return CapturedFrame(image=image, result=result)
        finally:
            if studio:
                self._set_studio_lights_visible(False)

    def frame_source(self, camera: CameraState):
        camera.pan_x = 0.0
        camera.pan_y = 0.0
        camera.dolly = 1.0
        camera.auto_fit = True
        self._invalidate_visual_bounds("frame_requested")
        self._source_changed = True

    def cleanup(self):
        if self.closed:
            return
        if self.capture_actor is not None:
            try:
                self.capture_actor.capture_component2d.texture_target = None
            except Exception:
                pass
        if self.render_target is not None:
            try:
                unreal.RenderingLibrary.release_render_target2d(self.render_target)
            except Exception:
                pass
        self.render_target = None
        if self.bounds_render_target is not None:
            try:
                unreal.RenderingLibrary.release_render_target2d(
                    self.bounds_render_target
                )
            except Exception:
                pass
        self.bounds_render_target = None
        self._destroy_studio_lights()
        self._destroy_owned_sources()
        if self.capture_actor is not None:
            try:
                self.actor_sub.destroy_actor(self.capture_actor)
            except Exception as exc:
                unreal.log_warning("[ThumbnailCreator] Capture cleanup failed: %s" % exc)
        self.capture_actor = None
        try:
            valid = [actor for actor in self.previous_selection if actor is not None]
            self.actor_sub.set_selected_level_actors(valid)
        except Exception as exc:
            unreal.log_warning("[ThumbnailCreator] Could not restore actor selection: %s" % exc)
        self.closed = True
