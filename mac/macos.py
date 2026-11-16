"""macOS part of the overlay: everything that talks to macOS.

overlay.py imports this module on macOS and windows.py on Windows; both offer
the same names (see "platform interface" below). Needs PyObjC (mac/requirements.txt).

- Hidden from capture: NSWindow.sharingType = none.
- Global hotkeys and mouse buttons: a Quartz event tap (needs the Accessibility
  permission for the app that runs the overlay, e.g. Terminal).
- Sound: sounddevice; the other side of a call comes from a virtual device such
  as BlackHole, because macOS cannot record the speakers by itself.
- Screenshot: `screencapture -x`, which takes it silently (no shutter sound).
"""
from __future__ import annotations

import ctypes
import fcntl
import logging
import subprocess
import tempfile
import threading

from PySide6 import QtCore, QtGui
from PySide6.QtCore import Qt, Signal

import core

log = logging.getLogger("overlay")

NAME = "macOS"
NO_WINDOW = 0  # creationflags must be 0 outside Windows
CAPTURE_HIDDEN_TEXT = "🔒 приховано від захоплення"
HOTKEY_HELP = "Наприклад Ctrl+Option+S або F8. Esc — скасувати."

core.MOD_LABELS.update(alt="Option", win="Cmd")

# Physical key codes (kVK_* of HIToolbox): the same key whatever the layout.
KEYCODES = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06, "x": 0x07,
    "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10,
    "t": 0x11, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "plus": 0x18,
    "9": 0x19, "7": 0x1A, "minus": 0x1B, "8": 0x1C, "0": 0x1D, "rbracket": 0x1E, "o": 0x1F,
    "u": 0x20, "lbracket": 0x21, "i": 0x22, "p": 0x23, "enter": 0x24, "l": 0x25, "j": 0x26,
    "quote": 0x27, "k": 0x28, "semicolon": 0x29, "backslash": 0x2A, "comma": 0x2B,
    "slash": 0x2C, "n": 0x2D, "m": 0x2E, "period": 0x2F, "tab": 0x30, "space": 0x31,
    "backquote": 0x32, "backspace": 0x33, "esc": 0x35,
    "num.": 0x41, "num*": 0x43, "num+": 0x45, "num/": 0x4B, "num-": 0x4E,
    "num0": 0x52, "num1": 0x53, "num2": 0x54, "num3": 0x55, "num4": 0x56, "num5": 0x57,
    "num6": 0x58, "num7": 0x59, "num8": 0x5B, "num9": 0x5C,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61, "f7": 0x62,
    "f8": 0x64, "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F, "f13": 0x69, "f14": 0x6B,
    "f15": 0x71, "f16": 0x6A, "f17": 0x40, "f18": 0x4F, "f19": 0x50, "f20": 0x5A,
    "insert": 0x72, "home": 0x73, "pageup": 0x74, "delete": 0x75, "end": 0x77,
    "pagedown": 0x79, "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
}
_KEY_NAMES = {code: name for name, code in KEYCODES.items()}
_KEY_NAMES[0x4C] = "enter"  # Enter of the numeric keypad


def _ns_window(widget):
    import objc

    view = objc.objc_object(c_void_p=ctypes.c_void_p(int(widget.winId())))
    return view.window()


def exclude_from_capture(widget) -> bool:
    """Leave the window out of screenshots and screen shares, keep it over full-screen
    apps and on every desktop, and do not hide it when another app is active."""
    try:
        import AppKit

        window = _ns_window(widget)
        if window is None:
            return False
        window.setSharingType_(AppKit.NSWindowSharingNone)
        window.setHidesOnDeactivate_(False)
        window.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                      | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        return window.sharingType() == AppKit.NSWindowSharingNone
    except Exception:  # noqa: BLE001
        log.exception("sharingType")
        return False


def before_app():
    """Before QApplication: Ctrl in hotkeys is the Control key, not Command
    (Qt swaps them on macOS by default); Cmd is "win"."""
    QtCore.QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_MacDontSwapCtrlAndMeta, True)


def prepare_app(app):
    """No Dock icon and no menu bar of its own: the overlay is a helper window."""
    try:
        import AppKit

        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory)
    except Exception:  # noqa: BLE001
        log.exception("activation policy")


def activate():
    """Take the keyboard from the active app (the question field gets the cursor)."""
    try:
        import AppKit

        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:  # noqa: BLE001
        log.exception("activate")


def is_modifier_key(event) -> bool:
    return event.key() in (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Meta,
                           Qt.Key.Key_Alt, Qt.Key.Key_CapsLock)


def key_name_from_event(event) -> str | None:
    return _KEY_NAMES.get(event.nativeVirtualKey())


def parse_hotkey(combo: str) -> tuple[int, int]:
    """'ctrl+alt+s' -> (core.MOD_* bits, macOS key code)."""
    mods, rest = core._split_combo(combo)
    if len(rest) != 1 or rest[0] not in KEYCODES:
        raise ValueError(f"unknown key in hotkey '{combo}'")
    return mods, KEYCODES[rest[0]]


# ------------------------------------------------------- hotkeys (event tap)

PERMISSION_TEXT = ("Гарячі клавіші не працюють: дайте дозвіл у «Системні параметри → "
                   "Приватність і безпека → Універсальний доступ» програмі, з якої запущено "
                   "оверлей (Terminal), і перезапустіть оверлей.")


def _ask_accessibility():
    """Show the system dialog that leads to the Accessibility permission (once)."""
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

        AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    except Exception:  # noqa: BLE001
        log.exception("accessibility prompt")


def _mods(flags: int) -> int:
    import Quartz

    mods = 0
    if flags & Quartz.kCGEventFlagMaskControl:
        mods |= core.MOD_CONTROL
    if flags & Quartz.kCGEventFlagMaskAlternate:
        mods |= core.MOD_ALT
    if flags & Quartz.kCGEventFlagMaskShift:
        mods |= core.MOD_SHIFT
    if flags & Quartz.kCGEventFlagMaskCommand:
        mods |= core.MOD_WIN
    return mods


class _EventTap(QtCore.QObject):
    """Global hotkeys of one kind through a Quartz event tap in its own thread.
    A matched key or click is swallowed, as RegisterHotKey does on Windows."""

    pressed = Signal(str)  # action name
    KIND = ""              # "key" or "mouse"

    def __init__(self):
        super().__init__()
        self.bindings: dict[tuple[int, object], str] = {}
        self.failed: list[str] = []
        self.problem = ""
        self._swallowed: set[str] = set()
        self._thread = None
        self._loop = None
        self._tap = None

    def _parse(self, combo: str):
        return parse_hotkey(combo) if self.KIND == "key" else core.parse_mouse(combo)

    def set_bindings(self, mapping: dict[str, str]):
        bindings, self.failed = {}, []
        for action, combo in mapping.items():
            if not combo:
                continue
            try:
                bindings[self._parse(combo)] = action
            except ValueError as exc:
                log.warning("%s", exc)
                self.failed.append(combo)
        self.bindings = bindings
        if bindings and self._thread is None:
            ready = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(ready,),
                                            name=f"event-tap-{self.KIND}", daemon=True)
            self._thread.start()
            ready.wait(2)
            if self.problem:
                self._thread = None
        elif not bindings:
            self.stop()

    def stop(self):
        thread, self._thread = self._thread, None
        if thread and self._loop is not None:
            import CoreFoundation as CF

            CF.CFRunLoopStop(self._loop)
            thread.join(2)

    def _run(self, ready):
        import CoreFoundation as CF
        import Quartz

        if self.KIND == "key":
            mask = 1 << Quartz.kCGEventKeyDown
        else:
            mask = (1 << Quartz.kCGEventOtherMouseDown) | (1 << Quartz.kCGEventOtherMouseUp)
        tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                      Quartz.kCGEventTapOptionDefault, mask, self._callback, None)
        if tap is None:
            self.problem = PERMISSION_TEXT
            log.error("event tap failed: no Accessibility permission")
            _ask_accessibility()
            ready.set()
            return
        self.problem = ""
        self._tap = tap
        source = CF.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._loop = CF.CFRunLoopGetCurrent()
        CF.CFRunLoopAddSource(self._loop, source, CF.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        ready.set()
        CF.CFRunLoopRun()
        Quartz.CGEventTapEnable(tap, False)
        CF.CFMachPortInvalidate(tap)
        self._tap = self._loop = None

    def _callback(self, proxy, event_type, event, refcon):
        import Quartz

        try:
            if event_type in (Quartz.kCGEventTapDisabledByTimeout,
                              Quartz.kCGEventTapDisabledByUserInput):
                if self._tap is not None:  # macOS turns a slow tap off: turn it back on
                    Quartz.CGEventTapEnable(self._tap, True)
                return event
            mods = _mods(Quartz.CGEventGetFlags(event))
            if event_type == Quartz.kCGEventKeyDown:
                code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                action = self.bindings.get((mods, code))
                if action:
                    if not Quartz.CGEventGetIntegerValueField(
                            event, Quartz.kCGKeyboardEventAutorepeat):
                        self.pressed.emit(action)  # queued to the GUI thread
                    return None
            else:
                number = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventButtonNumber)
                button = {2: "mouse3", 3: "mouse4", 4: "mouse5"}.get(number)
                if button and event_type == Quartz.kCGEventOtherMouseDown:
                    action = self.bindings.get((mods, button))
                    if action:
                        self._swallowed.add(button)
                        self.pressed.emit(action)
                        return None
                elif button in self._swallowed:
                    self._swallowed.discard(button)
                    return None
        except Exception:  # noqa: BLE001 - never break the keyboard or the mouse
            log.exception("event tap")
        return event


class GlobalHotkeys(_EventTap):
    KIND = "key"


class MouseHotkeys(_EventTap):
    KIND = "mouse"


# ------------------------------------------------------------------- sound

def default_audio_ids() -> tuple[str, str]:
    """PortAudio cannot re-read devices while streams are open: switching devices
    on macOS needs a restart of the overlay."""
    return "", ""


def open_audio(cfg: dict, put) -> tuple[object, list[str]]:
    """Microphone and the system sound device (BlackHole). put(source, int16 bytes,
    channels, rate) is called from audio threads."""
    import sounddevice as sd

    streams, notes = [], []

    def open_device(index: int, source: str):
        info = sd.query_devices(index)
        channels = max(1, min(int(info["max_input_channels"]), 2))
        rate = int(info["default_samplerate"])

        def callback(indata, frames, time_info, status):
            put(source, bytes(indata), channels, rate)

        stream = sd.RawInputStream(device=index, channels=channels, samplerate=rate,
                                   dtype="int16", blocksize=max(256, rate // 50),
                                   callback=callback)
        stream.start()
        streams.append(stream)
        log.info("audio %s: %s (%d Hz, %d ch)", source, info["name"], rate, channels)

    if cfg["listen_mic"]:
        try:
            open_device(sd.query_devices(kind="input")["index"], "mic")
            notes.append("🎤 ✓")
        except Exception:  # noqa: BLE001
            log.exception("microphone")
            notes.append("🎤 ✗")
    if cfg["listen_system"]:
        wanted = str(cfg.get("mac_system_device") or "BlackHole").lower()
        try:
            for info in sd.query_devices():
                if wanted in info["name"].lower() and info["max_input_channels"] > 0:
                    open_device(info["index"], "system")
                    notes.append("🔊 ✓")
                    break
            else:
                log.warning("no input device named like %r (install BlackHole)", wanted)
                notes.append("🔊 ✗ (немає BlackHole)")
        except Exception:  # noqa: BLE001
            log.exception("system audio")
            notes.append("🔊 ✗")
    return streams, notes


def close_audio(handle):
    for stream in handle:
        try:
            stream.abort()
            stream.close()
        except Exception:  # noqa: BLE001
            pass


# -------------------------------------------------------------- screenshot

def grab_screen(screen, path) -> QtGui.QImage:
    """Silent screenshot of one screen: `screencapture -x` plays no shutter sound
    (on Apple silicon and Intel alike). Needs the Screen Recording permission."""
    g = screen.geometry()  # points in global coordinates, as screencapture -R wants
    try:
        subprocess.run(["screencapture", "-x", "-t", "png",
                        "-R", f"{g.x()},{g.y()},{g.width()},{g.height()}", str(path)],
                       check=False, timeout=15, capture_output=True)
    except (OSError, subprocess.TimeoutExpired):
        log.exception("screencapture")
        return QtGui.QImage()
    return QtGui.QImage(str(path))


# ----------------------------------------------------------------- startup

_lock_file = None


def single_instance() -> bool:
    """False if another overlay is running already."""
    global _lock_file
    _lock_file = open(f"{tempfile.gettempdir()}/ai-overlay.lock", "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def message_box(text: str, error: bool = False):
    script = ['on run argv',
              f'display alert "AI overlay" message (item 1 of argv)'
              f'{" as critical" if error else ""}',
              'end run']
    args = ["osascript"]
    for line in script:
        args += ["-e", line]
    subprocess.run(args + [text], check=False)
