from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget


class PreviewSurface(QWidget):
    camera_dragged = Signal(float, float, str)
    camera_wheeled = Signal(float, bool)
    frame_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 420)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._pixmap = QPixmap()
        self._background = "Checker"
        self._last_pos = QPoint()
        self._drag_mode = ""

    def set_background_mode(self, mode: str):
        self._background = mode
        self.update()

    def set_pixmap(self, pixmap: QPixmap):
        self._pixmap = pixmap
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        rect = self.rect()
        if self._background == "Dark":
            painter.fillRect(rect, QColor("#15171a"))
        elif self._background == "Light":
            painter.fillRect(rect, QColor("#d8d8d8"))
        else:
            tile = 16
            for y in range(0, rect.height(), tile):
                for x in range(0, rect.width(), tile):
                    color = QColor("#36393d") if ((x // tile + y // tile) % 2) else QColor("#25282b")
                    painter.fillRect(QRect(x, y, tile, tile), color)
        if not self._pixmap.isNull():
            scaled = self._pixmap.scaled(rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x = (rect.width() - scaled.width()) // 2
            y = (rect.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            painter.setPen(QColor("#8d949c"))
            painter.drawText(rect, Qt.AlignCenter, "Select a source to start the live preview")
        painter.setPen(QPen(QColor("#555b62"), 1))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))

    def mousePressEvent(self, event):
        self.setFocus()
        self._last_pos = event.position().toPoint()
        if event.button() == Qt.LeftButton:
            self._drag_mode = "roll" if event.modifiers() & Qt.ShiftModifier else "orbit"
        elif event.button() == Qt.MiddleButton:
            self._drag_mode = "pan"
        else:
            self._drag_mode = ""
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._drag_mode:
            return
        position = event.position().toPoint()
        delta = position - self._last_pos
        self._last_pos = position
        self.camera_dragged.emit(float(delta.x()), float(delta.y()), self._drag_mode)
        event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_mode = ""
        event.accept()

    def wheelEvent(self, event):
        steps = float(event.angleDelta().y()) / 120.0
        self.camera_wheeled.emit(steps, bool(event.modifiers() & Qt.ControlModifier))
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.frame_requested.emit()
            event.accept()



