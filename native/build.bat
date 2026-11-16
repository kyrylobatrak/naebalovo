@echo off
rem Builds overlay-guard.exe (mouse button: stop / start the overlay) next to
rem overlay.py with MinGW g++. Only Windows' own DLLs are needed at run time.
cd /d "%~dp0"
g++ -O2 -static -s -municode -mwindows -fno-exceptions -fno-rtti -o ..\overlay-guard.exe overlay_guard.cpp -luser32 -lshell32 || exit /b 1
echo built ..\overlay-guard.exe
