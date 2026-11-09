@echo off
cd /d "%~dp0"
python dynomax_launcher_visual.py
if errorlevel 1 pause
