#!/usr/bin/env bash
# launch_cdp.sh - open Chrome the way a human would, with a local debug port,
# so mrtool.py --cdp can attach and drive it. (Linux/macOS variant of
# launch_cdp.bat.) Keep the window open while running mrtool with --cdp.
set -euo pipefail
cd "$(dirname "$0")"
PROFILE="$PWD/profile-cdp"
PORT=9222

CHROME=""
for c in google-chrome google-chrome-stable chromium-browser chromium; do
  if command -v "$c" >/dev/null 2>&1; then CHROME="$c"; break; fi
done
if [ -z "$CHROME" ]; then
  echo "No Chrome/Chromium found on PATH."
  echo "Start one manually instead:"
  echo "  $CHROME --user-data-dir=\"$PROFILE\" --remote-debugging-port=$PORT --no-first-run https://mastersrankings.com"
  exit 1
fi

"$CHROME" --user-data-dir="$PROFILE" --remote-debugging-port="$PORT" \
  --no-first-run --no-default-browser-check https://mastersrankings.com &

echo
echo "Chrome opened with debug port $PORT (profile: profile-cdp)."
echo "WAIT for mastersrankings.com to load normally, then run:"
echo "  ./.venv/bin/python mrtool.py --cdp check"
echo
echo "While that Chrome window stays open you can run, e.g.:"
echo "  ./.venv/bin/python mrtool.py --cdp search \"Jane Smith\""
echo "  ./.venv/bin/python mrtool.py --cdp refresh --store"
echo
echo "TIP: if mrtool says it cannot attach to port $PORT, close ALL other"
echo "     Chrome/Chromium windows first (Chrome can swallow the debug-port flag"
echo "     into an existing instance) and run this launcher again."
