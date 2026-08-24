# deploy/ — putting the harness + mrtool on a Windows machine (yours or another PC)

The hard constraint of this project is that **the browser which passes
Cloudflare must run on a trusted Windows machine**. This folder installs the
*whole stack* on such a machine so that a DeepSeek Harness agent runs **on
that machine**, next to the browser:

```
Windows box
  ├─ Chrome + launch_cdp.bat          ← the CF-passing browser (human step)
  └─ WSL2 distro (default: Ubuntu-dsh)  ← a small real VM ("smolvm")
       ├─ DeepSeek Harness (agent; bash tool runs mrtool locally)
       ├─ mrtool + store.db + data/    ← research data lives here, not on C:\
       └─ egress: 127.0.0.1:9222 (CDP) + dev-box model endpoint — that's all
Dev box: model endpoint (LLM calls only, OpenAI-compatible API on port 8889)
```

Everything else is centralized: the **model** stays on the dev box (no GPU or
model download on the Windows side), each Windows box has its **own** CF
browser + store, and machines can exchange data with `sync-export`/`sync-import`
if ever needed.

## What the installer does (check-first, in order)

0. **Preflight** — reports the Windows build, and the total/free space on
   the drive that will hold the new distro's image (the drive of
   `%LOCALAPPDATA%`), including how big your existing WSL images already
   are. A fresh distro needs ~8 GB free (2 GB base image + Node 22 + DSH +
   Python venv + headroom); the installer stops with a clear message if
   there isn't room (`-RequiredFreeGB` to override). Reusing an existing
   distro never adds a new image, so the check is informational there.
1. **Tailscale** — install + join the tailnet (skip if already running).
2. **WSL2 + a dedicated distro** (`Ubuntu-dsh` by default) — never touches
   distros that already exist; on Windows 10 enables the required Windows
   features.
3. **Inside the distro** (via `bootstrap-wsl.sh`): Node 22,
   `@deepseek-ai/dsh` (npm), a Python venv with `playwright` (no browser
   binaries — CDP mode attaches to the Windows Chrome), and mrtool extracted
   from a source bundle.
4. **DSH config** — writes `~/.dsh/settings.yaml` pointing at the dev-box
   endpoint, stages the API key, and exports `CDP_HOST` via a bashrc hook so
   agents just run `mrtool.py --cdp …`.

### Windows version ladder (auto-detected from the build number)

| Detected | Action |
|---|---|
| Win 11, build ≥ 22621 (23H2+) | WSL2 + mirrored networking (default for new distros). `127.0.0.1:9222` works as-is. |
| Win 11, 22000–22620 (22H2) | WSL2 + mirrored, opt-in for our distro only. |
| Win 10, 19041–19045 (21H2/22H2) | WSL2 **NAT mode**: installer adds a `netsh` portproxy on the WSL `vEthernet` adapter IP (scoped firewall rule) so the distro can reach `127.0.0.1:9222`; mrtool gets `CDP_HOST=<gateway>` via the env hook. |
| Win 10, 1903 (20H1) | WSL2 unsupported → installer stops with guidance (update to 21H2+, or WSL1 degraded). |
| older | hard stop. |

The installer **verifies empirically** (temporary loopback listener + curl
from inside the distro) whether localhost is shared, and applies the NAT
workaround only when needed.

### Idempotency rules (safe to re-run, safe on a machine that already has stuff)

- **Never deletes anything.** Existing distros, repos, profiles and Chrome
  profiles are untouched.
- **Dedicated distro name** (`Ubuntu-dsh`) so it cannot collide with distros
  you already have; pass `-Distro <name>` to reuse an existing one.
- Every step is check-first: present → `[skip] (already present)`, missing →
  installed. `-Force` refreshes *managed* files (mrtool, DSH settings,
  credentials) and backs them up first (`.prev.<stamp>`).
- `-DryRun` prints the full plan and checks, changing nothing.
- **An interrupted first run heals itself.** If a previous run created the
  distro but died partway, the installer recognizes its own fresh distro
  (marker file `/etc/dsh-managed-by-mrtool`; a distro whose default user is
  still `root` is by definition unpersonalized), creates the missing user,
  moves the distro to mirrored networking where the build supports it, and
  removes any NAT leftovers (portproxy, firewall rule) once mirrored takes
  effect.
- `-RepoPath` must be a real git checkout (the installer verifies this up
  front). A GitHub "Download ZIP" copy has no `.git` and is refused with
  guidance - use `-BundlePath` in that case.

### Why WSL2 (and not Docker)

The deciding constraint is `launch_cdp.bat`: it launches Chrome with
`--remote-debugging-port=9222` and **no** `--remote-debugging-address`, so
the debug port binds to **127.0.0.1 only**. That is deliberate - the agent
can drive the browser, but nothing else on the network can.

- **WSL2 mirrored networking** (Windows 11 23H2+): `127.0.0.1` is shared
  with the host, so the agent reaches Chrome directly and the loopback-only
  property is preserved. On NAT machines the installer adds a scoped
  portproxy - a workaround, but one that keeps the port loopback-scoped.
- **Docker**: a container reaches the host via `host.docker.internal`,
  which resolves to a *non-loopback* host address. A 127.0.0.1-bound
  listener refuses that. Making it work means either adding
  `--remote-debugging-address=0.0.0.0` (exposes the CF-passing browser's
  debug port to the whole LAN - unacceptable) or a host-side proxy, i.e.
  the same portproxy plumbing as the NAT case, for a worse default.

Secondary: on a household PC, WSL2 is a free OS feature already present on
these machines; Docker Desktop is a full app plus its own managed VM.

Docker *would* be the better call if we wanted image-based distribution
(`docker pull` updates, bit-identical environments) or the same agent image
across Linux/Mac/Windows. Neither applies to a Windows-only box driving a
loopback Chrome.

## Setup flows

### Your own machine (already has WSL2 + Tailscale)

```powershell
cd C:\Users\mma\Documents\Repos\mrtool\deploy
powershell -ExecutionPolicy Bypass -File .\install.ps1 -DryRun      # see the plan
powershell -ExecutionPolicy Bypass -File .\install.ps1 `
    -RepoPath C:\Users\mma\Documents\Repos\mrtool `
    -ModelKey <your-8889-token>
```

Expected: tailscale `[skip]`, your existing distros `[skip]`, a fresh
`Ubuntu-dsh` created, in-distro stack installed, DSH pointed at
`http://100.81.7.23:8889/v1`.

### Another household PC (fresh machine)

One-time admin actions **on your side**:

1. **Tailscale auth key** for the new machine (Tailscale admin console →
   Machines → *Get auth key*; scoped to one machine, revocable anytime).
2. **Bundle**: on the dev box, `deploy/make-bundle.sh` (writes
   `~/dsh-deploy/mrtool-bundle.tar.gz`, prints the sha256).

Then on the new machine (run as an **Administrator** PowerShell):

```powershell
# get the installer + bundle (e.g. via the tailnet, once Tailscale is installed,
# or copy them over USB / chat)
tailscale cp <devbox-hostname>:~/dsh-deploy/mrtool-bundle.tar.gz .
powershell -ExecutionPolicy Bypass -File .\install.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File .\install.ps1 `
    -AuthKey tskey-... -BundlePath .\mrtool-bundle.tar.gz -ModelKey <token>
```

### Daily use (either machine)

```powershell
# 1. pass Cloudflare (the only human step) — double-click
\\wsl.localhost\Ubuntu-dsh\home\dsh\dsh\mrtool\launch_cdp.bat
# 2. start the agent in the distro
wsl -d Ubuntu-dsh -u dsh dsh
# 3. inside the agent, everything is local:
#    mrtool.py --cdp check / search / refresh --store ...
```

## Removal (freeing the disk space)

Everything in the distro except the research data is re-creatable by
re-running the installer, so removal is a backup + unregister.

**1. Back up the data** (the only irreplaceable part is `store.db` +
`data/`, inside the distro):

```powershell
wsl -d Ubuntu-dsh -u dsh bash -lc "cd ~/dsh/mrtool && tar czf /tmp/mrtool-backup.tgz store.db athletes.json data 2>/dev/null; echo done"
copy "\\wsl.localhost\Ubuntu-dsh\temp\mrtool-backup.tgz" C:\backups\
```

**2. Delete the distro** (irreversible — do step 1 first):

```powershell
wsl --terminate Ubuntu-dsh
wsl --unregister Ubuntu-dsh
```

`wsl --unregister` deletes the distro's `ext4.vhdx` (the ~3-5 GB). The
`profile-cdp/` Cloudflare profile goes with it — the next install just means
passing Cloudflare again.

**3. Leftovers (only on NAT-mode machines, e.g. Windows 10):**

```powershell
netsh interface portproxy delete v4tov4 listenaddress=<wsl-nat-ip> listenport=9222
Remove-NetFirewallRule -DisplayName "mrtool CDP (WSL)"
```

On mirrored-mode machines (Windows 11 23H2+) there are no Windows-side
artifacts to remove. Tailscale, Chrome, and any pre-existing distros are
untouched by the installer and therefore by this.

**Coming back later:** re-run `install.ps1` as before, then restore the
backup inside the fresh distro:

```powershell
wsl -d Ubuntu-dsh -u dsh bash -lc "cd ~/dsh/mrtool && tar xzf /mnt/c/backups/mrtool-backup.tgz"
```

## Security notes

- The agent's only meaningful egress is the tailnet (model endpoint +
  CDP on localhost). Optional hardening: a Windows/WSL firewall rule
  restricting the distro's outbound traffic to the dev box.
- The CDP portproxy (NAT machines only) is scoped to the WSL `vEthernet`
  adapter by firewall rule.
- `deploy/credentials` (the model key) is gitignored; the bootstrap stages
  the key as a 0600 file and deletes it after use.
- The dev-box model endpoint is auth-gated (Bearer token) and reachable on
  the tailnet (`http://100.81.7.23:8889/v1`).

## Caveats

- The dev-box `llama-server` runs a single inference slot (`--parallel 1`):
  two machines running agents at once will queue. Bump `--parallel` if that
  bites.
- The machine must be on (the agent and the browser are co-located, so "the
  box is on" guarantees both).
- `wsl --install` may require one reboot on some builds; the installer
  detects it and re-runs cleanly (idempotent).
