"""Logic of the overlay that does not depend on the GUI.

Everything here is importable without PySide6 or audio drivers, so it can be
tested on any machine: config, audio conversion, speech segmentation, the
Whisper wrapper, the Claude Code command line and the parser of its output.
"""
from __future__ import annotations

import copy
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from collections import deque
from pathlib import Path

import numpy as np

APP_DIR = Path(__file__).resolve().parent
SAMPLE_RATE = 16000  # what Whisper expects

# Settings: edit them here and restart the overlay.
CONFIG = {
    # --- Claude Code ---
    "claude_path": "",            # empty = find "claude" automatically
    "claude_model": "opus",       # alias or full model name: opus, sonnet, haiku
    "claude_model_alt": "sonnet", # the other model; hotkeys["model"] switches between the two
    "claude_extra_args": [],      # extra flags for the claude command, if you need any
    # Files that go with every question for the whole session (pinned at start).
    # A folder means all files directly inside it. Use r"..." for Windows paths:
    # "context_files": [r"D:\docs\resume.pdf", r"D:\docs\lecture-notes"],
    # PDFs converted by pdf_to_markdown.py are pinned as their Markdown (exact formulas).
    "context_files": [APP_DIR / "Lectures"],
    # Pinned files are read by Claude once, in the background; later questions
    # continue from that reading (prompt cache), so answers do not wait for files.
    "pdf_text_min_pages": 30,     # pinned PDFs this long go to Claude as plain text (cheaper)
    # --- speech recognition ---
    "whisper_model": "auto",      # auto = large-v3-turbo on an NVIDIA GPU, small on CPU
    "whisper_device": "auto",     # auto | cuda | cpu
    # GPU precision. On a GTX 1660 Ti: int8_float16 2.1 s per phrase, float32 3.5 s,
    # float16 5.3 s (GTX 16xx has no fast float16 path), with the same text.
    "whisper_compute_type": "int8_float16",
    "beam_size": 5,               # 5 = a bit more accurate; on the GPU only +0.1-0.2 s vs 1
    "language": "uk",             # "uk", "en", ... or None to detect per phrase
    "listen_mic": True,           # your voice
    "listen_system": True,        # what plays in speakers/headphones (the other side)
    # macOS cannot record the speakers by itself: system sound comes from a virtual
    # device such as BlackHole (see mac/README.md); this is part of its name.
    "mac_system_device": "BlackHole",
    "auto_answer_mic": False,     # send every finished phrase of yours to Claude
    "silence_sec": 0.5,           # pause that ends a phrase
    "max_segment_sec": 8.0,       # longest phrase before a forced cut
    "partial_sec": 0.6,           # re-recognize the phrase being spoken this often (0 = off)
    "rms_threshold": 0.006,       # loudness above which a chunk counts as speech
    # --- prompt ---
    "context_minutes": None,      # how much transcript goes to Claude (None = all of it)
    "history_pairs": None,        # how many previous Q/A pairs go to Claude (None = all)
    "screenshot_max_side": 1920,  # screenshots bigger than this are scaled down (0 = never)
    # --- reading answers aloud (edge-tts: Microsoft neural voices, needs internet) ---
    "tts_voice": "uk-UA-PolinaNeural",   # or uk-UA-OstapNeural; list: edge-tts --list-voices
    "tts_voice_en": "en-US-AvaNeural",   # for answers that are mostly in English ("" = same voice)
    "tts_auto": True,                    # read every answer aloud as it streams in ("авто")
    "tts_speed": 1.0,                    # 0.5-3.0; the overlay's −/+ change it and save it
    "tts_seek_sec": 5,                   # how far ⏪ / ⏩ jump
    # While the answer is read aloud the overlay does not listen to the speakers; with
    # True it does not listen to the microphone either (it hears the speakers too).
    # With headphones you can set False to keep your own voice in the transcript.
    "tts_mute_mic": True,
    # --- window ---
    "opacity": 0.92,
    "width": 540,
    "height": 460,
    "font_size": 11,
    # Defaults; the ⚙ button of the overlay changes them and saves to settings.json.
    "hotkeys": {
        "ask": "ctrl+alt+enter",      # answer from the transcript (+ typed question)
        "screenshot": "ctrl+alt+s",   # screenshot of the screen -> Claude right away
        "toggle": "ctrl+alt+h",       # show / hide the overlay
        "listen": "ctrl+alt+l",       # pause / resume listening
        "auto": "ctrl+alt+a",         # voice mode on / off
        "focus": "ctrl+alt+t",        # put the cursor into the question field
        "clear": "ctrl+alt+c",        # clear transcript and answer
        "model": "ctrl+alt+z",        # switch claude_model <-> claude_model_alt
        "settings": "",               # open the hotkey settings
        "speak": "ctrl+alt+p",        # read the answer aloud / pause / go on
        "speak_back": "ctrl+alt+left",     # rewind tts_seek_sec
        "speak_forward": "ctrl+alt+right", # skip ahead tts_seek_sec
        "speak_slower": "ctrl+alt+down",
        "speak_faster": "ctrl+alt+up",
        "speak_stop": "",
        "speak_auto": "",             # auto reading of answers on / off
        "quit": "ctrl+alt+q",
    },
    # Mouse buttons for the same actions: "mouse3" (wheel click), "mouse4", "mouse5"
    # (side buttons), with modifiers if wanted: "ctrl+mouse4". Empty = none.
    "mouse_hotkeys": {},
}

SETTINGS_FILE = APP_DIR / "settings.json"  # what the user changed in the overlay

SYSTEM_PROMPT = """Ти — підказувач у маленькому вікні поверх екрана під час живої розмови. Користувач читає тебе на ходу, тому відповідь має читатися за кілька секунд.

Вхід:
- транскрипт Whisper: «Я» — користувач (мікрофон), «Співрозмовник» — звук із колонок; останній рядок може бути обірваним;
- іноді — набране запитання, файли (читай інструментом Read) і матеріали сеансу.

Розпізнавання помиляється: схожі слова, спотворені терміни, формули словами («ікс квадрат»). Відновлюй зміст із контексту, не перепитуй і не коментуй помилки розпізнавання.

На що відповідати (за пріоритетом):
1. Набране або сказане користувачем запитання.
2. Інакше — останнє запитання співрозмовника, на яке ще не відповіли.
3. Запитання немає — одним реченням підкажи, що сказати далі.

Формат:
- Перше речення — пряма відповідь. Без вступів, заголовків і переказу запитання.
- За замовчуванням 2–4 речення: відповідь + одне «чому» (ключова ідея чи механізм). Довше — лише якщо інакше відповідь буде неповною чи хибною.
- Формула — максимум одна, коротка, з поясненням словами, що вона означає.
- Задача, код, доведення — повністю, але тільки ключові кроки, без пояснень очевидного.
- Списки — лише для справжнього переліку: до 4 пунктів, кожен в один рядок.
- Два можливі прочитання — відповідай на ймовірніше; друге — одним рядком «Якщо …, то …», лише коли відповідь суттєво інша.
- Пиши впевнено: без «ймовірно», «схоже», без порад щось уточнити.
- Без посилань на джерела (номери лекцій, теорем, §); теореми — загальновідомою назвою.
- Матеріали сеансу — джерело точних визначень, позначень і чисел.
- Мова — мова розмови або запитання.
- Markdown: $...$ у тексті, $$...$$ окремим рядком, код — у блоках.
- Якщо даних бракує — одне речення, чого саме.

Перед відповіддю: якщо речення можна викреслити без втрати змісту — викресли.
"""


# ----------------------------------------------------------------- config

def load_config() -> dict:
    """A copy of CONFIG with the hotkeys saved from the overlay (settings.json)."""
    cfg = copy.deepcopy(CONFIG)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    for section in ("hotkeys", "mouse_hotkeys"):
        values = saved.get(section) if isinstance(saved, dict) else None
        if isinstance(values, dict):
            cfg[section].update({action: combo for action, combo in values.items()
                                 if action in CONFIG["hotkeys"] and isinstance(combo, str)})
    if isinstance(saved, dict) and isinstance(saved.get("tts_speed"), (int, float)):
        cfg["tts_speed"] = float(saved["tts_speed"])
    if isinstance(saved, dict) and isinstance(saved.get("tts_auto"), bool):
        cfg["tts_auto"] = saved["tts_auto"]
    return cfg


def save_settings(cfg: dict):
    """Keep the hotkeys and reading settings chosen in the overlay for the next start."""
    data = {section: dict(cfg[section]) for section in ("hotkeys", "mouse_hotkeys")}
    data["tts_speed"] = cfg["tts_speed"]
    data["tts_auto"] = bool(cfg.get("tts_auto"))
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- hotkeys

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
_MODS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
         "shift": MOD_SHIFT, "win": MOD_WIN}
_MOD_ORDER = (("ctrl", MOD_CONTROL), ("alt", MOD_ALT), ("shift", MOD_SHIFT), ("win", MOD_WIN))
# The first name of a code is the one written into settings.
_KEYS = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B,
         "escape": 0x1B, "backspace": 0x08, "insert": 0x2D, "delete": 0x2E,
         "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
         "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
         "printscreen": 0x2C, "pause": 0x13, "scrolllock": 0x91,
         "semicolon": 0xBA, "plus": 0xBB, "comma": 0xBC, "minus": 0xBD, "period": 0xBE,
         "slash": 0xBF, "backquote": 0xC0, "lbracket": 0xDB, "backslash": 0xDC,
         "rbracket": 0xDD, "quote": 0xDE,
         "num*": 0x6A, "num+": 0x6B, "num-": 0x6D, "num.": 0x6E, "num/": 0x6F,
         **{f"num{i}": 0x60 + i for i in range(10)}}
_KEY_NAMES: dict[int, str] = {}
for _name, _vk in _KEYS.items():
    _KEY_NAMES.setdefault(_vk, _name)
# Keys that may be a hotkey without Ctrl/Alt/Shift/Win: they do not type text.
_SOLO_KEYS = {"insert", "pause", "scrolllock", "printscreen"}

MOUSE_BUTTONS = {"mouse3": "Колесо (клік)", "mouse4": "Бічна 1", "mouse5": "Бічна 2"}
# How modifiers are shown; macOS shows "alt" as Option and "win" as Cmd.
MOD_LABELS = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}


def _split_combo(text: str) -> tuple[int, list[str]]:
    mods, rest = 0, []
    for part in text.lower().replace(" ", "").split("+"):
        if part in _MODS:
            mods |= _MODS[part]
        elif part:
            rest.append(part)
    return mods, rest


def parse_hotkey(text: str) -> tuple[int, int]:
    """'ctrl+alt+s' -> (modifiers, virtual key code) for RegisterHotKey."""
    mods, rest = _split_combo(text)
    if len(rest) != 1:
        raise ValueError(f"hotkey '{text}' must have exactly one main key")
    part = rest[0]
    if part in _KEYS:
        vk = _KEYS[part]
    elif len(part) == 1 and (part.isascii() and (part.isalpha() or part.isdigit())):
        vk = ord(part.upper())
    elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
        vk = 0x70 + int(part[1:]) - 1
    else:
        raise ValueError(f"unknown key '{part}' in hotkey '{text}'")
    return mods, vk


def parse_mouse(text: str) -> tuple[int, str]:
    """'ctrl+mouse4' -> (modifiers, button name)."""
    mods, rest = _split_combo(text)
    if len(rest) != 1 or rest[0] not in MOUSE_BUTTONS:
        raise ValueError(f"unknown mouse button in '{text}' (mouse3, mouse4 or mouse5)")
    return mods, rest[0]


def key_name(vk: int) -> str | None:
    """Virtual key code -> its name in a hotkey ('s', 'f5', 'enter'); None if unsupported."""
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        return chr(vk).lower()
    if 0x70 <= vk <= 0x87:
        return f"f{vk - 0x70 + 1}"
    return _KEY_NAMES.get(vk)


def needs_modifier(name: str) -> bool:
    """A key that types text or moves the cursor would be taken from every program."""
    return not (name in _SOLO_KEYS or name.startswith("num")
                or (name.startswith("f") and name[1:].isdigit()))


def make_combo(mods: int, main: str) -> str:
    """(modifiers, key or button name) -> 'ctrl+alt+s', in a fixed order."""
    return "+".join([name for name, bit in _MOD_ORDER if mods & bit] + [main])


def pretty_combo(text: str) -> str:
    """'ctrl+alt+mouse4' -> 'Ctrl+Alt+Бічна 1' for the window."""
    if not text:
        return ""
    mods, rest = _split_combo(text)
    names = [MOD_LABELS[name] for name, bit in _MOD_ORDER if mods & bit]
    for part in rest:
        if part in MOUSE_BUTTONS:
            names.append(MOUSE_BUTTONS[part])
        elif len(part) == 1 or (part[0] == "f" and part[1:].isdigit()):
            names.append(part.upper())
        else:
            names.append(part.capitalize())
    return "+".join(names)


# ------------------------------------------------------------------ audio

def to_mono_16k(raw: bytes, channels: int, rate: int) -> np.ndarray:
    """int16 interleaved PCM -> float32 mono at 16 kHz."""
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data[: len(data) - len(data) % channels].reshape(-1, channels).mean(axis=1)
    if rate == SAMPLE_RATE or len(data) == 0:
        return data
    if rate % SAMPLE_RATE == 0:
        # 48 kHz -> 16 kHz and similar: average groups of samples (cheap low-pass)
        factor = rate // SAMPLE_RATE
        data = data[: len(data) - len(data) % factor]
        return data.reshape(-1, factor).mean(axis=1)
    # 44.1 kHz and other rates: smooth a little, then interpolate
    width = max(1, int(round(rate / SAMPLE_RATE)))
    if width > 1:
        data = np.convolve(data, np.ones(width, dtype=np.float32) / width, mode="same")
    n_out = int(len(data) * SAMPLE_RATE / rate)
    positions = np.linspace(0, len(data) - 1, n_out)
    return np.interp(positions, np.arange(len(data)), data).astype(np.float32)


class Segmenter:
    """Cuts a stream of 16 kHz chunks into phrases separated by pauses."""

    def __init__(self, threshold=0.006, silence_sec=0.8, max_sec=15.0,
                 min_voiced_sec=0.25, preroll_sec=0.3):
        self.threshold = threshold
        self.silence = int(silence_sec * SAMPLE_RATE)
        self.max_len = int(max_sec * SAMPLE_RATE)
        self.min_voiced = int(min_voiced_sec * SAMPLE_RATE)
        self.preroll_len = int(preroll_sec * SAMPLE_RATE)
        self.reset()

    def reset(self):
        self.active = False
        self.buf: list[np.ndarray] = []
        self.buf_len = 0
        self.voiced = 0
        self.quiet = 0  # samples since the last voiced chunk
        self.preroll: deque[np.ndarray] = deque()
        self.preroll_total = 0

    def feed(self, chunk: np.ndarray) -> np.ndarray | None:
        """Add a chunk; returns a finished phrase or None."""
        if len(chunk) == 0:
            return None
        rms = float(np.sqrt(np.mean(chunk * chunk)))
        is_voiced = rms >= self.threshold
        if is_voiced and not self.active:
            self.active = True
            self.buf = list(self.preroll)
            self.buf_len = self.preroll_total
            self.preroll.clear()
            self.preroll_total = 0
            self.voiced = 0
        if self.active:
            self.buf.append(chunk)
            self.buf_len += len(chunk)
            if is_voiced:
                self.voiced += len(chunk)
                self.quiet = 0
            else:
                self.quiet += len(chunk)
            if self.quiet >= self.silence or self.buf_len >= self.max_len:
                return self.flush()
        else:
            self.preroll.append(chunk)
            self.preroll_total += len(chunk)
            while self.preroll_total - len(self.preroll[0]) >= self.preroll_len:
                self.preroll_total -= len(self.preroll.popleft())
        return None

    def current(self) -> np.ndarray | None:
        """Audio of the phrase still being spoken, for a draft transcript."""
        if not self.active or self.voiced < self.min_voiced:
            return None
        return np.concatenate(self.buf).astype(np.float32)

    def flush(self) -> np.ndarray | None:
        """Finish the current phrase (also used when the device goes silent)."""
        audio = None
        if self.active and self.voiced >= self.min_voiced:
            audio = np.concatenate(self.buf).astype(np.float32)
        self.reset()
        return audio


# ---------------------------------------------------------------- whisper

def add_nvidia_dll_dirs():
    """Make CUDA DLLs installed by pip (nvidia-cublas-cu12, nvidia-cudnn-cu12) loadable."""
    if os.name != "nt":
        return
    for entry in sys.path:
        base = Path(entry) / "nvidia"
        if not base.is_dir():
            continue
        for bin_dir in base.glob("*/bin"):
            try:
                os.add_dll_directory(str(bin_dir))
            except OSError:
                pass
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


class Transcriber:
    """faster-whisper with a GPU -> CPU fallback."""

    def __init__(self, cfg: dict, log=print):
        self.cfg = cfg
        self.log = log
        self.model = None
        self.description = ""
        self.device = "cpu"

    def load(self):
        add_nvidia_dll_dirs()
        from faster_whisper import WhisperModel

        name = self.cfg["whisper_model"]
        want = self.cfg["whisper_device"]
        candidates = []
        if want in ("auto", "cuda") and self._cuda_available():
            candidates.append(("cuda", self.cfg.get("whisper_compute_type") or "int8_float16",
                               "large-v3-turbo" if name == "auto" else name))
        candidates.append(("cpu", "int8", "small" if name == "auto" else name))

        last_error = None
        for device, compute_type, model_name in candidates:
            try:
                model = WhisperModel(model_name, device=device, compute_type=compute_type)
                # Warm-up: a missing CUDA/cuDNN DLL only shows up on the first run.
                segments, _ = model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en")
                list(segments)
                self.model, self.device = model, device
                self.description = f"{model_name} ({device})"
                return
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                last_error = exc
                self.log(f"Whisper {model_name} on {device} failed: {exc!r}")
        raise RuntimeError(f"could not load a Whisper model: {last_error!r}")

    @staticmethod
    def _cuda_available() -> bool:
        try:
            import ctranslate2
            return ctranslate2.get_cuda_device_count() > 0
        except Exception:  # noqa: BLE001
            return False

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self.model.transcribe(
            audio,
            language=self.cfg.get("language") or None,
            beam_size=int(self.cfg.get("beam_size") or (5 if self.device == "cuda" else 1)),
            vad_filter=True,                   # drops non-speech, cuts hallucinations
            condition_on_previous_text=False,  # phrases are independent
        )
        return " ".join(seg.text.strip() for seg in segments).strip()


# -------------------------------------------------------------- transcript

SOURCE_LABELS = {"mic": "Я", "system": "Співрозмовник"}


class Transcript:
    def __init__(self):
        self.items: deque[tuple[float, str, str]] = deque()

    def add(self, source: str, text: str, now: float | None = None):
        self.items.append((time.time() if now is None else now, source, text))

    def clear(self):
        self.items.clear()

    def recent(self, minutes: float | None, now: float | None = None) -> list[tuple[str, str]]:
        if not minutes:
            return [(src, text) for _, src, text in self.items]
        now = time.time() if now is None else now
        return [(src, text) for ts, src, text in self.items if now - ts <= minutes * 60]


def build_prompt(question: str, transcript: list[tuple[str, str]],
                 history: list[tuple[str, str]], files: list[str] | None = None,
                 pinned: list[str] | None = None, pinned_read: bool = False,
                 screenshot: bool = False) -> str:
    """files: names of attachments for this question; pinned: names of files for the
    whole session. Both lie in ATTACH_DIR, the working folder of Claude Code.
    pinned_read: the conversation already contains Claude's full reading of them.
    screenshot: the last file is a screenshot the user just took; it is what to answer."""
    parts = []
    if pinned and pinned_read:
        parts.append("Матеріали сеансу (" + ", ".join(pinned) + ") ти вже повністю прочитав вище. "
                     "Спирайся на них і не перечитуй.")
    elif pinned:
        names = "\n".join(f"- {name}" for name in pinned)
        parts.append("Матеріали користувача на весь сеанс (вони в поточній теці):\n" + names +
                     "\nЯкщо запитання або розмова можуть їх стосуватися, прочитай потрібні "
                     "інструментом Read і спирайся на них у відповіді.")
    if history:
        lines = []
        for q, a in history:
            lines.append(f"Запитання: {q}\nВідповідь: {a}")
        parts.append("Попередні запитання й відповіді в цьому сеансі:\n" + "\n\n".join(lines))
    if transcript:
        lines = [f"[{SOURCE_LABELS.get(src, src)}] {text}" for src, text in transcript]
        parts.append("Транскрипт останніх хвилин розмови:\n" + "\n".join(lines))
    if files:
        names = "\n".join(f"- {name}" for name in files)
        parts.append("Користувач додав файли (вони в поточній теці):\n" + names +
                     "\nСпершу прочитай їх інструментом Read і врахуй у відповіді.")
    if question:
        parts.append("Запитання користувача: " + question)
    elif screenshot:
        parts.append("Запитання не задано. Користувач щойно зробив знімок свого екрана "
                     f"({files[-1]}). Якщо на ньому задача, тест, код чи запитання — дай "
                     "розв'язок чи відповідь; інакше коротко поясни, що на ньому важливо. "
                     "Транскрипт — лише контекст.")
    elif files and not transcript:
        parts.append("Запитання не задано. Допоможи з тим, що у файлах: якщо там задача "
                     "або запитання — дай розв'язок чи відповідь, інакше коротко поясни зміст.")
    else:
        parts.append("Запитання не задано. Допоможи з тим, про що йдеться просто зараз: "
                     "дай відповідь на останнє запитання співрозмовника або підкажи, що сказати далі.")
    return "\n\n".join(parts)


# ------------------------------------------------------------- claude code

def find_claude(cfg: dict) -> str | None:
    """Path to the official Claude Code executable."""
    if cfg.get("claude_path"):
        return cfg["claude_path"] if Path(cfg["claude_path"]).exists() else None
    found = shutil.which("claude")
    if found:
        return found
    exe = ".exe" if os.name == "nt" else ""
    for candidate in (Path.home() / ".local" / "bin" / f"claude{exe}",
                      Path.home() / ".claude" / "local" / f"claude{exe}",
                      Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
        if candidate.exists():
            return str(candidate)
    return None


def _claude_base(exe: str, cfg: dict) -> list[str]:
    """Flags shared by every call. They must not differ between the reading of the
    pinned files and the questions after it, or the prompt cache is not reused."""
    return [
        exe, "-p",
        "--model", str(cfg["claude_model"]),
        "--system-prompt", SYSTEM_PROMPT,
        "--tools", "Read",
        "--allowedTools", "Read",
        "--strict-mcp-config",          # do not start the user's MCP servers
        *[str(a) for a in cfg.get("claude_extra_args", [])],
    ]


def claude_command(exe: str, cfg: dict, resume: str | None = None) -> list[str]:
    """Non-interactive Claude Code call; the prompt goes to stdin as a stream-json message.

    With stream-json input the process waits for the message as long as needed,
    so it can be started before the question is asked.

    Only the Read tool is on: Claude reads the attached files with it (PDF,
    images, text). It runs in ATTACH_DIR, where the attachments are copied.
    resume: id of the session in which Claude read the pinned files; the question
    continues a copy of it (the original stays as it is, the copy is not saved).
    Flags checked against Claude Code 2.1.x.
    """
    cmd = _claude_base(exe, cfg) + [
        "--input-format", "stream-json",
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "--no-session-persistence",     # do not save these calls as sessions
    ]
    if resume:
        cmd += ["--resume", resume, "--fork-session"]
    return cmd


def reading_command(exe: str, cfg: dict, session_id: str) -> list[str]:
    """Call in which Claude reads the pinned files; saved as session `session_id`."""
    return _claude_base(exe, cfg) + ["--session-id", session_id, "--output-format", "json"]


def reading_prompt(names: list[str]) -> str:
    listing = "\n".join(f"- {name}" for name in names)
    return ("Це матеріали користувача на весь сеанс (файли в поточній теці):\n" + listing +
            "\n\nПрочитай кожен файл повністю інструментом Read: довгі текстові файли — частинами "
            "(offset/limit), PDF — усі сторінки (pages), нічого не пропускай. Наступні запитання "
            "стосуватимуться цих матеріалів, і відповідати треба буде точно за їхнім змістом. "
            "Після читання відповідай одним словом: Готово.")


def delete_session(session_id: str):
    """Remove a saved Claude Code session (the reading of old pinned files)."""
    for path in (Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"):
        try:
            path.unlink()
        except OSError:
            pass


# ------------------------------------------------------------ attachments

# Working folder of Claude Code: attached files are copied here so that Read
# can open them without permission prompts. Lives in %TEMP%, not next to the app.
ATTACH_DIR = Path(tempfile.gettempdir()) / "ai-overlay-files"


def reset_attach_dir():
    shutil.rmtree(ATTACH_DIR, ignore_errors=True)
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)


def free_name(name: str) -> Path:
    """A path in ATTACH_DIR that does not exist yet: report.pdf, report_2.pdf, ..."""
    target = ATTACH_DIR / name
    stem, suffix, n = target.stem, target.suffix, 2
    while target.exists():
        target = ATTACH_DIR / f"{stem}_{n}{suffix}"
        n += 1
    return target


def docx_to_text(path: Path) -> str:
    """Text of a Word .docx, paragraph by paragraph (tables come out cell by cell)."""
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    lines = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        para = re.sub(r"<w:tab/>", "<w:t>\t</w:t>", para)
        para = re.sub(r"<w:br/>", "<w:t>\n</w:t>", para)
        lines.append(html.unescape("".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", para))))
    return "\n".join(lines)


def transcript_path(pdf: Path) -> Path:
    """Where pdf_to_markdown.py puts the Markdown version of a PDF."""
    return pdf.parent / "_text" / (pdf.stem + ".md")


def transcript_for(pdf: Path) -> Path | None:
    """The Markdown version of a PDF if it exists and is not older than the PDF."""
    md = transcript_path(pdf)
    try:
        if md.is_file() and md.stat().st_mtime >= pdf.stat().st_mtime:
            return md
    except OSError:
        pass
    return None


def _lecture_order(path: Path):
    """"Лекція 2" before "Лекція 10": numbers compare as numbers."""
    name = re.sub(r"[’'ʼ`]", "", path.name)  # "Комп’ютерна" and "Компютерна" sort together
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def expand_context_paths(paths: list[str]) -> tuple[list[Path], list[str]]:
    """Paths from CONFIG["context_files"] -> existing files (folders give the files
    directly inside them) and messages about the paths that were not found.
    A PDF with a Markdown version from pdf_to_markdown.py is replaced by it."""
    files, errors = [], []
    for raw in paths:
        path = Path(os.path.expandvars(os.path.expanduser(str(raw))))
        if path.is_dir():
            files += sorted((p for p in path.iterdir() if p.is_file()), key=_lecture_order)
        elif path.is_file():
            files.append(path)
        else:
            errors.append(f"{raw}: файл або теку не знайдено (CONFIG → context_files)")
    files = [(transcript_for(p) or p) if p.suffix.lower() == ".pdf" else p for p in files]
    return files, errors


def pdf_to_text(path: Path, min_pages: int) -> Path | None:
    """A .txt copy of a long PDF with a text layer, page by page; None if the PDF is
    short, scanned (no text) or pypdf is missing. Text costs Claude far fewer
    tokens than PDF pages, so more fits into one reading."""
    if path.suffix.lower() != ".pdf":
        return None
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        if len(reader.pages) < min_pages:
            return None
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception:  # noqa: BLE001 - a broken PDF stays a PDF
        return None
    if sum(len(p.strip()) for p in pages) < 100 * len(pages):  # mostly scans
        return None
    target = free_name(path.stem + ".txt")
    target.write_text("\n\n".join(f"=== Сторінка {i} ===\n{text.strip()}"
                                  for i, text in enumerate(pages, 1)), encoding="utf-8")
    return target


def prepare_attachment(src: Path) -> Path:
    """Copy a file into ATTACH_DIR in a form Claude Code can read; returns the copy.

    Read handles text, PDF and images itself; a .docx becomes a .txt.
    Raises ValueError with a message for the user if the file cannot be used.
    """
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower()
    if suffix == ".doc":
        raise ValueError(f"{src.name}: старий формат .doc не підтримується, збережіть його як .docx або PDF")
    if suffix == ".docx":
        try:
            text = docx_to_text(src)
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            raise ValueError(f"{src.name}: не вдалося прочитати документ ({exc})") from exc
        target = free_name(src.stem + ".txt")
        target.write_text(text, encoding="utf-8")
        return target
    target = free_name(src.name)
    try:
        shutil.copyfile(src, target)
    except OSError as exc:
        raise ValueError(f"{src.name}: не вдалося скопіювати ({exc})") from exc
    return target


def user_message(prompt: str) -> str:
    """One stream-json input line for Claude Code."""
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": prompt}]}}, ensure_ascii=False)


class StreamParser:
    """Turns Claude Code's stream-json lines into text deltas and a final result."""

    def __init__(self):
        self.text = ""          # everything shown so far
        self.final = None       # final answer from the "result" line
        self.is_error = False
        self.finished = False   # the "result" line arrived
        self.noise: list[str] = []   # non-JSON lines (stderr, warnings)
        self._streamed = False       # deltas seen since the last full assistant message
        self._pending_break = False  # start the next turn's text on a new paragraph

    def feed(self, line: str) -> str:
        """Returns new text to append to the answer ('' if nothing)."""
        line = line.strip()
        if not line:
            return ""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            self.noise.append(line)
            return ""
        if not isinstance(data, dict):
            return ""
        kind = data.get("type")
        if kind == "stream_event":
            event = data.get("event") or {}
            if event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    self._streamed = True
                    return self._append(delta["text"])
            return ""
        if kind == "assistant":
            new = ""
            if not self._streamed:  # no partial events came: take the whole message
                blocks = (data.get("message") or {}).get("content") or []
                text = "".join(b.get("text", "") for b in blocks
                               if isinstance(b, dict) and b.get("type") == "text")
                if text:
                    new = self._append(text)
            self._streamed = False
            if self.text and not self.text.endswith("\n"):
                # the next turn of the same answer starts on a new paragraph
                self._pending_break = True
            return new
        if kind == "result":
            self.finished = True
            self.is_error = bool(data.get("is_error"))
            result = data.get("result")
            self.final = result if isinstance(result, str) else None
        return ""

    def _append(self, text: str) -> str:
        if self._pending_break:
            text = "\n\n" + text
            self._pending_break = False
        self.text += text
        return text

    def answer(self) -> str:
        """Full answer after the process ended.

        The "result" line holds only the last turn, i.e. the answer without
        interim remarks of earlier turns, so it wins.
        """
        if self.final and self.final.strip():
            return self.final
        return self.text
