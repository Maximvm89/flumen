@echo off
REM One-time setup for Windows artists. Double-click this once.
REM Requires Python 3 installed from python.org (check "Add Python to PATH").
cd /d "%~dp0\.."

where py >nul 2>nul && (set PY=py) || (set PY=python)

echo Creating virtual environment...
%PY% -m venv .venv
call ".venv\Scripts\activate.bat"

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo.
echo ============================================================
echo Setup complete.
echo Next:
echo   1. Copy .env.example to .env and enter your FTP login.
echo   2. Run launcher\Legami-Launch-windows.bat to start Blender.
echo ============================================================
pause
