#!/bin/bash
# AI overlay for macOS. Double-click in Finder or run in Terminal.
# The first run creates .venv-mac and installs the dependencies (a few minutes).
cd "$(dirname "$0")/.." || exit 1

if [ ! -x .venv-mac/bin/python ]; then
    echo "Перший запуск: встановлюю залежності, це кілька хвилин..."
    PY=""
    for candidate in python3.12 python3.11 python3.13 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    done
    if [ -z "$PY" ]; then
        echo "Потрібен Python 3.10 або новіший: brew install python@3.12"
        read -r -n 1 -p "Натисніть будь-яку клавішу..."
        exit 1
    fi
    if ! { "$PY" -m venv .venv-mac &&
           .venv-mac/bin/python -m pip install --upgrade pip &&
           .venv-mac/bin/python -m pip install -r mac/requirements.txt; }; then
        rm -rf .venv-mac
        echo "Встановлення не вдалося, деталі вище."
        read -r -n 1 -p "Натисніть будь-яку клавішу..."
        exit 1
    fi
fi

# New PDFs in Lectures become Markdown with LaTeX formulas (only once each).
.venv-mac/bin/python pdf_to_markdown.py

# The overlay keeps running after this Terminal window is closed; errors go to overlay.log.
nohup .venv-mac/bin/python overlay.py >/dev/null 2>&1 &
echo "Оверлей запущено. Це вікно Terminal можна закрити."
