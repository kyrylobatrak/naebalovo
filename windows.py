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
