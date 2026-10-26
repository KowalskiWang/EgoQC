#!/bin/bash
# One-time setup on macOS: Python environment, hand model, and EgoQC.app in ~/Applications.
#   EGOQC_VENV=/path/to/venv packaging/setup.sh     (default: .venv inside this folder)
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${EGOQC_VENV:-$APP_ROOT/.venv}"
command -v ffmpeg >/dev/null || { echo "ffmpeg is missing / 需要 ffmpeg:  brew install ffmpeg"; exit 1; }
if command -v uv >/dev/null; then
  [ -x "$VENV/bin/python" ] || uv venv --python 3.12 "$VENV"
  uv pip install -q --python "$VENV/bin/python" -r "$APP_ROOT/requirements.txt"
else
  PY="$(command -v python3.12 || command -v python3)"
  [ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install -q --upgrade pip
  "$VENV/bin/python" -m pip install -q -r "$APP_ROOT/requirements.txt"
fi
MODEL="$APP_ROOT/models/hand_landmarker.task"
if [ ! -f "$MODEL" ]; then   # MediaPipe Hand Landmarker (Apache-2.0)
  mkdir -p "$APP_ROOT/models"
  curl -fL -o "$MODEL" https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task
fi
EGOQC_VENV="$VENV" "$APP_ROOT/packaging/make_app.sh"
