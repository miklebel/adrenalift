"""
startup_task.py -- Manage a Windows Task Scheduler entry for run-on-startup.

Uses a fixed task name so that different exe builds (Adrenalift_0.1_11,
Adrenalift_0.1_12, ...) all share one startup slot.  When the current
exe path differs from the stored path, the task is silently updated.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

_log = logging.getLogger("overclock.startup_task")

TASK_NAME = "Adrenalift_Startup"


def _current_exe() -> str:
    """Return the full path to the running executable."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(sys.argv[0])


def get_startup_task_exe() -> str | None:
    """Return the exe path registered in the startup task, or None."""
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"],
            capture_output=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as exc:
        _log.warning("schtasks query failed: %s", exc)
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if "Task To Run:" in line:
            path = line.split(":", 1)[1].strip().strip('"')
            return path
    return None


def is_startup_enabled() -> bool:
    """True if a startup task exists (regardless of which exe it points to)."""
    return get_startup_task_exe() is not None


def enable_startup(exe_path: str | None = None) -> bool:
    """Create or overwrite the startup task pointing to *exe_path*."""
    exe_path = exe_path or _current_exe()
    try:
        r = subprocess.run(
            [
                "schtasks", "/Create", "/F",
                "/TN", TASK_NAME,
                "/TR", f'"{exe_path}"',
                "/SC", "ONLOGON",
                "/RL", "HIGHEST",
            ],
            capture_output=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            _log.info("Startup task created/updated -> %s", exe_path)
            return True
        _log.warning("schtasks /Create failed (%d): %s", r.returncode, r.stderr.strip())
        return False
    except OSError as exc:
        _log.error("Failed to create startup task: %s", exc)
        return False


def disable_startup() -> bool:
    """Remove the startup task."""
    try:
        r = subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            capture_output=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            _log.info("Startup task removed")
            return True
        _log.warning("schtasks /Delete failed (%d): %s", r.returncode, r.stderr.strip())
        return False
    except OSError as exc:
        _log.error("Failed to delete startup task: %s", exc)
        return False


def ensure_startup_points_to_current() -> None:
    """If a startup task exists but points to a different exe, update it."""
    registered = get_startup_task_exe()
    if registered is None:
        return
    current = _current_exe()
    if os.path.normcase(os.path.normpath(registered)) != os.path.normcase(os.path.normpath(current)):
        _log.info("Startup task points to stale exe (%s), updating to %s", registered, current)
        enable_startup(current)
