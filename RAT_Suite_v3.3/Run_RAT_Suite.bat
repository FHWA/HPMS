@echo off
title RAT Suite v3.3
rem Run from this file's folder so the relative path below works from any shortcut.
cd /d "%~dp0"
echo Starting RAT Suite v3.3...
python apps/rat_unified_gui.py
echo.
echo RAT Suite exited. Press any key to close this window.
pause > nul