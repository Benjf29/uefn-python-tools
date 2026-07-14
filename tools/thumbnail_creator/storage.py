from __future__ import annotations

import json
import os
import shutil
import copy
import uuid
from datetime import datetime
from typing import Any


SCHEMA_VERSION = 2
CUSTOM_LIGHTING_PRESET_PREFIX = "custom:"


class LightingPresetError(ValueError):
    pass


def default_saved_directory() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    content_dir = os.path.dirname(os.path.dirname(script_dir))
    project_dir = os.path.dirname(content_dir)
    if os.path.basename(content_dir).lower() == "content":
        return os.path.join(project_dir, "Saved", "ThumbnailCreator")
    try:
        import unreal

        return os.path.abspath(
            os.path.join(unreal.Paths.project_saved_dir(), "ThumbnailCreator")
        )
    except Exception:
        return os.path.abspath(os.path.join(os.getcwd(), "Saved", "ThumbnailCreator"))


class JsonStore:
    def __init__(self, root: str | None = None):
        self.root = os.path.abspath(root or default_saved_directory())
        os.makedirs(self.root, exist_ok=True)

    def path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def read(self, name: str, default: Any) -> Any:
        path = self.path(name)
        if not os.path.isfile(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
            if not isinstance(document, dict):
                raise ValueError("The JSON root must be an object")
            if int(document.get("schema_version", 0)) > SCHEMA_VERSION:
                raise ValueError("Unsupported future schema version")
            return document.get("data", default)
        except Exception:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = "%s.corrupt_%s" % (path, stamp)
            try:
                shutil.copy2(path, backup)
            except Exception:
                pass
            return default

    def write(self, name: str, data: Any) -> str:
        path = self.path(name)
        temp = path + ".tmp"
        document = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "data": data,
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temp, path)
        return path


class ThumbnailCreatorStore:
    def __init__(self, root: str | None = None):
        self.json = JsonStore(root)

    @property
    def root(self) -> str:
        return self.json.root

    def load_presets(self) -> dict[str, dict[str, Any]]:
        default = {"objects": {}, "whole_view": {}}
        data = self.json.read("presets.json", default)
        if not isinstance(data, dict):
            return default
        data.setdefault("objects", {})
        data.setdefault("whole_view", {})
        return data

    def save_presets(self, data: dict[str, Any]) -> str:
        return self.json.write("presets.json", data)

    def load_lighting_presets(self) -> dict[str, dict[str, Any]]:
        data = self.json.read("lighting_presets.json", {})
        if not isinstance(data, dict):
            return {}
        valid = {}
        for identifier, raw in data.items():
            if not isinstance(raw, dict):
                continue
            preset_id = str(raw.get("id") or identifier).strip()
            name = str(raw.get("name") or "").strip()
            lighting = raw.get("lighting")
            if (
                not preset_id
                or not name
                or len(name) > 64
                or not isinstance(lighting, dict)
                or not isinstance(lighting.get("rig"), dict)
            ):
                continue
            record = copy.deepcopy(raw)
            record["id"] = preset_id
            record["name"] = name
            valid[preset_id] = record
        return valid

    def save_lighting_presets(self, data: dict[str, Any]) -> str:
        return self.json.write("lighting_presets.json", data)

    @staticmethod
    def _lighting_preset_name(
        name: str,
        records: dict[str, dict[str, Any]],
        exclude_id: str = "",
    ) -> str:
        normalized = str(name or "").strip()
        if not normalized:
            raise LightingPresetError("Preset name is required.")
        if len(normalized) > 64:
            raise LightingPresetError("Preset name cannot exceed 64 characters.")
        folded = normalized.casefold()
        for identifier, record in records.items():
            if identifier != exclude_id and str(record.get("name", "")).casefold() == folded:
                raise LightingPresetError("A lighting preset with this name already exists.")
        return normalized

    @staticmethod
    def _lighting_payload(
        identifier: str,
        name: str,
        lighting: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(lighting, dict) or not isinstance(lighting.get("rig"), dict):
            raise LightingPresetError("A custom lighting preset requires a complete rig snapshot.")
        payload = copy.deepcopy(lighting)
        payload["mode"] = "studio"
        payload["preset"] = CUSTOM_LIGHTING_PRESET_PREFIX + identifier
        payload["preset_name"] = name
        return payload

    def create_lighting_preset(
        self,
        name: str,
        lighting: dict[str, Any],
    ) -> dict[str, Any]:
        records = self.load_lighting_presets()
        name = self._lighting_preset_name(name, records)
        identifier = uuid.uuid4().hex
        now = datetime.now().isoformat(timespec="seconds")
        record = {
            "id": identifier,
            "name": name,
            "lighting": self._lighting_payload(identifier, name, lighting),
            "created_at": now,
            "updated_at": now,
        }
        records[identifier] = record
        self.save_lighting_presets(records)
        return copy.deepcopy(record)

    def update_lighting_preset(
        self,
        identifier: str,
        *,
        name: str | None = None,
        lighting: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        identifier = str(identifier or "").strip()
        records = self.load_lighting_presets()
        if identifier not in records:
            raise LightingPresetError("The lighting preset no longer exists.")
        record = records[identifier]
        next_name = self._lighting_preset_name(
            record["name"] if name is None else name,
            records,
            exclude_id=identifier,
        )
        source_lighting = record["lighting"] if lighting is None else lighting
        record["name"] = next_name
        record["lighting"] = self._lighting_payload(
            identifier, next_name, source_lighting
        )
        record["updated_at"] = datetime.now().isoformat(timespec="seconds")
        records[identifier] = record
        self.save_lighting_presets(records)
        return copy.deepcopy(record)

    def rename_lighting_preset(
        self,
        identifier: str,
        name: str,
    ) -> dict[str, Any]:
        return self.update_lighting_preset(identifier, name=name)

    def delete_lighting_preset(self, identifier: str) -> bool:
        identifier = str(identifier or "").strip()
        records = self.load_lighting_presets()
        if identifier not in records:
            return False
        del records[identifier]
        self.save_lighting_presets(records)
        return True

    def load_session(self) -> dict[str, Any]:
        data = self.json.read("last_session.json", {})
        return data if isinstance(data, dict) else {}

    def save_session(self, data: dict[str, Any]) -> str:
        return self.json.write("last_session.json", data)

    def load_library(self) -> list[dict[str, Any]]:
        data = self.json.read("library.json", [])
        return data if isinstance(data, list) else []

    def save_library(self, data: list[dict[str, Any]]) -> str:
        return self.json.write("library.json", data)


