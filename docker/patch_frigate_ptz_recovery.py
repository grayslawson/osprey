"""Patch the pinned Frigate image so one ONVIF fault cannot kill autotracking.

Frigate 0.18.0-rc1 has a single movement-queue coroutine.  A transient SOAP
fault escapes that coroutine and leaves ``OnvifController.active`` set, so all
later detections enqueue movement forever without dispatching it.  This build
time patch is deliberately guarded by exact anchors: an upstream source change
must fail the image build instead of silently producing an unknown patch.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one source anchor, found {count}")
    return source.replace(old, new, 1)


def patch_onvif(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    new = '''        try:
            await self.cams[camera_name]["ptz"].RelativeMove(move_request)
        finally:
            # A SOAP/network fault must not leave every later move suppressed.
            move_request.Translation.PanTilt.x = 0
            move_request.Translation.PanTilt.y = 0
            if "Zoom" in move_request.Translation:
                del move_request["Translation"]["Zoom"]
            self.cams[camera_name]["active"] = False
'''
    start = source.index(
        '        await self.cams[camera_name]["ptz"].RelativeMove(move_request)'
    )
    active = source.index(
        '        self.cams[camera_name]["active"] = False', start
    )
    end = source.index("\n", active) + 1
    source = source[:start] + new + source[end:]

    new = '''        try:
            await self.cams[camera_name]["ptz"].GotoPreset(
                {
                    "ProfileToken": move_request.ProfileToken,
                    "PresetToken": preset_token,
                }
            )
        finally:
            self.cams[camera_name]["active"] = False
'''
    start = source.index(
        '        await self.cams[camera_name]["ptz"].GotoPreset(', end
    )
    active = source.index(
        '        self.cams[camera_name]["active"] = False', start
    )
    end = source.index("\n", active) + 1
    source = source[:start] + new + source[end:]
    path.write_text(source, encoding="utf-8")


def patch_autotrack(path: Path) -> None:
    source = path.read_text(encoding="utf-8")

    queue_start = source.index("    async def _process_move_queue(self, camera):")
    enqueue_start = source.index("    def _enqueue_move(", queue_start)
    queue_source = source[queue_start:enqueue_start]
    lock_start = queue_source.index("            async with self.move_queue_locks[camera]:")
    cleanup_start = queue_source.index(
        "        while not move_queue.empty():", lock_start
    )
    one_move = queue_source[lock_start:cleanup_start]
    one_move = "\n".join(
        line[4:] if line.startswith("    ") else line for line in one_move.splitlines()
    ).rstrip()
    one_move_lines = one_move.splitlines()
    continue_lines = [
        index for index, line in enumerate(one_move_lines) if line.strip() == "continue"
    ]
    if len(continue_lines) != 1:
        raise RuntimeError(
            "stale-move exit: expected one continue anchor, "
            f"found {len(continue_lines)}"
        )
    index = continue_lines[0]
    one_move_lines[index] = one_move_lines[index].replace("continue", "return")
    one_move = "\n".join(one_move_lines)

    replacement = '''            try:
                await self._process_one_move(camera, move_data)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "%s: ONVIF movement failed; resynchronizing and continuing queue",
                    camera,
                )
                try:
                    await asyncio.wait_for(
                        self.onvif.get_camera_status(camera), timeout=3.0
                    )
                except Exception:
                    logger.warning(
                        "%s: ONVIF status resynchronization failed", camera,
                        exc_info=True,
                    )

'''
    queue_source = queue_source[:lock_start] + replacement + queue_source[cleanup_start:]

    # Replace the two unbounded status loops inside one move with one bounded helper.
    wait_pattern = re.compile(
        r'(?m)^(?P<indent> +)while not self\.ptz_metrics\[camera\]\.motor_stopped\.is_set\(\):\n'
        r'(?P=indent)    await self\.onvif\.get_camera_status\(camera\)\n'
    )
    one_move, wait_count = wait_pattern.subn(
        lambda match: f'{match.group("indent")}await self._wait_for_motor_stop(camera)\n',
        one_move,
    )
    if wait_count != 2:
        raise RuntimeError(
            "movement status waits: expected two source anchors, "
            f"found {wait_count}"
        )

    helpers = f'''    async def _wait_for_motor_stop(self, camera, timeout=15.0):
        deadline = self.onvif.loop.time() + timeout
        while not self.ptz_metrics[camera].motor_stopped.is_set():
            remaining = deadline - self.onvif.loop.time()
            if remaining <= 0:
                raise TimeoutError(f"{{camera}}: timed out waiting for truthful IDLE")
            await asyncio.wait_for(
                self.onvif.get_camera_status(camera), timeout=min(3.0, remaining)
            )
            if not self.ptz_metrics[camera].motor_stopped.is_set():
                await asyncio.sleep(0.1)

    async def _process_one_move(self, camera, move_data):
{one_move}

'''
    source = source[:queue_start] + queue_source + helpers + source[enqueue_start:]

    # Returning home races the queue in upstream Frigate.  Use the same lock,
    # clear the status event before dispatch, and keep every wait bounded.
    maintenance_start = source.index("    async def camera_maintenance(self, camera):")
    first_wait = source.index(
        "            while not self.ptz_metrics[camera].motor_stopped.is_set():",
        maintenance_start,
    )
    tracking_clear = source.index(
        "            self.ptz_metrics[camera].tracking_active.clear()", first_wait
    )
    old_return = source[first_wait:tracking_clear]
    expected_markers = (
        "await self.onvif._move_to_preset(",
        "returning to preset",
    )
    if not all(marker in old_return for marker in expected_markers):
        raise RuntimeError("return-preset serialization: source anchors changed")
    new_return = '''            async with self.move_queue_locks[camera]:
                try:
                    await self._wait_for_motor_stop(camera)
                    logger.debug(
                        f"{camera}: Time is "
                        f"{self.ptz_metrics[camera].frame_time.value}, "
                        f"returning to preset: {autotracker_config.return_preset}"
                    )
                    self.ptz_metrics[camera].motor_stopped.clear()
                    await self.onvif._move_to_preset(
                        camera, autotracker_config.return_preset.lower()
                    )
                    await self._wait_for_motor_stop(camera)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "%s: return-to-preset failed; continuing maintenance", camera
                    )
                    try:
                        await asyncio.wait_for(
                            self.onvif.get_camera_status(camera), timeout=3.0
                        )
                    except Exception:
                        logger.warning(
                            "%s: preset status resynchronization failed", camera,
                            exc_info=True,
                        )

'''
    source = source[:first_wait] + new_return + source[tracking_clear:]
    path.write_text(source, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default="/opt/frigate")
    args = parser.parse_args()
    root = Path(args.root)
    patch_onvif(root / "frigate" / "ptz" / "onvif.py")
    patch_autotrack(root / "frigate" / "ptz" / "autotrack.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
