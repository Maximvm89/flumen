#!/bin/bash
# One-time setup for macOS artists. Double-click once (right-click > Open if
# Gatekeeper blocks it). Requires Python 3 (macOS ships python3, or install from
# python.org / Homebrew).
cd "$(dirname "$0")/.." || exit 1

echo "Creating virtual environment..."
python3 -m venv .venv
source .venv/bin/activate

echo "Installing dependencies..."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

echo
echo "============================================================"
echo "Setup complete."
echo "Next:"
echo "  1. Copy .env.example to .env and enter your FTP login."
echo "  2. Run launcher/Legami-Launch-mac.command to start Blender."
echo "============================================================"
