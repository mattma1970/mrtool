#!/usr/bin/env bash
# Set up mrtool: python venv + playwright + chromium browser.
# Browser is installed into ./.browsers (self-contained, no system writes).
# Works on Linux and macOS as-is; on Windows run it under Git Bash, MSYS2,
# or WSL.
set -euo pipefail
cd "$(dirname "$0")"

# --- find a python interpreter ------------------------------------------
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "error: no python3/python on PATH. Install Python 3.10+ first." >&2
    echo "  macOS: xcode-select --install   (or https://www.python.org)" >&2
    exit 1
fi

"$PY" -m venv .venv

# venv python: .venv/bin (unix) or .venv/Scripts (Windows)
if [ -x .venv/bin/python ]; then
    VENV_PY=.venv/bin/python
    RUN=.venv/bin/python
elif [ -x .venv/Scripts/python.exe ]; then
    VENV_PY=.venv/Scripts/python.exe
    RUN=".venv\\Scripts\\python.exe"
else
    echo "error: could not find the venv python after creation" >&2
    exit 1
fi

"$VENV_PY" -m pip install --upgrade pip
"$VENV_PY" -m pip install playwright
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers"
"$VENV_PY" -m playwright install chromium

echo
echo "Setup complete. Start with (the one-time Cloudflare step):"
echo "  $RUN mrtool.py auth"
