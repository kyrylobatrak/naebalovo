"""AI overlay for Windows and macOS: voice -> Claude Code, hidden from screen capture.

Windows: run.bat (pythonw, so that no console window exists).
macOS: mac/run.command (see mac/README.md).
Errors go to overlay.log next to this file (created only when an error happens).
Everything that talks to the operating system is in windows.py / mac/macos.py.
"""
from __future__ import annotations

import html
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ folder next to the scripts

APP_DIR = Path(__file__).resolve().parent
LOG_FILE = APP_DIR / "overlay.log"

# pythonw has no stdout/stderr; libraries that print progress bars would crash.
if sys.stdout is None or sys.stderr is None:
    _null_stream = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or _null_stream
    sys.stderr = sys.stderr or _null_stream

# Only warnings and errors are logged; delay=True means the file is created
# only when the first one happens, so normally there is no overlay.log.
logging.basicConfig(
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8", delay=True)],
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("overlay")
# Libraries log every phrase and every HTTP request at INFO: keep only their warnings.
for _name in ("faster_whisper", "httpx", "httpx2", "huggingface_hub"):
    logging.getLogger(_name).setLevel(logging.WARNING)

# pythonw has nowhere to print errors: errors in Qt slots (button clicks) and in
# background threads would vanish without a trace, so they go to the log.
sys.excepthook = lambda kind, value, tb: log.error(
    "uncaught error:\n%s", "".join(traceback.format_exception(kind, value, tb)))
threading.excepthook = lambda args: log.error(
    "error in thread %s:\n%s", args.thread.name if args.thread else "?",
    "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))


def fatal(message: str):
    log.error(message)
    print(message, file=sys.stderr)
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "AI overlay", 0x10)
    elif sys.platform == "darwin":
        subprocess.run(["osascript", "-e", "on run argv", "-e",
                        'display alert "AI overlay" message (item 1 of argv) as critical',
                        "-e", "end run", message[:1500]], check=False)
    sys.exit(1)


if os.name != "nt" and sys.platform != "darwin":
    fatal("The overlay works on Windows and macOS only.")

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt, Signal

    import core
    import mathview
    import speech
    if sys.platform == "darwin":
        sys.path.insert(0, str(APP_DIR / "mac"))
        import macos as system
    else:
        import windows as system
except Exception:  # noqa: BLE001
    fatal("Не вдалося імпортувати залежності. Запустіть run.bat (Windows) або "
          "mac/run.command (macOS)\n\n" + traceback.format_exc())

GlobalHotkeys, MouseHotkeys = system.GlobalHotkeys, system.MouseHotkeys
exclude_from_capture = system.exclude_from_capture

# ------------------------------------------------------- audio + whisper

class Pipeline(QtCore.QObject):
    """Captures microphone and system sound, cuts phrases, transcribes them."""

    status = Signal(str)          # one-line state for the header
    phrase = Signal(str, str)     # source ("mic" / "system"), final text of a phrase
    partial = Signal(str, str)    # source, draft of the phrase being spoken ("" = remove)

    def __init__(self, cfg: dict, transcriber=None):
        super().__init__()
        self.cfg = cfg
        self.frames: queue.Queue = queue.Queue()
        self.listening = True
        # The model may already be loading (started in main() before the window was built).
        self.transcriber = transcriber or core.Transcriber(cfg, log=log.warning)
        self.transcriber.start_loading()
        self._audio = None  # what system.open_audio returned
        self._device_ids = ("", "")
        self._partial_len: dict[str, int] = {}     # phrase length at the last draft
        self._partial_shown: dict[str, bool] = {}  # a draft is on screen
        self._sources_info = ""
        # True while the overlay reads an answer aloud: the loopback (and a microphone
        # near the speakers) would hear that voice and Whisper would write it down.
        self.speaking = lambda: False

    def start(self):
        threading.Thread(target=self._run, name="pipeline", daemon=True).start()

    def describe(self) -> str:
        state = "слухаю" if self.listening else "пауза"
        return f"Whisper {self.transcriber.description} · {self._sources_info} · {state}"

    # runs in the worker thread
    def _run(self):
        try:
            self.status.emit("Завантажую модель Whisper (перший запуск завантажує її з інтернету)…")
            # Update the header when the CPU bridge is swapped for the GPU model.
            self.transcriber.on_ready = lambda: self.status.emit(self.describe())
            self.transcriber.wait_loaded()  # started already; this only waits for it to finish
            self._open_audio()
        except Exception as exc:  # noqa: BLE001
            log.exception("pipeline start")
            self.status.emit(f"⚠ Голос недоступний: {exc}")
            return
        self.status.emit(self.describe())

        cfg = self.cfg
        segmenters = {
            source: core.Segmenter(cfg["rms_threshold"], cfg["silence_sec"], cfg["max_segment_sec"])
            for source in ("mic", "system")
        }
        partial_step = int(float(cfg.get("partial_sec") or 0) * core.SAMPLE_RATE)
        last_rx: dict[str, float] = {}
        next_check = time.monotonic() + 1.0

        def drop_all():
            for src, seg in segmenters.items():
                seg.reset()
                self._finish(src, None)

        while True:
            now = time.monotonic()
            if now >= next_check:
                next_check = now + 1.0
                if self._default_devices_changed():
                    self._reopen_audio()
                    drop_all()
                    last_rx.clear()
            # Draft of the phrase that is still being spoken, only when no audio is waiting.
            if partial_step and self.listening and self.frames.qsize() < 10:
                for src, seg in segmenters.items():
                    if seg.active and seg.buf_len - self._partial_len.get(src, 0) >= partial_step:
                        self._partial(src, seg)
            try:
                source, raw, channels, rate = self.frames.get(timeout=0.02)
            except queue.Empty:
                # A loopback device sends nothing while nothing plays: close open phrases.
                now = time.monotonic()
                for src, seg in segmenters.items():
                    if seg.active and now - last_rx.get(src, now) >= cfg["silence_sec"]:
                        self._finish(src, seg.flush())
                continue
            seg = segmenters[source]
            if not self.listening:
                if seg.active:
                    seg.reset()
                    self._finish(source, None)
                continue
            if self.frames.qsize() > 1500:  # recognition fell far behind: skip the backlog
                log.warning("audio backlog dropped")
                with self.frames.mutex:
                    self.frames.queue.clear()
                drop_all()
                continue
            last_rx[source] = time.monotonic()
            was_active = seg.active
            audio = seg.feed(core.to_mono_16k(raw, channels, rate))
            if audio is not None or (was_active and not seg.active):
                self._finish(source, audio)

    def _recognize(self, audio) -> str:
        try:
            return self.transcriber.transcribe(audio)
        except Exception:  # noqa: BLE001
            log.exception("transcribe")
            return ""

    def _partial(self, source: str, seg):
        audio = seg.current()
        if audio is None:
            return
        self._partial_len[source] = seg.buf_len
        text = self._recognize(audio)
        if text:
            self._partial_shown[source] = True
            self.partial.emit(source, text)

    def _finish(self, source: str, audio):
        """A phrase ended: send the final text, or remove its draft if nothing was said."""
        self._partial_len[source] = 0
        text = self._recognize(audio) if audio is not None else ""
        shown = self._partial_shown.pop(source, False)
        if text:
            self.phrase.emit(source, text)
        elif shown:
            self.partial.emit(source, "")

    def _default_devices_changed(self) -> bool:
        try:
            ids = system.default_audio_ids()
        except Exception:  # noqa: BLE001
            log.exception("default audio device check")
            return False
        return ids != self._device_ids

    def _close_audio(self):
        if self._audio is not None:
            system.close_audio(self._audio)
            self._audio = None

    def _reopen_audio(self):
        """The user switched speakers/headphones or microphone: follow the new defaults."""
        log.info("default audio device changed, reopening")
        self._close_audio()
        with self.frames.mutex:
            self.frames.queue.clear()
        try:
            self._open_audio()
        except Exception:  # noqa: BLE001
            log.exception("reopen audio")
            self._sources_info = "🎤 ✗ 🔊 ✗"
        self.status.emit(self.describe())

    def _open_audio(self):
        try:
            self._device_ids = system.default_audio_ids()
        except Exception:  # noqa: BLE001
            self._device_ids = ("", "")
        mute_mic = self.cfg.get("tts_mute_mic", True)

        def put(source, raw, channels, rate):
            # Dropped when recorded, not when recognized: Whisper runs seconds behind,
            # and by then the voice may have stopped.
            if not ((source == "system" or mute_mic) and self.speaking()):
                self.frames.put((source, raw, channels, rate))

        self._audio, notes = system.open_audio(self.cfg, put)
        self._sources_info = " ".join(notes) or "звук вимкнено"


# ----------------------------------------------------------- claude code

def _kill(proc):
    if proc and proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


class ClaudeRunner(QtCore.QObject):
    """Runs the official Claude Code CLI in the background, without any window.

    Starting Claude Code takes 1-2 s, more than the model needs for the first
    words. So one process is always started in advance and waits for the
    question on stdin; each question takes it and starts the next one.
    """

    delta = Signal(int, str)        # request id, new text
    done = Signal(int, str, bool)   # request id, full answer, is_error
    files_read = Signal(int, str, str, str)  # reading id, model, session id ("" = failed), error

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg       # shared with the window; cfg["claude_model"] changes on a hotkey
        self.request_id = 0
        self.resume = None   # session with Claude's reading of the pinned files
        self._proc = None    # process answering the current question
        self._spare = None   # (process, (resume, model)) started in advance for the next question
        self._reading = None  # process reading the pinned files
        self.reading_id = 0
        self._lock = threading.Lock()
        self.prewarm()

    def _setup(self):
        """What a process is started for: questions need one started for the same."""
        return self.resume, self.cfg["claude_model"]

    def _spawn(self, setup=None):
        resume, model = setup or self._setup()
        exe = core.find_claude(self.cfg)
        if not exe:
            return None
        cmd = core.claude_command(exe, dict(self.cfg, claude_model=model), resume)
        try:
            return subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=str(core.ATTACH_DIR), creationflags=system.NO_WINDOW,
                text=True, encoding="utf-8", errors="replace",
            )
        except OSError:
            log.exception("claude start")
            return None

    def prewarm(self):
        """Start the process for the next question in the background."""
        setup = self._setup()

        def work():
            proc = self._spawn(setup)
            with self._lock:
                old, self._spare = self._spare, (proc, setup)
            _kill(old[0] if old else None)

        threading.Thread(target=work, name="claude-prewarm", daemon=True).start()

    def _take_spare(self):
        with self._lock:
            spare, self._spare = self._spare, None
        proc, setup = spare if spare else (None, None)
        if proc and proc.poll() is None and setup == self._setup():
            return proc
        _kill(proc)  # started for other pinned files or another model
        return None

    def set_resume(self, session_id):
        """Questions from now on continue this session (None = start from scratch)."""
        self.resume = session_id or None
        self.prewarm()

    def read_files(self, names: list[str]) -> int:
        """Let Claude read the pinned files once, in the background; emits files_read."""
        self.stop_reading()
        self.reading_id += 1
        threading.Thread(target=self._read, args=(self.reading_id, list(names)),
                         name="claude-read", daemon=True).start()
        return self.reading_id

    def stop_reading(self):
        self.reading_id += 1  # its result, if it still comes, is outdated
        with self._lock:
            proc, self._reading = self._reading, None
        _kill(proc)

    def _read(self, reading_id: int, names: list[str]):
        model = self.cfg["claude_model"]  # the reading serves questions to this model
        exe = core.find_claude(self.cfg)
        if not exe:
            self.files_read.emit(reading_id, model, "", "не знайдено Claude Code")
            return
        session_id = str(uuid.uuid4())
        try:
            proc = subprocess.Popen(
                core.reading_command(exe, dict(self.cfg, claude_model=model), session_id),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(core.ATTACH_DIR), creationflags=system.NO_WINDOW,
                text=True, encoding="utf-8", errors="replace",
            )
        except OSError:
            log.exception("claude start (reading)")
            self.files_read.emit(reading_id, model, "", "не вдалося запустити Claude Code")
            return
        with self._lock:
            if reading_id != self.reading_id:
                _kill(proc)
                return
            self._reading = proc
        out, err = proc.communicate(core.reading_prompt(names))
        if reading_id != self.reading_id:  # files changed meanwhile, or the overlay quits
            core.delete_session(session_id)
            return
        try:
            result = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            result = {}
        if proc.returncode != 0 or result.get("is_error") or not result.get("session_id"):
            detail = str(result.get("result") or err or out).strip()[-500:]
            log.error("reading pinned files failed (%s): %s", proc.returncode, detail)
            core.delete_session(session_id)
            self.files_read.emit(reading_id, model, "", detail or f"код {proc.returncode}")
            return
        usage = result.get("usage") or {}
        log.info("pinned files read: %s tokens in context",
                 sum(int(usage.get(k) or 0) for k in
                     ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")))
        self.files_read.emit(reading_id, model, result["session_id"], "")

    def ask(self, prompt: str) -> int:
        self.cancel()
        self.request_id += 1
        proc = self._take_spare()
        self.prewarm()
        threading.Thread(target=self._run, args=(self.request_id, prompt, proc),
                         name="claude", daemon=True).start()
        return self.request_id

    def cancel(self):
        with self._lock:
            proc, self._proc = self._proc, None
        _kill(proc)

    def shutdown(self):
        self.cancel()
        self.stop_reading()
        with self._lock:
            spare, self._spare = self._spare, None
        _kill(spare[0] if spare else None)

    def _run(self, request_id: int, prompt: str, proc=None):
        prewarmed = proc is not None
        if proc is None:
            if not core.find_claude(self.cfg):
                self.done.emit(request_id, "Не знайдено Claude Code. Встановіть його, увійдіть у свій "
                               "акаунт командою `claude` або вкажіть шлях у core.py (claude_path).", True)
                return
            proc = self._spawn()
            if proc is None:
                self.done.emit(request_id, "Не вдалося запустити Claude Code (деталі в overlay.log).", True)
                return
        with self._lock:
            if request_id != self.request_id:  # a newer request started meanwhile
                _kill(proc)
                return
            self._proc = proc

        def send_prompt():
            # Separate thread: a long prompt must not block while the CLI is already printing.
            try:
                proc.stdin.write(core.user_message(prompt) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass

        threading.Thread(target=send_prompt, daemon=True).start()
        parser = core.StreamParser()
        for line in proc.stdout:
            new_text = parser.feed(line)
            if new_text:
                self.delta.emit(request_id, new_text)
            if parser.finished:
                break
        try:
            proc.stdin.close()  # no more questions for this process: let it exit
        except (OSError, ValueError):
            pass
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill(proc)
            code = proc.wait()
        answer = parser.answer().strip()
        is_error = parser.is_error
        if not answer:
            if request_id != self.request_id:  # cancelled by a newer question
                return
            tail = "\n".join(parser.noise[-5:])
            log.error("claude exit %s: %s", code, tail)
            if prewarmed:
                # The waiting process may have gone stale: one more try with a fresh one.
                self._run(request_id, prompt)
                return
            is_error = True
            answer = f"Claude Code не повернув відповіді (код {code}).\n\n{tail}".strip()
        self.done.emit(request_id, answer, is_error)


# ------------------------------------------------------------------ window

STYLE = """
QWidget#root { background: #14161b; border: 1px solid #2c313c; }
QLabel { color: #9aa3b2; }
QLabel#title { color: #e6e9ef; font-weight: 600; }
QLabel#ok { color: #5ecb8a; }
QLabel#bad { color: #ff6b6b; font-weight: 700; }
QPlainTextEdit, QTextBrowser, QLineEdit {
    background: #1b1e25; color: #e6e9ef; border: 1px solid #2c313c; border-radius: 6px;
    padding: 6px; selection-background-color: #3b5bdb;
}
QPlainTextEdit { color: #aeb6c4; }
QLineEdit:focus { border-color: #5c7cfa; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical { background: #3a4050; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolButton { color: #9aa3b2; background: transparent; border: none; border-radius: 4px; padding: 0 3px; }
QToolButton:hover { background: #2c313c; color: #e6e9ef; }
QScrollArea#files, QWidget#files { background: transparent; }
QToolButton:checked { color: #5c7cfa; font-weight: 600; }
QPushButton {
    background: #1b1e25; color: #e6e9ef; border: 1px solid #2c313c; border-radius: 6px;
    padding: 4px 10px;
}
QPushButton:hover { border-color: #5c7cfa; }
QPushButton#capturing { border-color: #5c7cfa; color: #5c7cfa; }
QPushButton#primary { background: #3b5bdb; border-color: #3b5bdb; }
QSlider::groove:horizontal { height: 4px; background: #2c313c; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #5c7cfa; border-radius: 2px; }
QSlider::handle:horizontal { background: #e6e9ef; width: 10px; margin: -4px 0; border-radius: 5px; }
QDialog#root { background: #14161b; }
"""

# Actions that hotkeys and mouse buttons can be bound to, in the order of the settings.
ACTIONS = {
    "ask": "Відповідь (транскрипт + запитання)",
    "screenshot": "Знімок екрана → Claude",
    "focus": "Курсор у поле запитання",
    "model": "Перемкнути модель",
    "auto": "Голосовий режим",
    "listen": "Пауза / слухати",
    "clear": "Очистити",
    "toggle": "Показати / сховати",
    "settings": "Налаштування клавіш",
    "speak": "Озвучити відповідь / пауза",
    "speak_back": "Озвучення: назад",
    "speak_forward": "Озвучення: вперед",
    "speak_slower": "Озвучення: повільніше",
    "speak_faster": "Озвучення: швидше",
    "speak_stop": "Озвучення: стоп",
    "speak_auto": "Авто-озвучення вкл / викл",
    "quit": "Вихід",
}


def qt_modifiers(mods) -> int:
    """Qt keyboard modifiers -> core.MOD_* bits."""
    m = Qt.KeyboardModifier
    bits = 0
    for flag, bit in ((m.ControlModifier, core.MOD_CONTROL), (m.AltModifier, core.MOD_ALT),
                      (m.ShiftModifier, core.MOD_SHIFT), (m.MetaModifier, core.MOD_WIN)):
        if mods & flag:
            bits |= bit
    return bits


class ComboField(QtWidgets.QPushButton):
    """Shows a hotkey; click it, then press keys (kind "key") or a mouse button ("mouse")."""

    changed = Signal()

    def __init__(self, kind: str, combo: str, on_info):
        super().__init__()
        self.kind = kind
        self.combo = combo
        self.capturing = False
        self._on_info = on_info
        self._eat_release = False
        self.setAutoDefault(False)
        self.setMinimumWidth(150)
        self.clicked.connect(self.start)
        self._show()

    def set_combo(self, combo: str):
        self.combo = combo
        self._show()
        self.changed.emit()

    def _show(self, text: str | None = None):
        self.setObjectName("capturing" if self.capturing else "")
        self.style().unpolish(self)
        self.style().polish(self)
        self.setText(text if text is not None else (core.pretty_combo(self.combo) or "—"))

    def start(self):
        if self.capturing:
            return
        self.capturing = True
        self.setFocus()
        if self.kind == "mouse":
            self.grabMouse()
            self._show("Натисніть кнопку миші…")
            self._on_info("Колесо або бічна кнопка, можна з Ctrl/Alt/Shift. "
                          "Ліва чи права кнопка або Esc — скасувати.")
        else:
            self._show("Натисніть комбінацію…")
            self._on_info(system.HOTKEY_HELP)

    def stop(self, combo: str | None = None):
        if not self.capturing:
            return
        self.capturing = False
        if self.kind == "mouse":
            self.releaseMouse()
        if combo is not None:
            self.set_combo(combo)
        else:
            self._show()

    def event(self, event):  # noqa: D401 - catch Tab and shortcuts while capturing
        if self.capturing and event.type() == QtCore.QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        if self.capturing and event.type() == QtCore.QEvent.Type.KeyPress:
            self._key(event)
            return True
        return super().event(event)

    def _key(self, event):
        mods = qt_modifiers(event.modifiers())
        if event.key() == Qt.Key.Key_Escape and not mods:
            self.stop()
            self._on_info("")
            return
        if system.is_modifier_key(event):
            prefix = core.pretty_combo(core.make_combo(mods, "x"))[:-1]
            self._show(prefix + "…")
            return
        if self.kind == "mouse":
            return
        name = system.key_name_from_event(event)
        if name is None:
            self._on_info("⚠ Ця клавіша не підтримується, оберіть іншу.")
            return
        if not mods and core.needs_modifier(name):
            self._on_info(f"⚠ {core.pretty_combo(name)} без Ctrl/Alt/Shift/Win заблокує цю клавішу "
                          "в усіх програмах. Додайте модифікатор.")
            return
        self.stop(core.make_combo(mods, name))
        self._on_info("")

    def mousePressEvent(self, event):  # noqa: N802 - Qt name
        if self.capturing and self.kind == "mouse":
            self._eat_release = True
            button = {Qt.MouseButton.MiddleButton: "mouse3", Qt.MouseButton.BackButton: "mouse4",
                      Qt.MouseButton.ForwardButton: "mouse5"}.get(event.button())
            mods = qt_modifiers(QtWidgets.QApplication.keyboardModifiers())
            self.stop(core.make_combo(mods, button) if button else None)
            self._on_info("")
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt name
        if self._eat_release:
            self._eat_release = False
            return
        super().mouseReleaseEvent(event)

    def focusOutEvent(self, event):  # noqa: N802 - Qt name
        self.stop()
        super().focusOutEvent(event)


class SettingsDialog(QtWidgets.QDialog):
    """Hotkeys and mouse buttons for every action; saved by the overlay on OK."""

    def __init__(self, parent, keys: dict[str, str], mouse: dict[str, str]):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("root")
        self.setWindowTitle("Гарячі клавіші")
        self.setStyleSheet(STYLE)
        self.setFont(parent.font())
        layout = QtWidgets.QVBoxLayout(self)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(6)
        for col, text in ((1, "Клавіатура"), (3, "Мишка")):
            head = QtWidgets.QLabel(text)
            head.setObjectName("title")
            grid.addWidget(head, 0, col)
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        self.fields: dict[str, tuple[ComboField, ComboField]] = {}
        for row, (action, label) in enumerate(ACTIONS.items(), 1):
            grid.addWidget(QtWidgets.QLabel(label), row, 0)
            pair = []
            for col, kind, combo in ((1, "key", keys.get(action, "")), (3, "mouse", mouse.get(action, ""))):
                field = ComboField(kind, combo, self.info.setText)
                field.changed.connect(self._validate)
                clear = QtWidgets.QToolButton()
                clear.setText("✕")
                clear.clicked.connect(lambda _=False, f=field: f.set_combo(""))
                grid.addWidget(field, row, col)
                grid.addWidget(clear, row, col + 1)
                pair.append(field)
            self.fields[action] = tuple(pair)
        layout.addLayout(grid)
        layout.addWidget(self.info)

        buttons = QtWidgets.QHBoxLayout()
        defaults = QtWidgets.QPushButton("За замовчуванням")
        defaults.setAutoDefault(False)
        defaults.clicked.connect(self._defaults)
        cancel = QtWidgets.QPushButton("Скасувати")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        self.save = QtWidgets.QPushButton("Зберегти")
        self.save.setObjectName("primary")
        self.save.setAutoDefault(False)
        self.save.clicked.connect(self.accept)
        buttons.addWidget(defaults)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save)
        layout.addLayout(buttons)
        self.info.setText("Клацніть поле й натисніть комбінацію. ✕ — прибрати.")
        int(self.winId())  # create the native window now, to hide it from capture before showing
        exclude_from_capture(self)

    def showEvent(self, event):  # noqa: N802 - Qt name
        super().showEvent(event)
        exclude_from_capture(self)  # it is a separate window: a share would show it
        self.activateWindow()

    def keyPressEvent(self, event):  # noqa: N802 - Enter must not close the dialog
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return
        super().keyPressEvent(event)

    def _defaults(self):
        for action, (key, mouse) in self.fields.items():
            key.set_combo(core.CONFIG["hotkeys"].get(action, ""))
            mouse.set_combo(core.CONFIG["mouse_hotkeys"].get(action, ""))

    def _validate(self):
        problems = []
        for index, kind in ((0, "клавіатура"), (1, "мишка")):
            seen: dict[str, str] = {}
            for action, pair in self.fields.items():
                combo = pair[index].combo
                if combo and combo in seen:
                    problems.append(f"{core.pretty_combo(combo)} ({kind}) уже стоїть на "
                                    f"«{ACTIONS[seen[combo]]}» і «{ACTIONS[action]}»")
                seen.setdefault(combo, action)
        self.save.setEnabled(not problems)
        if problems:
            self.info.setText("⚠ " + "; ".join(problems))
        elif "уже стоїть" in self.info.text():
            self.info.setText("")
        return not problems

    def result_bindings(self) -> tuple[dict[str, str], dict[str, str]]:
        keys = {action: pair[0].combo for action, pair in self.fields.items()}
        mouse = {action: pair[1].combo for action, pair in self.fields.items()}
        return keys, mouse


class FoldHandle(QtWidgets.QSplitterHandle):
    """The strip between two parts of the window: drag it to make the foldable part
    smaller, down to nothing; click it to fold or unfold. Folded, it says what is hidden."""

    def __init__(self, orientation, splitter):
        super().__init__(orientation, splitter)
        self.setCursor(Qt.CursorShape.SplitVCursor)
        self._press_y = None

    def paintEvent(self, event):  # noqa: N802 - Qt name
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        splitter = self.splitter()
        if splitter.folded():
            font = painter.font()
            font.setPointSize(max(7, font.pointSize() - 2))
            painter.setFont(font)
            painter.setPen(QtGui.QColor("#9aa3b2"))
            text = QtGui.QFontMetrics(font).elidedText(
                f"{splitter.arrow} {splitter.summary()}", Qt.TextElideMode.ElideRight,
                self.width() - 16)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor("#3a4050"))
            painter.drawRoundedRect(QtCore.QRectF(self.width() / 2 - 20, self.height() / 2 - 1.5,
                                                  40, 3), 1.5, 1.5)

    def mousePressEvent(self, event):  # noqa: N802 - Qt name
        self._press_y = event.position().y()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt name
        super().mouseReleaseEvent(event)
        if self._press_y is not None and abs(event.position().y() - self._press_y) < 3:
            self.splitter().toggle_fold()  # a click, not a drag
        self._press_y = None


class FoldSplitter(QtWidgets.QSplitter):
    """Two parts one above the other; one of them (the transcript, the list of files)
    can be squeezed or folded away to give the answer more room."""

    folded_changed = Signal(bool)

    def __init__(self, fold_index: int, summary, natural_height):
        super().__init__(Qt.Orientation.Vertical)
        self.fold_index = fold_index          # 0 = the upper part folds, 1 = the lower one
        self.arrow = "▾" if fold_index == 0 else "▴"  # where to drag to open it
        self.summary = summary                # () -> text on the folded handle
        self.natural_height = natural_height  # () -> height of the part when unfolded
        self.setHandleWidth(14)
        self.setChildrenCollapsible(False)
        self._open_height = 0                 # height of the part before it was folded
        self._was_folded = False
        self.splitterMoved.connect(lambda *_: self._moved())

    def createHandle(self):  # noqa: N802 - Qt name
        return FoldHandle(self.orientation(), self)

    def add(self, upper, lower):
        self.addWidget(upper)
        self.addWidget(lower)
        other = 1 - self.fold_index
        self.setCollapsible(self.fold_index, True)  # dragged all the way, the part disappears
        self.setCollapsible(other, False)
        self.setStretchFactor(self.fold_index, 0)
        self.setStretchFactor(other, 1)

    def folded(self) -> bool:
        return self.sizes()[self.fold_index] == 0

    def _set_part(self, height: int):
        sizes = self.sizes()
        total = sum(sizes)
        height = max(0, min(height, total))
        sizes[self.fold_index] = height
        sizes[1 - self.fold_index] = total - height
        self.setSizes(sizes)

    def fold(self, folded: bool):
        part = self.sizes()[self.fold_index]
        if folded and part:
            self._open_height = part
            self._set_part(0)
        elif not folded and not part:
            self._set_part(self._open_height or self.natural_height())
        self._moved()

    def toggle_fold(self):
        self.fold(not self.folded())

    def fit(self):
        """Give the part its natural height (unless it is folded)."""
        if not self.folded():
            self._set_part(self.natural_height())
        self._moved()

    def _moved(self):
        self.handle(1).update()
        folded = self.folded()
        if folded != self._was_folded:
            self._was_folded = folded
            self.folded_changed.emit(folded)


class QuestionField(QtWidgets.QLineEdit):
    """Question input; Ctrl+V of a copied file or picture attaches it instead of pasting text."""

    def __init__(self, on_paste):
        super().__init__()
        self._on_paste = on_paste

    def keyPressEvent(self, event):  # noqa: N802 - Qt name
        if event.matches(QtGui.QKeySequence.StandardKey.Paste):
            if self._on_paste(QtWidgets.QApplication.clipboard().mimeData()):
                return
        super().keyPressEvent(event)


TRANSCRIPT_HEIGHT = 92  # the transcript's height when it is not squeezed or folded


class Overlay(QtWidgets.QWidget):
    def __init__(self, cfg: dict, transcriber=None):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,  # no taskbar button, not in Alt+Tab
        )
        self.cfg = cfg
        self._transcriber = transcriber
        self.setObjectName("root")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowOpacity(float(cfg["opacity"]))
        self.setStyleSheet(STYLE)
        font = self.font()
        font.setPointSize(int(cfg["font_size"]))
        self.setFont(font)

        self.transcript = core.Transcript()
        self.history: list[tuple[str, str]] = []
        self.partials: dict[str, str] = {}  # drafts of phrases still being spoken
        self._partial_lines = 0              # how many draft lines end the transcript view
        self.auto = bool(cfg["auto_answer_mic"])
        self.current_request = 0
        self.current_label = ""
        self.answer_text = ""
        self.waiting = False
        self.answer_done = True
        self.spoken_request = -1  # the request whose answer the speaker holds
        self.attachments: list[Path] = []  # files for the next question, copied into ATTACH_DIR
        self.pinned: list[Path] = []       # files for every question of this session, also there
        self._models = (str(cfg["claude_model"]), str(cfg.get("claude_model_alt") or "sonnet"))
        self.read_sessions: dict[str, str] = {}  # model -> session in which it read these files
        self.read_state = ""               # for the current model: "", "reading", "ready", "error"
        core.reset_attach_dir()            # also the working folder of Claude Code
        self.speaker = speech.Speaker(cfg, log=log.warning)

        self._build_ui()
        self.setAcceptDrops(True)

        self.runner = ClaudeRunner(cfg)
        self.runner.delta.connect(self.on_delta)
        self.runner.done.connect(self.on_done)
        self.runner.files_read.connect(self.on_files_read)

        self.pipeline = Pipeline(cfg, self._transcriber)
        self.pipeline.status.connect(self.status_label.setText)
        self.pipeline.phrase.connect(self.on_phrase)
        self.pipeline.partial.connect(self.on_partial)
        self.pipeline.speaking = self.speaker.audible

        self.hotkeys = GlobalHotkeys()
        self.hotkeys.pressed.connect(self.run_action)
        self.mouse = MouseHotkeys()
        self.mouse.pressed.connect(self.run_action)
        self._settings_open = False

    @property
    def actions(self) -> dict:
        return {"ask": self.ask, "screenshot": self.screenshot, "focus": self.focus_input,
                "model": self.toggle_model, "auto": self.toggle_auto,
                "listen": self.toggle_listening, "clear": self.clear_all,
                "toggle": self.toggle_visible, "settings": self.open_settings, "quit": self.quit,
                "speak": self.speak, "speak_stop": self.speaker.stop,
                "speak_auto": self.toggle_auto_speech,
                "speak_back": lambda: self.seek_speech(-1),
                "speak_forward": lambda: self.seek_speech(1),
                "speak_slower": lambda: self.change_speech_speed(-0.25),
                "speak_faster": lambda: self.change_speech_speed(0.25)}

    def run_action(self, name: str):
        action = self.actions.get(name)
        if action:
            action()
            if name.startswith("speak"):
                self._update_player()

    # ---- ui

    def _build_ui(self):
        cfg = self.cfg
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 4)
        layout.setSpacing(6)

        header = QtWidgets.QHBoxLayout()
        self.title_label = title = QtWidgets.QLabel(f"AI · {cfg['claude_model']}")
        title.setObjectName("title")
        self.status_label = QtWidgets.QLabel("Запуск…")
        self.capture_label = QtWidgets.QLabel("")
        # Long status text is clipped instead of stretching the window.
        self.status_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                        QtWidgets.QSizePolicy.Policy.Preferred)
        header.addWidget(title)
        header.addWidget(self.status_label, 1)
        header.addWidget(self.capture_label)
        for icon, action in (("📷", self.screenshot), ("⚙", self.open_settings)):
            button = QtWidgets.QToolButton()
            button.setText(icon)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the question field keeps the cursor
            button.clicked.connect(lambda _=False, a=action: a())
            header.addWidget(button)
        layout.addLayout(header)

        self.transcript_view = QtWidgets.QPlainTextEdit()
        self.transcript_view.setReadOnly(True)
        self.transcript_view.setPlaceholderText("Тут з'являтиметься транскрипт розмови")

        self.answer_view = mathview.AnswerView()  # draws LaTeX formulas
        self.answer_view.setOpenLinks(False)
        self.answer_view.setPlaceholderText("Відповідь Claude")
        answer_box = QtWidgets.QWidget()
        answer_layout = QtWidgets.QVBoxLayout(answer_box)
        answer_layout.setContentsMargins(0, 0, 0, 0)
        answer_layout.setSpacing(6)
        answer_layout.addWidget(self.answer_view, 1)
        answer_layout.addLayout(self._build_player())

        self.pinned_label = QtWidgets.QLabel()
        self.pinned_label.setWordWrap(True)
        self.pinned_label.linkActivated.connect(self._on_pinned_link)
        self.pinned_label.hide()
        self.attach_label = QtWidgets.QLabel()
        self.attach_label.setWordWrap(True)
        self.attach_label.linkActivated.connect(self._on_attach_link)
        self.attach_label.hide()
        files_list = QtWidgets.QWidget()
        files_list.setObjectName("files")
        files_layout = QtWidgets.QVBoxLayout(files_list)
        files_layout.setContentsMargins(0, 0, 4, 0)
        files_layout.setSpacing(4)
        files_layout.addWidget(self.pinned_label)
        files_layout.addWidget(self.attach_label)
        files_layout.addStretch(1)
        # Squeezed by the handle, a long list scrolls instead of being cut off.
        self.files_panel = QtWidgets.QScrollArea()
        self.files_panel.setObjectName("files")
        self.files_panel.setWidgetResizable(True)
        self.files_panel.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.files_panel.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.files_panel.setWidget(files_list)
        self.files_panel.hide()

        self.files_splitter = FoldSplitter(1, self._files_summary, self._files_height)
        self.files_splitter.add(answer_box, self.files_panel)
        self.files_splitter.folded_changed.connect(
            lambda folded: self._remember_fold("files_folded", folded))

        self.transcript_splitter = FoldSplitter(0, self._transcript_summary,
                                                lambda: TRANSCRIPT_HEIGHT)
        self.transcript_splitter.add(self.transcript_view, self.files_splitter)
        self.transcript_splitter.folded_changed.connect(
            lambda folded: self._remember_fold("transcript_folded", folded))
        layout.addWidget(self.transcript_splitter, 1)
        # Sizes are known once the window is shown.
        QtCore.QTimer.singleShot(0, lambda: self.transcript_splitter.fold(True)
                                 if self.cfg.get("transcript_folded")
                                 else self.transcript_splitter.fit())

        self.input = QuestionField(self.attach_mime)
        self.input.setPlaceholderText("Запитання… Enter — надіслати · файл: перетягніть або Ctrl+V")
        self.input.returnPressed.connect(self.ask)
        layout.addWidget(self.input)

        # Dropped files go to the window (attachments), not into the text widgets.
        for widget in (self.transcript_view, self.answer_view, self.input):
            widget.setAcceptDrops(False)

        footer = QtWidgets.QHBoxLayout()
        self.hint_label = hint = QtWidgets.QLabel()
        small = hint.font()
        small.setPointSize(max(7, int(cfg["font_size"]) - 3))
        hint.setFont(small)
        hint.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                           QtWidgets.QSizePolicy.Policy.Preferred)
        self._update_hint()
        footer.addWidget(hint, 1)
        footer.addWidget(QtWidgets.QSizeGrip(self))
        layout.addLayout(footer)

        # Context menus and tooltips are separate windows that a screen share would show.
        for widget in (self.transcript_view, self.answer_view, self.input):
            widget.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        self.resize(int(cfg["width"]), int(cfg["height"]))
        screen = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 24, screen.top() + 24)

    def _build_player(self) -> QtWidgets.QHBoxLayout:
        """▶ ⏪ ⏩ ⏹, position slider, time and speed for reading the answer aloud."""
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(2)

        def button(text, slot):
            b = QtWidgets.QToolButton()
            b.setText(text)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setAutoRepeat(text in ("⏪", "⏩"))  # hold to keep seeking
            b.clicked.connect(lambda _=False: (slot(), self._update_player()))
            row.addWidget(b)
            return b

        self.play_button = button("▶", self.speak)
        button("⏪", lambda: self.seek_speech(-1))
        button("⏩", lambda: self.seek_speech(1))
        button("⏹", self.speaker.stop)
        self.player_slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.player_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.player_slider.setSingleStep(1000)
        self.player_slider.setPageStep(int(float(self.cfg.get("tts_seek_sec") or 5) * 1000))
        # Dragging moves only the handle; the voice jumps on release.
        self.player_slider.setTracking(False)
        self.player_slider.valueChanged.connect(lambda ms: self.speaker.seek_to(ms / 1000))
        row.addWidget(self.player_slider, 1)
        self.player_time = QtWidgets.QLabel()
        row.addWidget(self.player_time)
        button("−", lambda: self.change_speech_speed(-0.25))
        self.speed_label = QtWidgets.QLabel()
        row.addWidget(self.speed_label)
        button("+", lambda: self.change_speech_speed(0.25))
        self.auto_button = button("авто", self.toggle_auto_speech)
        self.auto_button.setCheckable(True)
        self.auto_button.setChecked(bool(self.cfg.get("tts_auto")))
        self._player_timer = QtCore.QTimer(self, interval=150)
        self._player_timer.timeout.connect(self._update_player)
        self._update_player()
        return row

    # ---- reading aloud

    def speak(self):
        """Read the answer on screen aloud; again = pause / go on."""
        if self.spoken_request == self.current_request and self.speaker.state()["has_audio"]:
            self.speaker.toggle()
        elif self.answer_text.strip() and not self.answer_text.startswith("⚠"):
            self.spoken_request = self.current_request
            self.speaker.speak(self.answer_text, final=self.answer_done or not self.current_request)
        else:
            self.player_time.setText("немає відповіді")
            return
        self._player_timer.start()

    def seek_speech(self, direction: int):
        self.speaker.seek(direction * float(self.cfg.get("tts_seek_sec") or 5))

    def toggle_auto_speech(self):
        """Auto mode on/off: every new answer is read aloud as it streams in."""
        self.cfg["tts_auto"] = not self.cfg.get("tts_auto")
        self.auto_button.setChecked(self.cfg["tts_auto"])  # highlighted while on
        self._save_settings()

    def change_speech_speed(self, step: float):
        self.cfg["tts_speed"] = self.speaker.change_speed(step)
        self._save_settings()

    def _save_settings(self):
        try:
            core.save_settings(self.cfg)
        except OSError:
            log.exception("save settings")

    def _update_player(self):
        state = self.speaker.state()
        self.play_button.setText("⏸" if state["playing"] else "▶")
        self.speed_label.setText(f"{state['speed']:g}×")
        if not self.player_slider.isSliderDown():
            self.player_slider.blockSignals(True)
            self.player_slider.setRange(0, int(state["duration"] * 1000))
            self.player_slider.setValue(int(state["position"] * 1000))
            self.player_slider.blockSignals(False)

        def clock(sec):
            return f"{int(sec) // 60}:{int(sec) % 60:02d}"

        if state["error"]:
            text = "⚠ " + state["error"]
        elif state["loading"]:
            text = "⏳ озвучую…"
        elif state["has_audio"]:
            total = clock(state["duration"]) + ("" if state["complete"] else "+")
            text = f"{clock(state['position'])} / {total}"
        else:
            text = ""
        self.player_time.setText(text)
        if not state["playing"] and state["complete"]:
            self._player_timer.stop()
        elif not self._player_timer.isActive():
            self._player_timer.start()

    # ---- attachments

    def dragEnterEvent(self, event):  # noqa: N802 - Qt name
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasImage():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802 - Qt name
        if self.attach_mime(event.mimeData()):
            event.acceptProposedAction()

    def attach_mime(self, mime) -> bool:
        """Attach files or a picture from a drop or the clipboard; False if there are none."""
        paths = [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()] if mime.hasUrls() else []
        paths = [p for p in paths if p.is_file()]
        errors = []
        if paths:
            for path in paths:
                try:
                    self.attachments.append(core.prepare_attachment(path))
                except ValueError as exc:
                    errors.append(str(exc))
        elif mime.hasImage():  # e.g. a Win+Shift+S snip
            image = QtGui.QImage(mime.imageData())
            target = core.free_name("зображення.png")
            if image.isNull() or not image.save(str(target), "PNG"):
                errors.append("не вдалося зберегти зображення з буфера обміну")
            else:
                self.attachments.append(target)
        else:
            return False
        self._update_attach_label()
        if errors:
            self.show_message("⚠ " + "\n\n⚠ ".join(errors))
        return True

    def clear_attachments(self):
        self.attachments.clear()
        self._update_attach_label()

    def _update_attach_label(self):
        if not self.attachments:
            self.attach_label.hide()
            self._update_files_panel()
            return
        names = ", ".join(p.name for p in self.attachments)
        self.attach_label.setText(f"📎 {html.escape(names)} · "
                                  f"<a href='pin' style='color:#9aa3b2'>на всю сесію</a> · "
                                  f"<a href='clear' style='color:#9aa3b2'>прибрати</a>")
        self.attach_label.show()
        self._update_files_panel()

    # ---- list of files under the answer (folds away to give the answer more room)

    def _files_summary(self) -> str:
        parts = []
        if self.pinned:
            parts.append(f"📌 {len(self.pinned)}")
        if self.attachments:
            parts.append(f"📎 {len(self.attachments)}")
        return " · ".join(parts) + " · файли згорнуто"

    def _update_files_panel(self):
        """Show the list while there are files, sized to them; hide it with the handle
        when there are none."""
        has_files = bool(self.pinned or self.attachments)
        if not has_files:
            self.files_panel.hide()
            return
        first_time = self.files_panel.isHidden()
        self.files_panel.show()
        if first_time and self.cfg.get("files_folded"):
            QtCore.QTimer.singleShot(0, lambda: self.files_splitter.fold(True))
        else:
            QtCore.QTimer.singleShot(0, self.files_splitter.fit)  # after the labels are laid out
        self.files_splitter.handle(1).update()

    def _files_height(self) -> int:
        """Height that shows the whole list, but never more than a third of the space."""
        content = self.files_panel.widget().sizeHint().height() + 4
        return max(24, min(content, max(60, sum(self.files_splitter.sizes()) // 3)))

    def _remember_fold(self, key: str, folded: bool):
        if self.cfg.get(key) != folded:
            self.cfg[key] = folded
            self._save_settings()

    def _transcript_summary(self) -> str:
        lines = self.transcript_view.toPlainText().strip().splitlines()
        return "транскрипт згорнуто" + (f" · {lines[-1]}" if lines else "")

    def _transcript_changed(self):
        if self.transcript_splitter.folded():  # the folded handle shows the last phrase
            self.transcript_splitter.handle(1).update()

    def _on_attach_link(self, link: str):
        if link == "pin":
            self.pin_attachments()
        else:
            self.clear_attachments()

    def _pin_form(self, path: Path) -> Path:
        """A long PDF is pinned as its text: many more pages fit into Claude's reading."""
        return core.pdf_to_text(path, int(self.cfg.get("pdf_text_min_pages") or 30)) or path

    def pin_attachments(self):
        """Move the files of the next question to the files of the whole session."""
        new = [self._pin_form(p) for p in self.attachments]
        self.pinned += [p for p in new if p not in self.pinned]
        self.clear_attachments()
        self._pinned_changed()

    def load_context_files(self):
        """Pin the files from CONFIG["context_files"]."""
        sources, errors = core.expand_context_paths(self.cfg.get("context_files") or [])
        for src in sources:
            try:
                self.pinned.append(self._pin_form(core.prepare_attachment(src)))
            except ValueError as exc:
                errors.append(str(exc))
        if self.pinned:
            self._pinned_changed()
        if errors:
            self.show_message("⚠ " + "\n\n⚠ ".join(errors))

    @property
    def model(self) -> str:
        return str(self.cfg["claude_model"])

    def _pinned_changed(self):
        """Forget Claude's reading of the old set of files and start reading the new one."""
        self._forget_reading()
        if self.pinned:
            self._start_reading()
        else:
            self.read_state = ""
            self.runner.stop_reading()
        self._update_pinned_label()

    def _start_reading(self):
        """Claude reads the pinned files once for the current model (each model has its own
        prompt cache, so a reading by the other model would not make answers faster)."""
        self.read_state = "reading"
        self.runner.read_files([p.name for p in self.pinned])

    def _forget_reading(self):
        for session_id in self.read_sessions.values():
            core.delete_session(session_id)
        if self.read_sessions:
            self.read_sessions.clear()
            self.runner.set_resume(None)

    def on_files_read(self, reading_id: int, model: str, session_id: str, error: str):
        if reading_id != self.runner.reading_id:  # the files or the model changed meanwhile
            if session_id:
                core.delete_session(session_id)
            return
        if session_id:
            old = self.read_sessions.pop(model, None)
            if old:
                core.delete_session(old)
            self.read_sessions[model] = session_id
            if model == self.model:
                self.read_state = "ready"
                self.runner.set_resume(session_id)
        else:
            self.read_state = "error"
            hint = (" Файли не вмістилися в контекст моделі: закріпіть менше або коротші."
                    if "too long" in error.lower() or "context" in error.lower() else "")
            self.show_message(f"⚠ Claude не зміг прочитати закріплені файли.{hint} Вони все одно "
                              f"йтимуть із запитаннями, але відповіді будуть повільніші.\n\n{error}")
        self._update_pinned_label()

    def _update_pinned_label(self):
        if not self.pinned:
            self.pinned_label.hide()
            self._update_files_panel()
            return
        links = ", ".join(f"<a href='unpin:{i}' style='color:#d7dbe3;text-decoration:none'>"
                          f"{html.escape(p.name)} ✕</a>" for i, p in enumerate(self.pinned))
        state = {"reading": " · ⏳ Claude читає…", "ready": " · ✓ прочитано",
                 "error": " · ⚠ не прочитано"}.get(self.read_state, "")
        self.pinned_label.setText(f"📌 {links}{state} · "
                                  f"<a href='unpin:all' style='color:#9aa3b2'>відкріпити всі</a>")
        self.pinned_label.show()
        self._update_files_panel()

    def _on_pinned_link(self, link: str):
        which = link.partition(":")[2]
        if which == "all":
            self.pinned.clear()
        elif which.isdigit() and int(which) < len(self.pinned):
            del self.pinned[int(which)]
        else:
            return
        self._pinned_changed()

    def mousePressEvent(self, event):  # noqa: N802 - drag the window by its background
        if event.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()

    # ---- startup

    def start(self):
        self.show()
        self.protect()
        self.apply_hotkeys()
        if not core.find_claude(self.cfg):
            self.show_message("⚠ Не знайдено Claude Code. Встановіть його та увійдіть у свій акаунт "
                              "командою `claude` у терміналі.")
        self.load_context_files()
        self.pipeline.start()

    def protect(self):
        """Hide the window from capture and show whether Windows accepted it."""
        hidden = exclude_from_capture(self)
        self.capture_label.setObjectName("ok" if hidden else "bad")
        self.capture_label.setText(system.CAPTURE_HIDDEN_TEXT if hidden else "⚠ ВИДНО НА ДЕМОНСТРАЦІЇ")
        self.capture_label.style().unpolish(self.capture_label)
        self.capture_label.style().polish(self.capture_label)
        if not hidden:
            log.error("hiding the window from capture failed (%s)", system.NAME)

    # ---- hotkeys and settings

    def apply_hotkeys(self):
        """(Re)register the keyboard hotkeys and mouse buttons from cfg."""
        self.hotkeys.set_bindings({name: combo for name, combo in self.cfg["hotkeys"].items()
                                   if name in self.actions})
        self.mouse.set_bindings({name: combo for name, combo in self.cfg["mouse_hotkeys"].items()
                                 if name in self.actions})
        failed = [core.pretty_combo(c) for c in self.hotkeys.failed + self.mouse.failed]
        problem = getattr(self.hotkeys, "problem", "") or getattr(self.mouse, "problem", "")
        if problem:
            self.show_message("⚠ " + problem)
        elif failed:
            self.show_message("⚠ Ці гарячі клавіші зайняті іншою програмою або неправильні: "
                              + ", ".join(failed) + ". Оберіть інші через ⚙.")
        self._update_hint()

    def _update_hint(self):
        names = (("ask", "відповідь"), ("screenshot", "знімок"), ("speak", "озвучити"),
                 ("model", "модель"),
                 ("auto", "голос"), ("listen", "пауза"), ("toggle", "сховати"), ("quit", "вихід"))
        keys, mouse = self.cfg["hotkeys"], self.cfg["mouse_hotkeys"]
        items = [(keys.get(a) or mouse.get(a, ""), label) for a, label in names]
        items = [(core.pretty_combo(c), label) for c, label in items if c]
        prefix = core.pretty_combo("ctrl+alt+x")[:-1]
        if items and all(c.startswith(prefix) for c, _ in items):
            text = prefix[:-1] + " + " + " · ".join(f"{c[len(prefix):]} {label}" for c, label in items)
        else:
            text = " · ".join(f"{c} {label}" for c, label in items)
        self.hint_label.setText((text + " · " if text else "") + "⚙ налаштувати")

    def open_settings(self):
        if self._settings_open:
            return
        self._settings_open = True
        # Off while choosing: pressing a combo must not run its old action.
        self.hotkeys.set_bindings({})
        self.mouse.set_bindings({})
        if not self.isVisible():
            self.show()
            self.protect()
        try:
            dialog = SettingsDialog(self, self.cfg["hotkeys"], self.cfg["mouse_hotkeys"])
            if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
                keys, mouse = dialog.result_bindings()
                self.cfg["hotkeys"].update(keys)
                self.cfg["mouse_hotkeys"] = {a: c for a, c in mouse.items() if c}
                try:
                    core.save_settings(self.cfg)
                except OSError as exc:
                    self.show_message(f"⚠ Не вдалося зберегти settings.json: {exc}. "
                                      "Клавіші діятимуть до перезапуску.")
            dialog.deleteLater()
        finally:
            self._settings_open = False
            self.apply_hotkeys()

    # ---- hotkey actions

    def screenshot(self):
        """Grab the screen under the mouse and send it to Claude right away, with the
        typed question if there is one. The overlay is excluded from capture, so it
        is not on the picture."""
        if self._settings_open:
            return
        screen = (QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
                  or QtGui.QGuiApplication.primaryScreen())
        target = core.free_name("знімок.png")
        image = system.grab_screen(screen, target)
        if image.isNull():
            self.show_message("⚠ Не вдалося зробити знімок екрана.")
            return
        longest = int(self.cfg.get("screenshot_max_side") or 0)
        if longest and max(image.width(), image.height()) > longest:
            # Claude scales big images down anyway; a smaller file is read faster.
            image = image.scaled(longest, longest, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        if not image.save(str(target), "PNG"):
            self.show_message("⚠ Не вдалося зберегти знімок екрана.")
            return
        self.attachments.append(target)
        self.ask(screenshot=True)

    def toggle_visible(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.protect()

    def toggle_listening(self):
        self.pipeline.listening = not self.pipeline.listening
        self.status_label.setText(self.pipeline.describe())

    def toggle_model(self):
        """Switch claude_model <-> claude_model_alt; the answer on screen stays.
        The window title shows the model the next question goes to."""
        main, alt = self._models
        self.cfg["claude_model"] = alt if self.model == main else main
        self.title_label.setText(f"AI · {self.model}")
        session = self.read_sessions.get(self.model)
        self.runner.set_resume(session)  # also restarts the waiting process for this model
        if self.pinned:
            if session:
                self.runner.stop_reading()  # a reading for the other model is not needed now
                self.read_state = "ready"
            else:
                self._start_reading()
            self._update_pinned_label()

    def toggle_auto(self):
        self.auto = not self.auto
        self.show_message("Голосовий режим увімкнено: кожна ваша фраза з мікрофона одразу йде до Claude."
                          if self.auto else "Голосовий режим вимкнено.")

    def focus_input(self):
        if not self.isVisible():
            self.show()
            self.protect()
        system.activate()
        self.activateWindow()
        self.input.setFocus()

    def clear_all(self):
        self.runner.cancel()
        self.current_request = 0
        self.transcript.clear()
        self.history.clear()
        self.partials.clear()
        self._partial_lines = 0
        self.clear_attachments()
        self.transcript_view.clear()
        self._transcript_changed()
        self.answer_view.clear()
        self.answer_text = ""
        self.speaker.stop()
        self._update_player()

    def quit(self):
        self.speaker.close()
        self.runner.shutdown()
        for session_id in self.read_sessions.values():
            core.delete_session(session_id)
        self.hotkeys.stop()
        self.mouse.stop()
        QtWidgets.QApplication.instance().quit()

    # ---- transcript

    def on_phrase(self, source: str, text: str):
        self.partials.pop(source, None)
        self.transcript.add(source, text)
        self._remove_partial_lines()
        self.transcript_view.appendPlainText(f"{core.SOURCE_LABELS.get(source, source)}: {text}")
        self._show_partial_lines()
        if self.auto and source == "mic":
            self.ask(question=text)

    def on_partial(self, source: str, text: str):
        if text:
            self.partials[source] = text
        else:
            self.partials.pop(source, None)
        self._remove_partial_lines()
        self._show_partial_lines()

    def _remove_partial_lines(self):
        doc = self.transcript_view.document()
        for _ in range(self._partial_lines):
            if doc.blockCount() <= 1:
                self.transcript_view.clear()
                break
            cursor = QtGui.QTextCursor(doc)
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.StartOfBlock,
                                QtGui.QTextCursor.MoveMode.KeepAnchor)
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.Left,
                                QtGui.QTextCursor.MoveMode.KeepAnchor)  # and the line break
            cursor.removeSelectedText()
        self._partial_lines = 0

    def _show_partial_lines(self):
        for source, text in self.partials.items():
            self.transcript_view.appendPlainText(f"{core.SOURCE_LABELS.get(source, source)}: {text} …")
            self._partial_lines += 1
        self._transcript_changed()

    # ---- asking claude

    def ask(self, question: str | None = None, screenshot: bool = False):
        if self._settings_open:
            return
        if question is None:  # typed into the question field
            question = self.input.text().strip()
            self.input.clear()
        minutes = self.cfg.get("context_minutes")
        recent = self.transcript.recent(float(minutes) if minutes else None)
        recent += list(self.partials.items())  # what is being said right now
        files = [p.name for p in self.attachments]
        if not question and not recent and not files:
            self.show_message("Поки немає ні транскрипту, ні запитання. Напишіть запитання, "
                              "додайте файл або зачекайте на мовлення.")
            return
        pairs = self.cfg.get("history_pairs")
        history = self.history[-int(pairs):] if pairs else self.history
        pinned_read = (self.read_state == "ready"
                       and self.runner.resume == self.read_sessions.get(self.model))
        prompt = core.build_prompt(question, recent, history, files,
                                   [p.name for p in self.pinned], pinned_read,
                                   screenshot=screenshot)
        self.current_label = question or ("📷 Знімок екрана" if screenshot else
                                          "Файли" if files and not recent else "Підказка за розмовою")
        if files:
            if not (screenshot and len(files) == 1 and not question):  # "📷 Знімок екрана" says it
                self.current_label += f" 📎 {', '.join(files)}"
            self.clear_attachments()  # the copies stay on disk for Claude to read
        self.answer_text = ""
        self.waiting = True
        self.answer_done = False
        self.speaker.stop()
        self._update_player()
        self.current_request = self.runner.ask(prompt)
        self.render_answer()
        if not self.isVisible():
            self.show()
            self.protect()

    def on_delta(self, request_id: int, text: str):
        if request_id != self.current_request:
            return
        self.waiting = False
        self.answer_text += text
        self.render_answer(streaming=True)
        if self.spoken_request == request_id:
            self.speaker.update(self.answer_text, final=False)
        elif self.cfg.get("tts_auto"):
            self._auto_speak(request_id, final=False)

    def _auto_speak(self, request_id: int, final: bool):
        """Auto mode: read the answer while it is still being written. The first
        sentence is synthesized as soon as it is finished."""
        self.spoken_request = request_id
        self.speaker.speak(self.answer_text, final=final)
        self._update_player()

    def on_done(self, request_id: int, answer: str, is_error: bool):
        if request_id != self.current_request:
            return
        self.waiting = False
        self.answer_done = True
        self.answer_text = ("⚠ " + answer) if is_error else answer
        self.render_answer()
        if self.spoken_request == request_id:
            if is_error:
                self.speaker.stop()
            else:
                self.speaker.update(self.answer_text, final=True)
        elif self.cfg.get("tts_auto") and not is_error:  # the answer came in one piece
            self._auto_speak(request_id, final=True)
        if not is_error:
            self.history.append((self.current_label, answer))

    def render_answer(self, streaming: bool = False):
        body = "*…думаю*" if self.waiting and not self.answer_text else self.answer_text
        self._set_markdown(f"**➤ {self.current_label}**\n\n{body}", streaming)

    def show_message(self, text: str):
        self.current_request = 0  # a late answer must not overwrite the message
        self._set_markdown(text)

    def _set_markdown(self, markdown: str, streaming: bool = False):
        bar = self.answer_view.verticalScrollBar()
        position = bar.value()
        self.answer_view.set_answer_markdown(markdown, streaming)
        bar.setValue(position)  # keep the reading position while text streams in


def main():
    if not system.single_instance():
        quit_key = core.pretty_combo(core.load_config()["hotkeys"].get("quit", ""))
        system.message_box("Оверлей уже запущений. Закрийте його"
                           + (f" ({quit_key})" if quit_key else "") + " перед новим запуском.")
        return
    cfg = core.load_config()
    # Start loading Whisper right away, so it runs while Qt starts and the window is
    # built — the model is ready sooner after the overlay is (re)started.
    transcriber = core.Transcriber(cfg, log=log.warning)
    transcriber.start_loading()
    system.before_app()
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    system.prepare_app(app)
    window = Overlay(cfg, transcriber)
    window.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        fatal("AI overlay зупинився через помилку:\n\n" + traceback.format_exc())
