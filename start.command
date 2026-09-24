#!/bin/bash
# PDF Editor — double-click to start (macOS / Linux). First run sets everything up.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up PDF Editor (about a minute)..."
  PYTHON="$(command -v python3)"
  if [ -z "$PYTHON" ]; then
    echo "Python 3 is required. Install it from https://www.python.org/downloads/ and try again."
    read -r -p "Press Enter to close."
    exit 1
  fi
  "$PYTHON" -m venv .venv || { read -r -p "Could not create the environment. Press Enter to close."; exit 1; }
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt || { read -r -p "Install failed. Press Enter to close."; exit 1; }
fi

echo "Starting PDF Editor... (close this window or press Ctrl+C to stop)"
exec .venv/bin/python run.py "$@"
