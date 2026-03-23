"""
Adrenalift -- Logging Infrastructure
=====================================

File logger, global/thread/Qt exception hooks, atexit handler.
Imported early in the startup sequence (before heavy Qt or engine imports).
"""

from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import sys
import threading
import traceback

from src.app.constants import _script_dir

from PySide6.QtCore import QtMsgType, qInstallMessageHandler

# ---------------------------------------------------------------------------
# File logger -- appends timestamped messages to overclock_log.txt
# ---------------------------------------------------------------------------

_LOG_FILE = os.path.join(_script_dir, "overclock_log.txt")
_file_logger = logging.getLogger("overclock")
_file_logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(_LOG_FILE, mode="a", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_file_logger.addHandler(_fh)
_file_logger.info("=" * 60)
_file_logger.info("Session started")

try:
    _fault_fh = open(_LOG_FILE, "a", encoding="utf-8")
    faulthandler.enable(file=_fault_fh, all_threads=True)
except Exception:
    faulthandler.enable(all_threads=True)


def _log_to_file(msg: str):
    """Write a single log line to the persistent log file."""
    try:
        _file_logger.info(msg)
        _fh.flush()
    except Exception:
        pass


def _log_exception_to_file(context: str = ""):
    """Log the current exception traceback to the persistent log file."""
    try:
        tb = traceback.format_exc()
        _file_logger.error(f"EXCEPTION ({context}):\n{tb}")
        _fh.flush()
    except Exception:
        pass


def _install_global_exception_hook():
    """Replace sys.excepthook so unhandled exceptions are logged to file."""
    _original_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        try:
            tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            _file_logger.critical(f"UNHANDLED EXCEPTION:\n{tb_text}")
            _fh.flush()
        except Exception:
            pass
        _original_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook

_install_global_exception_hook()


def _install_threading_exception_hook():
    """Catch unhandled exceptions on non-main threads (Python 3.8+)."""
    def _thread_hook(args):
        try:
            tb_text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            _file_logger.critical(
                f"UNHANDLED THREAD EXCEPTION (thread={args.thread}):\n{tb_text}"
            )
            _fh.flush()
        except Exception:
            pass
    threading.excepthook = _thread_hook

_install_threading_exception_hook()


def _install_qt_message_handler():
    """Redirect Qt internal warnings/errors to the log file."""
    _msg_type_names = {
        QtMsgType.QtDebugMsg: "QtDebug",
        QtMsgType.QtInfoMsg: "QtInfo",
        QtMsgType.QtWarningMsg: "QtWarning",
        QtMsgType.QtCriticalMsg: "QtCritical",
        QtMsgType.QtFatalMsg: "QtFatal",
    }
    def _handler(msg_type, context, message):
        label = _msg_type_names.get(msg_type, f"Qt({msg_type})")
        loc = ""
        if context.file:
            loc = f" [{context.file}:{context.line}]"
        try:
            _file_logger.warning(f"{label}{loc}: {message}")
            if msg_type in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
                _fh.flush()
        except Exception:
            pass
    qInstallMessageHandler(_handler)

_install_qt_message_handler()


# ---------------------------------------------------------------------------
# Atexit handler
# ---------------------------------------------------------------------------

_atexit_clean = False


def set_atexit_clean(clean: bool = True):
    """Mark the session as cleanly exited (called from main())."""
    global _atexit_clean
    _atexit_clean = clean


def _atexit_handler():
    if _atexit_clean:
        _file_logger.info("Session ended (clean exit)")
    else:
        _file_logger.critical("Session ended (atexit without clean flag — possible crash or kill)")
    _fh.flush()

atexit.register(_atexit_handler)
