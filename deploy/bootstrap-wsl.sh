#!/usr/bin/env bash
# bootstrap-wsl.sh - in-distro setup for the mrtool + DSH install.
#
# Run inside the target WSL2 distro (install.ps1 does this for you; manual
# invocation is also fine). Idempotent: every step is check-first, and
# nothing pre-existing is deleted or overwritten unless FORCE=1 (in which
# case managed files are backed up first).
#
# Args (all optional, positional):
#   $1 WORKDIR      home subdir where everything lands   (default: dsh)
#   $2 MODEL_BASE   OpenAI-compatible endpoint           (default: http://100.81.7.23:8889/v1)
#   $3 MODEL_ID     model id                             (default: unsloth/Qwen3.8-27B-GGUF)
#   $4 PROVIDER_ID  provider id in dsh settings          (default: devbox-01)
#   $5 FORCE        1 = refresh managed files (backed up)
#   $6 CDP_MODE     mirrored | nat | unknown  (from install.ps1's loopback probe)
#
# The model API key is read from $WORKDIR/.bootstrap-key (a 0600 file that
# install.ps1 stages) and is removed after use.
set -euo pipefail

# Never block on stdin (apt/debconf prompts read it): EOF beats a hang.
exec </dev/null

# Remove garbage from interrupted runs (a CRLF-corrupted run creates a
# directory literally named 'dsh<CR>' under $HOME).
CR="$(printf '\r')"
for g in "$HOME"/*; do case "$g" in *"$CR"*) rm -rf -- "$g" ;; esac; done

WORKDIR="${1:-dsh}"
MODEL_BASE="${2:-http://100.81.7.23:8889/v1}"
MODEL_ID="${3:-unsloth/Qwen3.8-27B-GGUF}"
PROVIDER_ID="${4:-devbox-01}"
FORCE="${5:-0}"
CDP_MODE="${6:-unknown}"

# Runs as root, or as a user with NOPASSWD sudo (install.ps1 arranges both).
if [ "$(id -u)" = "0" ]; then SUDO=""; else SUDO="sudo -n"; fi

BASE="$HOME/$WORKDIR"
MARK="# >>> dsh-mrtool >>>"
MARK_END="# <<< dsh-mrtool <<<"

say()  { printf '\n\033[1m[bootstrap]\033[0m %s\n' "$*"; }
ok()   { printf '[ok] %s\n' "$*"; }
skip() { printf '[skip] %s (already present)\n' "$*"; }
fail() { printf '\n\033[1;31m[bootstrap] FAILED: %s\033[0m\n' "$*" >&2; exit 1; }

say "target: $BASE  (force=$FORCE cdp_mode=$CDP_MODE)"
mkdir -p "$BASE/bundle"

# ---------------------------------------------------------------- apt ------
say "system packages"
need=()
command -v python3 >/dev/null || need+=(python3 python3-venv python3-pip)
command -v git     >/dev/null || need+=(git)
command -v curl    >/dev/null || need+=(curl ca-certificates)
if [ ${#need[@]} -gt 0 ]; then
  say "apt-get install ${need[*]}"
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "${need[@]}" >/dev/null
  ok "installed: ${need[*]}"
else
  skip "system packages"
fi

# ----------------------------------------------------------------- node -----
say "node.js (>= 20)"
have_node=0
if command -v node >/dev/null; then
  nver=$(node -p 'process.versions.node.split(".")[0]')
  [ "$nver" -ge 20 ] && have_node=1 && ok "node $(node --version)"
fi
if [ "$have_node" -eq 0 ]; then
  if [ "$FORCE" = "1" ] && command -v node >/dev/null; then
    skip "existing node ($(node --version)) kept (use FORCE only for managed files)"
  else
    say "installing Node 22 via NodeSource"
    curl -fsSL https://deb.nodesource.com/setup_22.x | $SUDO bash - >/dev/null
    $SUDO apt-get install -y -qq nodejs >/dev/null
    ok "node $(node --version)"
  fi
fi

# ------------------------------------------------------------------- dsh ----
say "DeepSeek Harness (npm: @deepseek-ai/dsh)"
if command -v dsh >/dev/null; then
  skip "dsh ($(/usr/bin/env bash -lc 'dsh --version' 2>/dev/null | head -1 || echo present))"
elif [ "$FORCE" = "1" ]; then
  say "force: npm update -g @deepseek-ai/dsh"
  $SUDO npm update -g @deepseek-ai/dsh >/dev/null
  ok "dsh updated"
else
  say "npm install -g @deepseek-ai/dsh"
  $SUDO npm install -g @deepseek-ai/dsh >/dev/null
  ok "dsh installed"
fi

# ----------------------------------------------------------------- venv -----
say "python venv ($BASE/venv) with playwright"
if [ -x "$BASE/venv/bin/python" ]; then
  skip "venv"
  [ "$FORCE" = "1" ] && "$BASE/venv/bin/pip" install -q playwright >/dev/null && ok "playwright refreshed"
else
  python3 -m venv "$BASE/venv"
  "$BASE/venv/bin/pip" install -q --upgrade pip >/dev/null
  # CDP mode attaches to the Windows Chrome; no browser binaries needed here.
  "$BASE/venv/bin/pip" install -q playwright >/dev/null
  ok "venv created"
fi

# ------------------------------------------------------------- mrtool -------
say "mrtool (from bundle)"
# Timestamped bundles: pick the NEWEST one (lexicographic = chronological).
BUNDLE=$(ls -1 "$BASE"/bundle/mrtool-bundle-*.tar.gz 2>/dev/null | sort | tail -1 || true)
[ -n "$BUNDLE" ] || fail "no bundle in $BASE/bundle - install.ps1 copies one in (or pass -BundlePath)"
if [ -d "$BASE/mrtool" ] && [ "$FORCE" != "1" ]; then
  skip "mrtool at $BASE/mrtool (use -Force to refresh; old bundle kept for you)"
else
  if [ -d "$BASE/mrtool" ]; then
    bak="$BASE/mrtool.prev.$(date +%s)"
    mv "$BASE/mrtool" "$bak"
    say "existing mrtool backed up to $bak"
  fi
  tar -xzf "$BUNDLE" -C "$BASE"
  ok "mrtool extracted from $(basename "$BUNDLE")"
fi
# Windows-side 'git archive' materializes CRLF (core.autocrlf); shell scripts
# must be LF to run. No-op on files that are already clean.
find "$BASE/mrtool" -type f -name '*.sh' -exec sed -i 's/\r$//' {} +
"$BASE/venv/bin/python" "$BASE/mrtool/mrtool.py" --help >/dev/null 2>&1 \
  || fail "mrtool.py --help failed after extraction"

# --------------------------------------------------------------- settings ---
say "dsh model provider config (~/.dsh/settings.yaml)"
SETTINGS="$HOME/.dsh/settings.yaml"
mkdir -p "$HOME/.dsh"
if [ -f "$SETTINGS" ] && [ "$FORCE" != "1" ]; then
  skip "settings.yaml exists - NOT touched (edit by hand, or re-run with -Force to overwrite from backup)"
else
  [ -f "$SETTINGS" ] && cp "$SETTINGS" "$SETTINGS.prev.$(date +%s)" && say "previous settings.yaml backed up"
  cat > "$SETTINGS" <<EOF
# Managed by mrtool deploy/bootstrap-wsl.sh (force=$FORCE). Edit freely.
llm-pi-ai:
  providers:
    $PROVIDER_ID:
      displayName: DevBox
      apiKeyEnv: DSH_MODEL_API_KEY
      api: openai-completions
      baseURL: $MODEL_BASE
      models:
        - id: $MODEL_ID
          contextWindow: 140000
agent-default-model:
  provider: $PROVIDER_ID
  model: $MODEL_ID
EOF
  ok "settings.yaml written (provider $PROVIDER_ID -> $MODEL_BASE)"
fi

# --------------------------------------------------------- api key ---------
say "model api key (~$WORKDIR/credentials.env)"
KEYFILE="$BASE/.bootstrap-key"
if [ -f "$KEYFILE" ]; then
  KEY=$(cat "$KEYFILE"); rm -f "$KEYFILE"
elif [ "$FORCE" = "1" ]; then
  KEY=""   # force without a key: keep existing file as-is
else
  KEY=""
fi
if [ -n "$KEY" ]; then
  if [ -f "$BASE/credentials.env" ] && [ "$FORCE" != "1" ]; then
    skip "credentials.env exists (kept)"
  else
    umask 077
    printf 'DSH_MODEL_API_KEY=%s\n' "$KEY" > "$BASE/credentials.env"
    chmod 600 "$BASE/credentials.env"
    ok "credentials.env written (0600)"
  fi
elif [ -f "$BASE/credentials.env" ]; then
  skip "credentials.env exists (kept)"
else
  say "NOTE: no key supplied and none stored - put DSH_MODEL_API_KEY=... in $BASE/credentials.env (0600)"
fi

# ------------------------------------------------------- cdp host env -------
say "CDP host env (~$WORKDIR/env.sh)"
if [ "$CDP_MODE" = "nat" ]; then
  cat > "$BASE/env.sh" <<'EOF'
# WSL2 NAT mode: Windows Chrome's debug port is exposed on the NAT gateway IP
# via a netsh portproxy added by install.ps1 (scoped to the vEthernet(WSL) adapter).
_cdpmr_gateway() { ip route show default 2>/dev/null | awk '{print $3}' | head -1; }
export CDP_HOST="$(_cdpmr_gateway)"
export CDP_PORT=9222
EOF
elif [ "$CDP_MODE" = "mirrored" ]; then
  cat > "$BASE/env.sh" <<'EOF'
# WSL2 mirrored networking: localhost is shared with Windows.
export CDP_HOST=127.0.0.1
export CDP_PORT=9222
EOF
else
  cat > "$BASE/env.sh" <<'EOF'
# CDP mode unknown at install time: heuristic. In NAT mode the WSL IP is in
# 172.16.0.0/12 (the WSL NAT subnet) and Windows is the default gateway;
# in mirrored mode the WSL IP is the normal LAN IP and localhost is shared.
_cdpmr_pick() {
  local ip gw
  ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  gw=$(ip route show default 2>/dev/null | awk '{print $3}' | head -1)
  case "$ip" in
    172.1[6-9].*|172.2[0-9].*|172.3[01].*) [ -n "$gw" ] && echo "$gw" || echo 127.0.0.1 ;;
    *) echo 127.0.0.1 ;;
  esac
}
export CDP_HOST="$(_cdpmr_pick)"
export CDP_PORT=9222
EOF
fi
ok "env.sh written (CDP_MODE=$CDP_MODE)"

# ---------------------------------------------------------- shell hooks -----
say "bashrc hooks"
BASHRC="$HOME/.bashrc"
touch "$BASHRC"
if grep -qF "$MARK" "$BASHRC"; then
  sed -i "/$MARK/,/$MARK_END/d" "$BASHRC"
fi
{
  echo "$MARK"
  echo "[ -f \"$HOME/$WORKDIR/credentials.env\" ] && . \"$HOME/$WORKDIR/credentials.env\""
  echo "[ -f \"$HOME/$WORKDIR/env.sh\" ] && . \"$HOME/$WORKDIR/env.sh\""
  echo "$MARK_END"
} >> "$BASHRC"
ok "bashrc marker block (re)written"

# ------------------------------------------------------------- verify -------
say "verification"
ok "dsh: $(dsh --version 2>/dev/null | head -1 || echo 'present (no --version)')"
"$BASE/venv/bin/python" "$BASE/mrtool/mrtool.py" --help >/dev/null && ok "mrtool --help"
if [ -f "$BASE/credentials.env" ]; then
  . "$BASE/credentials.env"
  code=$(curl -s -o /tmp/dsh_model_probe -w '%{http_code}' --max-time 6 \
    -H "Authorization: Bearer ${DSH_MODEL_API_KEY}" "$MODEL_BASE/models" || true)
  if [ "$code" = "200" ]; then
    ok "model endpoint reachable: $MODEL_BASE (HTTP 200)"
  else
    say "WARNING: model endpoint returned HTTP $code from this distro - is the tailnet up? (body: $(head -c 120 /tmp/dsh_model_probe 2>/dev/null))"
  fi
else
  say "skipping endpoint probe (no key configured)"
fi
rm -f /tmp/dsh_model_probe

say "DONE"
cat <<EOF

Next steps (from a Windows terminal):
  1. Start the agent:        wsl -d <distro> -u <user> dsh
  2. Pass Cloudflare once:   double-click
     \\wsl.localhost\<distro>\home\<user>\$WORKDIR\mrtool\launch_cdp.bat
  3. Verify the CDP attach (inside WSL):
     cd ~/$WORKDIR/mrtool && ../venv/bin/python mrtool.py --cdp check
EOF
