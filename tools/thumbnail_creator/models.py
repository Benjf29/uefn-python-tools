from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class SourceKind(str, Enum):
    STATIC_MESH = "static_mesh"
    SKELETAL_MESH = "skeletal_mesh"
    BLUEPRINT = "blueprint"
    ACTORS = "actors"
    NIAGARA = "niagara"
    WHOLE_VIEW = "whole_view"


class LightingMode(str, Enum):
    WORLD = "world"
    STUDIO = "studio"


LIGHTING_PRESET_IDS = ("neutral", "soft", "dramatic", "flat")
STUDIO_LIGHT_ROLES = ("key", "fill", "rim")
CUSTOM_LIGHTING_PRESET_PREFIX = "custom:"


@dataclass
class CaptureSource:
    kind: SourceKind
    paths: list[str] = field(default_factory=list)
    display_name: str = ""
    niagara_time: float = 0.0

    @property
    def key(self) -> str:
        return "%s:%s" % (self.kind.value, "|".join(self.paths))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaptureSource":
        return cls(
            kind=SourceKind(data.get("kind", SourceKind.STATIC_MESH.value)),
            paths=list(data.get("paths") or []),
            display_name=str(data.get("display_name") or ""),
            niagara_time=float(data.get("niagara_time", 0.0)),
        )


@dataclass
class CameraState:
    yaw: float = 35.0
    pitch: float = 20.0
    roll: float = 0.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    dolly: float = 1.0
    fov: float = 35.0
    framing_margin: float = 1.18
    auto_fit: bool = True


@dataclass
class AdjustState:
    hue: float = 0.0
    saturation: float = 1.0
    brightness: float = 1.0
    contrast: float = 1.0
    exposure: float = 0.0
    outline_width: int = 0
    outline_color: tuple[int, int, int, int] = (0, 0, 0, 255)


def _clamped_float(value, default, minimum, maximum) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    return max(float(minimum), min(float(maximum), number))


@dataclass
class StudioLightState:
    intensity_multiplier: float = 1.0
    temperature_offset: int = 0
    size_multiplier: float = 1.0
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    cast_shadows: bool = True

    def __post_init__(self):
        self.intensity_multiplier = _clamped_float(
            self.intensity_multiplier, 1.0, 0.0, 3.0
        )
        try:
            offset = int(float(self.temperature_offset))
        except (TypeError, ValueError):
            offset = 0
        self.temperature_offset = max(-4000, min(4000, offset))
        self.size_multiplier = _clamped_float(
            self.size_multiplier, 1.0, 0.1, 5.0
        )
        values = self.position if isinstance(self.position, (list, tuple)) else ()
        if len(values) != 3:
            values = (0.0, 0.0, 0.0)
        self.position = tuple(
            _clamped_float(value, 0.0, -5.0, 5.0) for value in values
        )
        self.cast_shadows = bool(self.cast_shadows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intensity_multiplier": self.intensity_multiplier,
            "temperature_offset": self.temperature_offset,
            "size_multiplier": self.size_multiplier,
            "position": list(self.position),
            "cast_shadows": self.cast_shadows,
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any] | None,
        default: "StudioLightState | None" = None,
    ) -> "StudioLightState":
        source = dict(data) if isinstance(data, dict) else {}
        fallback = default or cls()
        return cls(
            intensity_multiplier=source.get(
                "intensity_multiplier", fallback.intensity_multiplier
            ),
            temperature_offset=source.get(
                "temperature_offset", fallback.temperature_offset
            ),
            size_multiplier=source.get(
                "size_multiplier", fallback.size_multiplier
            ),
            position=source.get("position", fallback.position),
            cast_shadows=source.get("cast_shadows", fallback.cast_shadows),
        )


def _neutral_key() -> StudioLightState:
    return StudioLightState(1.0, -400, 1.8, (2.8, -1.8, 2.2), True)


def _neutral_fill() -> StudioLightState:
    return StudioLightState(0.35, 200, 2.4, (2.4, 2.0, 0.8), False)


def _neutral_rim() -> StudioLightState:
    return StudioLightState(0.55, 1200, 1.2, (-2.2, 0.3, 2.0), True)


@dataclass
class StudioRigState:
    key: StudioLightState = field(default_factory=_neutral_key)
    fill: StudioLightState = field(default_factory=_neutral_fill)
    rim: StudioLightState = field(default_factory=_neutral_rim)

    def __post_init__(self):
        defaults = (_neutral_key(), _neutral_fill(), _neutral_rim())
        for role, default in zip(STUDIO_LIGHT_ROLES, defaults):
            value = getattr(self, role)
            if not isinstance(value, StudioLightState):
                value = StudioLightState.from_dict(value, default)
            setattr(self, role, value)

    def to_dict(self) -> dict[str, Any]:
        return {
            role: getattr(self, role).to_dict()
            for role in STUDIO_LIGHT_ROLES
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "StudioRigState":
        source = dict(data) if isinstance(data, dict) else {}
        defaults = cls()
        return cls(
            **{
                role: StudioLightState.from_dict(
                    source.get(role), getattr(defaults, role)
                )
                for role in STUDIO_LIGHT_ROLES
            }
        )


@dataclass
class LightingState:
    mode: LightingMode = LightingMode.STUDIO
    preset: str = "neutral"
    preset_name: str = ""
    intensity: float = 1.0
    temperature_kelvin: int = 6500
    rig: StudioRigState | None = None

    def __post_init__(self):
        if not isinstance(self.mode, LightingMode):
            try:
                self.mode = LightingMode(str(self.mode).lower())
            except ValueError:
                self.mode = LightingMode.STUDIO
        self.preset = str(self.preset or "neutral").strip().lower()
        custom = self.preset.startswith(CUSTOM_LIGHTING_PRESET_PREFIX)
        if self.preset not in LIGHTING_PRESET_IDS and not custom:
            self.preset = "neutral"
        self.preset_name = str(self.preset_name or "").strip()[:64]
        try:
            intensity = float(self.intensity)
        except (TypeError, ValueError):
            intensity = 1.0
        try:
            temperature_kelvin = int(float(self.temperature_kelvin))
        except (TypeError, ValueError):
            temperature_kelvin = 6500
        self.intensity = max(0.1, min(2.0, intensity))
        self.temperature_kelvin = max(
            2500, min(10000, temperature_kelvin)
        )
        if self.rig is not None and not isinstance(self.rig, StudioRigState):
            self.rig = StudioRigState.from_dict(self.rig)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "mode": self.mode.value,
            "preset": self.preset,
            "intensity": self.intensity,
            "temperature_kelvin": self.temperature_kelvin,
        }
        if self.preset_name:
            data["preset_name"] = self.preset_name
        if self.rig is not None:
            data["rig"] = self.rig.to_dict()
        return data

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any] | None,
        *,
        default_mode: LightingMode = LightingMode.STUDIO,
    ) -> "LightingState":
        data = dict(data) if isinstance(data, dict) else {}
        return cls(
            mode=data.get("mode", default_mode.value),
            preset=data.get("preset", "neutral"),
            preset_name=data.get("preset_name", ""),
            intensity=data.get("intensity", 1.0),
            temperature_kelvin=data.get("temperature_kelvin", 6500),
            rig=data.get("rig"),
        )


@dataclass
class ExportOptions:
    output_size: int = 512
    supersample: int = 2
    transparent: bool = True
    background_color: tuple[int, int, int, int] = (32, 32, 32, 255)
    output_directory: str = ""
    import_texture: bool = True
    import_path: str = ""
    naming_pattern: str = "{name}_icon_{size}"
    preset_name: str = "Default"


@dataclass
class CaptureRequest:
    source: CaptureSource
    camera: CameraState = field(default_factory=CameraState)
    adjust: AdjustState = field(default_factory=AdjustState)
    lighting: LightingState = field(default_factory=LightingState)
    export: ExportOptions = field(default_factory=ExportOptions)
    preview: bool = False
    preview_fast: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "camera": asdict(self.camera),
            "adjust": asdict(self.adjust),
            "lighting": self.lighting.to_dict(),
            "export": asdict(self.export),
            "preview": self.preview,
            "preview_fast": self.preview_fast,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaptureRequest":
        camera = dict(data.get("camera") or {})
        adjust = dict(data.get("adjust") or {})
        export = dict(data.get("export") or {})
        lighting_data = data.get("lighting")
        has_lighting = "lighting" in data
        if "outline_color" in adjust:
            adjust["outline_color"] = tuple(adjust["outline_color"])
        if "background_color" in export:
            export["background_color"] = tuple(export["background_color"])
        return cls(
            source=CaptureSource.from_dict(data.get("source") or {}),
            camera=CameraState(**camera),
            adjust=AdjustState(**adjust),
            lighting=LightingState.from_dict(
                lighting_data,
                default_mode=(
                    LightingMode.STUDIO
                    if has_lighting
                    else LightingMode.WORLD
                ),
            ),
            export=ExportOptions(**export),
            preview=bool(data.get("preview", False)),
            preview_fast=bool(data.get("preview_fast", True)),
        )


@dataclass
class CaptureResult:
    success: bool
    source_key: str
    png_path: str = ""
    texture_path: str = ""
    error: str = ""
    output_size: int = 0
    capture_size: int = 0
    elapsed_seconds: float = 0.0
    alpha_nonzero: int = 0
    alpha_fractional: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
