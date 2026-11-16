"""Reading Claude's answers aloud, with pause, seek and speed.

Cross-platform: Microsoft Edge neural voices through edge-tts (needs internet),
MP3 decoding and pitch-preserving speed (ffmpeg's atempo) through PyAV,
playback through PyAudio. No Qt here: the overlay polls state() for its controls.

An answer can still be streaming when it is read: update() adds the paragraphs
that have finished since, and the player waits for them if it catches up.
"""
from __future__ import annotations

import asyncio
import io
import re
import threading
import time
import unicodedata
from collections import deque

import numpy as np

import mathtext

RATE = 24000          # edge-tts sends 24 kHz mono
BLOCK = 1200          # samples written at a time: 50 ms, the delay of pause and seek
SPEEDS = (0.5, 3.0)   # slowest and fastest speed
GROUP_CHARS = 260     # sentences are synthesized in groups about this long
PARALLEL = 3          # groups synthesized at the same time


# ------------------------------------------------------------ text to speak

_GREEK = {
    "α": "альфа", "β": "бета", "γ": "гамма", "δ": "дельта", "ε": "епсилон", "ζ": "дзета",
    "η": "ета", "θ": "тета", "ϑ": "тета", "ι": "йота", "κ": "каппа", "λ": "лямбда", "μ": "мю",
    "ν": "ню", "ξ": "ксі", "π": "пі", "ρ": "ро", "σ": "сигма", "ς": "сигма", "τ": "тау",
    "υ": "іпсилон", "φ": "фі", "χ": "хі", "ψ": "псі", "ω": "омега",
    "Γ": "гамма", "Δ": "дельта", "Θ": "тета", "Λ": "лямбда", "Ξ": "ксі", "Π": "пі",
    "Σ": "сигма", "Φ": "фі", "Ψ": "псі", "Ω": "омега",
}
_FORMULA_WORDS = {
    "→": "прямує до", "⟶": "прямує до", "⇒": "отже", "⇔": "тоді й лише тоді, коли",
    "↔": "еквівалентно", "≤": "менше або дорівнює", "≥": "більше або дорівнює",
    "≠": "не дорівнює", "≈": "приблизно дорівнює", "≡": "тотожно дорівнює", "~": "має розподіл",
    "±": "плюс-мінус", "×": "помножити на", "·": "помножити на", "∗": "помножити на",
    "÷": "поділити на", "/": "поділити на", "=": "дорівнює", "+": "плюс", "-": "мінус",
    "−": "мінус", "<": "менше", ">": "більше", "∞": "нескінченність", "∑": "сума",
    "∏": "добуток", "∫": "інтеграл", "√": "корінь з", "∈": "належить", "∉": "не належить",
    "∀": "для всіх", "∃": "існує", "∂": "де", "∇": "набла", "∪": "об'єднання",
    "∩": "перетин", "⊂": "підмножина", "⊆": "підмножина", "∅": "порожня множина",
    "∧": "і", "∨": "або", "¬": "не", "!": "факторіал", "%": "відсотків", "^": "в степені",
    "…": "і так далі", "⋯": "і так далі",
}
_SUPERSCRIPTS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿᵃᵇᶜᵈᵉᶠᵍʰʲᵏˡᵐᵒᵖʳˢᵗᵘᵛʷˣʸᶻ"
_SCRIPTS = re.compile(f"[{_SUPERSCRIPTS}]+")
_SUBSCRIPTS = re.compile("[₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₔₕᵢⱼₖₗₘₙₚᵣₛₜᵤᵥ]+")


def _superscript(match: re.Match) -> str:
    text = match.group(0)
    if text == "²":
        return " у квадраті "
    if text == "³":
        return " у кубі "
    return " в степені " + unicodedata.normalize("NFKC", text) + " "


def spoken_formula(tex: str) -> str:
    """'\\hat\\theta_n \\to \\theta' -> 'тета n прямує до тета'."""
    text = mathtext.latex_to_unicode(tex)
    text = _SCRIPTS.sub(_superscript, text)
    text = _SUBSCRIPTS.sub(lambda m: " " + unicodedata.normalize("NFKC", m.group(0)) + " ", text)
    # Hats, bars and arrows over letters are combining marks: drop them; subscripts -> letters.
    text = "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))
    text = "".join(_GREEK.get(c) and f" {_GREEK[c]} " or c for c in text)
    text = "".join(f" {_FORMULA_WORDS[c]} " if c in _FORMULA_WORDS else c for c in text)
    text = re.sub(r"[(){}\[\]|]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def speech_text(markdown: str) -> str:
    """Claude's Markdown answer -> plain text a voice can read."""
    text = re.sub(r"```.*?(?:```|$)", "\n\nКод показано на екрані.\n\n", markdown, flags=re.S)
    text = mathtext.replace_math(
        text, lambda tex, display: (f"\n\n{spoken_formula(tex)}.\n\n" if display
                                    else f" {spoken_formula(tex)} "))
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)        # pictures
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)     # links -> their text
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\*\*|__|~~|(?<!\w)\*|\*(?!\w)|(?<!\w)_(?=\w)|(?<=\w)_(?!\w)", "", text)
    text = text.replace("→", " — ").replace("⇒", " — ").replace("⚠", "")
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if re.fullmatch(r"\|?[\s:|-]*-[\s:|-]*", line):        # table separator row
            continue
        line = re.sub(r"^(#{1,6}|>+|[-*+•]|\d+[.)])\s+", "", line)
        line = re.sub(r"\s*\|\s*", ", ", line.strip().strip("|")).strip()
        if not line:
            continue
        if line[-1] not in ".!?…:;,":
            line += "."  # a pause after headings and list items
        lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _closed(text: str) -> bool:
    """No code block, $$ formula or $ formula is left open at the end of text."""
    return (text.count("```") % 2 == 0 and text.count("$$") % 2 == 0
            and text.replace("$$", "").count("$") % 2 == 0)


# Where a streaming answer may be cut: a paragraph break or the end of a sentence.
_BOUNDARY = re.compile(r"\n[ \t]*\n|(?<=[.!?…])[ \t]+(?=\S)|(?<=[.!?…:])\n")
_LIST_MARK = re.compile(r"(?:^|\n)[ \t]*(?:\d+|[-*+•])[.)]?$")  # "1." of a list is no sentence


def next_piece(text: str, start: int, final: bool) -> tuple[str, int] | None:
    """The finished part of a streaming answer after `start`: up to the last finished
    sentence or paragraph that is not inside code or a formula, so the voice can start
    with the first sentence. Returns (piece, new start) or None."""
    rest = text[start:]
    if final:
        return (rest, len(text)) if rest.strip() else None
    for match in reversed(list(_BOUNDARY.finditer(rest))):
        head = rest[:match.start()]
        if _closed(head) and not _LIST_MARK.search(head):
            return (head, start + match.end()) if head.strip() else None
    return None


def rebase(old: str, taken: int, new: str) -> tuple[str, int]:
    """The finished answer replaced the streamed text: Claude Code's final result is only
    its last turn, while the stream also had the remarks between tool calls. Returns
    (text, taken) so that what is already read is not read again and nothing is lost."""
    body = new.strip()
    start = old.rfind(body[:40]) if body else -1  # the last turn is at the end of the stream
    if start < 0:
        # Not found: read the new text, skipping the paragraphs that were already read.
        done = {p.strip() for p in re.split(r"\n[ \t]*\n", old[:taken]) if p.strip()}
        skip = 0
        for match in re.finditer(r"[^\n]+(?:\n(?![ \t]*\n)[^\n]*)*", new):
            if match.group(0).strip() not in done:
                break
            skip = match.end()
        return new, skip
    lead = len(new) - len(new.lstrip())
    return new, min(max(taken - start, 0) + (lead if taken > start else 0), len(new))


def sentence_groups(text: str, first_alone: bool) -> list[str]:
    """Split into sentences and join them into groups of about GROUP_CHARS; the very
    first sentence of an answer goes alone, so the voice starts sooner."""
    sentences = [s for s in re.split(r"(?<=[.!?…:;])\s+", text) if s.strip()]
    groups: list[str] = []
    for sentence in sentences:
        if groups and len(groups[-1]) + len(sentence) < GROUP_CHARS and not (first_alone and len(groups) == 1):
            groups[-1] += " " + sentence
        else:
            groups.append(sentence)
    return groups


def pick_voice(text: str, cfg: dict) -> str:
    latin = len(re.findall(r"[A-Za-z]", text))
    cyrillic = len(re.findall(r"[А-Яа-яЁёЇїІіЄєҐґ]", text))
    if latin > cyrillic * 2 and cfg.get("tts_voice_en"):
        return cfg["tts_voice_en"]
    return cfg.get("tts_voice") or "uk-UA-PolinaNeural"


# ------------------------------------------------------------------- audio

def decode_mp3(data: bytes) -> np.ndarray:
    """MP3 bytes -> float32 mono samples at RATE."""
    import av

    resampler = av.AudioResampler(format="flt", layout="mono", rate=RATE)
    parts = []
    with av.open(io.BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                parts.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            parts.append(out.to_ndarray().reshape(-1))
    return np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, np.float32)


class _Tempo:
    """Changes speed without changing pitch (ffmpeg atempo), block by block."""

    def __init__(self, speed: float):
        import av

        self._av = av
        self.graph = av.filter.Graph()
        source = self.graph.add_abuffer(format="flt", sample_rate=RATE, layout="mono")
        tempo = self.graph.add("atempo", f"{speed:.3f}")
        sink = self.graph.add("abuffersink")
        source.link_to(tempo)
        tempo.link_to(sink)
        self.graph.configure()
        self.pts = 0

    def process(self, block: np.ndarray) -> np.ndarray:
        frame = self._av.AudioFrame.from_ndarray(block.reshape(1, -1), format="flt", layout="mono")
        frame.sample_rate = RATE
        frame.pts = self.pts
        self.pts += len(block)
        self.graph.push(frame)
        out = []
        while True:
            try:
                out.append(self.graph.pull().to_ndarray().reshape(-1))
            except (self._av.error.BlockingIOError, self._av.error.EOFError):
                break
        return np.concatenate(out) if out else np.zeros(0, np.float32)


class _Output:
    """Default speakers, opened anew for every play (they may have changed).
    PyAudio on Windows (the overlay has it already); sounddevice elsewhere,
    since its macOS wheels bring their own PortAudio."""

    def __init__(self):
        self._pa = self._stream = self._sd = None
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            pyaudio = None
        if pyaudio is not None:
            self._pa = pyaudio.PyAudio()
            try:
                self._stream = self._pa.open(format=pyaudio.paFloat32, channels=1, rate=RATE,
                                             output=True, frames_per_buffer=BLOCK,
                                             output_device_index=_sound_mapper(self._pa, pyaudio))
            except Exception:
                self._pa.terminate()
                raise
        else:
            import sounddevice as sd
            self._sd = sd.OutputStream(samplerate=RATE, channels=1, dtype="float32",
                                       blocksize=BLOCK)
            self._sd.start()

    def write(self, block: np.ndarray):
        block = np.ascontiguousarray(block, dtype=np.float32)
        if self._sd is not None:
            self._sd.write(block.reshape(-1, 1))
        else:
            self._stream.write(block.tobytes(), len(block))

    def drain(self):
        """Wait until what is buffered has played."""
        if self._sd is not None:
            self._sd.stop()
        else:
            self._stream.stop_stream()

    def close(self):
        """Drops what is still buffered: pause is immediate."""
        try:
            if self._sd is not None:
                self._sd.abort()
                self._sd.close()
            else:
                self._stream.close()
        except Exception:  # noqa: BLE001
            pass
        if self._pa is not None:
            self._pa.terminate()


def _sound_mapper(pa, pyaudio) -> int | None:
    """Windows "Microsoft Sound Mapper - Output": Windows sends it to the default
    device as it is when the stream opens. PortAudio's own default is the one from
    when PortAudio started, and the transcription keeps PortAudio running, so
    headphones plugged in later would stay silent while the voice went to the speakers.
    PortAudio lists the mapper first among the MME outputs."""
    try:
        mme = pa.get_host_api_info_by_type(pyaudio.paMME)
        for k in range(mme["deviceCount"]):
            info = pa.get_device_info_by_host_api_device_index(mme["index"], k)
            if info["maxOutputChannels"] > 0:
                return int(info["index"])
    except Exception:  # noqa: BLE001 - no MME: PortAudio's default device
        pass
    return None


class Speaker:
    """Synthesizes an answer in the background and plays it; every method is thread-safe."""

    def __init__(self, cfg: dict, log=None):
        self.cfg = cfg
        self._log = log or (lambda *a: None)
        self._cond = threading.Condition()
        self._gen = 0                 # bumped on every new text; old threads see it and stop
        self._text = ""
        self._taken = 0               # characters of _text already given to synthesis
        self._final = False           # _text will not grow any more
        self._synth_done = True
        self._pcm = np.zeros(0, np.float32)
        self._pos = 0                 # in samples of _pcm
        self._playing = False
        self._tempo: _Tempo | None = None
        self._last_sound = 0.0
        self._closed = False
        self.error = ""
        self.speed = min(max(float(cfg.get("tts_speed") or 1.0), SPEEDS[0]), SPEEDS[1])
        threading.Thread(target=self._play_loop, name="speaker", daemon=True).start()

    # ---- control

    def speak(self, text: str, final: bool):
        """Read this text from the start (an answer; it may still be streaming)."""
        with self._cond:
            self._gen += 1
            self._text, self._taken, self._final = text, 0, final
            self._synth_done = False
            self._pcm = np.zeros(0, np.float32)
            self._pos = 0
            self._tempo = None
            self._playing = True
            self.error = ""
            gen = self._gen
            self._cond.notify_all()
        threading.Thread(target=self._synth_loop, args=(gen,), name="tts", daemon=True).start()

    def update(self, text: str, final: bool):
        """The streaming answer grew (or ended): read the new paragraphs too."""
        with self._cond:
            if self._synth_done:  # stopped (or all read): nothing to add to
                return
            if text.startswith(self._text[:self._taken]):
                self._text = text
            else:
                self._text, self._taken = rebase(self._text, self._taken, text)
            self._final = final
            self._cond.notify_all()

    def toggle(self):
        """Pause or go on; at the end, start over."""
        with self._cond:
            if not self._playing and self._synth_done and self._pos >= len(self._pcm):
                self._pos = 0
                self._tempo = None
            self._playing = not self._playing
            self._cond.notify_all()

    def stop(self):
        with self._cond:
            self._gen += 1
            self._text, self._taken, self._final = "", 0, True
            self._synth_done = True
            self._pcm = np.zeros(0, np.float32)
            self._pos = 0
            self._playing = False
            self.error = ""
            self._cond.notify_all()

    def seek(self, seconds: float):
        with self._cond:
            self._seek_to(self._pos / RATE + seconds)

    def seek_to(self, seconds: float):
        with self._cond:
            self._seek_to(seconds)

    def _seek_to(self, seconds: float):
        self._pos = int(min(max(seconds, 0.0), len(self._pcm) / RATE) * RATE)
        self._tempo = None
        self._cond.notify_all()

    def change_speed(self, step: float) -> float:
        with self._cond:
            self.speed = round(min(max(self.speed + step, SPEEDS[0]), SPEEDS[1]), 2)
            self._tempo = None
            return self.speed

    def close(self):
        with self._cond:
            self._closed = True
            self._gen += 1
            self._cond.notify_all()

    # ---- state for the window

    def state(self) -> dict:
        with self._cond:
            return {
                "has_audio": bool(self._text),
                "playing": self._playing,
                "position": self._pos / RATE,
                "duration": len(self._pcm) / RATE,
                "complete": self._synth_done,
                "loading": self._playing and self._pos >= len(self._pcm) and not self._synth_done,
                "speed": self.speed,
                "error": self.error,
            }

    def audible(self, tail: float = 1.0) -> bool:
        """Sound is coming out of the speakers now (or just stopped): what a loopback
        recording hears then is this voice, not the other person."""
        return self._playing or time.monotonic() - self._last_sound < tail

    # ---- synthesis thread

    async def _synthesize(self, text: str) -> bytes:
        import edge_tts

        chunks = []
        async for chunk in edge_tts.Communicate(text, pick_voice(text, self.cfg)).stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        return b"".join(chunks)

    async def _render(self, text: str, limit: asyncio.Semaphore) -> np.ndarray | None:
        """One group of sentences -> samples; None if it failed twice (it is skipped)."""
        async with limit:
            for attempt in (1, 2):
                try:
                    pcm = decode_mp3(await self._synthesize(text))
                    if len(pcm):
                        return pcm
                except Exception as exc:  # noqa: BLE001 - no internet, service hiccup, ...
                    self._log("text to speech failed (try %d): %r", attempt, exc)
            return None

    async def _produce(self, gen: int):
        """Sends the answer to synthesis as it streams in, several groups at once, and glues
        the rendered groups to the audio strictly in order, as soon as each is ready."""
        limit = asyncio.Semaphore(PARALLEL)
        pending: deque[asyncio.Task] = deque()
        first, rendered, failed = True, 0, 0
        try:
            while gen == self._gen:
                with self._cond:
                    piece = next_piece(self._text, self._taken, self._final)
                    final = self._final
                    if piece is not None:
                        self._taken = piece[1]
                if piece is not None:
                    for group in sentence_groups(speech_text(piece[0]), first):
                        first = False
                        if re.search(r"\w", group):  # "." alone has nothing to say
                            pending.append(asyncio.ensure_future(self._render(group, limit)))
                    continue
                while pending and pending[0].done():
                    pcm = pending.popleft().result()
                    rendered += 1
                    if pcm is None:
                        failed += 1
                        continue
                    with self._cond:
                        if gen != self._gen:
                            return
                        self._pcm = np.concatenate([self._pcm, pcm])
                        self._cond.notify_all()
                if final and not pending:
                    break
                if pending:
                    await asyncio.wait({pending[0]}, timeout=0.05)
                else:
                    await asyncio.sleep(0.05)  # waiting for the next paragraph of the answer
        finally:
            for task in pending:
                task.cancel()
        with self._cond:
            if gen == self._gen:
                if failed and failed == rendered:
                    self.error = "Не вдалося озвучити (потрібен інтернет)"
                elif failed:
                    self.error = f"не озвучено частин: {failed}"
                self._synth_done = True
                self._cond.notify_all()

    def _synth_loop(self, gen: int):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._produce(gen))
        finally:
            loop.close()

    # ---- playback thread

    def _take(self) -> tuple[np.ndarray | None, str]:
        """Next samples to play (under the lock): (block, "play"), (None, "wait") while
        synthesis is behind, (None, "end") when everything is played."""
        if self._pos >= len(self._pcm):
            return None, ("end" if self._synth_done else "wait")
        if abs(self.speed - 1.0) < 1e-3:
            block = self._pcm[self._pos:self._pos + BLOCK]
            self._pos += len(block)
            return block, "play"
        if self._tempo is None:
            self._tempo = _Tempo(self.speed)
        source = self._pcm[self._pos:self._pos + int(BLOCK * self.speed)]
        self._pos += len(source)
        return self._tempo.process(source), "play"

    def _play_loop(self):
        stream = None

        def close_stream():
            nonlocal stream
            if stream is not None:
                stream.close()
                stream = None

        while True:
            with self._cond:
                if self._closed:
                    break
                block, what = self._take() if self._playing else (None, "paused")
                if what == "end":
                    self._playing = False
                    self._cond.notify_all()
            if what in ("end", "paused"):
                if what == "end" and stream is not None:
                    try:
                        stream.drain()  # let the last words in the buffer play out
                    except Exception:  # noqa: BLE001
                        pass
                    self._last_sound = time.monotonic()
                close_stream()
                with self._cond:
                    self._cond.wait_for(lambda: self._playing or self._closed)
                continue
            if what == "wait":
                # The next sentence is still being synthesized: play silence meanwhile,
                # so the device is open and Bluetooth headphones are awake when it comes.
                block = np.zeros(BLOCK, np.float32)
            if not len(block):
                continue
            try:
                if stream is None:
                    stream = _Output()
                    # Bluetooth headphones wake up on the first sound and lose it.
                    stream.write(np.zeros(RATE // 4, np.float32))
                stream.write(block)
                self._last_sound = time.monotonic()
            except Exception as exc:  # noqa: BLE001 - no output device, device unplugged
                self._log("audio output failed: %r", exc)
                close_stream()
                with self._cond:
                    self._playing = False
                    self.error = "Не вдалося відтворити звук"
        close_stream()
