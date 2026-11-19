"""Windows part of the overlay: everything that talks to the Windows API.

overlay.py imports this module on Windows and mac/macos.py on macOS; both offer
the same names (see "platform interface" at the end of this file).
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes

from PySide6 import QtCore, QtGui
from PySide6.QtCore import Signal

import core

log = logging.getLogger("overlay")

WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11  # Windows 10 2004+
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
CREATE_NO_WINDOW = 0x08000000

user32 = ctypes.windll.user32

# ------------------------------------------------------------ win32 helpers

user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowDisplayAffinity.restype = wintypes.BOOL
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]


def exclude_from_capture(widget) -> bool:
    """Ask Windows to leave this window out of screenshots, recordings and screen shares.

    Returns True only if Windows confirms that the flag is set.
    """
    hwnd = int(widget.winId())
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


# Mouse buttons as hotkeys. RegisterHotKey knows only keys, and a low-level mouse
# hook in Python is dangerous: Windows holds up every mouse event in the system
# until the hook returns, and a Python hook first needs the GIL. While Whisper,
# the GUI or a hung call keep the GIL, the whole PC stops reacting to the mouse
# and keyboard (with LowLevelHooksTimeout raised, for seconds per event).
# So the hook lives in overlay-guard.exe (native/overlay_guard.cpp). The overlay
# only keeps a message-only window whose title lists its bindings; the guard
# posts matching presses to it and swallows them. PostMessage never waits, so a
# busy overlay can delay its own action, never the mouse.

WM_QUIT = 0x0012
WM_OVERLAY_MOUSE = 0x8000 + 2  # WM_APP + 2, as in overlay_guard.cpp
MOUSE_SINK_CLASS = "AiOverlayMouseSink"
HWND_MESSAGE = wintypes.HWND(-3)
kernel32 = ctypes.windll.kernel32

user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


_sink_class_registered = False


def _register_sink_class():
    """The window procedure is DefWindowProcW itself: no Python callback anywhere."""
    global _sink_class_registered
    if _sink_class_registered:
        return
    wc = _WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p).value
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = MOUSE_SINK_CLASS
    if not user32.RegisterClassW(ctypes.byref(wc)):
        raise OSError(ctypes.GetLastError(), "RegisterClassW")
    _sink_class_registered = True


def _guard_running() -> bool:
    handle = kernel32.OpenMutexW(0x00100000, False, r"Local\ai-overlay-guard")  # SYNCHRONIZE
    if handle:
        kernel32.CloseHandle(handle)
    return bool(handle)


class MouseHotkeys(QtCore.QObject):
    """Mouse buttons (wheel click, side buttons) with optional modifiers as global hotkeys.

    overlay-guard.exe catches them and swallows the click, so a side button does
    not also go "back" in a browser. Without the guard they do nothing.
    """

    pressed = Signal(str)  # action name

    def __init__(self):
        super().__init__()
        self.failed: list[str] = []
        self.problem = ""
        self._thread = None
        self._thread_id = 0

    def set_bindings(self, mapping: dict[str, str]):
        """mapping: action -> combo ('ctrl+mouse4'); empty combos are skipped."""
        self.stop()
        bindings, self.failed, self.problem = {}, [], ""
        for action, combo in mapping.items():
            if not combo:
                continue
            try:
                mods, button = core.parse_mouse(combo)
            except ValueError as exc:
                log.warning("%s", exc)
                self.failed.append(combo)
                continue
            bindings[(mods, int(button[len("mouse"):]))] = action
        if not bindings:
            return
        if not _guard_running():
            self.problem = ("Кнопки миші працюють, лише коли оверлей запущено через run.bat "
                            "(overlay-guard.exe).")
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(bindings, ready),
                                        name="mouse-sink", daemon=True)
        self._thread.start()
        ready.wait(2)

    def stop(self):
        thread, self._thread = self._thread, None
        if thread:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            thread.join(2)

    def _run(self, bindings: dict[tuple[int, int], str], ready: threading.Event):
        self._thread_id = kernel32.GetCurrentThreadId()
        title = "ai-overlay-mouse:" + ",".join(f"{button}.{mods}" for mods, button in bindings)
        try:
            _register_sink_class()
            hwnd = user32.CreateWindowExW(0, MOUSE_SINK_CLASS, title, 0, 0, 0, 0, 0,
                                          HWND_MESSAGE, None, kernel32.GetModuleHandleW(None), None)
        except OSError:
            hwnd = None
        ready.set()  # the thread has a message queue now: stop() can reach it
        if not hwnd:
            log.error("mouse hotkey window failed: %s", ctypes.GetLastError())
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_OVERLAY_MOUSE:
                action = bindings.get((int(msg.lParam), int(msg.wParam)))
                if action:
                    self.pressed.emit(action)  # queued to the GUI thread
            else:
                user32.DispatchMessageW(ctypes.byref(msg))
        user32.DestroyWindow(hwnd)


# ------------------------------------------------------- platform interface

NAME = "Windows"
NO_WINDOW = CREATE_NO_WINDOW  # creationflags for child processes: no console window
CAPTURE_HIDDEN_TEXT = "🔒 приховано від захоплення"
HOTKEY_HELP = "Наприклад Ctrl+Alt+S або F8. Esc — скасувати."


def before_app():
    """Called before QApplication is created."""


def prepare_app(app):
    """Called right after QApplication is created."""


def activate():
    """Before the question field takes the cursor (activateWindow is enough here)."""


_MODIFIER_VKS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}


def is_modifier_key(event) -> bool:
    return event.nativeVirtualKey() in _MODIFIER_VKS


def key_name_from_event(event) -> str | None:
    """A key press in the settings -> its name in a hotkey ('s', 'f5'); None if unsupported."""
    return core.key_name(event.nativeVirtualKey())


def open_audio(cfg: dict, put) -> tuple[object, list[str]]:
    """Open the microphone and the loopback of the default speakers (WASAPI).
    put(source, int16 bytes, channels, rate) is called from audio threads.
    Returns (handle for close_audio, status notes)."""
    import pyaudiowpatch as pyaudio

    pa = pyaudio.PyAudio()
    streams = []
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    notes = []

    def open_device(info: dict, source: str):
        channels = int(info["maxInputChannels"])
        rate = int(info["defaultSampleRate"])

        def callback(in_data, frame_count, time_info, status_flags):
            put(source, in_data, channels, rate)
            return None, pyaudio.paContinue

        stream = pa.open(
            format=pyaudio.paInt16, channels=channels, rate=rate, input=True,
            input_device_index=int(info["index"]),
            frames_per_buffer=max(256, rate // 50), stream_callback=callback,
        )
        streams.append(stream)
        log.info("audio %s: %s (%d Hz, %d ch)", source, info["name"], rate, channels)

    if cfg["listen_mic"]:
        try:
            open_device(pa.get_device_info_by_index(wasapi["defaultInputDevice"]), "mic")
            notes.append("🎤 ✓")
        except Exception:  # noqa: BLE001
            log.exception("microphone")
            notes.append("🎤 ✗")
    if cfg["listen_system"]:
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
    return (pa, streams), notes


def close_audio(handle):
    pa, streams = handle
    for stream in streams:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:  # noqa: BLE001
            pass
    pa.terminate()  # the next PyAudio() re-reads the device list


def grab_screen(screen, path) -> QtGui.QImage:
    """Picture of one screen (QScreen). The overlay is excluded from capture, so it
    is not on it."""
    return screen.grabWindow(0).toImage()


_mutex = None


def single_instance() -> bool:
    """False if another overlay is running already."""
    global _mutex
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    _mutex = kernel32.CreateMutexW(None, False, "Local\\ai-overlay-single-instance")
    return ctypes.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def message_box(text: str, error: bool = False):
    user32.MessageBoxW(None, text, "AI overlay", 0x10 if error else 0x40)
