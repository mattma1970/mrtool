#Requires -Version 5.1
<#
.SYNOPSIS
    Idempotent bootstrap for the mrtool + DeepSeek Harness stack on a
    Windows 10/11 machine (yours, or another household PC).

.DESCRIPTION
    Installs only what is missing, in this order:
      1. Tailscale (if absent) and joins the tailnet
      2. WSL2 + a DEDICATED distro (default: Ubuntu-dsh) - distros that
         already exist are never touched
      3. Inside that distro: Node 22, DeepSeek Harness (npm), a Python
         venv with playwright, and mrtool extracted from a source bundle
      4. Points the in-distro DSH at the dev-box model endpoint
         (OpenAI-compatible, over the tailnet) and exports CDP_HOST for
         this machine's WSL network mode (verified with an empirical
         loopback probe, not just the build number)

    Every step is check-first: re-running reports "already present" and
    changes nothing, unless -Force is passed (managed files are backed up
    before refresh). Use -DryRun to see the full plan without touching
    anything.

.PARAMETER DryRun
    Show the plan and the checks; change nothing.
.PARAMETER Force
    Refresh managed files (mrtool, dsh settings, credentials) from backup.
.PARAMETER Distro
    WSL distro to create or reuse. Default: Ubuntu-dsh.
.PARAMETER WorkDir
    Home subdir inside the distro where everything lands. Default: dsh.
.PARAMETER RepoPath
    Local path to an mrtool git checkout (bundle built via git archive).
.PARAMETER BundlePath
    Local path to a prebuilt mrtool bundle tarball (used instead of RepoPath).
.PARAMETER AuthKey
    Tailscale auth key (tskey-...) for a non-interactive join.
.PARAMETER ModelBase
    OpenAI-compatible endpoint of the dev-box model. Default: the devbox.
.PARAMETER ModelKey
    API key for that endpoint (or put MODEL_KEY=... in deploy/credentials).
.PARAMETER ModelId / ProviderId / DshUser
    Model id, DSH provider id, and local user created in a NEW distro.

.EXAMPLE
    .\install.ps1 -DryRun
    .\install.ps1 -RepoPath C:\Users\mma\Documents\Repos\mrtool
    .\install.ps1 -AuthKey tskey-... -BundlePath .\mrtool-bundle-20260101.tar.gz
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Force,
    [string]$Distro = "Ubuntu-dsh",
    [string]$WorkDir = "dsh",
    [string]$RepoPath = "",
    [string]$BundlePath = "",
    [string]$AuthKey = "",
    [string]$ModelBase = "http://100.81.7.23:8889/v1",
    [string]$ModelKey = "",
    [string]$ModelId = "unsloth/Qwen3.8-27B-GGUF",
    [string]$ProviderId = "devbox-01",
    [string]$DshUser = "dsh",
    [int]$RequiredFreeGB = 8
)

$ErrorActionPreference = "Stop"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

$FORCEINT = if ($Force) { "1" } else { "0" }

function Say  { Write-Host "[install] $args" -ForegroundColor Cyan }
function Ok   { Write-Host "  [ok]   $args" }
function Skip { Write-Host "  [skip] $args (already present)" -ForegroundColor DarkGray }
function Warn { Write-Host "  [warn] $args" -ForegroundColor Yellow }
function Fail { param([string]$Msg) Write-Host ""; Write-Host "[install] FAILED: $Msg" -ForegroundColor Red; exit 1 }
function Step {
    param([string]$What, [scriptblock]$Body)
    if ($DryRun) { Write-Host "  [dry-run] would: $What" -ForegroundColor DarkCyan }
    else { Say "  $What"; & $Body }
}
function Resolve-TsBin {
    $c = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in @(
        "${env:ProgramFiles(x86)}\Tailscale\tailscale.exe",
        "$env:ProgramFiles\Tailscale\tailscale.exe",
        "$env:LOCALAPPDATA\Programs\Tailscale\tailscale.exe")) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}
function Test-TsConnected {
    param([string]$Bin)
    if (-not $Bin) { return $false }
    & $Bin status *> $null
    return ($LASTEXITCODE -eq 0)
}
function Get-CleanLine {
    # wsl.exe (old clients especially) emits ANSI escapes / control chars that
    # .Trim() does not remove - such lines look blank and break -eq tests.
    # Strip everything outside printable ASCII.
    param($Line)
    return (($Line.ToString() -replace '[^\x20-\x7E]', '').Trim())
}
function Send-ToDistro {
    # Push a local file into the distro by copying it from the Windows drvfs
    # mount (/mnt/c/...) inside WSL. Avoids all three known-bad channels:
    #   - \\wsl.localhost (SMB share): flaky after distro state changes
    #   - wsl stdin pipe: old clients drop/corrupt large piped input
    #   - .NET Process stdio: old clients exit -1 when fully piped
    # Staging in %TEMP%\mrtool-deploy keeps the /mnt path space-free.
    param([string]$LocalFile, [string]$DestPath, [string]$Mode)
    $bytes = [System.IO.File]::ReadAllBytes($LocalFile)
    $stageDir = Join-Path $env:TEMP "mrtool-deploy"
    New-Item -ItemType Directory -Force -Path $stageDir | Out-Null
    Get-ChildItem $stageDir -Filter "mrtool-bundle-*.tar.gz" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    $stage = Join-Path $stageDir (Split-Path -Leaf $LocalFile)
    Copy-Item -Path $LocalFile -Destination $stage -Force
    $drive = $stage.Substring(0, 1).ToLower()
    $mnt = "/mnt/$drive" + ($stage.Substring(2) -replace '\\', '/')
    $d = Split-Path -Parent $DestPath
    $cmd = "mkdir -p '$d' && cp -f '$mnt' '$DestPath' && chmod $Mode '$DestPath' && wc -c < '$DestPath'"
    $out = & wsl -d $Distro -u root -- /bin/bash -c $cmd 2>&1
    if ($LASTEXITCODE -ne 0) { Fail "could not copy $DestPath into $Distro (wsl exited $LASTEXITCODE). The Windows drive must be mounted inside the distro (/mnt/$drive) - check /etc/wsl.conf for a disabled automount." }
    $n = ""
    foreach ($l in @($out)) { $t = Get-CleanLine $l; if ($t -match '^\d+$') { $n = $t } }
    if ($n -ne "$($bytes.Length)") { Fail "size mismatch writing $DestPath (expected $($bytes.Length) bytes, got $n)" }
    return $n
}

# --------------------------------------------------------------- preflight --
Say "preflight"
$os = Get-CimInstance Win32_OperatingSystem
$Build = [int]$os.BuildNumber
$IsWin11 = $Build -ge 22000
$IsWin10 = ($Build -ge 19041) -and (-not $IsWin11)
Ok ("Windows: {0} (build {1})" -f $os.Caption.Trim(), $Build)

switch ($true) {
    ($Build -ge 22621) { $ModeTier = "mirrored networking (default on this build)" }
    ($Build -ge 22000) { $ModeTier = "mirrored networking (opt-in for our distro)" }
    ($IsWin10)         { $ModeTier = "NAT mode (Windows 10) - portproxy workaround will be applied if needed" }
    ($Build -eq 1903)  { Fail "Windows 10 20H1 (build 1903) does not support WSL2. Update Windows to 21H2+ and re-run, or accept WSL1 (degraded)." }
    default            { Fail "Windows build $Build is too old for WSL2 (need >= 19041)." }
}
Ok "WSL2 tier: $ModeTier"

$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Ok ("administrator: {0}" -f $IsAdmin)

# Model key from -ModelKey or deploy/credentials (never committed)
if (-not $ModelKey) {
    $credFile = Join-Path $PSScriptRoot "credentials"
    if (Test-Path $credFile) {
        foreach ($line in (Get-Content $credFile)) {
            if ($line -match '^\s*MODEL_KEY\s*=\s*(\S+)\s*$') { $ModelKey = $Matches[1] }
        }
        if ($ModelKey) { Ok "model key: read from deploy/credentials" }
    }
}
if (-not $ModelKey) { Warn "no model key supplied (deploy/credentials or -ModelKey) - you'll add it later in the distro" }

# Tailscale state
$TsBin = Resolve-TsBin
$TsUp = Test-TsConnected $TsBin
if ($TsUp) { Skip "tailscale (running)" }
elseif ($TsBin) { Warn "tailscale: installed, not connected - will run 'tailscale up'" }
else { Warn "tailscale: not installed - will download and install" }

# WSL state
$WslCmd = Get-Command wsl -ErrorAction SilentlyContinue
$Distros = @()
if ($WslCmd) {
    $lst = & wsl -l -q 2>&1
    if ($LASTEXITCODE -eq 0) {
        foreach ($l in @($lst)) { $t = Get-CleanLine $l; if ($t) { $Distros += $t } }
    }
    else { Warn "wsl is present but 'wsl -l' failed - assuming a broken/uninitialized install" }
}
if (-not $WslCmd) { Warn "wsl: command not found - will install (admin)" }
else {
    Ok ("wsl distros present: {0}" -f $(if ($Distros.Count) { $Distros -join ", " } else { "(none)" }))
}
$HasDistro = @($Distros) -contains $Distro
if ($HasDistro) { Skip "distro '$Distro' (will be reused; only our own files are added inside it)" }
else { Warn ("distro '{0}': will be created fresh" -f $Distro) }

# Disk space. WSL distro images land under the drive that holds
# %LOCALAPPDATA% (the Store-package default), so that's the drive that matters.
# GetPathRoot returns 'C:\'; Win32_LogicalDisk.DeviceID is 'C:' - strip the backslash.
$driveRoot = [System.IO.Path]::GetPathRoot($env:LOCALAPPDATA).TrimEnd('\')
$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$driveRoot'" -ErrorAction SilentlyContinue
if ($disk) {
    $totalGB = [math]::Round($disk.Size / 1GB, 1)
    $freeGB  = [math]::Round($disk.FreeSpace / 1GB, 1)
    $driveLabel = $driveRoot.TrimEnd(':')
    Ok ("disk {0}: {1} GB total, {2} GB free" -f $driveLabel, $totalGB, $freeGB)
    if (-not $HasDistro -and $freeGB -lt $RequiredFreeGB) {
        Fail ("only {0} GB free on drive {1}, but a fresh distro needs about {2} GB (2 GB base image + node/dsh/venv stack + headroom). Free up space, or lower -RequiredFreeGB if you know what you're doing." -f $freeGB, $driveLabel, $RequiredFreeGB)
    }
    try {
        $vhdxSum = (Get-ChildItem "$env:LOCALAPPDATA\Packages" -Recurse -Filter *.vhdx -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
        if ($vhdxSum) {
            Ok ("existing WSL images on drive {0}: ~{1} GB (a fresh one adds ~{2} GB on top of that)" -f $driveLabel, [math]::Round($vhdxSum / 1GB, 1), $RequiredFreeGB)
        }
    } catch { Warn "could not enumerate existing WSL images (non-fatal)" }
} else {
    Warn "could not read free space for drive ${driveRoot} - continuing; the WSL install will fail clearly if the disk is full"
}

# Bundle source
$Bundle = ""
if ($BundlePath) {
    if (-not (Test-Path $BundlePath)) { Fail "bundle not found: $BundlePath" }
    $Bundle = (Resolve-Path $BundlePath).Path
    Ok "bundle: $Bundle"
} elseif ($RepoPath) {
    if (-not (Test-Path $RepoPath)) { Fail "repo not found: $RepoPath" }
    $rp = (Resolve-Path $RepoPath).Path
    $grOut = & git -C $rp rev-parse --is-inside-work-tree 2>&1
    $gr = ""
    if ($LASTEXITCODE -eq 0 -and $grOut) { $gr = @($grOut)[-1].ToString().Trim() }
    if ($gr -ne "true") {
        Fail "$RepoPath is not a git checkout (no .git there). 'git archive' needs a real clone - re-clone the repo, or pass -BundlePath <bundle.tar.gz> instead (build one on the dev box with deploy/make-bundle.sh)."
    }
    $RepoPath = $rp
    Ok "bundle: will be built from $rp via git archive"
} else {
    Warn "no bundle source yet (-RepoPath or -BundlePath) - required at install time"
}

Say "plan"
Ok "  1. tailscale ......... " + $(if ($TsUp) { "skip (running)" } elseif ($TsBin) { "connect" } else { "install + join" })
Ok "  2. wsl2 distro ....... " + $(if ($HasDistro) { "reuse '$Distro'" } else { "create '$Distro' ($ModeTier)" })
Ok "  3. in-distro stack ... node22 + dsh + venv(playwright) + mrtool  [idempotent]"
Ok "  4. dsh model ......... $ProviderId -> $ModelBase"
if ($DryRun) { Say "dry-run complete - nothing was changed."; exit 0 }

# ---------------------------------------------------------------- tailscale --
if (-not $TsUp) {
    if (-not $TsBin) {
        if (-not $IsAdmin) { Fail "installing Tailscale needs an admin PowerShell. Re-run as Administrator (or pass -DryRun)." }
        $exe = Join-Path $env:TEMP "tailscale-setup-latest.exe"
        Step "download + install Tailscale (silent)" {
            Invoke-WebRequest -Uri "https://downloads.tailscale.com/windows/tailscale-setup-latest.exe" -OutFile $exe
            if ($AuthKey) {
                # --exec runs the join from inside the installer (correct PATH), then exits
                Start-Process -FilePath $exe -Wait -ArgumentList @('-silent', '--exec', "tailscale up --authkey=$AuthKey")
            } else {
                Start-Process -FilePath $exe -Wait -ArgumentList '-silent'
            }
            Ok "installer finished"
        }
        $TsBin = Resolve-TsBin
        if (-not $TsBin) { Fail "Tailscale installed but tailscale.exe not found at the standard paths." }
        if (-not $AuthKey) {
            Warn "no -AuthKey supplied: run 'tailscale up' in a terminal (or give the box an auth key), then re-run (idempotent). Continuing - the model endpoint probe may warn."
        }
    } else {
        if ($AuthKey) {
            Step "connect tailscale to the tailnet (auth key)" { & $TsBin up --authkey $AuthKey }
        } else {
            Step "connect tailscale to the tailnet (interactive 'tailscale up')" { & $TsBin up }
        }
    }
    if ($AuthKey) {
        $ok = $false
        for ($i = 0; $i -lt 30; $i++) { Start-Sleep -Seconds 2; if (Test-TsConnected $TsBin) { $ok = $true; break } }
        if (-not $ok) { Fail "tailscale is not connected yet - re-run this script (idempotent), or check the key." }
        Skip "tailscale (running)"
    }
}

# ---------------------------------------------------------------- wsl2 -------
if (-not $WslCmd) {
    if ($IsWin11) {
        Fail "the 'wsl' command is missing. Install the 'Windows Subsystem for Linux' app from the Microsoft Store (it normally ships with Windows 11 - it may have been uninstalled), then re-run (idempotent)."
    }
    if (-not $IsAdmin) { Fail "enabling WSL on Windows 10 needs an admin PowerShell. Re-run as Administrator." }
    Step "enable WSL prerequisite features (Windows 10)" {
        foreach ($f in @("Microsoft-Windows-Subsystem-Linux", "VirtualMachinePlatform")) {
            $st = (Get-WindowsOptionalFeature -Online -FeatureName $f).State
            if ($st -ne "Enabled") {
                Say "  enabling $f"
                Enable-WindowsOptionalFeature -Online -FeatureName $f -All -NoRestart | Out-Null
            } else { Skip "feature $f" }
        }
        $pend = @()
        foreach ($f in @("Microsoft-Windows-Subsystem-Linux", "VirtualMachinePlatform")) {
            if ((Get-WindowsOptionalFeature -Online -FeatureName $f).State -ne "Enabled") { $pend += $f }
        }
        if ($pend) { Fail "feature(s) $pend are pending a reboot. Reboot, then re-run (idempotent)." }
        $WslCmd = Get-Command wsl -ErrorAction SilentlyContinue
        if (-not $WslCmd) { Fail "'wsl' still not on PATH after enabling the features - reboot, then re-run (idempotent)." }
        Ok "WSL base features present"
    }
}

$CreatedDistro = $false
if (-not $HasDistro) {
    if (-not $IsAdmin) { Fail "creating the distro needs an admin PowerShell. Re-run as Administrator." }

    Step "install WSL2 distro (Ubuntu-24.04) as '$Distro'" {
        $installed = $false
        $wslOut = & wsl --install -d Ubuntu-24.04 --name $Distro --no-launch 2>&1
        if ($LASTEXITCODE -eq 0) { $installed = $true }
        if (-not $installed) {
            Warn "retrying with 'wsl --install -d Ubuntu-24.04' (older wsl.exe)"
            Warn "if a 'Enter new UNIX username' prompt appears, just press Enter (users are managed as root afterwards)"
            if (@($Distros) -contains "Ubuntu-24.04") {
                Fail "a distro named 'Ubuntu-24.04' already exists on this machine - refusing to touch it. Reuse it with '-Distro Ubuntu-24.04', or update wsl.exe (Microsoft Store: 'Windows Subsystem for Linux') and re-run."
            }
            & wsl --install -d Ubuntu-24.04
            $try = & wsl -l -q 2>&1
            if (-not ($try | Select-String -SimpleMatch "Ubuntu-24.04" -Quiet)) {
                Fail "the wsl.exe on this machine is too old to install distros non-interactively. Update it (Microsoft Store: 'Windows Subsystem for Linux', or 'winget install Microsoft.WSL'), then re-run (idempotent)."
            }
            & wsl --terminate Ubuntu-24.04 *> $null
            & wsl --rename Ubuntu-24.04 $Distro
        }
    }
    $seen = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 2
        $now = & wsl -l -q 2>&1
        if ($LASTEXITCODE -eq 0 -and ($now | Select-String -SimpleMatch $Distro -Quiet)) { $seen = $true; break }
    }
    if (-not $seen) {
        # WSL sometimes defers the import until the engine is up to date.
        Say "  distro not visible yet - trying 'wsl --update' (engine update)"
        & wsl --update 2>&1 | ForEach-Object { Say "  $_" }
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 2
            $now = & wsl -l -q 2>&1
            if ($LASTEXITCODE -eq 0 -and ($now | Select-String -SimpleMatch $Distro -Quiet)) { $seen = $true; break }
        }
    }
    if (-not $seen) {
        $tail = ""
        if ($wslOut) { $tail = ($wslOut | ForEach-Object { $_.ToString() } | Select-Object -Last 5) -join " / " }
        $st = & wsl -l -v 2>&1
        $stj = if ($st) { ($st | ForEach-Object { $_.ToString() }) -join " ; " } else { "(none)" }
        Fail "distro did not appear after install. wsl said: $tail -- distro list now: $stj . A reboot is probably required: reboot, then re-run (idempotent)."
    }
    $vline = (& wsl -l -v 2>&1) | Select-String -SimpleMatch $Distro
    if ($vline -and ($vline.Line -notmatch '\b2\s*$')) {
        Fail "distro is not running on WSL2. Run 'wsl --update' in an admin PowerShell (installs the WSL2 kernel), then re-run (idempotent)."
    }
    $CreatedDistro = $true
    Ok "distro ready: $Distro (WSL2)"

    Step "create local user '$DshUser' + set as default (first boot as root)" {
        $tmpPass = [guid]::NewGuid().ToString("N")
        $bs = "id $DshUser >/dev/null 2>&1 || useradd -m -s /bin/bash $DshUser; echo '${DshUser}:${tmpPass}' | chpasswd; echo '$DshUser ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/$DshUser; chmod 440 /etc/sudoers.d/$DshUser; printf '[user]\ndefault=$DshUser\n' > /etc/wsl.conf; echo 'created by mrtool deploy installer' > /etc/dsh-managed-by-mrtool"
        & wsl -d $Distro -u root -- /bin/bash -c $bs
        if ($LASTEXITCODE -ne 0) { Fail "could not create $DshUser inside $Distro" }
        Ok "user $DshUser created and set as default"
    }
}
$TargetUser = $DshUser
if (-not $CreatedDistro) {
    # last stdout line is the username (wsl may prepend notice lines)
    $ruOut = & wsl -d $Distro -- whoami 2>&1
    $defaultUser = ""
    if ($LASTEXITCODE -eq 0 -and $ruOut) { $defaultUser = Get-CleanLine (@($ruOut)[-1]) }
    if (-not $defaultUser) {
        Fail "could not determine the default user of '$Distro' (whoami failed). Check 'wsl -l -v'; try 'wsl --repair $Distro' and re-run."
    }
    $idOut = & wsl -d $Distro -u root -- /bin/bash -c "id $DshUser >/dev/null 2>&1 && echo present" 2>&1
    $hasUser = ($LASTEXITCODE -eq 0 -and $idOut -match "present")
    if ($hasUser) {
        Ok "user $DshUser already present in $Distro (default user: $defaultUser)"
    } elseif ($defaultUser -eq "root") {
        # A distro whose default user is still root is a fresh, unpersonalized
        # one - typically left behind by an earlier, interrupted run of this
        # installer. Creating our user there touches nothing pre-existing.
        Step "create local user '$DshUser' + set as default (recovered fresh distro)" {
            $tmpPass = [guid]::NewGuid().ToString("N")
            $bs = "id $DshUser >/dev/null 2>&1 || useradd -m -s /bin/bash $DshUser; echo '${DshUser}:${tmpPass}' | chpasswd; echo '$DshUser ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/$DshUser; chmod 440 /etc/sudoers.d/$DshUser; printf '[user]\ndefault=$DshUser\n' > /etc/wsl.conf; echo 'created by mrtool deploy installer' > /etc/dsh-managed-by-mrtool"
            & wsl -d $Distro -u root -- /bin/bash -c $bs
            if ($LASTEXITCODE -ne 0) { Fail "could not create $DshUser inside $Distro" }
            & wsl --terminate $Distro *> $null
            Ok "user $DshUser created and set as default"
        }
    } else {
        Fail "distro '$Distro' has no user '$DshUser' and its default user is '$defaultUser' - refusing to modify a pre-existing distro. Use '-DshUser $defaultUser' to target the existing user, or re-run without -Distro to use the default fresh distro."
    }
}

# ------------------------------------------------------------- networking ---
# 'ours' = created by this installer (this run or an earlier one; the marker
# file is written at user creation). Never reconfigures unmanaged distros.
$Managed = $CreatedDistro
if (-not $Managed) {
    $mkOut = & wsl -d $Distro -u root -- /bin/bash -c "test -f /etc/dsh-managed-by-mrtool && echo managed" 2>&1
    if ($LASTEXITCODE -eq 0 -and $mkOut -match "managed") { $Managed = $true }
}
if ($Managed -and $IsWin11) {
    Step "set mirrored networking on '$Distro' (ours only - never rewrites other distros)" {
        & wsl --terminate $Distro *> $null
        $sc = & wsl --set-config $Distro networkingMode=mirrored 2>&1
        if ($LASTEXITCODE -ne 0) {
            $scj = if ($sc) { ($sc | ForEach-Object { $_.ToString() }) -join " / " } else { "(no output)" }
            Say "  wsl said: $scj"
            Say "  trying 'wsl --update' (engine) and retrying"
            $upd = & wsl --update 2>&1
            if ($upd) { foreach ($l in @($upd)) { $t = Get-CleanLine $l; if ($t) { Say "  $t" } } }
            $sc2 = & wsl --set-config $Distro networkingMode=mirrored 2>&1
            if ($LASTEXITCODE -ne 0) {
                Warn "could not set mirrored mode; staying NAT (the portproxy path below covers it). To opt in later: 'wsl --update', then 'wsl --set-config $Distro networkingMode=mirrored'."
            } else {
                Ok "engine updated; mirrored mode set for '$Distro'"
            }
        } else {
            Ok "mirrored mode set for '$Distro'"
        }
    }
}

# Empirical probe: can the distro see Windows loopback? (mirrored: yes;
# NAT: no - the Windows host's 127.0.0.1 is not reachable from inside.)
$Shared = $null
$listener = $null
try {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 18889)
    $listener.Start()
    # plain TCP connect: the listener sends no HTTP reply, so curl's
    # http_code is 000 in BOTH modes and cannot tell them apart
    $codeOut = & wsl -d $Distro -- bash -c "(exec 3<>/dev/tcp/127.0.0.1/18889) 2>/dev/null && echo 1 || echo 0" 2>$null
    $code = ""
    if ($codeOut) { $code = Get-CleanLine (@($codeOut)[-1]) }
    $Shared = ($code -eq "1")
} catch { $Shared = $null }
finally { if ($listener) { try { $listener.Stop() } catch {} } }

$CdpMode = "unknown"
if ($Shared -eq $true) { $CdpMode = "mirrored" }
elseif ($Shared -eq $false) { $CdpMode = "nat" }
Ok "localhost shared with ${Distro}: $Shared  ->  CDP mode: $CdpMode"

if ($CdpMode -eq "mirrored") {
    # a previous NAT-mode run may have left portproxy + firewall artifacts;
    # in mirrored mode the distro reaches 127.0.0.1 directly, and they are an
    # unnecessary open inbound port - remove ours (recognized by shape).
    if ($IsAdmin) {
        $ppOut = & netsh interface portproxy show v4tov4 2>&1
        foreach ($line in @($ppOut)) {
            $t = $line.ToString().Trim()
            if ($t -match '^(0\.0\.0\.0|\d{1,3}(?:\.\d{1,3}){3}):9222\b' -and $t -match '127\.0\.0\.1:9222\b') {
                $la = ($t -split ':')[0]
                & netsh interface portproxy delete v4tov4 listenaddress=$la listenport=9222 *> $null
                Ok ("removed leftover NAT portproxy {0}:9222 (mirrored mode)" -f $la)
            }
        }
        if (Get-NetFirewallRule -DisplayName "mrtool CDP (WSL)" -ErrorAction SilentlyContinue) {
            Remove-NetFirewallRule -DisplayName "mrtool CDP (WSL)" | Out-Null
            Ok "removed leftover NAT firewall rule 'mrtool CDP (WSL)' (mirrored mode)"
        }
    }
}

if ($CdpMode -eq "nat") {
    if (-not $IsAdmin) {
        Warn "NAT mode needs admin for the portproxy + firewall rule; skipping (re-run as admin to apply)."
    } else {
        Step "expose Chrome debug port to the WSL NAT subnet (portproxy + scoped firewall rule)" {
            $gwOut = & wsl -d $Distro -- bash -c "ip route show default | cut -d' ' -f3 | head -1" 2>$null
            $gw = ""
            if ($gwOut) { $gw = Get-CleanLine (@($gwOut)[-1]) }
            if (-not $gw) { Fail "could not determine the WSL NAT gateway" }
            $gwSub = (($gw -split '\.')[0..2]) -join '.'
            $hostIp = (Get-NetIPAddress | Where-Object { $_.IPAddress -like ($gwSub + '*') } | Select-Object -First 1).IPAddress
            if (-not $hostIp) { Fail "no Windows-side IP found on the WSL NAT subnet ($gwSub.x)" }
            Ok ("WSL NAT gateway {0}; windows-side ip {1}" -f $gw, $hostIp)
            $pp = & netsh interface portproxy show all 2>&1
            if (-not ($pp | Select-String -Pattern '\b9222\b' -Quiet)) {
                $r1 = & netsh interface portproxy add v4tov4 listenaddress=$hostIp listenport=9222 connectaddress=127.0.0.1 connectport=9222
                if ($LASTEXITCODE -ne 0) {
                    Warn "specific-address portproxy failed; using 0.0.0.0 (firewall rule scopes it to the WSL adapter)"
                    & netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=9222 connectaddress=127.0.0.1 connectport=9222
                }
                Ok ("portproxy: {0}:9222 -> 127.0.0.1:9222" -f $hostIp)
            } else { Skip "portproxy for 9222" }
            $alias = (Get-NetIPConfiguration | Where-Object { $_.IPv4Address.IPAddress -eq $hostIp } | Select-Object -First 1).InterfaceAlias
            if (-not $alias) { Fail "could not find the interface alias for $hostIp" }
            if (-not (Get-NetFirewallRule -DisplayName "mrtool CDP (WSL)" -ErrorAction SilentlyContinue)) {
                New-NetFirewallRule -DisplayName "mrtool CDP (WSL)" -Direction Inbound -Protocol TCP -LocalPort 9222 -InterfaceAlias $alias -Action Allow | Out-Null
                Ok "firewall rule: TCP 9222 allowed only on '$alias'"
            } else { Skip "firewall rule for 9222" }
        }
    }
}

# ----------------------------------------------------------------- bundle ----
$stamp = Get-Date -Format "yyyyMMddHHmmss"
if ($RepoPath -and -not $BundlePath) {
    $Bundle = Join-Path $env:TEMP ("mrtool-bundle-{0}.tar.gz" -f $stamp)
    Step "build bundle from $RepoPath (git archive) -> $Bundle" {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = "git"
        $psi.Arguments = "-C `"$RepoPath`" archive --format=tar.gz --prefix=mrtool/ HEAD"
        $psi.RedirectStandardOutput = $true
        $psi.UseShellExecute = $false
        $proc = [System.Diagnostics.Process]::Start($psi)
        $fs = [System.IO.File]::Create($Bundle)
        $proc.StandardOutput.BaseStream.CopyTo($fs)
        $fs.Close()
        $proc.WaitForExit()
        if ($proc.ExitCode -ne 0) { Fail "git archive failed" }
        Ok "bundle built"
    }
}
if (-not $Bundle) { Fail "no bundle source: pass -RepoPath (a local mrtool git checkout) or -BundlePath (a bundle tarball)." }

# ------------------------------------------------------------ distro files --
# All file transfers go through the 'wsl' channel (see Send-ToDistro), never
# through the \\wsl.localhost file share.

Step "copy bundle into $Distro; bootstrap script comes from the bundle" {
    # must match the bootstrap's $BASE/bundle (= /home/<user>/<workdir>/bundle)
    $dst = "/home/$TargetUser/$WorkDir/bundle"
    $bpath = "$dst/mrtool-bundle-$stamp.tar.gz"
    $n1 = Send-ToDistro $Bundle $bpath "644"
    # The bootstrap script is extracted from the BUNDLE, not from the working
    # tree: git archive contains the committed bytes (always LF), while a
    # Windows checkout with core.autocrlf=true rewrites .sh files to CRLF,
    # which makes bash choke (set: pipefail^M) and corrupts every variable.
    # sed strips CRLF if the Windows-side git archive applied autocrlf; no-op
    # on clean files. LF is enforced here, not trusted upstream.
    $ex = & wsl -d $Distro -u root -- /bin/bash -c "rm -rf /tmp/mrtool-x && mkdir -p /tmp/mrtool-x && tar -xzf '$bpath' -C /tmp/mrtool-x 'mrtool/deploy/bootstrap-wsl.sh' && sed -i 's/\r$//' /tmp/mrtool-x/mrtool/deploy/bootstrap-wsl.sh && cp -f /tmp/mrtool-x/mrtool/deploy/bootstrap-wsl.sh /root/bootstrap-wsl.sh && chmod 755 /root/bootstrap-wsl.sh && wc -c < /root/bootstrap-wsl.sh" 2>&1
    if ($LASTEXITCODE -ne 0) { Fail "could not extract bootstrap-wsl.sh from the bundle (wsl exited $LASTEXITCODE)" }
    $n2 = ""
    foreach ($l in @($ex)) { $t = Get-CleanLine $l; if ($t -match '^\d+$') { $n2 = $t } }
    if ($n2 -eq "") { Fail "could not verify /root/bootstrap-wsl.sh (no size readback)" }
    Ok "bundle in distro: $bpath ($n1 bytes)"
    Ok "bootstrap script: /root/bootstrap-wsl.sh ($n2 bytes, from bundle)"
}

if ($ModelKey) {
    Step "stage model api key (0600; removed by the bootstrap after use)" {
        $kdir = "/home/$TargetUser/$WorkDir"
        $kb64 = [System.Convert]::ToBase64String([System.Text.Encoding]::ASCII.GetBytes($ModelKey))
        $kcmd = "mkdir -p '$kdir' && printf %s '$kb64' | base64 -d > '$kdir/.bootstrap-key' && chmod 600 '$kdir/.bootstrap-key'"
        & wsl -d $Distro -u root -- /bin/bash -c $kcmd
        if ($LASTEXITCODE -ne 0) { Fail "could not stage the model key into $Distro" }
        Ok "key staged (0600)"
    }
}

Step "run bootstrap inside $Distro (node, dsh, venv, mrtool, dsh config)" {
    & wsl -d $Distro -u root -- /bin/bash -c "HOME=/home/$TargetUser bash /root/bootstrap-wsl.sh $WorkDir $ModelBase $ModelId $ProviderId $FORCEINT $CdpMode"
    if ($LASTEXITCODE -ne 0) { Fail "bootstrap failed (see output above)" }
    # .cache too: the bootstrap runs pip as root, which creates root-owned
    # ~/.cache/pip and makes later user-mode pip warn about ownership.
    $chown = "g=`$(id -gu $TargetUser 2>/dev/null || echo $TargetUser); chown -R ${TargetUser}:`$g /home/$TargetUser/$WorkDir /home/$TargetUser/.dsh; [ -d /home/$TargetUser/.cache ] && chown -R ${TargetUser}:`$g /home/$TargetUser/.cache; chown $TargetUser /home/$TargetUser/.bashrc"
    & wsl -d $Distro -u root -- /bin/bash -c $chown
    Ok "bootstrap complete; ownership set to $TargetUser"
}

# ------------------------------------------------------------------- report --
Say "done."
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor Green
Write-Host ("    1. Start the agent:    wsl -d {0} -u {1} dsh" -f $Distro, $TargetUser)
Write-Host ("    2. Pass Cloudflare:    double-click  \\wsl.localhost\{0}\home\{1}\{2}\mrtool\launch_cdp.bat" -f $Distro, $TargetUser, $WorkDir)
Write-Host ("    3. Verify CDP attach:  wsl -d {0} -u {1} -- /bin/bash -lc `"cd ~/{2}/mrtool && ../venv/bin/python mrtool.py --cdp check`"" -f $Distro, $TargetUser, $WorkDir)
Write-Host ""
Write-Host ("  The bashrc hook in the distro exports CDP_HOST for this machine's mode ({0});" -f $CdpMode)
Write-Host "  agents just run 'mrtool.py --cdp ...' and the right host is used automatically."
Write-Host "  If the model key was not supplied, add it with:"
Write-Host ("    echo 'DSH_MODEL_API_KEY=...' >> /home/{0}/{1}/credentials.env   (inside {2})" -f $TargetUser, $WorkDir, $Distro)
