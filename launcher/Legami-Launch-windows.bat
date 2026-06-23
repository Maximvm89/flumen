@echo off
REM Double-click launcher for Windows. Activates the venv and launches Blender
REM with the project OCIO config.
cd /d "%~dp0\.."
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python -m animpipe launch
if errorlevel 1 pause
