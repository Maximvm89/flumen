#!/bin/bash
# One-time setup for Linux artists. Run: bash launcher/Setup-linux.sh
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
echo "  2. Run launcher/Legami-Launch-linux.sh to start Blender."
echo "============================================================"
