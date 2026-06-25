#!/bin/bash
# Build the standalone Legami bundle on macOS (for proving the spec / Mac artists).
# Requires Python 3 + build deps:
#   pip install -r requirements.txt -r requirements-gui.txt -r requirements-build.txt
cd "$(dirname "$0")/.." || exit 1
if [ -d ".venv" ]; then source .venv/bin/activate; fi
python3 build.py --zip
