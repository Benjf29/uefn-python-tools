"""Standalone UEFN launcher for ThumbnailCreator.

Run with ``Tools > Execute Python Script...``.  The launcher also registers a
session-persistent entry in UEFN's Tools menu.
"""

from __future__ import annotations

import os
import sys

import unreal


TOOL_NAME = "Thumbnail Creator"
WINDOW_REF = "_thumbnail_creator_window"
MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

# Always resolve package imports next to this launcher.
_expected_module_dir = os.path.normcase(os.path.realpath(MODULE_DIR))
sys.path[:] = [
    entry
    for entry in sys.path
    if os.path.normcase(os.path.realpath(os.path.abspath(entry or os.curdir)))
    != _expected_module_dir
]
sys.path.insert(0, MODULE_DIR)


def _message(kind: str, text: str) -> None:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication([])
        getattr(QMessageBox, kind)(None, TOOL_NAME, text)
    except Exception:
        logger = "error" if kind == "critical" else "warning"
        getattr(unreal, "log_%s" % logger)(text)


def ensure_runtime_ready() -> bool:
    from thumbnail_creator.pillow_support import (
        install_runtime_dependencies,
        missing_runtime_dependencies,
    )

    missing = missing_runtime_dependencies()
    if not missing:
        return True
    unreal.log(
        "[ThumbnailCreator] Installing missing runtime dependencies in "
        "Saved/ThumbnailCreator/python_packages: %s" % ", ".join(missing)
    )
    try:
        target = install_runtime_dependencies(missing)
    except Exception as exc:
        _message("critical", "Automatic dependency installation failed:\n\n%s" % exc)
        return False
    _message(
        "information",
        "Dependencies were installed successfully in:\n%s\n\n"
        "Restart UEFN once, then launch Thumbnail Creator again." % target,
    )
    return False


def register_tools_menu() -> bool:
    try:
        menus = unreal.ToolMenus.get()
        menu = menus.extend_menu("LevelEditor.MainMenu.Tools")
        entry = unreal.ToolMenuEntry(
            name="ThumbnailCreator",
            type=unreal.MultiBlockType.MENU_ENTRY,
        )
        entry.set_label(TOOL_NAME)
        entry.set_tool_tip(
            "Open the thumbnail capture, batch, preset, and library tool."
        )
        entry.set_string_command(
            unreal.ToolMenuStringCommandType.PYTHON,
            "",
            "import runpy; runpy.run_path(%r, run_name='__main__')"
            % os.path.abspath(__file__),
        )
        menu.add_menu_entry("ThumbnailCreator", entry)
        menus.refresh_all_widgets()
        return True
    except Exception as exc:
        unreal.log_warning(
            "[ThumbnailCreator] Tools menu registration failed: %s" % exc
        )
        return False


def main():
    if not ensure_runtime_ready():
        return None

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    existing = getattr(app, WINDOW_REF, None)
    if existing is not None and existing.isVisible():
        existing.raise_()
        existing.activateWindow()
        return existing

    from thumbnail_creator.ui import launch_ui

    register_tools_menu()
    window = launch_ui()
    unreal.log(
        "[ThumbnailCreator] Runtime root: %s" % MODULE_DIR
    )
    return window


register_tools_menu()

if __name__ == "__main__":
    main()
