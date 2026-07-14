from __future__ import annotations

import importlib
import os
import site
import subprocess
import sys

from .storage import default_saved_directory


RUNTIME_DEPENDENCIES = {
    "Pillow": ("PIL.Image", "Pillow>=10.4,<13"),
    "PySide6": ("PySide6.QtWidgets", "PySide6>=6.5,<7"),
}


def package_directory() -> str:
    return os.path.join(default_saved_directory(), "python_packages")


def activate_vendor_path() -> str:
    path = package_directory()
    os.makedirs(path, exist_ok=True)
    if path not in sys.path:
        site.addsitedir(path)
    return path


def pillow_available() -> bool:
    activate_vendor_path()
    try:
        import PIL  # noqa: F401

        return True
    except ImportError:
        return False


def missing_runtime_dependencies() -> list[str]:
    activate_vendor_path()
    missing = []
    for display_name, (module_name, _requirement) in RUNTIME_DEPENDENCIES.items():
        try:
            importlib.import_module(module_name)
        except (ImportError, AttributeError):
            missing.append(display_name)
    return missing


def _embedded_python() -> str:
    try:
        import unreal

        engine = unreal.Paths.convert_relative_path_to_full(unreal.Paths.engine_dir())
        candidate = os.path.join(
            engine, "Binaries", "ThirdParty", "Python3", "Win64", "python.exe"
        )
        if os.path.isfile(candidate):
            return candidate
    except Exception:
        pass
    if os.path.basename(sys.executable).lower().startswith("python"):
        return sys.executable
    raise RuntimeError("UEFN's bundled python.exe could not be located.")


def install_pillow() -> str:
    """Install Pillow beside ThumbnailCreator's Saved data, never into Fortnite files."""
    return install_runtime_dependencies(["Pillow"])


def install_runtime_dependencies(names: list[str] | None = None) -> str:
    """Install missing UI/image dependencies into the tool-owned Saved folder."""
    target = activate_vendor_path()
    python_exe = _embedded_python()
    selected = names or list(RUNTIME_DEPENDENCIES)
    unknown = [name for name in selected if name not in RUNTIME_DEPENDENCIES]
    if unknown:
        raise ValueError("Unknown runtime dependencies: %s" % ", ".join(unknown))
    requirements = [RUNTIME_DEPENDENCIES[name][1] for name in selected]
    command = [
        python_exe,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--upgrade",
        "--target",
        target,
    ] + requirements
    subprocess.check_call(command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    importlib.invalidate_caches()
    remaining = [name for name in selected if name in missing_runtime_dependencies()]
    if remaining:
        raise RuntimeError(
            "Dependency installation completed but imports still fail: %s"
            % ", ".join(remaining)
        )
    return target

