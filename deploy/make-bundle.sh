#!/usr/bin/env bash
# make-bundle.sh — run on the DEV BOX: build a fresh mrtool source bundle for
# distribution to other machines over the tailnet (no git credentials needed
# on the receiving side).
#
# Usage:
#   make-bundle.sh [repo-path] [out-file]
# Defaults: repo=/home/mattma/dsh_test/mrtool  out=~/dsh-deploy/mrtool-bundle.tar.gz
#
# Hand the tarball to a Windows machine, then:
#   deploy\install.ps1 -BundlePath .\mrtool-bundle.tar.gz ...
# Or pull it from the dev box directly over the tailnet:
#   tailscale cp beastly:~/dsh-deploy/mrtool-bundle.tar.gz .
set -euo pipefail

REPO="${1:-/home/mattma/dsh_test/mrtool}"
OUT="${2:-$HOME/dsh-deploy/mrtool-bundle.tar.gz}"

[ -d "$REPO/.git" ] || { echo "error: $REPO is not a git checkout" >&2; exit 1; }
mkdir -p "$(dirname "$OUT")"
git -C "$REPO" archive --format=tar.gz --prefix=mrtool/ HEAD > "$OUT"

echo "bundle : $OUT ($(du -h "$OUT" | cut -f1))"
echo "commit : $(git -C "$REPO" rev-parse --short HEAD)"
sha256sum "$OUT"
echo
echo "distribute (from the dev box, or from any machine on the tailnet):"
echo "  tailscale cp <devbox-hostname>:~/dsh-deploy/mrtool-bundle.tar.gz ."
