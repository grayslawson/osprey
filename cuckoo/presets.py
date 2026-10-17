"""Durable named PTZ positions.

The camera keeps preset slots in its own flash, but it has no observed verb for
listing them back, so the slot table Osprey advertises over ONVIF used to live
only in memory and vanished on every restart (Frigate then reported its
``return_preset`` as invalid). This store keeps that table in a small JSON file
in Osprey's writable state directory, keyed by camera MAC, and is re-read when
a camera connects.

The file holds motor coordinates and operator-chosen names only; no secrets.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Final

from model import Position, Preset

log = logging.getLogger("cuckoo.presets")

FILE_VERSION: Final = 1
MAX_PRESETS_PER_CAMERA: Final = 64
MAX_NAME_LENGTH: Final = 64
HOME_PRESET_NAME: Final = "home"


def is_home(name: str) -> bool:
    return name.strip().casefold() == HOME_PRESET_NAME


class PresetStore:
    """Atomic JSON persistence for ``{mac: {index: Preset}}``.

    A missing, unreadable, or malformed file is treated as empty (and logged)
    rather than preventing the controller from starting.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable preset file %s: %s", self.path, exc)
            return {}
        cameras = raw.get("cameras") if isinstance(raw, dict) else None
        return cameras if isinstance(cameras, dict) else {}

    def load(self, mac: str) -> dict[int, Preset]:
        with self._lock:
            entries = self._read().get(mac)
        out: dict[int, Preset] = {}
        if not isinstance(entries, dict):
            return out
        for key, value in entries.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict) or not 0 <= index <= 255:
                continue
            name = value.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_LENGTH:
                continue
            axes = [value.get(axis) for axis in ("pan", "tilt", "zoom", "focus")]
            if not all(isinstance(a, int) and not isinstance(a, bool) for a in axes):
                continue
            pan, tilt, zoom, focus = (int(a) for a in axes)  # type: ignore[arg-type]
            out[index] = Preset(index=index, name=name, position=Position(pan, tilt, zoom, focus))
            if len(out) >= MAX_PRESETS_PER_CAMERA:
                break
        return out

    def save(self, mac: str, presets: dict[int, Preset]) -> bool:
        """Replace one camera's table. Returns False (and logs) on I/O failure."""
        entries = {
            str(index): {
                "name": preset.name,
                "pan": preset.position.pan,
                "tilt": preset.position.tilt,
                "zoom": preset.position.zoom,
                "focus": preset.position.focus,
            }
            for index, preset in sorted(presets.items())
        }
        with self._lock:
            cameras = self._read()
            cameras[mac] = entries
            document = {"version": FILE_VERSION, "cameras": cameras}
            temporary: str | None = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".presets-", dir=self.path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(document, handle, indent=1, sort_keys=True)
                    handle.write("\n")
                os.replace(temporary, self.path)
                temporary = None
            except OSError as exc:
                log.error("cannot persist presets to %s: %s", self.path, exc)
                return False
            finally:
                if temporary is not None and os.path.exists(temporary):
                    os.unlink(temporary)
        return True
