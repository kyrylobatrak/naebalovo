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

# mac/overlay-guard starts the overlay and puts it on a mouse button
# (mouse5 = side "forward"; mouse4 = side "back", mouse3 = wheel click):
# press = stop the overlay and all its processes / start it again,
# Ctrl+press = stop everything and the guard. Source: mac/overlay_guard.cpp
# It is built here on the first start (and after the source changes).
GUARD_BUTTON=mouse5
if [ ! -x mac/overlay-guard ] || [ mac/overlay_guard.cpp -nt mac/overlay-guard ]; then
    echo "Збираю mac/overlay-guard..."
    clang++ -std=c++17 -O2 -o mac/overlay-guard mac/overlay_guard.cpp \
        -framework ApplicationServices -framework CoreFoundation ||
        echo "Не вдалося зібрати (потрібні Xcode Command Line Tools: xcode-select --install)."
fi

# The overlay keeps running after this Terminal window is closed; errors go to overlay.log.
if [ -x mac/overlay-guard ]; then
    nohup mac/overlay-guard "$GUARD_BUTTON" >/dev/null 2>&1 &
    echo "Оверлей запущено. Бічна кнопка миші «вперед»: закрити все / запустити знову."
    echo "Ctrl + ця кнопка: закрити все разом з охоронцем. Це вікно Terminal можна закрити."
else
    nohup .venv-mac/bin/python overlay.py >/dev/null 2>&1 &
    echo "Оверлей запущено (без кнопки миші). Це вікно Terminal можна закрити."
fi
