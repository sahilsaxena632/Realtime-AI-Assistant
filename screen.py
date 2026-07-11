"""Periodic screen capture producing an in-memory 'latest frame'.

A background thread grabs the screen every SCREEN_CAPTURE_INTERVAL seconds
using the first available backend (cosmic-screenshot on COSMIC/Wayland, then
grim, gnome-screenshot, scrot, or a SCREEN_CAPTURE_CMD override), downsizes it
with Pillow and keeps only the latest JPEG in memory. Nothing persists on disk:
each grab writes one temp file into config.SCREENSHOT_DIR and deletes it after
encoding.

Providers call latest_frame(max_age_sec) to get the current screen as JPEG
bytes, or None when disabled/stale/unavailable.
"""

import io
import os
import shlex
import shutil
import subprocess
import threading
import time
import uuid

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

import config
from logutil import log


class ScreenCapture:
    def __init__(self, interval=None):
        self.interval = interval or config.SCREEN_CAPTURE_INTERVAL
        self._frame = None
        self._frame_ts = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._paused = False
        self._thread = None
        self._backend = self._detect_backend()
        self._last_err_log = 0.0

    def _detect_backend(self):
        if config.SCREEN_CAPTURE_CMD and "{path}" in config.SCREEN_CAPTURE_CMD:
            return "custom"
        if shutil.which("cosmic-screenshot"):
            return "cosmic-screenshot"
        if shutil.which("grim"):
            return "grim"
        if shutil.which("gnome-screenshot"):
            return "gnome-screenshot"
        if shutil.which("scrot"):
            return "scrot"
        return None

    def start(self):
        if self._running:
            return
        if self._backend is None:
            log(
                "screen: no capture backend found "
                "(install grim or gnome-screenshot, or set SCREEN_CAPTURE_CMD)"
            )
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log(
            f"screen: capture started backend={self._backend} "
            f"interval={self.interval}s"
        )

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        with self._lock:
            self._frame = None
            self._frame_ts = 0.0
        log("screen: capture stopped")

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def _loop(self):
        while self._running:
            if not self._paused:
                try:
                    jpeg = self._grab()
                    if jpeg:
                        with self._lock:
                            self._frame = jpeg
                            self._frame_ts = time.time()
                except Exception as e:
                    now = time.time()
                    if now - self._last_err_log > 30:
                        self._last_err_log = now
                        log(f"screen: capture failed: {e}")
            time.sleep(self.interval)

    def _grab(self):
        if Image is None:
            raise RuntimeError("Pillow not installed")

        path = os.path.join(config.SCREENSHOT_DIR, f".cap-{uuid.uuid4().hex}.png")

        if self._backend == "custom":
            cmd = shlex.split(config.SCREEN_CAPTURE_CMD.replace("{path}", path))
        elif self._backend == "cosmic-screenshot":
            cmd = [
                "cosmic-screenshot",
                "--interactive=false",
                "--notify=false",
                "-s",
                config.SCREENSHOT_DIR,
            ]
        elif self._backend == "grim":
            cmd = ["grim", path]
        elif self._backend == "gnome-screenshot":
            cmd = ["gnome-screenshot", "-f", path]
        elif self._backend == "scrot":
            cmd = ["scrot", "-o", path]
        else:
            raise RuntimeError(f"unknown backend {self._backend!r}")

        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "")[:200]
            raise RuntimeError(
                f"{self._backend} exited {proc.returncode}: {stderr}"
            )

        if self._backend == "cosmic-screenshot":
            actual = proc.stdout.strip()
        else:
            actual = path

        if not actual or not os.path.isfile(actual):
            raise RuntimeError("no screenshot file produced")

        try:
            img = Image.open(actual)
            img = img.convert("RGB")
            img.thumbnail((config.SCREEN_MAX_DIM, config.SCREEN_MAX_DIM))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=config.SCREEN_JPEG_QUALITY)
            return buf.getvalue()
        finally:
            try:
                os.remove(actual)
            except Exception:
                pass

    def latest_frame(self, max_age_sec=None):
        max_age = max_age_sec or config.SCREEN_FRAME_MAX_AGE_SEC
        with self._lock:
            if self._frame is None:
                return None
            if time.time() - self._frame_ts > max_age:
                return None
            return self._frame

    def capture_once(self):
        if self._backend is None:
            return False, "no capture backend found"
        try:
            jpeg = self._grab()
            w, h = Image.open(io.BytesIO(jpeg)).size
            return (
                True,
                f"backend={self._backend} resolution={w}x{h} "
                f"jpeg_kb={len(jpeg) // 1024}",
            )
        except Exception as e:
            return False, str(e)
