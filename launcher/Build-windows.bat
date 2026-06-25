@echo off
REM Build the standalone Legami bundle on Windows (run on the Windows build box).
REM Requires Python 3 + build deps:
REM   pip install -r requirements.txt -r requirements-gui.txt -r requirements-build.txt
cd /d "%~dp0\.."
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python build.py --zip
if errorlevel 1 pause
