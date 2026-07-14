"""Pure helpers for reconstructing world bounds from orthographic silhouettes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


Vector3 = tuple[float, float, float]
AxisIntervals = dict[int, tuple[float, float]]


@dataclass(frozen=True)
class Bounds3D:
    minimum: Vector3
    maximum: Vector3

    @property
    def center(self) -> Vector3:
        return tuple(
            (minimum + maximum) * 0.5
            for minimum, maximum in zip(self.minimum, self.maximum)
        )

    @property
    def extent(self) -> Vector3:
        return tuple(
            max(0.0, (maximum - minimum) * 0.5)
            for minimum, maximum in zip(self.minimum, self.maximum)
        )

    @property
    def radius(self) -> float:
        return math.sqrt(sum(value * value for value in self.extent))

    @property
    def maximum_span(self) -> float:
        return max(
            maximum - minimum
            for minimum, maximum in zip(self.minimum, self.maximum)
        )


def bounds_from_center_extent(center: Vector3, extent: Vector3) -> Bounds3D:
    return Bounds3D(
        tuple(value - radius for value, radius in zip(center, extent)),
        tuple(value + radius for value, radius in zip(center, extent)),
    )


def pixel_bounds_touch_edge(
    pixel_bounds: tuple[int, int, int, int] | None,
    size: int,
    padding: int = 2,
) -> bool:
    if not pixel_bounds:
        return False
    min_x, min_y, max_x, max_y = pixel_bounds
    edge = max(0, int(padding))
    return (
        min_x <= edge
        or min_y <= edge
        or max_x >= int(size) - 1 - edge
        or max_y >= int(size) - 1 - edge
    )


def projection_intervals(
    pixel_bounds: tuple[int, int, int, int],
    size: int,
    ortho_width: float,
    view_center: Vector3,
    right: Vector3,
    up: Vector3,
    forward: Vector3,
) -> AxisIntervals:
    """Map a pixel rectangle to conservative intervals on its two world axes."""
    if int(size) <= 0 or float(ortho_width) <= 0.0:
        raise ValueError("Projection size and width must be positive.")
    min_x, min_y, max_x, max_y = pixel_bounds
    scale = float(ortho_width) / float(size)
    # Pixel edges are used instead of centers, providing the one-pixel-cell
    # conservative coverage required by the visual-bounds probe.
    u_min = (float(min_x) - float(size) * 0.5) * scale
    u_max = (float(max_x + 1) - float(size) * 0.5) * scale
    # Render-target rows run downwards, while the camera up vector runs upwards.
    v_min = (float(size) * 0.5 - float(max_y + 1)) * scale
    v_max = (float(size) * 0.5 - float(min_y)) * scale
    corners = [
        tuple(
            view_center[axis] + right[axis] * u + up[axis] * v
            for axis in range(3)
        )
        for u in (u_min, u_max)
        for v in (v_min, v_max)
    ]
    intervals: AxisIntervals = {}
    for axis in range(3):
        # The view's depth axis cannot be reconstructed from a silhouette.
        if abs(float(forward[axis])) >= 0.5:
            continue
        values = [corner[axis] for corner in corners]
        intervals[axis] = (min(values), max(values))
    return intervals


def combine_projection_intervals(
    projections: Iterable[AxisIntervals],
    minimum_views: int = 2,
) -> Bounds3D | None:
    samples = [dict(projection) for projection in projections if projection]
    if len(samples) < int(minimum_views):
        return None
    minimum = []
    maximum = []
    for axis in range(3):
        intervals = [sample[axis] for sample in samples if axis in sample]
        if not intervals:
            return None
        minimum.append(min(interval[0] for interval in intervals))
        maximum.append(max(interval[1] for interval in intervals))
    bounds = Bounds3D(tuple(minimum), tuple(maximum))
    if bounds.radius <= 1.0e-4 or not math.isfinite(bounds.radius):
        return None
    return bounds


def inflate_bounds(
    bounds: Bounds3D,
    absolute_padding: float,
    fractional_padding: float = 0.02,
) -> Bounds3D:
    minimum = []
    maximum = []
    for low, high in zip(bounds.minimum, bounds.maximum):
        span = max(0.0, high - low)
        padding = max(0.0, float(absolute_padding)) + span * max(
            0.0, float(fractional_padding)
        )
        minimum.append(low - padding)
        maximum.append(high + padding)
    return Bounds3D(tuple(minimum), tuple(maximum))


def next_probe_frame(
    current_center: Vector3,
    current_width: float,
    candidate: Bounds3D,
    *,
    edge_touched: bool,
    pass_index: int,
    maximum_passes: int = 3,
    framing_margin: float = 1.25,
) -> tuple[Vector3, float] | None:
    """Choose an expanded or tighter frame, or accept the current candidate."""
    if int(pass_index) + 1 >= int(maximum_passes):
        return None
    if edge_touched:
        return tuple(current_center), max(1.0e-3, float(current_width) * 2.0)
    desired_width = max(1.0e-3, candidate.maximum_span * float(framing_margin))
    center_shift = math.sqrt(
        sum(
            (candidate.center[index] - current_center[index]) ** 2
            for index in range(3)
        )
    )
    pixel_size = float(current_width) / 256.0
    if (
        float(current_width) > desired_width * 1.10
        or desired_width > float(current_width)
        or center_shift > pixel_size
    ):
        return candidate.center, desired_width
    return None
