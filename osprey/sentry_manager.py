"""Multi-camera Sentry patrol manager.

The engine in :mod:`sentry` runs one tour.  A controller that serves more than
one camera needs one tour per camera, each with its own points, cadence and
speed, and each with its own scheduler thread — so this module owns the set of
runtimes and the policy around them.

Every patrol shares the one movement gate with the operator console, with MQTT
and with autotracking.  That is what makes a patrol safe: the gate is the same
object, so a patrol move and a person's move can never overlap, and the
manual hold that stops autotracking stops a patrol in exactly the same way.

The manager knows nothing about HTTP, MQTT or Frigate.  It asks the embedding
application three questions about a camera — may I move it, is an autotracker
driving it, is it idle — and reports back what it did.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping

from sentry_runtime import MovementGate, SentryConfig, SentryRuntime

log = logging.getLogger("osprey.sentry.manager")


class SentryStore:
    """Durable per-camera patrol configuration.

    The operator config file is the baseline.  This file holds the changes an
    operator made at runtime — through the API, the console or MQTT — so a
    restart does not silently drop a patrol that was running.  It lives in the
    state directory because that is the one place that survives a restart and
    is writable.

    The file holds preset names, intervals and speeds.  Never credentials.
    """

    FILE_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, dict[str, object]]:
        """The stored per-camera settings; empty when there are none."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable sentry file %s: %s", self.path, exc)
            return {}
        cameras = raw.get("cameras") if isinstance(raw, dict) else None
        if not isinstance(cameras, dict):
            return {}
        return {
            str(key): value
            for key, value in cameras.items()
            if isinstance(value, dict)
        }

    def save(self, cameras: Mapping[str, Mapping[str, object]]) -> bool:
        """Replace the stored settings.  Returns False (and logs) on failure."""
        document = {
            "version": self.FILE_VERSION,
            "cameras": {key: dict(value) for key, value in cameras.items()},
        }
        temporary: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".sentry-", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=1, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, self.path)
            temporary = None
        except OSError as exc:
            log.error("cannot persist sentry settings to %s: %s", self.path, exc)
            return False
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
        return True


class SentryManager:
    """Owns one patrol per camera.

    ``goto_preset(camera, name, speed)`` must move the camera to a *named*
    preset and report whether the move went out.  It returns ``False`` for a
    refusal and may raise ``BlockingIOError`` when the camera is momentarily
    busy, which defers the move without consuming a point of the tour.
    """

    def __init__(
        self,
        goto_preset: Callable[[str, str, int], bool],
        tracking_active: Callable[[str, float], bool],
        camera_idle: Callable[[str], bool],
        gate: MovementGate | None = None,
        preset_names: Callable[[str], list[str]] | None = None,
        store: SentryStore | None = None,
        is_night: Callable[[str], bool] | None = None,
    ) -> None:
        self._is_night = is_night
        self._goto_preset = goto_preset
        self._tracking_active = tracking_active
        self._camera_idle = camera_idle
        self._preset_names = preset_names
        self.store = store
        self.gate = gate or MovementGate()
        self._lock = threading.RLock()
        self._configs: dict[str, SentryConfig] = {}
        self._runtimes: dict[str, SentryRuntime] = {}
        # Cameras an operator removed.  A tombstone keeps the patrol gone even
        # if the config file still lists it.
        self._removed: set[str] = set()

    # ------------------------------------------------------------ configuration

    def configure(self, camera: str, config: SentryConfig, *, persist: bool = True) -> None:
        """Install or replace one camera's patrol and restart its scheduler.

        Replacing a patrol restarts its tour from the first point.  That is
        deliberate: an operator who changes the tour expects the new tour to
        begin, not to join one halfway through.
        """
        names = [point.name.strip().casefold() for point in config.points]
        if any(not name for name in names):
            raise ValueError("patrol point names must be non-empty")
        if len(set(names)) != len(names):
            raise ValueError("patrol point names must be unique")
        with self._lock:
            self._stop_runtime(camera)
            self._configs[camera] = config
            self._removed.discard(camera)
            if persist:
                self._persist()
            if not config.enabled or not config.points:
                log.info(
                    "sentry patrol for %s is configured but not running (%d points)",
                    camera, len(config.points),
                )
                return

            def goto_preset(name: str, speed: int) -> bool:
                return self._goto_preset(camera, name, speed)

            def tracking_active() -> bool:
                return self._tracking_active(camera, config.tracking_hold_sec)

            def camera_idle() -> bool:
                return self._camera_idle(camera)

            runtime = SentryRuntime(
                config,
                goto_preset=goto_preset,
                tracking_active=tracking_active,
                camera_idle=camera_idle,
                gate=self.gate,
                camera_key=camera,
                is_night=(lambda: is_night(camera)) if (is_night := self._is_night) else None,
            )
            self._runtimes[camera] = runtime
            runtime.start()
            log.info(
                "sentry patrol for %s started: %d points, every %.0fs, speed %d",
                camera, len(config.points), config.interval_sec, config.speed,
            )

    def enable(self, camera: str) -> None:
        """Start a configured patrol.  Enabling an enabled patrol does nothing."""
        config = self._require(camera)
        with self._lock:
            if config.enabled and camera in self._runtimes:
                return
        self.configure(camera, replace(config, enabled=True))

    def disable(self, camera: str) -> None:
        """Stop a patrol.  Its points are kept, so it can be started again."""
        config = self._require(camera)
        with self._lock:
            if not config.enabled and camera not in self._runtimes:
                return
        self.configure(camera, replace(config, enabled=False))

    def remove(self, camera: str) -> None:
        """Forget a camera's patrol entirely.

        A tombstone is written to the state file, so the patrol does not come
        back from the config file on the next start.
        """
        with self._lock:
            self._stop_runtime(camera)
            self._configs.pop(camera, None)
            self._removed.add(camera)
            self._persist()

    def _persist(self) -> None:
        """Write the per-camera settings to the state directory."""
        if self.store is None:
            return
        payload: dict[str, dict[str, object]] = {
            key: config.as_dict() for key, config in self._configs.items()
        }
        for camera in self._removed:
            payload.setdefault(camera, {"enabled": False, "points": []})
        self.store.save(payload)

    def config(self, camera: str) -> SentryConfig | None:
        with self._lock:
            return self._configs.get(camera)

    def restore(self, baseline: Mapping[str, Mapping[str, object]]) -> None:
        """Install the patrols a previous run left behind, over the baseline.

        The state file is the operator's latest word: a camera it mentions
        uses that configuration, and a camera it removed stays removed.  A
        camera it does not mention falls back to the config file.
        """
        stored = self.store.load() if self.store is not None else {}
        merged: dict[str, dict[str, object]] = {}
        for camera, raw in baseline.items():
            entry = stored.get(camera)
            merged[camera] = dict(entry) if entry is not None else dict(raw)
        for camera, entry in stored.items():
            merged.setdefault(camera, dict(entry))
        for camera, entry in merged.items():
            if not entry.get("points"):
                with self._lock:
                    self._removed.add(camera)
                continue
            self.configure(camera, SentryConfig.from_mapping(entry), persist=False)

    def cameras(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._configs))

    def _require(self, camera: str) -> SentryConfig:
        config = self.config(camera)
        if config is None:
            raise KeyError(camera)
        return config

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        with self._lock:
            for runtime in self._runtimes.values():
                runtime.start()

    def stop(self) -> None:
        with self._lock:
            for runtime in list(self._runtimes.values()):
                runtime.stop()

    def _stop_runtime(self, camera: str) -> None:
        runtime = self._runtimes.pop(camera, None)
        if runtime is not None:
            runtime.stop()

    # ------------------------------------------------------------------- status

    def snapshot(self, camera: str | None = None) -> dict[str, object]:
        """The patrol state of one camera, or of every camera."""
        with self._lock:
            if camera is not None:
                return self._camera_body(camera)
            return {
                "cameras": {key: self._camera_body(key) for key in sorted(self._configs)},
                "movement": self.gate.snapshot(),
            }

    def _camera_body(self, camera: str) -> dict[str, object]:
        config = self._configs.get(camera)
        runtime = self._runtimes.get(camera)
        if runtime is not None:
            state: dict[str, object] = runtime.patrol.snapshot().as_dict()
        else:
            state = {
                "enabled": False,
                "state": "disabled",
                "point": None,
                "points": [point.name for point in (config.points if config else ())],
                "next_move_at": None,
                "last_error": None,
                "moves": 0,
            }
        return {
            "camera": camera,
            "config": config.as_dict() if config is not None else None,
            "state": state,
            "presets": self._preset_list(camera),
            "movement": self.gate.snapshot(),
        }

    def _preset_list(self, camera: str) -> list[str]:
        if self._preset_names is None:
            return []
        try:
            return list(self._preset_names(camera))
        except Exception as exc:  # a camera that is not adopted yet has no table
            log.debug("no preset table for %s: %s", camera, exc)
            return []
