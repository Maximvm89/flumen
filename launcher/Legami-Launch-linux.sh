#!/bin/bash
# Launcher for Linux. Make executable: chmod +x Legami-Launch-linux.sh
cd "$(dirname "$0")/.." || exit 1
if [ -d ".venv" ]; then
  source .venv/bin/activate
fi
python3 -m animpipe launch
