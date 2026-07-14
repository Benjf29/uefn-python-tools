from __future__ import annotations

import copy
import os
import shutil
import subprocess
import time
from datetime import datetime

import unreal
from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QInputDialog,
)

from .capture import CaptureError, CaptureSession
from .image_ops import composite_background, scaled_preview_adjustments
from .importer import import_or_reimport_texture
from .lighting import get_builtin_studio_rig
from .models import (
    CUSTOM_LIGHTING_PRESET_PREFIX,
    STUDIO_LIGHT_ROLES,
    AdjustState,
    CameraState,
    CaptureRequest,
    CaptureSource,
    ExportOptions,
    LightingMode,
    LightingState,
    SourceKind,
    StudioLightState,
    StudioRigState,
)
from .naming import render_pattern, safe_name, unique_png_path
from .numeric_control import SliderSpinBox
from .preview_widget import PreviewSurface
from .project_paths import default_import_path
from .sources import (
    selected_actor_source,
    selected_asset_sources,
    selected_folder_sources,
)
from .storage import LightingPresetError, ThumbnailCreatorStore


TOOL_NAME = "Thumbnail Creator"
WINDOW_REF = "_thumbnail_creator_window"
PREVIEW_SIZE = 384
FAST_PREVIEW_DELAY = 0.12
REFINED_PREVIEW_DELAY = 0.45
BUILTIN_LIGHTING_PRESETS = (
    ("Neutral", "neutral"),
    ("Soft", "soft"),
    ("Dramatic", "dramatic"),
    ("Flat", "flat"),
)


def _pixmap_from_pillow(image) -> QPixmap:
    return QPixmap.fromImage(ImageQt(image.convert("RGBA")))


def _spin(
    minimum,
    maximum,
    value,
    step=1.0,
    decimals=1,
    suffix="",
    logarithmic=False,
):
    return SliderSpinBox(
        minimum,
        maximum,
        value,
        step,
        decimals,
        suffix,
        logarithmic,
    )


class ThumbnailCreatorWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.store = ThumbnailCreatorStore()
        self.default_import_path = default_import_path(unreal)
        self.session = CaptureSession()
        self.source: CaptureSource | None = None
        self.camera = CameraState()
        self.adjust = AdjustState()
        self.lighting = LightingState()
        self.export = ExportOptions(
            output_directory=self.store.root,
            import_path=self.default_import_path,
        )
        self.last_png = ""
        self.last_preview_image = None
        self.last_preview_metadata = {}
        self.preview_busy = False
        self.batch_cancel = False
        self.batch_rows: list[dict] = []
        self.library = self.store.load_library()
        self.presets = self.store.load_presets()
        self.custom_lighting_presets = self.store.load_lighting_presets()
        self._lighting_loading = False
        self._lighting_dirty = False
        self._lighting_selected_id = "neutral"
        self._lighting_baseline = None
        self._ensure_default_presets()
        self._viewport_signature = ""
        self._sync_guard = False
        self._fast_preview_due_at = None
        self._refined_preview_due_at = None
        self._last_viewport_poll = 0.0
        self._slate_tick_handle = None
        self._closing = False

        self.setWindowTitle(TOOL_NAME)
        self.resize(1180, 820)
        self._build_ui()
        self._write_controls()
        self._connect_shortcuts()
        self._restore_session()
        self._refresh_presets()
        self._refresh_library()
        self._register_slate_tick()

    # ---------- UI construction ----------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 10, 10, 10)
        title_row = QHBoxLayout()
        title = QLabel("Thumbnail Creator")
        title.setObjectName("title")
        title_row.addWidget(title)
        self.source_label = QLabel("No source")
        self.source_label.setObjectName("source")
        self.source_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        title_row.addWidget(self.source_label, 1)
        self.capture_button = QPushButton("Capture + Import")
        self.capture_button.setObjectName("primary")
        self.capture_button.clicked.connect(self._capture_export)
        title_row.addWidget(self.capture_button)
        outer.addLayout(title_row)

        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter, 1)
        self.tabs = QTabWidget()
        self.tabs.setMinimumWidth(390)
        self.tabs.setMaximumWidth(500)
        self.tabs.addTab(self._build_frame_tab(), "Frame")
        self.tabs.addTab(self._build_lighting_tab(), "Lighting")
        self.tabs.addTab(self._build_adjust_tab(), "Adjust")
        self.tabs.addTab(self._build_batch_tab(), "Batch")
        self.tabs.addTab(self._build_presets_tab(), "Presets")
        self.tabs.addTab(self._build_library_tab(), "Library")
        splitter.addWidget(self.tabs)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(8, 0, 0, 0)
        tools = QHBoxLayout()
        tools.addWidget(QLabel("Preview background"))
        self.preview_background = QComboBox()
        self.preview_background.addItems(["Checker", "Dark", "Light"])
        self.preview_background.currentTextChanged.connect(self._preview_background_changed)
        tools.addWidget(self.preview_background)
        self.wysiwyg = QCheckBox("WYSIWYG viewport sync")
        self.wysiwyg.toggled.connect(self._wysiwyg_changed)
        tools.addWidget(self.wysiwyg)
        tools.addStretch(1)
        self.fps_label = QLabel("Fast/Refined • 384 px")
        tools.addWidget(self.fps_label)
        preview_layout.addLayout(tools)
        self.preview = PreviewSurface()
        self.preview.camera_dragged.connect(self._preview_dragged)
        self.preview.camera_wheeled.connect(self._preview_wheeled)
        self.preview.frame_requested.connect(self._frame_source)
        preview_layout.addWidget(self.preview, 1)
        mini_row = QHBoxLayout()
        mini_row.addStretch(1)
        mini_row.addWidget(QLabel("128 px"))
        self.preview_128 = QLabel()
        self.preview_128.setFixedSize(128, 128)
        self.preview_128.setAlignment(Qt.AlignCenter)
        self.preview_128.setObjectName("mini")
        mini_row.addWidget(self.preview_128)
        mini_row.addSpacing(20)
        mini_row.addWidget(QLabel("64 px"))
        self.preview_64 = QLabel()
        self.preview_64.setFixedSize(64, 64)
        self.preview_64.setAlignment(Qt.AlignCenter)
        self.preview_64.setObjectName("mini")
        mini_row.addWidget(self.preview_64)
        mini_row.addStretch(1)
        preview_layout.addLayout(mini_row)
        self.status = QPlainTextEdit()
        self.status.setReadOnly(True)
        self.status.setMaximumHeight(105)
        self.status.setMaximumBlockCount(120)
        preview_layout.addWidget(self.status)
        splitter.addWidget(preview_panel)
        splitter.setStretchFactor(1, 1)

        self.setStyleSheet(
            """
            QMainWindow, QWidget { background:#202225; color:#d5d8dc; font-size:12px; }
            QLabel#title { color:white; font-size:20px; font-weight:650; padding:4px 12px 8px 2px; }
            QLabel#source { color:#8fbce8; }
            QTabWidget::pane { border:1px solid #41454a; }
            QTabBar::tab { background:#292c30; padding:8px 11px; }
            QTabBar::tab:selected { background:#3a3f45; color:white; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QListWidget, QTableWidget, QPlainTextEdit {
                background:#151719; border:1px solid #43484e; border-radius:3px; padding:4px;
            }
            QPushButton { background:#34383d; border:1px solid #50555c; border-radius:3px; padding:7px 9px; }
            QPushButton:hover { background:#454b52; }
            QPushButton#primary { background:#0875d1; color:white; font-weight:600; padding:9px 16px; }
            QPushButton#primary:hover { background:#168bed; }
            QLabel#mini { background:#17191b; border:1px solid #4b5056; }
            QHeaderView::section { background:#30343a; color:#ddd; border:0; padding:5px; }
            QProgressBar { border:1px solid #454a50; text-align:center; background:#151719; }
            QProgressBar::chunk { background:#0875d1; }
            QSlider::groove:horizontal {
                background:#151719; border:1px solid #43484e; border-radius:3px; height:5px;
            }
            QSlider::sub-page:horizontal { background:#0875d1; border-radius:3px; }
            QSlider::handle:horizontal {
                background:#d5d8dc; border:1px solid #70767d; border-radius:7px;
                width:14px; margin:-5px 0;
            }
            QSlider::handle:horizontal:hover { background:white; border-color:#168bed; }
            """
        )

    def _build_frame_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        source_row = QHBoxLayout()
        asset = QPushButton("Asset selection")
        asset.clicked.connect(self._use_asset_selection)
        source_row.addWidget(asset)
        actors = QPushButton("Level actors")
        actors.clicked.connect(self._use_actor_selection)
        source_row.addWidget(actors)
        whole = QPushButton("Whole View")
        whole.clicked.connect(self._use_whole_view)
        source_row.addWidget(whole)
        layout.addLayout(source_row)

        recipes = QHBoxLayout()
        for name in ("Front", "3/4", "Top", "Tilt"):
            button = QPushButton(name)
            button.clicked.connect(lambda _checked=False, recipe=name: self._apply_recipe(recipe))
            recipes.addWidget(button)
        layout.addLayout(recipes)

        form = QFormLayout()
        self.yaw = _spin(-180, 180, 35, 1, 1, "°")
        self.pitch = _spin(-89, 89, 20, 1, 1, "°")
        self.roll = _spin(-180, 180, 0, 1, 1, "°")
        self.pan_x = _spin(-5, 5, 0, .02, 2)
        self.pan_y = _spin(-5, 5, 0, .02, 2)
        self.dolly = _spin(.02, 20, 1, .05, 2, logarithmic=True)
        self.fov = _spin(5, 120, 35, 1, 1, "°")
        self.margin = _spin(1, 3, 1.18, .02, 2)
        for label, widget in (
            ("Yaw", self.yaw), ("Pitch", self.pitch), ("Roll", self.roll),
            ("Pan X", self.pan_x), ("Pan Y", self.pan_y), ("Zoom / dolly", self.dolly),
            ("FOV", self.fov), ("Framing margin", self.margin),
        ):
            form.addRow(label, widget)
            widget.valueChanged.connect(self._frame_control_changed)
        self.auto_fit = QCheckBox("Auto-fit only when out of frame")
        self.auto_fit.setChecked(True)
        self.auto_fit.toggled.connect(self._frame_control_changed)
        form.addRow("", self.auto_fit)
        layout.addLayout(form)
        frame = QPushButton("Frame / center selection (F)")
        frame.clicked.connect(self._frame_source)
        layout.addWidget(frame)

        export_form = QFormLayout()
        self.output_size = QComboBox()
        for size in (256, 512, 1024):
            self.output_size.addItem("%d × %d" % (size, size), size)
        self.output_size.setCurrentIndex(1)
        self.supersample = QComboBox()
        self.supersample.addItem("1×", 1)
        self.supersample.addItem("2×", 2)
        self.supersample.setCurrentIndex(1)
        export_form.addRow("Export", self.output_size)
        export_form.addRow("Supersampling", self.supersample)
        self.transparent = QCheckBox("Transparent")
        self.transparent.setChecked(True)
        self.transparent.toggled.connect(self._dirty)
        export_form.addRow("Background", self.transparent)
        self.background_button = QPushButton("Solid color…")
        self.background_button.clicked.connect(self._choose_background)
        export_form.addRow("Solid color", self.background_button)
        self.output_dir = QLineEdit(self.store.root)
        browse = QPushButton("…")
        browse.setFixedWidth(34)
        browse.clicked.connect(self._browse_output)
        output_row = QWidget()
        output_layout = QHBoxLayout(output_row)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.addWidget(self.output_dir, 1)
        output_layout.addWidget(browse)
        export_form.addRow("PNG folder", output_row)
        self.naming = QLineEdit("{name}_icon_{size}")
        self.naming.setToolTip("{name} {parent} {index} {date} {preset} {size}")
        export_form.addRow("Naming", self.naming)
        self.import_texture = QCheckBox("Import / reimport Texture2D")
        self.import_texture.setChecked(True)
        export_form.addRow("", self.import_texture)
        self.import_path = QLineEdit(self.default_import_path)
        export_form.addRow("UEFN folder", self.import_path)
        layout.addLayout(export_form)
        layout.addStretch(1)
        return page

    def _build_lighting_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.lighting_mode = QComboBox()
        self.lighting_mode.addItem("Studio", LightingMode.STUDIO.value)
        self.lighting_mode.addItem("World", LightingMode.WORLD.value)
        self.lighting_mode.currentIndexChanged.connect(self._lighting_mode_changed)
        form.addRow("Mode", self.lighting_mode)

        self.lighting_preset = QComboBox()
        self._refresh_lighting_preset_combo("neutral")
        self.lighting_preset.currentIndexChanged.connect(
            self._lighting_preset_changed
        )
        form.addRow("Preset", self.lighting_preset)

        self.lighting_intensity = _spin(0.1, 2.0, 1.0, .05, 2, "×")
        self.lighting_intensity.valueChanged.connect(
            self._lighting_control_changed
        )
        form.addRow("Master intensity", self.lighting_intensity)

        self.lighting_temperature = _spin(2500, 10000, 6500, 100, 0, " K")
        self.lighting_temperature.valueChanged.connect(
            self._lighting_control_changed
        )
        form.addRow("Temperature", self.lighting_temperature)
        layout.addLayout(form)

        actions = QHBoxLayout()
        self.lighting_save_as = QPushButton("Save As")
        self.lighting_save = QPushButton("Save")
        self.lighting_rename = QPushButton("Rename")
        self.lighting_delete = QPushButton("Delete")
        self.lighting_save_as.clicked.connect(self._save_lighting_preset_as)
        self.lighting_save.clicked.connect(self._save_lighting_preset)
        self.lighting_rename.clicked.connect(self._rename_lighting_preset)
        self.lighting_delete.clicked.connect(self._delete_lighting_preset)
        for button in (
            self.lighting_save_as,
            self.lighting_save,
            self.lighting_rename,
            self.lighting_delete,
        ):
            actions.addWidget(button)
        layout.addLayout(actions)

        self.lighting_modified = QLabel("")
        layout.addWidget(self.lighting_modified)

        self.rig_tabs = QTabWidget()
        self.rig_controls = {}
        labels = {"key": "Key", "fill": "Fill", "rim": "Rim"}
        for role in STUDIO_LIGHT_ROLES:
            tab = QWidget()
            rig_form = QFormLayout(tab)
            controls = {
                "intensity_multiplier": _spin(0.0, 3.0, 1.0, .05, 2, "Ã—"),
                "temperature_offset": _spin(-4000, 4000, 0, 100, 0, " K"),
                "size_multiplier": _spin(0.1, 5.0, 1.0, .1, 2, " R"),
                "toward": _spin(-5.0, 5.0, 0.0, .1, 2, " R"),
                "right": _spin(-5.0, 5.0, 0.0, .1, 2, " R"),
                "up": _spin(-5.0, 5.0, 0.0, .1, 2, " R"),
                "cast_shadows": QCheckBox("Enabled"),
            }
            for name in (
                "intensity_multiplier",
                "temperature_offset",
                "size_multiplier",
                "toward",
                "right",
                "up",
            ):
                controls[name].valueChanged.connect(self._lighting_rig_changed)
            controls["cast_shadows"].toggled.connect(self._lighting_rig_changed)
            rig_form.addRow("Intensity", controls["intensity_multiplier"])
            rig_form.addRow("Temperature offset", controls["temperature_offset"])
            rig_form.addRow("Size", controls["size_multiplier"])
            rig_form.addRow("Toward camera", controls["toward"])
            rig_form.addRow("Right", controls["right"])
            rig_form.addRow("Up", controls["up"])
            rig_form.addRow("Cast shadows", controls["cast_shadows"])
            self.rig_controls[role] = controls
            self.rig_tabs.addTab(tab, labels[role])
        layout.addWidget(self.rig_tabs)

        reset = QPushButton("Reset Lighting")
        reset.clicked.connect(self._reset_lighting)
        layout.addWidget(reset)
        self.lighting_status = QLabel("")
        self.lighting_status.setWordWrap(True)
        layout.addWidget(self.lighting_status)
        layout.addStretch(1)
        return page

    def _build_adjust_tab(self):
        page = QWidget()
        form = QFormLayout(page)
        self.hue = _spin(-180, 180, 0, 1, 1, "°")
        self.saturation = _spin(0, 3, 1, .05, 2)
        self.brightness = _spin(0, 3, 1, .05, 2)
        self.contrast = _spin(0, 3, 1, .05, 2)
        self.exposure = _spin(-4, 4, 0, .1, 2, " EV")
        self.outline_width = _spin(0, 32, 0, 1, 0)
        self.outline_color = (0, 0, 0, 255)
        self.outline_button = QPushButton("#000000")
        self.outline_button.clicked.connect(self._choose_outline)
        for label, widget in (
            ("Hue", self.hue), ("Saturation", self.saturation),
            ("Brightness", self.brightness), ("Contrast", self.contrast),
            ("Exposure", self.exposure), ("Outline width", self.outline_width),
        ):
            form.addRow(label, widget)
            widget.valueChanged.connect(self._adjust_control_changed)
        form.addRow("Outline color", self.outline_button)
        reset = QPushButton("Reset adjustments")
        reset.clicked.connect(self._reset_adjustments)
        form.addRow("", reset)
        return page

    def _build_batch_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        buttons = QHBoxLayout()
        assets = QPushButton("Prepare selected assets")
        assets.clicked.connect(self._prepare_batch_assets)
        buttons.addWidget(assets)
        folder = QPushButton("Prepare selected folder")
        folder.clicked.connect(self._prepare_batch_folder)
        buttons.addWidget(folder)
        layout.addLayout(buttons)
        self.batch_progress = QProgressBar()
        layout.addWidget(self.batch_progress)
        progress_row = QHBoxLayout()
        self.batch_eta = QLabel("Ready")
        progress_row.addWidget(self.batch_eta, 1)
        cancel = QPushButton("Cancel after current")
        cancel.clicked.connect(lambda: setattr(self, "batch_cancel", True))
        progress_row.addWidget(cancel)
        layout.addLayout(progress_row)
        self.batch_table = QTableWidget(0, 4)
        self.batch_table.setHorizontalHeaderLabels(["Export", "Source", "Status", "PNG"])
        self.batch_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.batch_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self.batch_table, 1)
        actions = QHBoxLayout()
        export = QPushButton("Export/import checked")
        export.clicked.connect(self._export_batch_checked)
        actions.addWidget(export)
        retry = QPushButton("Retry failures only")
        retry.clicked.connect(self._retry_batch_failures)
        actions.addWidget(retry)
        layout.addLayout(actions)
        self.batch_ignored = QLabel("")
        self.batch_ignored.setWordWrap(True)
        layout.addWidget(self.batch_ignored)
        return page

    def _build_presets_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.preset_scope = QComboBox()
        self.preset_scope.addItem("Object presets", "objects")
        self.preset_scope.addItem("Whole View presets", "whole_view")
        self.preset_scope.currentIndexChanged.connect(self._refresh_presets)
        layout.addWidget(self.preset_scope)
        self.preset_list = QListWidget()
        self.preset_list.itemDoubleClicked.connect(lambda _item: self._apply_selected_preset())
        layout.addWidget(self.preset_list, 1)
        row = QHBoxLayout()
        for label, slot in (
            ("Create", self._create_preset), ("Duplicate", self._duplicate_preset),
            ("Update", self._update_preset), ("Delete", self._delete_preset),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            row.addWidget(button)
        layout.addLayout(row)
        apply_button = QPushButton("Apply selected preset")
        apply_button.clicked.connect(self._apply_selected_preset)
        layout.addWidget(apply_button)
        return page

    def _build_library_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.library_search = QLineEdit()
        self.library_search.setPlaceholderText("Search name, source, UEFN path, tags or folder…")
        self.library_search.textChanged.connect(self._refresh_library)
        layout.addWidget(self.library_search)
        self.library_table = QTableWidget(0, 5)
        self.library_table.setHorizontalHeaderLabels(["Name", "Folder", "Tags", "Source", "Texture2D"])
        self.library_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.library_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.library_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.library_table, 1)
        row = QHBoxLayout()
        for label, slot in (
            ("Reveal PNG", self._library_reveal), ("Select Texture2D", self._library_select_texture),
            ("Copy path", self._library_copy_path), ("Regenerate", self._library_regenerate),
            ("Edit folder/tags", self._library_edit_metadata),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            row.addWidget(button)
        layout.addLayout(row)
        return page

    def _connect_shortcuts(self):
        capture = QAction(self)
        capture.setShortcut(QKeySequence("Ctrl+Return"))
        capture.triggered.connect(self._capture_export)
        self.addAction(capture)
        frame = QAction(self)
        frame.setShortcut(QKeySequence("F"))
        frame.triggered.connect(self._frame_source)
        self.addAction(frame)
        save = QAction(self)
        save.setShortcut(QKeySequence("Ctrl+Shift+S"))
        save.triggered.connect(self._save_last_png)
        self.addAction(save)

    def _ensure_default_presets(self):
        objects = self.presets.setdefault("objects", {})
        whole = self.presets.setdefault("whole_view", {})
        changed = False
        defaults = {
            "Front": CameraState(yaw=0.0, pitch=0.0),
            "3/4": CameraState(yaw=35.0, pitch=20.0),
            "Top": CameraState(yaw=0.0, pitch=89.0),
            "Tilt": CameraState(yaw=35.0, pitch=20.0, roll=-12.0),
        }
        base_export = ExportOptions(
            output_directory=self.store.root,
            import_path=self.default_import_path,
        )
        legacy_world = LightingState(mode=LightingMode.WORLD).to_dict()
        for payload in objects.values():
            if "lighting" not in payload:
                payload["lighting"] = copy.deepcopy(legacy_world)
                changed = True
        for payload in whole.values():
            if payload.get("lighting") != legacy_world:
                payload["lighting"] = copy.deepcopy(legacy_world)
                changed = True
        for name, camera in defaults.items():
            if name not in objects:
                objects[name] = {
                    "camera": camera.__dict__.copy(),
                    "adjust": AdjustState().__dict__.copy(),
                    "lighting": LightingState().to_dict(),
                    "export": base_export.__dict__.copy(),
                }
                changed = True
        if "Viewport Default" not in whole:
            whole["Viewport Default"] = {
                "camera": CameraState().__dict__.copy(),
                "adjust": AdjustState().__dict__.copy(),
                "lighting": legacy_world,
                "export": base_export.__dict__.copy(),
            }
            changed = True
        if changed:
            self.store.save_presets(self.presets)

    # ---------- state ----------
    def _log(self, message: str):
        self.status.appendPlainText(message)
        QApplication.processEvents()

    @staticmethod
    def _custom_lighting_identifier(preset_id: str) -> str:
        value = str(preset_id or "")
        if value.startswith(CUSTOM_LIGHTING_PRESET_PREFIX):
            return value[len(CUSTOM_LIGHTING_PRESET_PREFIX):]
        return ""

    @staticmethod
    def _builtin_lighting_name(preset_id: str) -> str:
        for label, identifier in BUILTIN_LIGHTING_PRESETS:
            if identifier == preset_id:
                return label
        return "Neutral"

    def _lighting_record(self, preset_id: str):
        identifier = self._custom_lighting_identifier(preset_id)
        return self.custom_lighting_presets.get(identifier) if identifier else None

    def _refresh_lighting_preset_combo(self, selected_id=None):
        if not hasattr(self, "lighting_preset"):
            return
        selected_id = str(
            selected_id
            or self.lighting_preset.currentData()
            or getattr(self.lighting, "preset", "neutral")
        )
        was_blocked = self.lighting_preset.blockSignals(True)
        self.lighting_preset.clear()
        for label, preset_id in BUILTIN_LIGHTING_PRESETS:
            self.lighting_preset.addItem(label, preset_id)
        records = sorted(
            self.custom_lighting_presets.values(),
            key=lambda record: str(record.get("name", "")).casefold(),
        )
        if records:
            self.lighting_preset.insertSeparator(self.lighting_preset.count())
        for record in records:
            self.lighting_preset.addItem(
                record["name"],
                CUSTOM_LIGHTING_PRESET_PREFIX + record["id"],
            )
        index = self.lighting_preset.findData(selected_id)
        if (
            index < 0
            and selected_id.startswith(CUSTOM_LIGHTING_PRESET_PREFIX)
            and getattr(self.lighting, "rig", None) is not None
        ):
            self.lighting_preset.insertSeparator(self.lighting_preset.count())
            label = self.lighting.preset_name or "Recovered preset"
            self.lighting_preset.addItem(label + " (Snapshot)", selected_id)
            index = self.lighting_preset.count() - 1
        if index < 0:
            index = self.lighting_preset.findData("neutral")
        self.lighting_preset.setCurrentIndex(index)
        self.lighting_preset.blockSignals(was_blocked)

    def _lighting_state_for_preset(self, preset_id: str) -> LightingState:
        preset_id = str(preset_id or "neutral")
        record = self._lighting_record(preset_id)
        if record is not None:
            state = LightingState.from_dict(record["lighting"])
            state.preset = preset_id
            state.preset_name = record["name"]
            return state
        if preset_id in {identifier for _label, identifier in BUILTIN_LIGHTING_PRESETS}:
            return LightingState(
                mode=LightingMode.STUDIO,
                preset=preset_id,
                preset_name=self._builtin_lighting_name(preset_id),
                intensity=1.0,
                temperature_kelvin=6500,
                rig=get_builtin_studio_rig(preset_id),
            )
        if (
            preset_id == getattr(self.lighting, "preset", "")
            and self.lighting.rig is not None
        ):
            return copy.deepcopy(self.lighting)
        return self._lighting_state_for_preset("neutral")

    def _lighting_rig_from_controls(self) -> StudioRigState:
        if not hasattr(self, "rig_controls"):
            return get_builtin_studio_rig(
                getattr(self.lighting, "preset", "neutral")
            )
        lights = {}
        for role in STUDIO_LIGHT_ROLES:
            controls = self.rig_controls[role]
            lights[role] = StudioLightState(
                intensity_multiplier=controls["intensity_multiplier"].value(),
                temperature_offset=controls["temperature_offset"].value(),
                size_multiplier=controls["size_multiplier"].value(),
                position=(
                    controls["toward"].value(),
                    controls["right"].value(),
                    controls["up"].value(),
                ),
                cast_shadows=controls["cast_shadows"].isChecked(),
            )
        return StudioRigState(**lights)

    def _lighting_from_controls(self) -> LightingState:
        preset_id = str(
            self.lighting_preset.currentData()
            or self._lighting_selected_id
            or "neutral"
        )
        record = self._lighting_record(preset_id)
        preset_name = (
            record["name"]
            if record is not None
            else self._builtin_lighting_name(preset_id)
        )
        if record is None and preset_id.startswith(CUSTOM_LIGHTING_PRESET_PREFIX):
            preset_name = self.lighting.preset_name or "Recovered preset"
        return LightingState(
            mode=self.lighting_mode.currentData(),
            preset=preset_id,
            preset_name=preset_name,
            intensity=self.lighting_intensity.value(),
            temperature_kelvin=self.lighting_temperature.value(),
            rig=self._lighting_rig_from_controls(),
        )

    @staticmethod
    def _lighting_signature(state: LightingState):
        rig = state.rig or get_builtin_studio_rig(state.preset)
        return {
            "intensity": state.intensity,
            "temperature_kelvin": state.temperature_kelvin,
            "rig": rig.to_dict(),
        }

    def _set_lighting_rig_controls(self, rig: StudioRigState):
        for role in STUDIO_LIGHT_ROLES:
            light = getattr(rig, role)
            controls = self.rig_controls[role]
            widgets = list(controls.values())
            states = [widget.blockSignals(True) for widget in widgets]
            try:
                controls["intensity_multiplier"].setValue(
                    light.intensity_multiplier
                )
                controls["temperature_offset"].setValue(
                    light.temperature_offset
                )
                controls["size_multiplier"].setValue(light.size_multiplier)
                controls["toward"].setValue(light.position[0])
                controls["right"].setValue(light.position[1])
                controls["up"].setValue(light.position[2])
                controls["cast_shadows"].setChecked(light.cast_shadows)
            finally:
                for widget, was_blocked in zip(widgets, states):
                    widget.blockSignals(was_blocked)

    def _update_lighting_modified_state(self):
        if self._lighting_loading or not hasattr(self, "lighting_modified"):
            return
        current = self._lighting_from_controls()
        baseline = self._lighting_baseline
        self._lighting_dirty = bool(
            baseline is not None
            and self._lighting_signature(current)
            != self._lighting_signature(baseline)
        )
        record = self._lighting_record(current.preset)
        if record is None and current.preset.startswith(CUSTOM_LIGHTING_PRESET_PREFIX):
            message = "Snapshot only — use Save As to restore this preset."
        elif self._lighting_dirty and record is None:
            message = "Modified — use Save As to create a custom preset."
        elif self._lighting_dirty:
            message = "Modified — changes are not saved."
        else:
            message = ""
        self.lighting_modified.setText(message)
        self._update_lighting_preset_buttons()

    def _update_lighting_preset_buttons(self):
        if not hasattr(self, "lighting_save_as"):
            return
        whole_view = bool(
            self.source is not None and self.source.kind == SourceKind.WHOLE_VIEW
        )
        studio = self.lighting_mode.currentData() == LightingMode.STUDIO.value
        preset_id = str(self.lighting_preset.currentData() or "")
        custom_exists = self._lighting_record(preset_id) is not None
        enabled = bool(studio and not whole_view)
        self.lighting_save_as.setEnabled(enabled)
        self.lighting_save.setEnabled(enabled and custom_exists and self._lighting_dirty)
        self.lighting_rename.setEnabled(enabled and custom_exists)
        self.lighting_delete.setEnabled(enabled and custom_exists)

    def _request(self, preview=False, refined=False):
        if self.source is None:
            raise CaptureError("Select an asset, level actors, or Whole View first.")
        self._read_controls()
        export = copy.deepcopy(self.export)
        adjust = copy.deepcopy(self.adjust)
        if preview:
            output_size = export.output_size
            export.output_size = PREVIEW_SIZE
            export.supersample = 1
            adjust = scaled_preview_adjustments(
                adjust,
                PREVIEW_SIZE,
                output_size,
            )
        return CaptureRequest(
            source=copy.deepcopy(self.source),
            camera=copy.deepcopy(self.camera),
            adjust=adjust,
            lighting=copy.deepcopy(self.lighting),
            export=export,
            preview=preview,
            preview_fast=bool(preview and not refined),
        )

    def _read_controls(self):
        self.camera = CameraState(
            yaw=self.yaw.value(), pitch=self.pitch.value(), roll=self.roll.value(),
            pan_x=self.pan_x.value(), pan_y=self.pan_y.value(), dolly=self.dolly.value(),
            fov=self.fov.value(), framing_margin=self.margin.value(), auto_fit=self.auto_fit.isChecked(),
        )
        self.adjust = AdjustState(
            hue=self.hue.value(), saturation=self.saturation.value(), brightness=self.brightness.value(),
            contrast=self.contrast.value(), exposure=self.exposure.value(),
            outline_width=self.outline_width.value(), outline_color=self.outline_color,
        )
        self.lighting = self._lighting_from_controls()
        self.export = ExportOptions(
            output_size=int(self.output_size.currentData()), supersample=int(self.supersample.currentData()),
            transparent=self.transparent.isChecked(), background_color=getattr(self, "background_color", (32, 32, 32, 255)),
            output_directory=self.output_dir.text().strip() or self.store.root,
            import_texture=self.import_texture.isChecked(), import_path=self.import_path.text().strip(),
            naming_pattern=self.naming.text().strip() or "{name}_icon_{size}",
            preset_name=getattr(self, "active_preset", "Default"),
        )

    def _write_controls(self):
        lighting_state = copy.deepcopy(self.lighting)
        widgets = [self.yaw, self.pitch, self.roll, self.pan_x, self.pan_y, self.dolly, self.fov, self.margin]
        for widget in widgets:
            widget.blockSignals(True)
        values = [self.camera.yaw, self.camera.pitch, self.camera.roll, self.camera.pan_x, self.camera.pan_y, self.camera.dolly, self.camera.fov, self.camera.framing_margin]
        for widget, value in zip(widgets, values):
            widget.setValue(value)
            widget.blockSignals(False)
        auto_fit_blocked = self.auto_fit.blockSignals(True)
        self.auto_fit.setChecked(self.camera.auto_fit)
        self.auto_fit.blockSignals(auto_fit_blocked)
        adjust_widgets = [self.hue, self.saturation, self.brightness, self.contrast, self.exposure, self.outline_width]
        adjust_values = [self.adjust.hue, self.adjust.saturation, self.adjust.brightness, self.adjust.contrast, self.adjust.exposure, self.adjust.outline_width]
        for widget, value in zip(adjust_widgets, adjust_values):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        self.outline_color = tuple(self.adjust.outline_color)
        self.outline_button.setText("#%02X%02X%02X" % self.outline_color[:3])
        self.lighting = lighting_state
        lighting_widgets = [
            self.lighting_mode,
            self.lighting_preset,
            self.lighting_intensity,
            self.lighting_temperature,
        ]
        previous_selected = self._lighting_selected_id
        self._lighting_loading = True
        states = [widget.blockSignals(True) for widget in lighting_widgets]
        try:
            self._refresh_lighting_preset_combo(self.lighting.preset)
            self._select_combo_data(self.lighting_mode, self.lighting.mode.value)
            self._select_combo_data(self.lighting_preset, self.lighting.preset)
            self.lighting_intensity.setValue(self.lighting.intensity)
            self.lighting_temperature.setValue(self.lighting.temperature_kelvin)
            rig = self.lighting.rig or get_builtin_studio_rig(
                self.lighting.preset
            )
            self._set_lighting_rig_controls(rig)
            selected = str(self.lighting_preset.currentData() or "neutral")
            self._lighting_selected_id = selected
            if self._lighting_baseline is None or previous_selected != selected:
                self._lighting_baseline = self._lighting_state_for_preset(selected)
        finally:
            for widget, was_blocked in zip(lighting_widgets, states):
                widget.blockSignals(was_blocked)
            self._lighting_loading = False
        self._update_lighting_controls()
        self._update_lighting_modified_state()

    def _restore_session(self):
        data = self.store.load_session()
        try:
            request = CaptureRequest.from_dict(data["request"])
            self._lighting_baseline = None
            self.source = request.source
            self.camera = request.camera
            self.adjust = request.adjust
            self.lighting = request.lighting
            self.export = request.export
            self.background_color = tuple(request.export.background_color)
            self._write_controls()
            self.output_dir.setText(self.export.output_directory or self.store.root)
            self.naming.setText(self.export.naming_pattern)
            self.import_texture.setChecked(self.export.import_texture)
            self.import_path.setText(self.export.import_path or self.default_import_path)
            self.transparent.setChecked(self.export.transparent)
            self._select_combo_data(self.output_size, self.export.output_size)
            self._select_combo_data(self.supersample, self.export.supersample)
            self.source_label.setText(self.source.display_name or self.source.key)
            self.last_png = str(data.get("last_png") or "")
            self._dirty()
        except Exception:
            self.background_color = (32, 32, 32, 255)
            sources = selected_asset_sources()
            if sources:
                self._set_source(sources[0])

    @staticmethod
    def _select_combo_data(combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _save_session(self):
        try:
            request = self._request(False)
            self.store.save_session({"request": request.to_dict(), "last_png": self.last_png})
        except Exception:
            pass

    # ---------- source and preview ----------
    def _set_source(self, source: CaptureSource):
        self.source = source
        self.source_label.setText(source.display_name or source.key)
        self._log("Source: %s" % (source.paths[0] if source.paths else source.display_name))
        self._update_lighting_controls()
        self._dirty()

    def _lighting_mode_changed(self, *_args):
        if self._lighting_loading:
            return
        self._read_controls()
        self._update_lighting_controls()
        self._dirty()

    def _lighting_control_changed(self, *_args):
        if self._lighting_loading:
            return
        self._read_controls()
        self._update_lighting_modified_state()
        self._dirty()

    def _lighting_rig_changed(self, *_args):
        self._lighting_control_changed()

    def _lighting_preset_changed(self, *_args):
        if self._lighting_loading:
            return
        new_id = str(self.lighting_preset.currentData() or "neutral")
        old_id = str(self._lighting_selected_id or "neutral")
        if new_id == old_id:
            return
        blocked = self.lighting_preset.blockSignals(True)
        self._select_combo_data(self.lighting_preset, old_id)
        self.lighting_preset.blockSignals(blocked)
        if self._lighting_dirty and not self._confirm_lighting_changes():
            return
        self._apply_lighting_preset_id(new_id)

    def _apply_lighting_preset_id(self, preset_id: str):
        state = self._lighting_state_for_preset(preset_id)
        self.lighting = state
        self._lighting_selected_id = state.preset
        self._lighting_baseline = copy.deepcopy(state)
        self._write_controls()
        self._dirty()

    def _ask_lighting_preset_name(self, title, initial=""):
        name, accepted = QInputDialog.getText(
            self,
            title,
            "Preset name",
            text=initial,
        )
        return name.strip() if accepted else ""

    def _save_lighting_preset_as(self, *_args):
        self._read_controls()
        current = self._lighting_from_controls()
        base_name = current.preset_name or self._builtin_lighting_name(
            current.preset
        )
        suffix = " Copy" if self._lighting_record(current.preset) else " Custom"
        name = self._ask_lighting_preset_name(
            "Save custom lighting preset",
            (base_name + suffix)[:64],
        )
        if not name:
            return False
        try:
            record = self.store.create_lighting_preset(
                name,
                current.to_dict(),
            )
        except Exception as exc:
            QMessageBox.warning(self, TOOL_NAME, str(exc))
            return False
        self.custom_lighting_presets = self.store.load_lighting_presets()
        self.lighting = LightingState.from_dict(record["lighting"])
        self._lighting_selected_id = self.lighting.preset
        self._lighting_baseline = copy.deepcopy(self.lighting)
        self._write_controls()
        self._dirty()
        self._log("Lighting preset saved: %s" % record["name"])
        return True

    def _save_lighting_preset(self, *_args):
        preset_id = str(self._lighting_selected_id or "")
        identifier = self._custom_lighting_identifier(preset_id)
        record = self.custom_lighting_presets.get(identifier)
        if record is None:
            return self._save_lighting_preset_as()
        self._read_controls()
        current = self._lighting_from_controls()
        current.preset = preset_id
        current.preset_name = record["name"]
        try:
            updated = self.store.update_lighting_preset(
                identifier,
                lighting=current.to_dict(),
            )
        except Exception as exc:
            QMessageBox.warning(self, TOOL_NAME, str(exc))
            return False
        self.custom_lighting_presets = self.store.load_lighting_presets()
        self.lighting = LightingState.from_dict(updated["lighting"])
        self._lighting_selected_id = self.lighting.preset
        self._lighting_baseline = copy.deepcopy(self.lighting)
        self._write_controls()
        self._dirty()
        self._log("Lighting preset updated: %s" % updated["name"])
        return True

    def _rename_lighting_preset(self, *_args):
        preset_id = str(self._lighting_selected_id or "")
        identifier = self._custom_lighting_identifier(preset_id)
        record = self.custom_lighting_presets.get(identifier)
        if record is None:
            return False
        name = self._ask_lighting_preset_name(
            "Rename lighting preset", record["name"]
        )
        if not name:
            return False
        current = self._lighting_from_controls()
        try:
            updated = self.store.rename_lighting_preset(identifier, name)
        except Exception as exc:
            QMessageBox.warning(self, TOOL_NAME, str(exc))
            return False
        self.custom_lighting_presets = self.store.load_lighting_presets()
        current.preset_name = updated["name"]
        self.lighting = current
        self._lighting_baseline = LightingState.from_dict(updated["lighting"])
        self._refresh_lighting_preset_combo(preset_id)
        self._lighting_selected_id = preset_id
        self._update_lighting_modified_state()
        self._dirty()
        self._log("Lighting preset renamed: %s" % updated["name"])
        return True

    def _delete_lighting_preset(self, *_args):
        preset_id = str(self._lighting_selected_id or "")
        identifier = self._custom_lighting_identifier(preset_id)
        record = self.custom_lighting_presets.get(identifier)
        if record is None:
            return False
        response = QMessageBox.question(
            self,
            TOOL_NAME,
            "Delete lighting preset '%s'?" % record["name"],
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return False
        if not self.store.delete_lighting_preset(identifier):
            QMessageBox.warning(self, TOOL_NAME, "The lighting preset no longer exists.")
            return False
        self.custom_lighting_presets = self.store.load_lighting_presets()
        self._apply_lighting_preset_id("neutral")
        self._log("Lighting preset deleted: %s" % record["name"])
        return True

    def _confirm_lighting_changes(self, revert_on_discard=False):
        if not self._lighting_dirty:
            return True
        custom = self._lighting_record(self._lighting_selected_id) is not None
        detail = (
            "Save changes to this custom preset?"
            if custom
            else "Save these changes as a custom preset?"
        )
        response = QMessageBox.question(
            self,
            "Unsaved Studio lighting",
            detail,
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if response == QMessageBox.StandardButton.Cancel:
            return False
        if response == QMessageBox.StandardButton.Save:
            return (
                self._save_lighting_preset()
                if custom
                else self._save_lighting_preset_as()
            )
        if revert_on_discard and self._lighting_baseline is not None:
            self.lighting = copy.deepcopy(self._lighting_baseline)
            self._lighting_selected_id = self.lighting.preset
            self._write_controls()
        self._lighting_dirty = False
        return True

    def _reset_lighting(self):
        self.lighting = self._lighting_state_for_preset("neutral")
        self._lighting_selected_id = "neutral"
        self._lighting_baseline = copy.deepcopy(self.lighting)
        self._write_controls()
        self._dirty()

    def _update_lighting_controls(self):
        if not hasattr(self, "lighting_mode"):
            return
        whole_view = bool(
            self.source is not None and self.source.kind == SourceKind.WHOLE_VIEW
        )
        studio = self.lighting_mode.currentData() == LightingMode.STUDIO.value
        self.lighting_mode.setEnabled(not whole_view)
        enabled = bool(not whole_view and studio)
        for widget in (
            self.lighting_preset,
            self.lighting_intensity,
            self.lighting_temperature,
            self.rig_tabs,
        ):
            widget.setEnabled(enabled)
        self._update_lighting_preset_buttons()
        if whole_view:
            self.lighting_status.setText(
                "Whole View always uses the level's World lighting."
            )
        elif studio:
            self.lighting_status.setText(
                "Studio isolates the source on Lighting Channel 2 and restores it after capture."
            )
        else:
            self.lighting_status.setText(
                "World uses the current level lighting and global environment."
            )

    def _use_asset_selection(self):
        sources = selected_asset_sources()
        if not sources:
            QMessageBox.warning(self, TOOL_NAME, "Select a supported StaticMesh, SkeletalMesh, Blueprint/Class, or NiagaraSystem.")
            return
        self._set_source(sources[0])

    def _use_actor_selection(self):
        source = selected_actor_source()
        if source is None:
            QMessageBox.warning(self, TOOL_NAME, "Select one or more actors in the level.")
            return
        self._set_source(source)

    def _use_whole_view(self):
        self._set_source(CaptureSource(SourceKind.WHOLE_VIEW, [], "Whole View"))
        self.wysiwyg.setChecked(True)

    def _dirty(self, *_args):
        if not self._closing:
            now = time.monotonic()
            self._fast_preview_due_at = now + FAST_PREVIEW_DELAY
            self._refined_preview_due_at = now + REFINED_PREVIEW_DELAY

    def _register_slate_tick(self):
        if self._slate_tick_handle is not None:
            return
        try:
            self._slate_tick_handle = unreal.register_slate_post_tick_callback(
                self._on_slate_tick
            )
        except Exception as exc:
            self.session.cleanup()
            raise RuntimeError(
                "Thumbnail Creator could not register its safe Unreal preview "
                "scheduler: %s" % exc
            ) from exc

    def _unregister_slate_tick(self):
        handle = self._slate_tick_handle
        self._slate_tick_handle = None
        if handle is None:
            return
        try:
            unreal.unregister_slate_post_tick_callback(handle)
        except Exception as exc:
            unreal.log_warning(
                "[ThumbnailCreator] Slate preview callback cleanup failed: %s"
                % exc
            )

    def _on_slate_tick(self, _delta_seconds):
        if self._closing:
            return
        now = time.monotonic()
        if now - self._last_viewport_poll >= 0.25:
            self._last_viewport_poll = now
            try:
                self._poll_viewport()
            except Exception as exc:
                unreal.log_warning(
                    "[ThumbnailCreator] Viewport polling failed: %s" % exc
                )
        fast_due_at = self._fast_preview_due_at
        if fast_due_at is not None and now >= fast_due_at:
            if self.preview_busy:
                self._fast_preview_due_at = now + FAST_PREVIEW_DELAY
                return
            self._fast_preview_due_at = None
            self._render_preview(refined=False)
            return
        refined_due_at = self._refined_preview_due_at
        if refined_due_at is None or now < refined_due_at:
            return
        if self.preview_busy:
            self._refined_preview_due_at = now + FAST_PREVIEW_DELAY
            return
        self._refined_preview_due_at = None
        self._render_preview(refined=True)

    @staticmethod
    def _lighting_label(metadata):
        effective = metadata.get("lighting") or {}
        mode = str(effective.get("mode") or "world").title()
        preset = ""
        if mode.casefold() == LightingMode.STUDIO.value:
            preset = str(
                effective.get("preset_name")
                or effective.get("effective_preset")
                or effective.get("preset")
                or ""
            )
        return "%s/%s" % (mode, preset) if preset else mode

    def _display_preview_frame(self, frame, request, phase, elapsed=None):
        image = frame.image
        display_image = (
            image
            if request.export.transparent
            else composite_background(image, request.export.background_color)
        )
        self.preview.set_pixmap(_pixmap_from_pillow(display_image))
        self.preview_128.setPixmap(
            _pixmap_from_pillow(
                display_image.resize((128, 128), Image.Resampling.LANCZOS)
            )
        )
        self.preview_64.setPixmap(
            _pixmap_from_pillow(
                display_image.resize((64, 64), Image.Resampling.LANCZOS)
            )
        )
        self.last_preview_image = image
        self.last_preview_metadata = copy.deepcopy(frame.result.metadata)
        elapsed = float(
            frame.result.elapsed_seconds if elapsed is None else elapsed
        )
        lighting_label = self._lighting_label(frame.result.metadata)
        if phase == "Export final":
            self.fps_label.setText(
                "%s | %.2fs render | %d px | %s"
                % (phase, elapsed, frame.result.output_size, lighting_label)
            )
        else:
            fps = 1.0 / elapsed if elapsed > 0 else 0.0
            self.fps_label.setText(
                "%s | %.1f FPS render | %d px | %s"
                % (phase, fps, frame.result.output_size, lighting_label)
            )

    def _render_preview(self, refined=False):
        if self.preview_busy or self.source is None:
            return
        self.preview_busy = True
        started = time.perf_counter()
        try:
            request = self._request(True, refined=refined)
            self.session.use_viewport_camera = self.wysiwyg.isChecked()
            frame = self.session.capture(request)
            if frame.result.metadata.get("auto_fitted"):
                self.camera = CameraState(**frame.result.metadata["fitted_camera"])
                self._write_controls()
            elapsed = time.perf_counter() - started
            self._display_preview_frame(
                frame,
                request,
                "Refined" if refined else "Fast",
                elapsed,
            )
        except Exception as exc:
            phase = "REFINED" if refined else "FAST"
            self._log("%s PREVIEW ERROR: %s" % (phase, exc))
        finally:
            self.preview_busy = False

    def _preview_background_changed(self, mode):
        self.preview.set_background_mode(mode)

    def _preview_dragged(self, dx, dy, mode):
        self._read_controls()
        if mode == "orbit":
            self.camera.yaw += dx * .35
            self.camera.pitch = max(-89.0, min(89.0, self.camera.pitch - dy * .35))
        elif mode == "pan":
            self.camera.pan_x += dx * .004
            self.camera.pan_y -= dy * .004
        else:
            self.camera.roll += dx * .35
        self._write_controls()
        self._camera_interacted()

    def _preview_wheeled(self, steps, change_fov):
        self._read_controls()
        if change_fov:
            self.camera.fov = max(5.0, min(120.0, self.camera.fov - steps * 2.0))
        else:
            self.camera.dolly = max(.02, min(20.0, self.camera.dolly * (0.88 ** steps)))
        self._write_controls()
        self._camera_interacted()

    def _camera_interacted(self):
        if self.wysiwyg.isChecked() and self.source:
            try:
                self._sync_guard = True
                self.session.push_camera_to_viewport(self.camera, self.source.kind)
            finally:
                self._sync_guard = False
        self._dirty()

    def _frame_control_changed(self, *_args):
        self._read_controls()
        self._camera_interacted()

    def _adjust_control_changed(self, *_args):
        self._read_controls()
        self._dirty()

    def _frame_source(self):
        self._read_controls()
        self.session.frame_source(self.camera)
        self._write_controls()
        self._camera_interacted()

    def _apply_recipe(self, name):
        recipes = {
            "Front": (0.0, 0.0, 0.0), "3/4": (35.0, 20.0, 0.0),
            "Top": (0.0, 89.0, 0.0), "Tilt": (35.0, 20.0, -12.0),
        }
        self.camera.yaw, self.camera.pitch, self.camera.roll = recipes[name]
        self.camera.pan_x = self.camera.pan_y = 0.0
        self.camera.dolly = 1.0
        self._write_controls()
        self._camera_interacted()

    def _wysiwyg_changed(self, enabled):
        self.session.use_viewport_camera = enabled
        if enabled:
            self.session.read_viewport_camera()
        self._dirty()

    def _poll_viewport(self):
        if not self.wysiwyg.isChecked() or self._sync_guard:
            return
        camera = self.session.read_viewport_camera()
        if camera:
            signature = "%s|%s|%.3f" % camera
            if signature != self._viewport_signature:
                self._viewport_signature = signature
                self._dirty()

    # ---------- adjustments/export ----------
    def _choose_outline(self):
        current = QColor(*self.outline_color)
        color = QColorDialog.getColor(current, self, "Outline color", QColorDialog.ShowAlphaChannel)
        if color.isValid():
            self.outline_color = (color.red(), color.green(), color.blue(), color.alpha())
            self.outline_button.setText(color.name().upper())
            self._adjust_control_changed()

    def _choose_background(self):
        color = QColor(*getattr(self, "background_color", (32, 32, 32, 255)))
        chosen = QColorDialog.getColor(color, self, "Solid background", QColorDialog.ShowAlphaChannel)
        if chosen.isValid():
            self.background_color = (chosen.red(), chosen.green(), chosen.blue(), chosen.alpha())
            self.background_button.setText(chosen.name().upper())
            self._dirty()

    def _reset_adjustments(self):
        self.adjust = AdjustState()
        self._write_controls()
        self._dirty()

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "PNG output folder", self.output_dir.text())
        if folder:
            self.output_dir.setText(folder)

    def _output_for_source(self, source, index=1, reserved=None):
        self._read_controls()
        source_path = source.paths[0] if source.paths else source.display_name
        stem = render_pattern(
            self.export.naming_pattern, source_path=source_path, index=index,
            preset=self.export.preset_name, size=self.export.output_size,
        )
        return unique_png_path(self.export.output_directory, stem, reserved)

    def _capture_export(self):
        if self.source is None:
            QMessageBox.warning(self, TOOL_NAME, "Select a capture source first.")
            return
        self._fast_preview_due_at = None
        self._refined_preview_due_at = None
        self.capture_button.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            request = self._request(False)
            output = self._output_for_source(self.source)
            frame = self.session.capture(request, output)
            if frame.result.metadata.get("auto_fitted"):
                self.camera = CameraState(**frame.result.metadata["fitted_camera"])
                self._write_controls()
            texture = ""
            import_info = None
            if request.export.import_texture:
                asset_name = safe_name(os.path.splitext(os.path.basename(output))[0])
                import_info = import_or_reimport_texture(output, request.export.import_path, asset_name)
                texture = import_info["texture_path"]
            frame.result.texture_path = texture
            self.last_png = output
            self._display_preview_frame(frame, request, "Export final")
            self._fast_preview_due_at = None
            self._refined_preview_due_at = None
            self._last_viewport_poll = time.monotonic()
            if self.wysiwyg.isChecked():
                viewport_camera = self.session.read_viewport_camera()
                if viewport_camera:
                    self._viewport_signature = "%s|%s|%.3f" % viewport_camera
            self._add_library_item(request, frame.result, import_info)
            self._log("DONE: %s%s" % (output, " -> " + texture if texture else ""))
            self._save_session()
        except Exception as exc:
            unreal.log_error("[ThumbnailCreator] Capture failed: %s" % exc)
            self._log("ERROR: %s" % exc)
            QMessageBox.critical(self, TOOL_NAME, str(exc))
        finally:
            QApplication.restoreOverrideCursor()
            self.capture_button.setEnabled(True)

    def _save_last_png(self):
        if not self.last_png or not os.path.isfile(self.last_png):
            self._capture_export()
            return
        destination, _filter = QFileDialog.getSaveFileName(
            self,
            "Save last PNG",
            self.last_png,
            "PNG image (*.png)",
        )
        if destination:
            if not destination.lower().endswith(".png"):
                destination += ".png"
            shutil.copy2(self.last_png, destination)
            self._log("Last PNG copied to: %s" % destination)

    # ---------- batch ----------
    def _prepare_batch_assets(self):
        sources = selected_asset_sources()
        self._prepare_batch(sources, [])

    def _prepare_batch_folder(self):
        sources, ignored = selected_folder_sources(True)
        self._prepare_batch(sources, ignored)

    def _prepare_batch(self, sources, ignored):
        if not sources:
            QMessageBox.warning(self, TOOL_NAME, "No supported assets were found in the selection.")
            return
        self.batch_rows = []
        self.batch_cancel = False
        cache_dir = os.path.join(self.store.root, "batch_cache")
        os.makedirs(cache_dir, exist_ok=True)
        self.batch_progress.setRange(0, len(sources))
        self.batch_progress.setValue(0)
        started = time.perf_counter()
        for index, source in enumerate(sources, 1):
            row = {"source": source, "status": "pending", "cache": "", "error": ""}
            try:
                request = self._request(False)
                request.source = copy.deepcopy(source)
                cache = os.path.join(cache_dir, "%03d_%s.png" % (index, safe_name(source.display_name)))
                frame = self.session.capture(request, cache)
                row.update(status="ready", cache=cache, result=frame.result)
            except Exception as exc:
                row.update(status="failed", error=str(exc))
            self.batch_rows.append(row)
            self.batch_progress.setValue(index)
            elapsed = time.perf_counter() - started
            remaining = elapsed / index * (len(sources) - index)
            self.batch_eta.setText("%d/%d • ETA %.1fs" % (index, len(sources), remaining))
            QApplication.processEvents()
            if self.batch_cancel:
                self.batch_eta.setText("Cancelled after %d/%d" % (index, len(sources)))
                break
        self.batch_ignored.setText("Ignored: %d%s" % (len(ignored), " • " + ", ".join(ignored[:4]) if ignored else ""))
        self._refresh_batch_table()

    def _refresh_batch_table(self):
        self.batch_table.setRowCount(len(self.batch_rows))
        for row_index, row in enumerate(self.batch_rows):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            check.setCheckState(Qt.Checked if row["status"] == "ready" else Qt.Unchecked)
            self.batch_table.setItem(row_index, 0, check)
            source = row["source"]
            self.batch_table.setItem(row_index, 1, QTableWidgetItem(source.paths[0] if source.paths else source.display_name))
            self.batch_table.setItem(row_index, 2, QTableWidgetItem(row["status"] + (": " + row.get("error", "") if row.get("error") else "")))
            self.batch_table.setItem(row_index, 3, QTableWidgetItem(row.get("cache", "")))

    def _export_batch_checked(self):
        if not self.batch_rows:
            return
        reserved = set()
        exported = 0
        for index, row in enumerate(self.batch_rows, 1):
            item = self.batch_table.item(index - 1, 0)
            if not item or item.checkState() != Qt.Checked or row["status"] != "ready":
                continue
            try:
                request = self._request(False)
                request.source = copy.deepcopy(row["source"])
                output = self._output_for_source(row["source"], index, reserved)
                os.makedirs(os.path.dirname(output), exist_ok=True)
                shutil.copy2(row["cache"], output)
                texture = ""
                import_info = None
                if request.export.import_texture:
                    import_info = import_or_reimport_texture(output, request.export.import_path, os.path.splitext(os.path.basename(output))[0])
                    texture = import_info["texture_path"]
                result = row["result"]
                result.png_path = output
                result.texture_path = texture
                self._add_library_item(request, result, import_info)
                row["status"] = "exported"
                exported += 1
            except Exception as exc:
                row["status"] = "failed"
                row["error"] = str(exc)
            QApplication.processEvents()
        self._refresh_batch_table()
        self._log("Batch: %d exported/imported." % exported)

    def _retry_batch_failures(self):
        failures = [row for row in self.batch_rows if row["status"] == "failed"]
        if not failures:
            self._log("Batch: no failures to retry.")
            return
        self.batch_cancel = False
        for row in failures:
            try:
                request = self._request(False)
                request.source = copy.deepcopy(row["source"])
                cache = row.get("cache") or os.path.join(self.store.root, "batch_cache", safe_name(row["source"].display_name) + ".png")
                frame = self.session.capture(request, cache)
                row.update(status="ready", cache=cache, result=frame.result, error="")
            except Exception as exc:
                row.update(status="failed", error=str(exc))
            QApplication.processEvents()
            if self.batch_cancel:
                break
        self._refresh_batch_table()

    # ---------- presets ----------
    def _preset_payload(self):
        request = self._request(False)
        payload = request.to_dict()
        lighting = payload["lighting"]
        if self.preset_scope.currentData() == "whole_view":
            lighting = LightingState(mode=LightingMode.WORLD).to_dict()
        return {
            "camera": payload["camera"],
            "adjust": payload["adjust"],
            "lighting": lighting,
            "export": payload["export"],
        }

    def _refresh_presets(self, *_args):
        if not hasattr(self, "preset_list"):
            return
        scope = self.preset_scope.currentData()
        self.preset_list.clear()
        for name in sorted(self.presets.get(scope, {}), key=str.lower):
            self.preset_list.addItem(name)

    def _ask_preset_name(self, title, initial=""):
        name, ok = QInputDialog.getText(self, title, "Preset name", text=initial)
        return name.strip() if ok else ""

    def _create_preset(self):
        name = self._ask_preset_name("Create preset")
        if not name:
            return
        scope = self.preset_scope.currentData()
        self.presets.setdefault(scope, {})[name] = self._preset_payload()
        self.store.save_presets(self.presets)
        self._refresh_presets()

    def _selected_preset_name(self):
        item = self.preset_list.currentItem()
        return item.text() if item else ""

    def _duplicate_preset(self):
        old = self._selected_preset_name()
        if not old:
            return
        name = self._ask_preset_name("Duplicate preset", old + " Copy")
        if not name:
            return
        scope = self.preset_scope.currentData()
        self.presets[scope][name] = copy.deepcopy(self.presets[scope][old])
        self.store.save_presets(self.presets)
        self._refresh_presets()

    def _update_preset(self):
        name = self._selected_preset_name()
        if not name:
            return
        scope = self.preset_scope.currentData()
        self.presets[scope][name] = self._preset_payload()
        self.store.save_presets(self.presets)

    def _delete_preset(self):
        name = self._selected_preset_name()
        if not name:
            return
        scope = self.preset_scope.currentData()
        del self.presets[scope][name]
        self.store.save_presets(self.presets)
        self._refresh_presets()

    def _apply_selected_preset(self):
        name = self._selected_preset_name()
        if not name:
            return
        scope = self.preset_scope.currentData()
        payload = self.presets[scope][name]
        self.camera = CameraState(**payload.get("camera", {}))
        adjust = dict(payload.get("adjust", {}))
        if "outline_color" in adjust:
            adjust["outline_color"] = tuple(adjust["outline_color"])
        self.adjust = AdjustState(**adjust)
        self.lighting = LightingState.from_dict(
            payload.get("lighting"),
            default_mode=LightingMode.WORLD,
        )
        export = dict(payload.get("export", {}))
        if "background_color" in export:
            export["background_color"] = tuple(export["background_color"])
        self.export = ExportOptions(**export)
        self.active_preset = name
        self._lighting_baseline = None
        self._write_controls()
        self.output_dir.setText(self.export.output_directory or self.store.root)
        self.naming.setText(self.export.naming_pattern)
        self._dirty()

    # ---------- library ----------
    def _add_library_item(self, request, result, import_info):
        item = {
            "name": os.path.splitext(os.path.basename(result.png_path))[0],
            "png_path": result.png_path,
            "texture_path": result.texture_path,
            "source": request.source.to_dict(),
            "source_key": request.source.key,
            "uefn_source": request.source.paths[0] if request.source.paths else request.source.display_name,
            "folder": "",
            "tags": [],
            "preset": request.export.preset_name,
            "lighting": dict(result.metadata.get("lighting") or request.lighting.to_dict()),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "import": import_info or {},
        }
        self.library = [existing for existing in self.library if existing.get("png_path") != result.png_path]
        self.library.append(item)
        self.store.save_library(self.library)
        self._refresh_library()

    def _refresh_library(self, *_args):
        if not hasattr(self, "library_table"):
            return
        query = self.library_search.text().strip().lower()
        visible = []
        for item in self.library:
            haystack = " ".join([
                str(item.get("name", "")), str(item.get("uefn_source", "")), str(item.get("texture_path", "")),
                str(item.get("folder", "")), " ".join(item.get("tags") or []),
            ]).lower()
            if not query or query in haystack:
                visible.append(item)
        self._visible_library = visible
        self.library_table.setRowCount(len(visible))
        for row, item in enumerate(visible):
            values = [item.get("name", ""), item.get("folder", ""), ", ".join(item.get("tags") or []), item.get("uefn_source", ""), item.get("texture_path", "")]
            for column, value in enumerate(values):
                self.library_table.setItem(row, column, QTableWidgetItem(str(value)))

    def _selected_library_item(self):
        row = self.library_table.currentRow()
        if row < 0 or row >= len(getattr(self, "_visible_library", [])):
            return None
        return self._visible_library[row]

    def _library_reveal(self):
        item = self._selected_library_item()
        if item and os.path.isfile(item.get("png_path", "")):
            subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(item["png_path"])])

    def _library_select_texture(self):
        item = self._selected_library_item()
        if not item:
            return
        texture = unreal.EditorAssetLibrary.load_asset(item.get("texture_path", ""))
        if texture:
            unreal.EditorUtilityLibrary.sync_browser_to_objects([texture])

    def _library_copy_path(self):
        item = self._selected_library_item()
        if item:
            QApplication.clipboard().setText(item.get("texture_path") or item.get("png_path", ""))

    def _library_regenerate(self):
        item = self._selected_library_item()
        if not item:
            return
        try:
            self._set_source(CaptureSource.from_dict(item["source"]))
            self.tabs.setCurrentIndex(0)
            self._capture_export()
        except Exception as exc:
            QMessageBox.critical(self, TOOL_NAME, str(exc))

    def _library_edit_metadata(self):
        item = self._selected_library_item()
        if not item:
            return
        folder, ok = QInputDialog.getText(self, "Library folder", "Folder", text=item.get("folder", ""))
        if not ok:
            return
        tags, ok = QInputDialog.getText(self, "Library tags", "Comma-separated tags", text=", ".join(item.get("tags") or []))
        if not ok:
            return
        item["folder"] = folder.strip()
        item["tags"] = [tag.strip() for tag in tags.split(",") if tag.strip()]
        self.store.save_library(self.library)
        self._refresh_library()

    def closeEvent(self, event):
        if not self._confirm_lighting_changes(revert_on_discard=True):
            event.ignore()
            return
        self._closing = True
        self._fast_preview_due_at = None
        self._refined_preview_due_at = None
        self._unregister_slate_tick()
        try:
            self._save_session()
        finally:
            self.session.cleanup()
        app = QApplication.instance()
        if app and getattr(app, WINDOW_REF, None) is self:
            setattr(app, WINDOW_REF, None)
        super().closeEvent(event)


def launch_ui():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    existing = getattr(app, WINDOW_REF, None)
    if existing is not None and existing.isVisible():
        existing.raise_()
        existing.activateWindow()
        return existing
    window = ThumbnailCreatorWindow()
    setattr(app, WINDOW_REF, window)
    window.show()
    window.raise_()
    window.activateWindow()
    return window
