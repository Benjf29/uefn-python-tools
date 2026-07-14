"""Pure Studio-lighting presets, scaling, and channel isolation helpers."""

from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable

from .models import (
    STUDIO_LIGHT_ROLES,
    LightingState,
    StudioLightState,
    StudioRigState,
)


BASE_INTENSITY_LUMENS = 6000.0
REFERENCE_RADIUS = 100.0
STUDIO_HIGHLIGHT_TRIGGER = 250.0
STUDIO_HIGHLIGHT_TARGET = 245.0
STUDIO_HIGHLIGHT_MIN_SCALE = 0.1


def disabled_capture_show_flags(studio: bool) -> tuple[str, ...]:
    """Return deterministic show flags for object/viewport capture."""
    disabled = [
        "Atmosphere",
        "Fog",
        "VolumetricFog",
        "Cloud",
        "MotionBlur",
        "EyeAdaptation",
    ]
    if studio:
        disabled.extend(("SkyLighting", "GlobalIllumination"))
    return tuple(disabled)


def studio_highlights_need_protection(stats: dict) -> bool:
    return bool(stats.get("samples")) and (
        float(stats.get("p95", 0.0)) >= STUDIO_HIGHLIGHT_TRIGGER
        or float(stats.get("white_fraction", 0.0)) >= 0.02
    )


def interpolate_studio_highlight_scale(
    base_scale: float,
    base_p95: float,
    low_scale: float,
    low_p95: float,
    target: float = STUDIO_HIGHLIGHT_TARGET,
) -> float:
    """Interpolate light energy logarithmically between two measured renders."""
    base_scale = max(1.0e-4, float(base_scale))
    low_scale = max(1.0e-4, min(base_scale, float(low_scale)))
    base_p95 = float(base_p95)
    low_p95 = float(low_p95)
    target = float(target)
    if base_p95 <= target:
        return base_scale
    if low_p95 >= target or base_p95 <= low_p95 + 1.0e-4:
        return low_scale
    amount = max(0.0, min(1.0, (target - low_p95) / (base_p95 - low_p95)))
    return math.exp(
        math.log(low_scale)
        + (math.log(base_scale) - math.log(low_scale)) * amount
    )


@dataclass(frozen=True)
class ResolvedStudioLight:
    role: str
    position: tuple[float, float, float]
    intensity: float
    temperature_kelvin: int
    size: float
    cast_shadows: bool


STUDIO_PRESETS = {
    "neutral": StudioRigState(),
    "soft": StudioRigState(
        key=StudioLightState(0.75, 0, 2.8, (2.8, -1.8, 2.2), True),
        fill=StudioLightState(0.50, 200, 3.2, (2.4, 2.0, 0.8), False),
        rim=StudioLightState(0.35, 500, 2.0, (-2.2, 0.3, 2.0), False),
    ),
    "dramatic": StudioRigState(
        key=StudioLightState(1.25, -1200, 0.8, (2.8, -1.8, 2.2), True),
        fill=StudioLightState(0.12, 0, 1.5, (2.4, 2.0, 0.8), False),
        rim=StudioLightState(0.90, 1800, 0.8, (-2.2, 0.3, 2.0), True),
    ),
    "flat": StudioRigState(
        key=StudioLightState(0.60, 0, 3.5, (2.8, -1.8, 2.2), False),
        fill=StudioLightState(0.60, 0, 3.5, (2.4, 2.0, 0.8), False),
        rim=StudioLightState(0.25, 0, 2.5, (-2.2, 0.3, 2.0), False),
    ),
}


def get_builtin_studio_rig(preset_id: str) -> StudioRigState:
    preset = STUDIO_PRESETS.get(str(preset_id or "").lower())
    if preset is None:
        preset = STUDIO_PRESETS["neutral"]
    return StudioRigState.from_dict(preset.to_dict())


class LightingIsolationError(RuntimeError):
    pass


def resolve_studio_lights(
    state: LightingState,
    bounds_radius: float,
    intensity_scale: float = 1.0,
) -> tuple[ResolvedStudioLight, ...]:
    """Resolve a stable preset into subject-scaled light parameters."""
    state = LightingState.from_dict(state.to_dict())
    rig = (
        StudioRigState.from_dict(state.rig.to_dict())
        if state.rig is not None
        else get_builtin_studio_rig(state.preset)
    )
    radius = max(1.0e-3, float(bounds_radius))
    base = (
        BASE_INTENSITY_LUMENS
        * (radius / REFERENCE_RADIUS) ** 2
        * state.intensity
        * max(0.0, float(intensity_scale))
    )
    resolved = []
    for role in STUDIO_LIGHT_ROLES:
        light = getattr(rig, role)
        resolved.append(
            ResolvedStudioLight(
                role=role,
                position=tuple(value * radius for value in light.position),
                intensity=base * light.intensity_multiplier,
                temperature_kelvin=max(
                    1000,
                    min(
                        15000,
                        state.temperature_kelvin + light.temperature_offset,
                    ),
                ),
                size=radius * light.size_multiplier,
                cast_shadows=light.cast_shadows,
            )
        )
    return tuple(resolved)


def _read_channels(component) -> tuple[bool, bool, bool]:
    channels = component.get_editor_property("lighting_channels")
    return tuple(
        bool(channels.get_editor_property("channel%d" % index))
        for index in range(3)
    )


@contextmanager
def isolate_lighting_channels(components: Iterable):
    """Move components to channel 2 and always restore their exact prior state."""
    snapshots = []
    seen = set()
    active_error = None
    try:
        for component in components:
            if component is None or id(component) in seen:
                continue
            seen.add(id(component))
            channels = _read_channels(component)
            snapshots.append((component, channels))
            component.set_lighting_channels(False, False, True)
        yield len(snapshots)
    except BaseException as exc:
        active_error = exc
        raise
    finally:
        restore_errors = []
        for component, channels in reversed(snapshots):
            try:
                component.set_lighting_channels(*channels)
            except Exception as exc:
                restore_errors.append(str(exc))
        if restore_errors:
            message = "Lighting Channel restoration failed: %s" % "; ".join(
                restore_errors
            )
            if active_error is not None and hasattr(active_error, "add_note"):
                active_error.add_note(message)
            elif active_error is None:
                raise LightingIsolationError(message)
