"""Tests for the log file: trimming, the home folder shown as ~, the session header, the
error hooks, View Logs and the Report a Problem link."""

import faulthandler
import logging
import sys
import threading
import urllib.parse

import pytest
from PyQt6.QtWidgets import QMessageBox

from verbatica import __version__, app, app_log


@pytest.fixture
def started_log(tmp_path):
    """Start logging into a temporary data folder, and undo every global change afterwards."""
    hooks = (sys.excepthook, threading.excepthook)
    root = logging.getLogger()
    level = root.level

    def start():
        return app_log.start(tmp_path / "data")

    yield start
    app_log.end_session()
    if app_log._handler is not None:
        root.removeHandler(app_log._handler)
        app_log._handler.close()
        app_log._handler = None
    if app_log._fault_file is not None:
        faulthandler.disable()
        app_log._fault_file.close()
        app_log._fault_file = None
    sys.excepthook, threading.excepthook = hooks
    root.setLevel(level)
    logging.captureWarnings(False)
    from PyQt6.QtCore import qInstallMessageHandler
    qInstallMessageHandler(None)


def test_trimming_keeps_the_newest_whole_lines(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("".join(f"line {i:05d}\n" for i in range(3000)))  # 33 000 bytes
    handler = app_log.TrimmedFileHandler(path, max_bytes=10_000, keep_bytes=5_000)
    lines = path.read_text().splitlines()
    assert lines[0] == app_log.TRIM_MARKER
    assert lines[1].startswith("line ") and len(lines[1]) == 10  # starts at a whole line
    assert lines[-1] == "line 02999"
    assert path.stat().st_size < 6_000

    logger = logging.getLogger("trim-test")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        for i in range(1000):
            logger.warning("new %05d", i)
    finally:
        logger.removeHandler(handler)
        handler.close()
    text = path.read_text()
    assert text.startswith(app_log.TRIM_MARKER + "\n")
    assert text.splitlines()[-1] == "new 00999"
    assert path.stat().st_size <= 10_000


def test_home_folder_is_hidden_on_every_platform():
    assert app_log.hide_home("/Users/alex/Music/talk.mp3", "/Users/alex", False) == "~/Music/talk.mp3"
    windows_home = "C:\\Users\\Alex"
    assert app_log.hide_home("C:\\Users\\Alex\\Music\\a.mp3", windows_home, True) == "~\\Music\\a.mp3"
    assert app_log.hide_home("c:/users/alex/Music/a.mp3", windows_home, True) == "~/Music/a.mp3"


def test_header_names_the_version_build_and_os(tmp_path, started_log):
    path = started_log()
    text = path.read_text()
    assert app_log.SESSION_SEPARATOR in text
    assert f"Verbatica {__version__} (development, from source)" in text
    assert app_log.os_name() in text
    assert "did not close normally" not in text


def test_a_session_without_an_end_line_is_reported_next_time(tmp_path, started_log):
    data = tmp_path / "data"
    data.mkdir()
    (data / app_log.LOG_FILE_NAME).write_text(app_log.SESSION_SEPARATOR + "\nsomething\n")
    text = started_log().read_text()
    assert "WARNING [app_log] The previous session did not close normally" in text


def test_a_clean_close_is_not_reported(tmp_path, started_log):
    data = tmp_path / "data"
    data.mkdir()
    (data / app_log.LOG_FILE_NAME).write_text(app_log.SESSION_SEPARATOR + "\nx\nSession ended\n")
    assert "did not close normally" not in started_log().read_text()


def test_lines_have_local_time_level_area_and_no_home_folder(tmp_path, started_log):
    from pathlib import Path
    path = started_log()
    logging.getLogger("verbatica.models").info("Folder %s", Path.home() / "Models")
    line = path.read_text().splitlines()[-1]
    assert " INFO    [models] Folder ~" in line
    assert str(Path.home()) not in line
    # 2026-11-18 14:03:22.481 +00:00
    assert line[4] == "-" and line[19] == "." and line[24] in "+-" and line[27] == ":"


def test_unhandled_errors_reach_the_log_with_a_traceback(tmp_path, started_log, monkeypatch):
    path = started_log()
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: None)

    try:
        raise ValueError("main thread boom")
    except ValueError:
        sys.excepthook(*sys.exc_info())

    def fail():
        raise RuntimeError("thread boom")
    thread = threading.Thread(target=fail, name="worker")
    thread.start()
    thread.join()

    text = path.read_text()
    assert "ERROR   [verbatica] Unhandled error\nTraceback" in text and "main thread boom" in text
    assert "Unhandled error in thread worker" in text and "RuntimeError: thread boom" in text


def test_session_ended_is_written_once(tmp_path, started_log):
    path = started_log()
    app_log.end_session()
    app_log.end_session()
    assert path.read_text().count("Session ended") == 1


def test_view_logs_command_for_each_platform(tmp_path):
    file = tmp_path / "Verbatica.log"
    assert app_log.reveal_command(file, "darwin") == ["open", "-R", str(file)]
    assert app_log.reveal_command(file, "win32")[:2] == ["explorer", "/select,"]
    assert app_log.reveal_command(file, "linux") == ["xdg-open", str(tmp_path)]
    assert app_log.file_manager_name("darwin") == "Finder"
    assert app_log.file_manager_name("win32") == "File Explorer"


def test_view_logs_shows_the_path_when_the_file_manager_fails(tmp_path, started_log, monkeypatch):
    from test_app import make_window
    started_log()
    calls = []
    monkeypatch.setattr(app_log.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    window = make_window(tmp_path)
    window.view_logs()
    assert calls and calls[0][-1].endswith(("Verbatica.log", "data"))

    def broken(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(app_log.subprocess, "Popen", broken)
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.informativeText()))
    window.view_logs()
    assert shown and "Verbatica.log" in shown[0]


def test_report_a_problem_fills_in_only_version_and_os():
    url = app_log.report_url("0.0.0.dev0", "macOS 26.1")
    assert url.startswith("https://github.com/TariqMK/Verbatica/issues/new?")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query == {"template": ["bug.yml"], "version": ["0.0.0.dev0"], "os": ["macOS 26.1"]}
    assert "macOS+26.1" in url or "macOS%2026.1" in url


def test_help_menu_and_settings_have_view_logs_and_report(tmp_path, monkeypatch):
    from test_app import make_window
    window = make_window(tmp_path)
    titles = [a.text() for m in window.menuBar().actions() for a in m.menu().actions()]
    assert "View Logs" in titles and "Report a Problem…" in titles
    opened = []
    monkeypatch.setattr(app.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    window.show_settings()
    window.settings_window.report_btn.click()
    assert opened and "template=bug.yml" in opened[0]
