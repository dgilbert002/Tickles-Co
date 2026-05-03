@echo off
REM Collector Catalogue Manager — Windows launcher
REM Usage: manage_sources.bat [--tui]
REM
REM Default behaviour prints the web panel URL and exits.
REM Pass --tui to launch the legacy interactive TUI.

cd /d "%~dp0"
python manage_sources.py %*
