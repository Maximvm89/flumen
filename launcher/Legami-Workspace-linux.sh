#!/bin/bash
# Launch the Legami Workspace desktop app (Linux).
cd "$(dirname "$0")/.." || exit 1
if [ -d ".venv" ]; then source .venv/bin/activate; fi
python3 -m workspace_app
