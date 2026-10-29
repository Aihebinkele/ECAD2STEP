@echo off
cd /d "%~dp0"
start "" pythonw "%~dp0ecad2step_gui.py"
if errorlevel 1 start "" python "%~dp0ecad2step_gui.py"
