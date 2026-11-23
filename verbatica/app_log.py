"""The app's one log file, Verbatica.log in the data folder. Always on, trims itself to the
newest ~1 MB, shows the home folder as ~ and never leaves the computer. Also builds the
View Logs and Report a Problem actions, which only open the file manager or the browser."""

from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import urllib.parse
from datetime import datetime
from pathlib import Path

from verbatica import APP_NAME, REPOSITORY, __version__

LOG_FILE_NAME = f"{APP_NAME}.log"
MAX_BYTES = 1_000_000
KEEP_BYTES = 500_000
TRIM_MARKER = "--- older entries removed ---"
SESSION_SEPARATOR = "=" * 72
SESSION_ENDED = "Session ended"

log = logging.getLogger("verbatica.app_log")

_handler: TrimmedFileHandler | None = None
_fault_file = None
_ended = False


class TrimmedFileHandler(logging.FileHandler):
    """One log file that drops its oldest lines when it grows past max_bytes."""

    def __init__(self, path, max_bytes=MAX_BYTES, keep_bytes=KEEP_BYTES):
        super().__init__(path, encoding="utf-8")
        self.max_bytes, self.keep_bytes = max_bytes, keep_bytes
        self._trim()

    def emit(self, record):
        super().emit(record)
        try:
            if self.stream and os.fstat(self.stream.fileno()).st_size > self.max_bytes:
                self._trim()
        except OSError:
            pass

    def _trim(self):
        self.stream.close()
        path = Path(self.baseFilename)
        if path.stat().st_size > self.max_bytes:
            tail = path.read_bytes()[-self.keep_bytes:]
            tail = tail[tail.find(b"\n") + 1:]
            path.write_bytes(TRIM_MARKER.encode() + b"\n" + tail)
        self.stream = self._open()


def _home_pattern(home: str, windows: bool) -> re.Pattern:
    """The home folder, matched with either slash direction (and any case) on Windows."""
    if windows:
        parts = [re.escape(p) for p in re.split(r"[\\/]", home) if p]
        return re.compile(r"[\\/]".join(parts), re.IGNORECASE)
    return re.compile(re.escape(home))


def hide_home(text: str, home: str | None = None, windows: bool | None = None) -> str:
    home = str(Path.home()) if home is None else home
    windows = sys.platform == "win32" if windows is None else windows
    if not home or home in ("/", "\\"):
        return text
    return _home_pattern(home.rstrip("\\/"), windows).sub("~", text)


class LogFormatter(logging.Formatter):
    """2026-11-18 14:03:22.481 +00:00 INFO    [models] Message, with the home folder as ~."""

    def format(self, record):
        moment = datetime.fromtimestamp(record.created).astimezone()
        offset = moment.strftime("%z")
        stamp = f"{moment:%Y-%m-%d %H:%M:%S}.{int(record.msecs):03d} {offset[:3]}:{offset[3:]}"
        area = record.name.rsplit(".", 1)[-1] if record.name.startswith("verbatica") else record.name
        text = f"{stamp} {record.levelname:<7} [{area}] {record.getMessage()}"
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            text += "\n" + self.formatStack(record.stack_info)
        return hide_home(text)


def build_kind() -> str:
    kind = "development" if __version__.startswith("0.0.0") else (
        "branch build" if "-" in __version__ else "release")
    where = "built app" if getattr(sys, "frozen", False) else "from source"
    return f"{kind}, {where}"


def os_name() -> str:
    """The OS as people name it: "macOS 26.1", "Windows 11 (build 26100)", "Ubuntu 24.04.1 LTS"."""
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]}".strip()
    if sys.platform == "win32":
        build = sys.getwindowsversion().build
        return f"Windows {'11' if build >= 22000 else platform.release()} (build {build})"
    try:
        return platform.freedesktop_os_release()["PRETTY_NAME"]
    except (AttributeError, OSError, KeyError):
        return f"Linux {platform.release()}"


def previous_session_ended(path: Path) -> bool:
    """False when the last session in the file has no "Session ended" line (a crash or force quit)."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, path.stat().st_size - 200_000))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return True
    last = text.rfind(SESSION_SEPARATOR)
    if last < 0:
        return True
    return SESSION_ENDED in text[last:]


def start(data_dir: Path) -> Path | None:
    """Set up logging first thing at start, before the window. Returns the log file, or None
    when it can't be written (the app then carries on without a log)."""
    global _handler, _fault_file, _ended
    path = Path(data_dir) / LOG_FILE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ended_cleanly = not path.exists() or previous_session_ended(path)
        handler = TrimmedFileHandler(path)
    except OSError:
        return None
    handler.setFormatter(LogFormatter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    _handler, _ended = handler, False
    logging.captureWarnings(True)

    handler.stream.write(SESSION_SEPARATOR + "\n")
    handler.flush()
    log.info("%s %s (%s)", APP_NAME, __version__, build_kind())
    log.info("%s, %s; Python %s", os_name(), platform.machine(), platform.python_version())
    log.info("Data folder: %s", data_dir)
    if not ended_cleanly:
        log.warning("The previous session did not close normally (crash or force quit)")

    install_error_hooks()
    try:
        _fault_file = open(path, "a", encoding="utf-8")
        faulthandler.enable(file=_fault_file)
    except OSError:
        _fault_file = None
    atexit.register(end_session)
    return path


def log_environment(app, changed_settings: dict) -> None:
    """The rest of the header, once Qt is running: toolkit, language, screens and settings."""
    from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, QLocale

    log.info("Qt %s, PyQt %s", QT_VERSION_STR, PYQT_VERSION_STR)
    locale = QLocale.system()
    log.info("Language %s, region %s", locale.name(), QLocale.territoryToString(locale.territory()))
    screens = app.screens()
    scales = ", ".join(f"{s.size().width()}x{s.size().height()} @{s.devicePixelRatio():g}x" for s in screens)
    log.info("%d screen(s): %s", len(screens), scales)
    if changed_settings:
        for key, value in changed_settings.items():
            log.info("Setting %s = %s", key, value)
    else:
        log.info("All settings at their defaults")


def install_error_hooks() -> None:
    """Send every unhandled error to the log: the main thread (which covers Qt slots),
    background threads and Qt's own messages."""
    previous = sys.excepthook

    def on_error(kind, value, tb):
        logging.getLogger("verbatica").error("Unhandled error", exc_info=(kind, value, tb))
        previous(kind, value, tb)

    sys.excepthook = on_error

    def on_thread_error(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "unknown"
        logging.getLogger("verbatica").error("Unhandled error in thread %s", name,
                                             exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = on_thread_error

    try:
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return
    qt_log = logging.getLogger("qt")
    levels = {QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
              QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
              QtMsgType.QtFatalMsg: logging.CRITICAL}

    def on_qt_message(kind, _context, message):
        qt_log.log(levels.get(kind, logging.WARNING), message)

    qInstallMessageHandler(on_qt_message)


def end_session() -> None:
    global _ended
    if _handler is None or _ended:
        return
    _ended = True
    log.info(SESSION_ENDED)
    _handler.flush()


def log_file() -> Path | None:
    return Path(_handler.baseFilename) if _handler is not None else None


def flush() -> None:
    if _handler is not None:
        _handler.flush()


# --- View Logs and Report a Problem -------------------------------------------

def reveal_command(path: Path, platform_name: str = sys.platform) -> list:
    """The command that opens the file manager with the file selected (the folder on Linux)."""
    if platform_name == "darwin":
        return ["open", "-R", str(path)]
    if platform_name == "win32":
        return ["explorer", "/select,", os.path.normpath(str(path))]  # same form as Show in Finder
    return ["xdg-open", str(Path(path).parent)]


def file_manager_name(platform_name: str = sys.platform) -> str:
    return {"darwin": "Finder", "win32": "File Explorer"}.get(platform_name, "the file manager")


def reveal(path: Path) -> bool:
    """Start the file manager without waiting for it. False when it could not be started.
    Explorer's exit code is 1 even when it worked, so the exit code is not checked."""
    command = reveal_command(path)
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        log.warning("Could not run %s", command[0], exc_info=True)
        return False
    log.info("View Logs: ran %s", command[0])
    return True


def report_url(version: str = __version__, os_text: str | None = None) -> str:
    """The bug form on GitHub with only the version and OS filled in."""
    query = urllib.parse.urlencode({"template": "bug.yml", "version": version,
                                    "os": os_text if os_text is not None else os_name()})
    return f"https://github.com/{REPOSITORY}/issues/new?{query}"
