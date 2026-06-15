"""Tiny thread-safe logger that writes to LOG_FILE.

After the daemon double-forks there is no stdout/stderr, so everything must go
to the log file. Before forking (and in foreground modes) we also echo to
stderr so the user can see what is happening.
"""

import sys
import threading
import time

import config

_lock = threading.Lock()
_echo_stderr = True  # disabled by the daemonizer after fork


def set_echo(enabled: bool) -> None:
    global _echo_stderr
    _echo_stderr = enabled


def log(msg: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    with _lock:
        try:
            config.ensure_dirs()
            with open(config.LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        if _echo_stderr:
            try:
                sys.stderr.write(line + "\n")
                sys.stderr.flush()
            except Exception:
                pass
