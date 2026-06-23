#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Download the project's pinned ACES OCIO config.
#
# We pin ONE specific config version for the whole project so every artist and
# every app produces identical color, regardless of which Blender build they
# happen to have installed. This is the ACES 1.3 "CG" config — self-contained
# (no external LUT files), the safe production default.
#
# Run this once, then commit/upload the resulting .ocio file to the project at
#   /shared/Legami/02_pipeline/ocio/
# so it ships with the project.
# -----------------------------------------------------------------------------
set -euo pipefail

CONFIG_NAME="cg-config-v2.2.0_aces-v1.3_ocio-v2.4.ocio"
URL="https://github.com/AcademySoftwareFoundation/OpenColorIO-Config-ACES/releases/download/v2.1.0-v2.2.0/${CONFIG_NAME}"

# Download next to this script by default.
DEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${DEST_DIR}/${CONFIG_NAME}"

echo "Downloading pinned ACES config:"
echo "  ${CONFIG_NAME}"
echo "  -> ${DEST}"
curl -fSL "${URL}" -o "${DEST}"

# A stable name Blender/env vars point at, so the version can be bumped later
# without touching everyone's settings.
ln -sf "${CONFIG_NAME}" "${DEST_DIR}/config.ocio"

echo
echo "Done."
echo "  Pinned file : ${CONFIG_NAME}"
echo "  Stable link : ${DEST_DIR}/config.ocio  ->  ${CONFIG_NAME}"
echo
echo "Next: point Blender at it with"
echo "  export BLENDER_OCIO=\"${DEST_DIR}/config.ocio\""
echo "(see COLOR_PIPELINE.md for the full setup)."
