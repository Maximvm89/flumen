#!/bin/bash
# Double-click launcher for macOS. Right-click > Open the first time if Gatekeeper
# complains. Lives next to the animpipe project; activates its venv and launches
# Blender with the project OCIO config.
cd "$(dirname "$0")/.." || exit 1
if [ -d ".venv" ]; then
  source .venv/bin/activate
fi
python3 -m animpipe launch
