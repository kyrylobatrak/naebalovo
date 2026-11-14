@echo off
rem Creates a virtual environment on the first run, then starts the overlay
rem with pythonw so that no console window exists.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo First run: installing dependencies, this takes a few minutes...
    rem Prefer python.org builds: an Anaconda base ships old runtime DLLs
    rem next to python.exe that break PySide6 ("DLL load failed ... QtCore").
    py -3.11 -m venv .venv 2>nul || py -3.10 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :error
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)
rem New PDFs in Lectures become Markdown with LaTeX formulas (about a minute each,
rem only once); the overlay pins those instead of the PDFs.
".venv\Scripts\python.exe" pdf_to_markdown.py
start "" ".venv\Scripts\pythonw.exe" overlay.py
exit /b 0

:error
echo.
echo Installation failed. Check that Python 3.10+ is installed (python.org) and try again.
pause
exit /b 1
