"""AI overlay for Windows: voice -> Claude Code, hidden from screen capture.

Run with pythonw (see run.bat) so that no console window exists.
Errors go to overlay.log next to this file (created only when an error happens).
"""
from __future__ import annotations

import ctypes
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
from ctypes import wintypes
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
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "AI overlay", 0x10)
    sys.exit(1)


if os.name != "nt":
    fatal("This prototype works on Windows only.")

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt, Signal

    import core
    import mathview
except Exception:  # noqa: BLE001
    fatal("Не вдалося імпортувати залежності. Запустіть run.bat або "
          "pip install -r requirements.txt\n\n" + traceback.format_exc())

user32 = ctypes.windll.user32

# ------------------------------------------------------------ win32 helpers

WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11  # Windows 10 2004+
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
CREATE_NO_WINDOW = 0x08000000

user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowDisplayAffinity.restype = wintypes.BOOL
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]


def exclude_from_capture(hwnd: int) -> bool:
    """Ask Windows to leave this window out of screenshots, recordings and screen shares.

    Returns True only if Windows confirms that the flag is set.
    """
    ok = user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    current = wintypes.DWORD(WDA_NONE)
    user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(current))
    return bool(ok) and current.value == WDA_EXCLUDEFROMCAPTURE


# Default audio devices through the Windows Core Audio COM API (IMMDeviceEnumerator).
# PortAudio reads the device list only once, so it cannot notice that the user
# switched to headphones; this can, and costs well under a millisecond.

ole32 = ctypes.windll.ole32


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    guid = _GUID()
    ole32.CLSIDFromString(text, ctypes.byref(guid))
    return guid


_CLSID_MMDeviceEnumerator = _guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_IID_IMMDeviceEnumerator = _guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")


def _com_method(obj: ctypes.c_void_p, index: int, *argtypes):
    """Method number `index` of a COM object's vtable, returning an HRESULT."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])


def default_audio_ids() -> tuple[str, str]:
    """IDs of the current default (speakers/headphones, microphone); '' if unknown."""
    ole32.CoInitializeEx(None, 0x2)  # apartment-threaded, like PortAudio; no-op if done
    enum = ctypes.c_void_p()
    if ole32.CoCreateInstance(ctypes.byref(_CLSID_MMDeviceEnumerator), None, 0x17,
                              ctypes.byref(_IID_IMMDeviceEnumerator), ctypes.byref(enum)) != 0:
        return "", ""
    ids = []
    try:
        for flow in (0, 1):  # eRender, eCapture
            device = ctypes.c_void_p()
            get_default = _com_method(enum, 4, ctypes.c_int, ctypes.c_int,
                                      ctypes.POINTER(ctypes.c_void_p))
            if get_default(enum, flow, 0, ctypes.byref(device)) != 0 or not device:
                ids.append("")
                continue
            text = ctypes.c_void_p()
            if _com_method(device, 5, ctypes.POINTER(ctypes.c_void_p))(device, ctypes.byref(text)) == 0 and text:
                ids.append(ctypes.wstring_at(text.value))
                ole32.CoTaskMemFree(text)
            else:
                ids.append("")
            _com_method(device, 2)(device)  # Release
    finally:
        _com_method(enum, 2)(enum)  # Release
    return ids[0], ids[1]


class GlobalHotkeys(QtCore.QObject):
    """Global hotkeys through RegisterHotKey: they work while another app has focus.

    They live in their own thread with its own message loop, not in a Qt native
    event filter: PySide6 6.12 breaks when Qt calls a Python filter in the middle
    of a slot (e.g. while a dialog window is being created).
    """

    pressed = Signal(str)  # action name

    def __init__(self):
        super().__init__()
        self.failed: list[str] = []
        self._thread = None
        self._thread_id = 0

    def set_bindings(self, mapping: dict[str, str]):
        """mapping: action -> combo ('ctrl+alt+s'); replaces all hotkeys, empty combos skipped."""
        self.stop()
        self.failed = []
        items = [(action, combo) for action, combo in mapping.items() if combo]
        if not items:
            return
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(items, ready),
                                        name="hotkeys", daemon=True)
        self._thread.start()
        ready.wait(2)

    def stop(self):
        thread, self._thread = self._thread, None
        if thread:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            thread.join(2)

    def _run(self, items, ready):
        self._thread_id = kernel32.GetCurrentThreadId()
        actions: dict[int, str] = {}
        for hotkey_id, (action, combo) in enumerate(items, 1):
            try:
                mods, vk = core.parse_hotkey(combo)
            except ValueError as exc:
                log.warning("%s", exc)
                self.failed.append(combo)
                continue
            if user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk):
                actions[hotkey_id] = action
            else:
                log.warning("hotkey %s is taken by another program", combo)
                self.failed.append(combo)
        ready.set()  # the thread has a message queue now: stop() can reach it
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                action = actions.get(int(msg.wParam))
                if action:
                    self.pressed.emit(action)  # queued to the GUI thread
        for hotkey_id in actions:
            user32.UnregisterHotKey(None, hotkey_id)


# Mouse buttons as hotkeys: RegisterHotKey knows only keys, so a low-level mouse
# hook watches the buttons. It lives in its own thread with its own message loop:
# Windows waits for the hook on every mouse event, and the GUI thread may be busy.

WH_MOUSE_LL = 14
WM_QUIT = 0x0012
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_XBUTTONDOWN, WM_XBUTTONUP = 0x020B, 0x020C
LLMHF_INJECTED = 0x1
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
kernel32 = ctypes.windll.kernel32


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def held_modifiers() -> int:
    """Ctrl/Alt/Shift/Win held right now, as core.MOD_* bits."""
    def down(vk):
        return user32.GetAsyncKeyState(vk) & 0x8000
    mods = 0
    if down(0x11):
        mods |= core.MOD_CONTROL
    if down(0x12):
        mods |= core.MOD_ALT
    if down(0x10):
        mods |= core.MOD_SHIFT
    if down(0x5B) or down(0x5C):
        mods |= core.MOD_WIN
    return mods


class MouseHotkeys(QtCore.QObject):
    """Mouse buttons (wheel click, side buttons) with optional modifiers as global hotkeys.

    A matched click is swallowed, so a side button does not also go "back" in a browser.
    """

    pressed = Signal(str)  # action name

    def __init__(self):
        super().__init__()
        self.bindings: dict[tuple[int, str], str] = {}  # (modifiers, button) -> action
        self.failed: list[str] = []
        self._swallowed: set[str] = set()  # buttons whose release must be swallowed too
        self._proc = HOOKPROC(self._hook)  # must stay referenced while the hook is set
        self._thread = None
        self._thread_id = 0
        self._ready = threading.Event()

    def set_bindings(self, mapping: dict[str, str]):
        """mapping: action -> combo ('ctrl+mouse4'); empty combos are skipped."""
        bindings, self.failed = {}, []
        for action, combo in mapping.items():
            if not combo:
                continue
            try:
                key = core.parse_mouse(combo)
            except ValueError as exc:
                log.warning("%s", exc)
                self.failed.append(combo)
                continue
            bindings[key] = action
        self.bindings = bindings
        if bindings and self._thread is None:
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name="mouse-hook", daemon=True)
            self._thread.start()
            self._ready.wait(2)
        elif not bindings:
            self.stop()

    def stop(self):
        thread, self._thread = self._thread, None
        if thread:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            thread.join(2)

    def _run(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        self._ready.set()
        if not hook:
            log.error("mouse hook failed: %s", ctypes.GetLastError())
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(hook)

    def _hook(self, code, wparam, lparam):
        try:
            if code == 0 and wparam in (WM_MBUTTONDOWN, WM_MBUTTONUP, WM_XBUTTONDOWN, WM_XBUTTONUP):
                info = MSLLHOOKSTRUCT.from_address(lparam)
                if not info.flags & LLMHF_INJECTED:  # leave clicks sent by programs alone
                    if wparam in (WM_MBUTTONDOWN, WM_MBUTTONUP):
                        button = "mouse3"
                    else:
                        button = "mouse4" if (info.mouseData >> 16) == 1 else "mouse5"
                    if wparam in (WM_MBUTTONDOWN, WM_XBUTTONDOWN):
                        action = self.bindings.get((held_modifiers(), button))
                        if action:
                            self._swallowed.add(button)
                            self.pressed.emit(action)  # queued to the GUI thread
                            return 1
                    elif button in self._swallowed:
                        self._swallowed.discard(button)
                        return 1
        except Exception:  # noqa: BLE001 - never break the mouse
            log.exception("mouse hook")
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt name
        try:
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                callback = self.callbacks.get(int(msg.wParam))
                if callback:
                    callback()
                    return True, 0
        except Exception:  # noqa: BLE001 - never break the event loop
            log.exception("hotkey filter")
        return False, 0


# ------------------------------------------------------- audio + whisper

class Pipeline(QtCore.QObject):
    """Captures microphone and system sound, cuts phrases, transcribes them."""

    status = Signal(str)          # one-line state for the header
    phrase = Signal(str, str)     # source ("mic" / "system"), final text of a phrase
    partial = Signal(str, str)    # source, draft of the phrase being spoken ("" = remove)

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.frames: queue.Queue = queue.Queue()
        self.listening = True
        self.transcriber = core.Transcriber(cfg, log=log.warning)
        self._pa = None
        self._streams = []
        self._device_ids = ("", "")
        self._partial_len: dict[str, int] = {}     # phrase length at the last draft
        self._partial_shown: dict[str, bool] = {}  # a draft is on screen
        self._sources_info = ""

    def start(self):
        threading.Thread(target=self._run, name="pipeline", daemon=True).start()

    def describe(self) -> str:
        state = "слухаю" if self.listening else "пауза"
        return f"Whisper {self.transcriber.description} · {self._sources_info} · {state}"

    # runs in the worker thread
    def _run(self):
        try:
            self.status.emit("Завантажую модель Whisper (перший запуск завантажує її з інтернету)…")
            self.transcriber.load()
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
            ids = default_audio_ids()
        except Exception:  # noqa: BLE001
            log.exception("default audio device check")
            return False
        return ids != self._device_ids

    def _close_audio(self):
        for stream in self._streams:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        self._streams = []
        if self._pa is not None:
            self._pa.terminate()  # next PyAudio() re-reads the device list
            self._pa = None

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
        import pyaudiowpatch as pyaudio

        try:
            self._device_ids = default_audio_ids()
        except Exception:  # noqa: BLE001
            self._device_ids = ("", "")
        self._pa = pa = pyaudio.PyAudio()
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        notes = []

        def open_device(info: dict, source: str):
            channels = int(info["maxInputChannels"])
            rate = int(info["defaultSampleRate"])

            def callback(in_data, frame_count, time_info, status_flags):
                self.frames.put((source, in_data, channels, rate))
                return None, pyaudio.paContinue

            stream = pa.open(
                format=pyaudio.paInt16, channels=channels, rate=rate, input=True,
                input_device_index=int(info["index"]),
                frames_per_buffer=max(256, rate // 50), stream_callback=callback,
            )
            self._streams.append(stream)
            log.info("audio %s: %s (%d Hz, %d ch)", source, info["name"], rate, channels)

        if self.cfg["listen_mic"]:
            try:
                open_device(pa.get_device_info_by_index(wasapi["defaultInputDevice"]), "mic")
                notes.append("🎤 ✓")
            except Exception:  # noqa: BLE001
                log.exception("microphone")
                notes.append("🎤 ✗")
        if self.cfg["listen_system"]:
            try:
                speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
                if not speakers.get("isLoopbackDevice"):
                    for loopback in pa.get_loopback_device_info_generator():
                        if speakers["name"] in loopback["name"]:
                            speakers = loopback
                            break
                    else:
                        raise RuntimeError("no loopback device for the default output")
                open_device(speakers, "system")
                notes.append("🔊 ✓")
            except Exception:  # noqa: BLE001
                log.exception("system audio")
                notes.append("🔊 ✗")
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
                cwd=str(core.ATTACH_DIR), creationflags=CREATE_NO_WINDOW,
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
                cwd=str(core.ATTACH_DIR), creationflags=CREATE_NO_WINDOW,
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
QPushButton {
    background: #1b1e25; color: #e6e9ef; border: 1px solid #2c313c; border-radius: 6px;
    padding: 4px 10px;
}
QPushButton:hover { border-color: #5c7cfa; }
QPushButton#capturing { border-color: #5c7cfa; color: #5c7cfa; }
QPushButton#primary { background: #3b5bdb; border-color: #3b5bdb; }
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
    "quit": "Вихід",
}
_MODIFIER_VKS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}


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
            self._on_info("Наприклад Ctrl+Alt+S або F8. Esc — скасувати.")

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
        vk = event.nativeVirtualKey()
        mods = qt_modifiers(event.modifiers())
        if vk == 0x1B and not mods:
            self.stop()
            self._on_info("")
            return
        if vk in _MODIFIER_VKS:
            prefix = core.pretty_combo(core.make_combo(mods, "x"))[:-1]
            self._show(prefix + "…")
            return
        if self.kind == "mouse":
            return
        name = core.key_name(vk)
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
        exclude_from_capture(int(self.winId()))

    def showEvent(self, event):  # noqa: N802 - Qt name
        super().showEvent(event)
        exclude_from_capture(int(self.winId()))  # it is a separate window: a share would show it
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


class Overlay(QtWidgets.QWidget):
    def __init__(self, cfg: dict):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,  # no taskbar button, not in Alt+Tab
        )
        self.cfg = cfg
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
        self.attachments: list[Path] = []  # files for the next question, copied into ATTACH_DIR
        self.pinned: list[Path] = []       # files for every question of this session, also there
        self._models = (str(cfg["claude_model"]), str(cfg.get("claude_model_alt") or "sonnet"))
        self.read_sessions: dict[str, str] = {}  # model -> session in which it read these files
        self.read_state = ""               # for the current model: "", "reading", "ready", "error"
        core.reset_attach_dir()            # also the working folder of Claude Code

        self._build_ui()
        self.setAcceptDrops(True)

        self.runner = ClaudeRunner(cfg)
        self.runner.delta.connect(self.on_delta)
        self.runner.done.connect(self.on_done)
        self.runner.files_read.connect(self.on_files_read)

        self.pipeline = Pipeline(cfg)
        self.pipeline.status.connect(self.status_label.setText)
        self.pipeline.phrase.connect(self.on_phrase)
        self.pipeline.partial.connect(self.on_partial)

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
                "toggle": self.toggle_visible, "settings": self.open_settings, "quit": self.quit}

    def run_action(self, name: str):
        action = self.actions.get(name)
        if action:
            action()

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
        self.transcript_view.setFixedHeight(92)
        self.transcript_view.setPlaceholderText("Тут з'являтиметься транскрипт розмови")
        layout.addWidget(self.transcript_view)

        self.answer_view = mathview.AnswerView()  # draws LaTeX formulas
        self.answer_view.setOpenLinks(False)
        self.answer_view.setPlaceholderText("Відповідь Claude")
        layout.addWidget(self.answer_view, 1)

        self.pinned_label = QtWidgets.QLabel()
        self.pinned_label.setWordWrap(True)
        self.pinned_label.linkActivated.connect(self._on_pinned_link)
        self.pinned_label.hide()
        layout.addWidget(self.pinned_label)

        self.attach_label = QtWidgets.QLabel()
        self.attach_label.setWordWrap(True)
        self.attach_label.linkActivated.connect(self._on_attach_link)
        self.attach_label.hide()
        layout.addWidget(self.attach_label)

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
            return
        names = ", ".join(p.name for p in self.attachments)
        self.attach_label.setText(f"📎 {html.escape(names)} · "
                                  f"<a href='pin' style='color:#9aa3b2'>на всю сесію</a> · "
                                  f"<a href='clear' style='color:#9aa3b2'>прибрати</a>")
        self.attach_label.show()

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
            return
        links = ", ".join(f"<a href='unpin:{i}' style='color:#d7dbe3;text-decoration:none'>"
                          f"{html.escape(p.name)} ✕</a>" for i, p in enumerate(self.pinned))
        state = {"reading": " · ⏳ Claude читає…", "ready": " · ✓ прочитано",
                 "error": " · ⚠ не прочитано"}.get(self.read_state, "")
        self.pinned_label.setText(f"📌 {links}{state} · "
                                  f"<a href='unpin:all' style='color:#9aa3b2'>відкріпити всі</a>")
        self.pinned_label.show()

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
        hidden = exclude_from_capture(int(self.winId()))
        self.capture_label.setObjectName("ok" if hidden else "bad")
        self.capture_label.setText("🔒 приховано від захоплення" if hidden else "⚠ ВИДНО НА ДЕМОНСТРАЦІЇ")
        self.capture_label.style().unpolish(self.capture_label)
        self.capture_label.style().polish(self.capture_label)
        if not hidden:
            log.error("SetWindowDisplayAffinity failed; Windows 10 2004+ is required")

    # ---- hotkeys and settings

    def apply_hotkeys(self):
        """(Re)register the keyboard hotkeys and mouse buttons from cfg."""
        self.hotkeys.set_bindings({name: combo for name, combo in self.cfg["hotkeys"].items()
                                   if name in self.actions})
        self.mouse.set_bindings({name: combo for name, combo in self.cfg["mouse_hotkeys"].items()
                                 if name in self.actions})
        failed = [core.pretty_combo(c) for c in self.hotkeys.failed + self.mouse.failed]
        if failed:
            self.show_message("⚠ Ці гарячі клавіші зайняті іншою програмою або неправильні: "
                              + ", ".join(failed) + ". Оберіть інші через ⚙.")
        self._update_hint()

    def _update_hint(self):
        names = (("ask", "відповідь"), ("screenshot", "знімок"), ("model", "модель"),
                 ("auto", "голос"), ("listen", "пауза"), ("toggle", "сховати"), ("quit", "вихід"))
        keys, mouse = self.cfg["hotkeys"], self.cfg["mouse_hotkeys"]
        items = [(keys.get(a) or mouse.get(a, ""), label) for a, label in names]
        items = [(core.pretty_combo(c), label) for c, label in items if c]
        prefix = "Ctrl+Alt+"
        if items and all(c.startswith(prefix) for c, _ in items):
            text = "Ctrl+Alt + " + " · ".join(f"{c[len(prefix):]} {label}" for c, label in items)
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
        image = screen.grabWindow(0).toImage()
        if image.isNull():
            self.show_message("⚠ Не вдалося зробити знімок екрана.")
            return
        longest = int(self.cfg.get("screenshot_max_side") or 0)
        if longest and max(image.width(), image.height()) > longest:
            # Claude scales big images down anyway; a smaller file is read faster.
            image = image.scaled(longest, longest, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        target = core.free_name("знімок.png")
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
        self.answer_view.clear()
        self.answer_text = ""

    def quit(self):
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

    def on_done(self, request_id: int, answer: str, is_error: bool):
        if request_id != self.current_request:
            return
        self.waiting = False
        self.answer_text = ("⚠ " + answer) if is_error else answer
        self.render_answer()
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


def already_running() -> bool:
    """A second overlay would fight the first over hotkeys and wipe its working folder."""
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    main.mutex = kernel32.CreateMutexW(None, False, "Local\\ai-overlay-single-instance")
    return ctypes.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def main():
    if already_running():
        user32.MessageBoxW(None, "Оверлей уже запущений. Закрийте його (Ctrl+Alt+Q) перед новим "
                           "запуском.", "AI overlay", 0x40)
        return
    cfg = core.load_config()
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = Overlay(cfg)
    window.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        fatal("AI overlay зупинився через помилку:\n\n" + traceback.format_exc())
