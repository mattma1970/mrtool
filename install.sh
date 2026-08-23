#!/usr/bin/env bash
# Set up mrtool: python venv + playwright + chromium browser.
# Browser is installed into ./.browsers (self-contained, no system writes).
# On a normal machine you can omit PLAYWRIGHT_BROWSERS_PATH if you prefer
# the default ~/.cache/ms-playwright location.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install playwright
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers"
./.venv/bin/python -m playwright install chromium

echo
echo "Setup complete. Start with:"
echo "  ./.venv/bin/python mrtool.py auth"
