@echo off
REM Launch the Legami Workspace desktop app (Windows).
cd /d "%~dp0\.."
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python -m workspace_app
if errorlevel 1 pause
