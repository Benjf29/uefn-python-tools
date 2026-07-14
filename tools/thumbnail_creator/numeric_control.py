from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QSlider,
    QSpinBox,
    QWidget,
)


class SliderSpinBox(QWidget):
    """A slider for quick changes paired with a spin box for exact input."""

    valueChanged = Signal(float)

    _LOG_SLIDER_STEPS = 1000

    def __init__(
        self,
        minimum: float,
        maximum: float,
        value: float,
        step: float = 1.0,
        decimals: int = 1,
        suffix: str = "",
        logarithmic: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        if maximum <= minimum:
            raise ValueError("maximum must be greater than minimum")
        if logarithmic and minimum <= 0:
            raise ValueError("a logarithmic slider requires a positive minimum")

        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self._decimals = max(0, int(decimals))
        self._scale = 10 ** self._decimals
        self._logarithmic = bool(logarithmic)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setTracking(True)
        self.slider.setMinimumWidth(120)
        if self._logarithmic:
            self.slider.setRange(0, self._LOG_SLIDER_STEPS)
            self.slider.setSingleStep(1)
            self.slider.setPageStep(50)
        else:
            slider_min = self._linear_slider_value(self._minimum)
            slider_max = self._linear_slider_value(self._maximum)
            slider_step = max(1, int(round(float(step) * self._scale)))
            self.slider.setRange(slider_min, slider_max)
            self.slider.setSingleStep(slider_step)
            self.slider.setPageStep(max(slider_step, (slider_max - slider_min) // 10))
        layout.addWidget(self.slider, 1)

        if self._decimals == 0:
            self.spin_box = QSpinBox()
            self.spin_box.setRange(int(round(minimum)), int(round(maximum)))
            self.spin_box.setSingleStep(max(1, int(round(step))))
        else:
            self.spin_box = QDoubleSpinBox()
            self.spin_box.setRange(self._minimum, self._maximum)
            self.spin_box.setSingleStep(float(step))
            self.spin_box.setDecimals(self._decimals)
        self.spin_box.setSuffix(suffix)
        self.spin_box.setAlignment(Qt.AlignRight)
        self.spin_box.setMinimumWidth(82)
        layout.addWidget(self.spin_box)

        tooltip = "Drag the slider for quick changes, or type an exact value."
        self.slider.setToolTip(tooltip)
        self.spin_box.setToolTip(tooltip)
        self.setFocusProxy(self.spin_box)

        self.slider.valueChanged.connect(self._slider_changed)
        self.spin_box.valueChanged.connect(self._spin_changed)
        self.setValue(value)

    def value(self):
        return self.spin_box.value()

    def setValue(self, value: float) -> None:
        old_value = self.value()
        clamped = max(self._minimum, min(self._maximum, float(value)))
        spin_value = int(round(clamped)) if self._decimals == 0 else clamped

        spin_was_blocked = self.spin_box.blockSignals(True)
        slider_was_blocked = self.slider.blockSignals(True)
        try:
            self.spin_box.setValue(spin_value)
            self.slider.setValue(self._slider_value(float(self.spin_box.value())))
        finally:
            self.slider.blockSignals(slider_was_blocked)
            self.spin_box.blockSignals(spin_was_blocked)

        new_value = self.value()
        if new_value != old_value:
            self.valueChanged.emit(float(new_value))

    def _linear_slider_value(self, value: float) -> int:
        return int(round(float(value) * self._scale))

    def _slider_value(self, value: float) -> int:
        value = max(self._minimum, min(self._maximum, float(value)))
        if not self._logarithmic:
            return self._linear_slider_value(value)
        ratio = math.log(value / self._minimum) / math.log(
            self._maximum / self._minimum
        )
        return int(round(ratio * self._LOG_SLIDER_STEPS))

    def _value_from_slider(self, slider_value: int) -> float:
        if not self._logarithmic:
            return float(slider_value) / self._scale
        ratio = float(slider_value) / self._LOG_SLIDER_STEPS
        return self._minimum * ((self._maximum / self._minimum) ** ratio)

    def _slider_changed(self, slider_value: int) -> None:
        value = self._value_from_slider(slider_value)
        spin_value = int(round(value)) if self._decimals == 0 else value
        spin_was_blocked = self.spin_box.blockSignals(True)
        try:
            self.spin_box.setValue(spin_value)
        finally:
            self.spin_box.blockSignals(spin_was_blocked)
        self.valueChanged.emit(float(self.spin_box.value()))

    def _spin_changed(self, value) -> None:
        slider_was_blocked = self.slider.blockSignals(True)
        try:
            self.slider.setValue(self._slider_value(float(value)))
        finally:
            self.slider.blockSignals(slider_was_blocked)
        self.valueChanged.emit(float(value))
