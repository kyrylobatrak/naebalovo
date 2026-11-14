"""Turn PDFs (lectures, notes) into Markdown with LaTeX formulas, once.

    .venv\\Scripts\\python.exe pdf_to_markdown.py            -> the Lectures folder
    .venv\\Scripts\\python.exe pdf_to_markdown.py D:\\notes   -> another folder

run.bat runs it for Lectures before every start (it does nothing when no PDF is new).

Each PDF gets <folder>\\_text\\<name>.md, written by Claude Code from the PDF
pages: the text word for word and every formula in LaTeX. Plain text extraction
breaks formulas ("¯X = 1 n nX i=1 ξi"), and PDF pages cost Claude about 3-4
times more tokens than this text. A Markdown file newer than its PDF is skipped,
so after adding a lecture just run the script again.

When a folder or PDF from CONFIG["context_files"] has such a file, the overlay
pins the Markdown instead of the PDF (core.expand_context_paths).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import core

MODEL = "opus"      # the most faithful transcription; it is done once per file
PARALLEL = 3        # files converted at the same time

PROMPT = """Прочитай файл «{name}» інструментом Read (усі сторінки) і перепиши його дослівно у Markdown.
- Текст — без змін, нічого не пропускай, не скорочуй і не додавай від себе.
- Усі формули — у LaTeX: $...$ у тексті, $$...$$ окремим рядком.
- Зберігай структуру: заголовки параграфів (##), теореми, означення, доведення, приклади, списки, таблиці.
- Малюнки й схеми — коротко опиши в [квадратних дужках].
- Колонтитули й номери сторінок пропускай.
Виведи лише результат, без вступу й пояснень."""


def convert(exe: str, pdf: Path) -> str:
    target = core.transcript_path(pdf)
    started = time.time()
    # Variables of a Claude Code session this script may run from must not leak into the call.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))}
    result = subprocess.run(
        [exe, "-p", "--model", MODEL, "--tools", "Read", "--allowedTools", "Read",
         "--strict-mcp-config", "--no-session-persistence", "--output-format", "json"],
        input=PROMPT.format(name=pdf.name), capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=pdf.parent, env=env,
    )
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        data = {}
    text = str(data.get("result") or "").strip()
    if result.returncode != 0 or data.get("is_error") or len(text) < 200:
        detail = (text or result.stderr or result.stdout).strip()[-300:]
        return f"✗ {pdf.name}: {detail}"
    target.parent.mkdir(exist_ok=True)
    partial = target.with_suffix(".md.part")
    partial.write_text(text + "\n", encoding="utf-8")
    partial.replace(target)
    return f"✓ {pdf.name} → _text\\{target.name} ({len(text)} символів, {time.time() - started:.0f} с)"


def main():
    folders = [Path(a) for a in sys.argv[1:]] or [core.APP_DIR / "Lectures"]
    pdfs = [pdf for folder in folders for pdf in sorted(folder.glob("*.pdf"))
            if not core.transcript_for(pdf)]
    if not pdfs:  # silent: run.bat calls this on every start
        return
    exe = core.find_claude(core.load_config())
    if not exe:
        sys.exit("Не знайдено Claude Code: встановіть його й увійдіть командою claude.")
    print(f"Перетворюю {len(pdfs)} PDF (по {PARALLEL} одночасно, ~1 хв на 7 сторінок)…", flush=True)
    failed = 0
    with ThreadPoolExecutor(PARALLEL) as pool:
        for line in pool.map(lambda p: convert(exe, p), pdfs):
            failed += line.startswith("✗")
            print(line, flush=True)
    if failed:
        sys.exit(f"Не вдалося: {failed}. Запустіть скрипт ще раз — готові файли буде пропущено.")


if __name__ == "__main__":
    main()
