"""End-to-end check of Verbatica: model download and load, transcription to .txt/.srt/.vtt, duplicate skip,
no overwrite, Stop mid-file, a failed file with its error log, settings and the log file, on copies of a sample file.

It drives the real main window (offscreen) with the real faster-whisper model, the same way a user does,
and checks each step's effect on disk, not only the window's state. It only touches the work folder.

Verbatica has no local API or automation mode, so this runs from source and needs the app's own
dependencies (pip install .). It does not test a frozen build; the build's --self-test covers that.

Usage:
  python scripts/check_e2e.py --sample <speech .wav> --expect <word> --work <new empty folder>
  Leave out --sample to use a made-up tone (checks run, but no words are expected).
  Leave out --work to use a new temporary folder. --models reuses a folder of already downloaded models.
"""
import argparse, hashlib, json, math, os, shutil, struct, sys, tempfile, time, wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- Fill in for this app ----------------------------------------------------------------------------
APP_NAME = "Verbatica"
SAMPLE_NAME = "sample.wav"  # Made-up 3 s tone, when no --sample is given
MODEL = "tiny"
FORMATS = [".txt", ".srt", ".vtt"]
# -----------------------------------------------------------------------------------------------------

failures = []


def check(name, ok, detail=""):
    """Print PASS or FAIL for one step and remember failures."""
    print(("PASS " if ok else "FAIL ") + name + (f" -- {detail}" if detail and not ok else ""), flush=True)
    if not ok:
        failures.append(name)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def make_work_folder(path):
    """Use a new temporary folder, or the given one only if it's new or empty, so real files are never touched."""
    if path is None:
        return Path(tempfile.mkdtemp(prefix="check_e2e_"))
    work = Path(path)
    if work.exists() and any(work.iterdir()):
        sys.exit(f"Work folder must be new or empty: {work}")
    work.mkdir(parents=True, exist_ok=True)
    return work


def write_tone(path, seconds=3):
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
        f.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000)))
                               for i in range(16000 * seconds)))


def write_repeated(source, path, times):
    """A long WAV made of the sample repeated, for the Stop check."""
    with wave.open(str(source), "rb") as src:
        params, frames = src.getparams(), src.readframes(src.getnframes())
    gap = b"\x00" * (params.sampwidth * params.nchannels * params.framerate // 2)
    with wave.open(str(path), "wb") as dst:
        dst.setparams(params)
        dst.writeframes((frames + gap) * times)


parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--sample", help="Speech sample (.wav for the Stop check); default is a made-up tone")
parser.add_argument("--expect", action="append", default=[], help="A word the transcript must contain (repeatable)")
parser.add_argument("--work", help="New or empty work folder; default is a new temporary folder")
parser.add_argument("--models", help="Folder of already downloaded models to reuse; default downloads into the work folder")
parser.add_argument("--timeout", type=int, default=600, help="Seconds allowed for each wait")
args = parser.parse_args()

work = make_work_folder(args.work)
inputs = work / "input"; inputs.mkdir()
copy = inputs / (Path(args.sample).name if args.sample else SAMPLE_NAME)
if args.sample:
    shutil.copyfile(args.sample, copy)
else:
    write_tone(copy)
orig = sha(copy)
print(f"Work folder: {work}")

# --- App checks --------------------------------------------------------------------------------------
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from verbatica import APP_NAME as REAL_NAME, app as va, app_log  # noqa: E402
from verbatica.settings import SettingsStore  # noqa: E402

# A dialog would block the check: answer any question with No and record it as a failure.
dialogs = []
QMessageBox.question = staticmethod(lambda *a, **k: (dialogs.append(a[1:3]), QMessageBox.StandardButton.No)[1])
QMessageBox.warning = QMessageBox.critical = staticmethod(lambda *a, **k: dialogs.append(a[1:3]))

check("App name", REAL_NAME == APP_NAME, REAL_NAME)
qa = QApplication([])
store = SettingsStore(work / "app", legacy_file=work / "no-legacy.json")
log_path = app_log.start(store.data_dir)
w = va.MainWindow(store, dark=True)
out = work / "out"
w.settings.models_dir = str(Path(args.models) if args.models else work / "models")
w.settings.output_dir = str(out)
w.settings.error_log_dir = str(work / "errors")
w.settings.output_formats = FORMATS
w.save_settings()
w.show()


def wait(condition, what):
    start = time.time()
    while not condition():
        qa.processEvents(); time.sleep(0.02)
        if time.time() - start > args.timeout:
            check(what, False, f"timed out after {args.timeout} s")
            return False
    for _ in range(10):
        qa.processEvents(); time.sleep(0.01)
    return True


def idle():
    return w.worker is None or not w.worker.isRunning()


# 1. Model download and load
w.model_combo.setCurrentIndex(w.model_combo.findData(MODEL))
w.download_and_load_model()
t = time.time()
wait(lambda: w.model_loaded or w.download_worker is None, "Model loads")
check(f"Model {MODEL} downloads and loads", w.model_loaded and w.loaded_model_name == MODEL, w.model_status)
print(f"     model ready in {time.time() - t:.1f} s")
if not w.model_loaded:
    print(f"RESULT: FAIL ({len(failures)} failed)"); sys.exit(1)

# 2. Transcribe the sample
w.add_files([str(copy)])
w.start_transcription()
wait(lambda: idle() and w.rows[0].state in ("done", "failed"), "Transcription finishes")
row = w.rows[0]
check("Sample transcribed", row.state == "done", f"state {row.state}")
for ext in FORMATS:
    check(f"{ext} saved", (out / f"{copy.stem}{ext}").is_file())
txt = (out / f"{copy.stem}.txt").read_text(encoding="utf-8") if (out / f"{copy.stem}.txt").exists() else ""
srt = (out / f"{copy.stem}.srt").read_text(encoding="utf-8") if (out / f"{copy.stem}.srt").exists() else ""
vtt = (out / f"{copy.stem}.vtt").read_text(encoding="utf-8") if (out / f"{copy.stem}.vtt").exists() else ""
print(f"     transcript: {txt.strip()[:200]!r}")
for word in args.expect:
    check(f"Transcript contains {word!r}", word.lower() in txt.lower(), txt[:200])
if args.sample:
    check(".srt has numbered cues with --> times", srt.startswith("1\n") and " --> " in srt and "," in srt, srt[:80])
    check(".vtt starts with WEBVTT and has cues", vtt.startswith("WEBVTT") and " --> " in vtt, vtt[:80])
check("Sample unchanged after transcription", sha(copy) == orig)

# 3. Duplicate skipped; Transcribe skips files already done
w.add_files([str(copy)])
check("Duplicate file skipped", len(w.rows) == 1 and "Skipped 1" in w.summary, w.summary)
before = sorted(p.name for p in out.iterdir())
w.start_transcription()
check("Transcribe does nothing when every file is done", idle() and sorted(p.name for p in out.iterdir()) == before)

# 4. Same name again never overwrites
other = work / "input2"; other.mkdir()
shutil.copyfile(copy, other / copy.name)
first_txt = sha(out / f"{copy.stem}.txt")
w.add_files([str(other / copy.name)])
w.start_transcription()
wait(lambda: idle() and w.rows[1].state in ("done", "failed"), "Second copy finishes")
check("Same name saved as a new file", (out / f"{copy.stem} (2).txt").is_file(), sorted(p.name for p in out.iterdir()))
check("First transcript not overwritten", sha(out / f"{copy.stem}.txt") == first_txt)

# 5. A broken file fails on its own and goes in the error log
broken = inputs / "broken.wav"
broken.write_bytes(b"not audio at all")
w.add_files([str(broken)])
w.start_transcription()
wait(lambda: idle() and w.rows[2].state in ("done", "failed"), "Broken file finishes")
check("Broken file marked failed", w.rows[2].state == "failed", w.rows[2].state)
logs = list((work / "errors").glob("transcription_errors_*.log"))
check("Error log written", len(logs) == 1 and "broken.wav" in logs[0].read_text(encoding="utf-8"))
check("No output for the broken file", not list(out.glob("broken*")))

# 6. Stop mid-file
if copy.suffix.lower() == ".wav" and args.sample:
    long_file = inputs / "long.wav"
    write_repeated(copy, long_file, 20)
    w.add_files([str(long_file)])
    long_row = w.rows[-1]
    w.start_transcription()
    wait(lambda: long_row.state == "active" and long_row.waveform.peaks, "Long file starts")
    time.sleep(1)
    w.cancel_transcription()
    wait(idle, "Stop finishes")
    check("Stop leaves the file stopped", long_row.state == "stopped", long_row.state)
    check("Stop saves no partial output", not list(out.glob("long*")), sorted(p.name for p in out.iterdir()))
    check("Stop summary shown", w.summary.startswith("Stopped"), w.summary)
else:
    print("SKIP Stop mid-file (needs a speech .wav sample)")

# 7. Settings and log file in the data folder
saved = json.loads(store.file.read_text(encoding="utf-8"))
check("Settings file remembers the model", saved.get("model") == MODEL, saved)
check("Settings file keeps the formats", saved.get("output_formats") == FORMATS, saved)
app_log.flush()
log_text = log_path.read_text(encoding="utf-8") if log_path else ""
check("Log file records the batch", "Transcribe:" in log_text and "Batch finished" in log_text, str(log_path))
check("No dialogs appeared", not dialogs, dialogs)
check("Sample unchanged at the end", sha(copy) == orig)
w.close()
# -----------------------------------------------------------------------------------------------------

print(f"RESULT: {'FAIL' if failures else 'PASS'} ({len(failures)} failed)")
sys.exit(1 if failures else 0)
