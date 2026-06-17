#Requires -RunAsAdministrator
<#
.SYNOPSIS
    One-shot Windows installer for duo-log-sync-http-collector-xdr.

.DESCRIPTION
    NOTE: This script is a work in progress. For production deployments, follow
    the manual setup steps in README.md instead.

    Creates the Python virtual environment, installs all dependencies (including
    Duo Log Sync), installs NSSM via winget if needed, writes configuration files,
    and registers both the XDR forwarder and DLS as auto-start Windows services.

    Re-running this script is idempotent - existing services are replaced in-place
    and existing configuration values are preserved as defaults.

.PARAMETER Action
    "install" (default) or "uninstall"

.PARAMETER XdrCollectorUrl
    Full HTTPS URL of the Cortex XDR HTTP Log Collector endpoint.

.PARAMETER XdrApiKey
    Cortex XDR API key value.

.PARAMETER XdrApiKeyId
    Cortex XDR API key ID. Only required for some XDR tenant configurations; omit
    if not needed.

.PARAMETER DuoIkey
    Duo Admin API integration key.

.PARAMETER DuoSkey
    Duo Admin API secret key.

.PARAMETER DuoHostname
    Duo Admin API hostname (e.g. api-XXXXXXXX.duosecurity.com).

.PARAMETER SkipDls
    Skip installing and registering Duo Log Sync as a service. Use this when DLS
    is already installed and managed externally on this host.

.EXAMPLE
    # Interactive install - prompts for credentials
    .\windows\setup.ps1

.EXAMPLE
    # Fully automated install - no prompts
    .\windows\setup.ps1 `
        -XdrCollectorUrl "https://api-tenant.xdr.us.paloaltonetworks.com/logs/v1/event" `
        -XdrApiKey "your-xdr-api-key" `
        -DuoIkey "DIXXXXXXXXXXXXXXXXXX" `
        -DuoSkey "your-duo-secret-key" `
        -DuoHostname "api-XXXXXXXX.duosecurity.com"

.EXAMPLE
    # Uninstall both services
    .\windows\setup.ps1 -Action uninstall
#>

param(
    [ValidateSet("install", "uninstall")]
    [string]$Action = "install",

    [string]$XdrCollectorUrl = "",
    [string]$XdrApiKey       = "",
    [string]$XdrApiKeyId     = "",

    [string]$DuoIkey         = "",
    [string]$DuoSkey         = "",
    [string]$DuoHostname     = "",

    [switch]$SkipDls
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
$ForwarderSvcName    = "DuoXdrForwarder"
$ForwarderSvcDisplay = "Duo XDR Forwarder"
$ForwarderSvcDesc    = "Tails Duo Log Sync output and forwards records to the Cortex XDR HTTP Log Collector."
$DlsSvcName          = "DuoLogSync"
$DlsSvcDisplay       = "Duo Log Sync"
$DlsSvcDesc          = "Polls the Duo Admin API and sends log records to the local XDR forwarder."
$StateDir            = "C:\ProgramData\duo-xdr-forwarder"
$LogDir              = "$StateDir\logs"
$CheckpointDir       = "$StateDir\checkpoints"

# ---------------------------------------------------------------------------
# Resolve project paths
# ---------------------------------------------------------------------------
$ScriptDir       = if ($MyInvocation.MyCommand.Path) { Split-Path -Parent $MyInvocation.MyCommand.Path } else { $PWD.Path }
$ProjectDir      = Split-Path -Parent $ScriptDir
$VenvDir         = Join-Path $ProjectDir ".venv"
$PythonExe       = Join-Path $VenvDir "Scripts\python.exe"
$PipExe          = Join-Path $VenvDir "Scripts\pip.exe"
$ForwarderScript = Join-Path $ProjectDir "duo_xdr_forwarder.py"
$DlsExe          = Join-Path $VenvDir "Scripts\duologsync.exe"
$EnvFile         = Join-Path $ProjectDir ".env"
$DlsConfigFile   = Join-Path $ProjectDir "duo_log_sync\config.yml"
$DlsSrcDir       = Join-Path $ProjectDir "duo_log_sync"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Find-NSSM {
    $candidates = @(
        "nssm",
        "C:\nssm\nssm.exe",
        "C:\tools\nssm\nssm.exe",
        "$env:ProgramFiles\nssm\nssm.exe"
    )
    foreach ($c in $candidates) {
        try { return (Get-Command $c -ErrorAction Stop).Source } catch {}
    }
    return $null
}

function Install-NSSM {
    Write-Host "  NSSM not found - attempting install via winget..." -ForegroundColor Cyan
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host ""
        Write-Host "  winget is not available on this system." -ForegroundColor Yellow
        Write-Host "  Download NSSM manually from: https://nssm.cc/download" -ForegroundColor Yellow
        Write-Host "  Place nssm.exe in C:\nssm\ or add it to PATH, then re-run this script." -ForegroundColor Yellow
        exit 1
    }
    winget install NSSM.NSSM --silent --accept-package-agreements --accept-source-agreements
    # Reload PATH so nssm is visible without opening a new shell
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("PATH", "User")
    $resolved = Find-NSSM
    if (-not $resolved) {
        Write-Error "NSSM install appeared to succeed but nssm.exe is still not discoverable. Verify the install and re-run."
    }
    return $resolved
}

function Read-EnvFile([string]$Path) {
    $vars = @{}
    if (-not (Test-Path $Path)) { return $vars }
    foreach ($line in Get-Content $Path) {
        $line = $line.Trim()
        if ($line -eq "" -or $line.StartsWith("#")) { continue }
        $idx = $line.IndexOf("=")
        if ($idx -lt 0) { continue }
        $key = $line.Substring(0, $idx).Trim()
        $val = $line.Substring($idx + 1).Trim()
        if (($val.StartsWith('"') -and $val.EndsWith('"')) -or
            ($val.StartsWith("'") -and $val.EndsWith("'"))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        $vars[$key] = $val
    }
    return $vars
}

# Prompts for a required value. Shows the current value (or "[set]" for secrets)
# so the user can press Enter to keep it. Loops until a non-empty value is provided.
function Prompt-Required([string]$Label, [string]$Current, [bool]$Secret = $false) {
    if ($Current) {
        $hint     = if ($Secret) { "[already set - press Enter to keep]" } else { "[$Current]" }
        $response = Read-Host "  $Label $hint"
        if ($response) { return $response } else { return $Current }
    }
    while ($true) {
        if ($Secret) {
            $secure = Read-Host "  $Label" -AsSecureString
            $bstr   = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            $plain  = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            if ($plain) { return $plain }
        } else {
            $val = Read-Host "  $Label"
            if ($val) { return $val }
        }
        Write-Host "    This value is required." -ForegroundColor Yellow
    }
}

function Remove-ServiceIfExists([string]$NssmPath, [string]$Name) {
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $svc) { return }
    Write-Host "    Stopping existing '$Name'..."
    & $NssmPath stop $Name 2>$null
    $waited = 0
    while ($waited -lt 10) {
        $s = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if (-not $s -or $s.Status -eq "Stopped") { break }
        Start-Sleep -Seconds 1; $waited++
    }
    & $NssmPath remove $Name confirm
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
if ($Action -eq "uninstall") {
    $nssm = Find-NSSM
    if (-not $nssm) { Write-Error "NSSM not found - cannot uninstall services." }

    # Stop DLS first so it can't reconnect while the forwarder is being removed
    foreach ($svc in @($DlsSvcName, $ForwarderSvcName)) {
        $existing = Get-Service -Name $svc -ErrorAction SilentlyContinue
        if ($existing) {
            Write-Host "Stopping and removing service '$svc'..."
            & $nssm stop $svc 2>$null
            $waited = 0
            while ($waited -lt 15) {
                $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
                if (-not $s -or $s.Status -eq "Stopped") { break }
                Start-Sleep -Seconds 1; $waited++
            }
            & $nssm remove $svc confirm
            Write-Host "  Removed '$svc'." -ForegroundColor Green
        } else {
            Write-Host "Service '$svc' not found - skipping."
        }
    }
    Write-Host ""
    Write-Host "Uninstall complete. Data in $StateDir was not removed." -ForegroundColor Green
    exit 0
}

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Duo XDR Forwarder - Windows Setup ===" -ForegroundColor Cyan
Write-Host "Project: $ProjectDir"
Write-Host ""

# ---------------------------------------------------------------------------
# [1/7] Python virtual environment
# ---------------------------------------------------------------------------
Write-Host "[1/7] Setting up Python virtual environment..." -ForegroundColor Cyan

$systemPython = try { (Get-Command python -ErrorAction Stop).Source } catch { $null }
if (-not $systemPython) {
    Write-Error "python.exe not found in PATH. Install Python 3.8+ from https://www.python.org/downloads/ (check 'Add to PATH') and re-run."
}
if ($systemPython -like "*\WindowsApps\*") {
    Write-Host ""
    Write-Host "  ERROR: '$systemPython' is the Windows Store Python stub, not a real Python install." -ForegroundColor Red
    Write-Host "  Fix one of two ways:" -ForegroundColor Yellow
    Write-Host "    1. Disable the stub: Settings > Apps > Advanced app settings > App execution aliases" -ForegroundColor Yellow
    Write-Host "       -> turn off 'python.exe' and 'python3.exe', then re-run this script." -ForegroundColor Yellow
    Write-Host "    2. Install Python 3.8+ from https://www.python.org/downloads/ (check 'Add to PATH')," -ForegroundColor Yellow
    Write-Host "       then re-run this script." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}
Write-Host "  Python: $systemPython"

if (-not (Test-Path $VenvDir)) {
    Write-Host "  Creating .venv..."
    & $systemPython -m venv $VenvDir
} else {
    Write-Host "  .venv already exists."
}
if (-not (Test-Path $PipExe)) {
    Write-Error "Venv creation failed - pip.exe not found at '$PipExe'. Verify your Python install is not corrupted and re-run."
}

Write-Host "  Installing forwarder dependencies..."
& $PipExe install --quiet -r (Join-Path $ProjectDir "requirements.txt")
Write-Host "  Done."

# ---------------------------------------------------------------------------
# [2/7] Duo Log Sync
# ---------------------------------------------------------------------------
if ($SkipDls) {
    Write-Host "[2/7] Skipping Duo Log Sync install (-SkipDls)." -ForegroundColor Gray
} else {
    Write-Host "[2/7] Installing Duo Log Sync..." -ForegroundColor Cyan

    if (-not (Test-Path $DlsSrcDir)) {
        Write-Host "  duo_log_sync not found - cloning from GitHub..." -ForegroundColor Cyan
        $git = try { (Get-Command git -ErrorAction Stop).Source } catch { $null }
        if (-not $git) {
            Write-Host ""
            Write-Host "  ERROR: git is not in PATH and duo_log_sync is not present." -ForegroundColor Red
            Write-Host "  Fix one of two ways:" -ForegroundColor Yellow
            Write-Host "    1. Install Git from https://git-scm.com/download/win, then re-run." -ForegroundColor Yellow
            Write-Host "    2. Manually clone into the project directory:" -ForegroundColor Yellow
            Write-Host "         git clone https://github.com/duosecurity/duo_log_sync.git" -ForegroundColor Yellow
            Write-Host "       then re-run this script." -ForegroundColor Yellow
            Write-Host ""
            exit 1
        }
        & $git clone --quiet https://github.com/duosecurity/duo_log_sync.git $DlsSrcDir
        if (-not (Test-Path $DlsSrcDir)) {
            Write-Error "git clone of duo_log_sync failed. Check your internet connection and re-run."
        }
        Write-Host "  Cloned duo_log_sync."
    } else {
        Write-Host "  duo_log_sync already present."
    }

    & $PipExe install --quiet setuptools
    & $PipExe install --quiet -e $DlsSrcDir
    # DLS pins Cerberus==1.3.2, which imports pkg_resources — removed in setuptools 81+.
    # Cerberus 1.3.5+ replaces it with importlib.metadata and is a drop-in replacement.
    # pip will warn about the version conflict with DLS's pin; the warning is harmless.
    Write-Host "  Applying cerberus compatibility fix (setuptools 81+ / pkg_resources)..."
    & $PipExe install --quiet "cerberus>=1.3.5"
    Write-Host "  Installed DLS from: $DlsSrcDir"
}

# ---------------------------------------------------------------------------
# [3/7] NSSM
# ---------------------------------------------------------------------------
Write-Host "[3/7] Checking for NSSM..." -ForegroundColor Cyan
$nssm = Find-NSSM
if (-not $nssm) { $nssm = Install-NSSM }
Write-Host "  NSSM: $nssm"

# ---------------------------------------------------------------------------
# [4/7] Configure .env
# ---------------------------------------------------------------------------
Write-Host "[4/7] Configuring .env..." -ForegroundColor Cyan

$existingEnv = Read-EnvFile $EnvFile
if (-not $XdrCollectorUrl) { $XdrCollectorUrl = $existingEnv["XDR_COLLECTOR_URL"] }
if (-not $XdrApiKey)       { $XdrApiKey       = $existingEnv["XDR_API_KEY"] }
if (-not $XdrApiKeyId)     { $XdrApiKeyId     = $existingEnv["XDR_API_KEY_ID"] }
if (-not $DuoIkey)         { $DuoIkey         = $existingEnv["DUO_IKEY"] }
if (-not $DuoSkey)         { $DuoSkey         = $existingEnv["DUO_SKEY"] }
if (-not $DuoHostname)     { $DuoHostname     = $existingEnv["DUO_HOSTNAME"] }

if (-not $XdrCollectorUrl -or -not $XdrApiKey) {
    Write-Host ""
    Write-Host "  Enter your Cortex XDR HTTP Log Collector details." -ForegroundColor Yellow
    Write-Host "  (XDR tenant: Settings -> Configurations -> HTTP Log Collector)" -ForegroundColor Yellow
    Write-Host ""
    $XdrCollectorUrl = Prompt-Required "Collector URL  " $XdrCollectorUrl
    $XdrApiKey       = Prompt-Required "API Key        " $XdrApiKey $true
    $idHint          = if ($XdrApiKeyId) { " [$XdrApiKeyId]" } else { " (press Enter to skip)" }
    $idInput         = Read-Host "  API Key ID$idHint"
    if ($idInput) { $XdrApiKeyId = $idInput }
    Write-Host ""
}

# Preserve any existing tuning values the user may have customised; fall back to defaults.
function Use-Existing([string]$Key, [string]$Default) {
    $v = $existingEnv[$Key]
    if ($v) { $v } else { $Default }
}
$listenHost    = Use-Existing "LISTEN_HOST"           "127.0.0.1"
$listenPort    = Use-Existing "LISTEN_PORT"           "9999"
$maxConns      = Use-Existing "MAX_CONNECTIONS"       "10"
$xdrDataset    = Use-Existing "XDR_DATASET"           "duo_logs"
$batchSize     = Use-Existing "BATCH_SIZE"            "100"
$flushInterval = Use-Existing "FLUSH_INTERVAL_SECONDS" "5"
$logLevel      = Use-Existing "LOG_LEVEL"             "INFO"
$maxRetries    = Use-Existing "MAX_RETRIES"           "3"
$retryBackoff  = Use-Existing "RETRY_BACKOFF_SECONDS" "5"

$keyIdLine = if ($XdrApiKeyId) { "XDR_API_KEY_ID=$XdrApiKeyId" } else { "# XDR_API_KEY_ID=" }

@"
# Cortex XDR HTTP Log Collector
XDR_COLLECTOR_URL=$XdrCollectorUrl
XDR_API_KEY=$XdrApiKey
$keyIdLine

# Duo Log Sync credentials -- used by setup.ps1 to write duo_log_sync\config.yml.
# Not read at runtime by the forwarder service; safe to leave populated here.
# Leave blank and pass -SkipDls if DLS is already configured on this host.
DUO_IKEY=$DuoIkey
DUO_SKEY=$DuoSkey
DUO_HOSTNAME=$DuoHostname

# TCP listener -- DLS sends logs here
LISTEN_HOST=$listenHost
LISTEN_PORT=$listenPort
MAX_CONNECTIONS=$maxConns

# Optional tuning
XDR_DATASET=$xdrDataset
BATCH_SIZE=$batchSize
FLUSH_INTERVAL_SECONDS=$flushInterval
LOG_LEVEL=$logLevel
MAX_RETRIES=$maxRetries
RETRY_BACKOFF_SECONDS=$retryBackoff
"@ | Set-Content -Path $EnvFile -Encoding UTF8

Write-Host "  Written: $EnvFile"

# ---------------------------------------------------------------------------
# [5/7] Configure duo_log_sync\config.yml
# ---------------------------------------------------------------------------
if ($SkipDls) {
    Write-Host "[5/7] Skipping DLS config (-SkipDls)." -ForegroundColor Gray
} else {
    Write-Host "[5/7] Configuring duo_log_sync\config.yml..." -ForegroundColor Cyan

    # Resolve Duo credentials: param -> .env -> existing config.yml -> prompt
    if ((-not $DuoIkey -or -not $DuoSkey -or -not $DuoHostname) -and (Test-Path $DlsConfigFile)) {
        $yaml = Get-Content $DlsConfigFile -Raw
        if (-not $DuoIkey     -and $yaml -match "ikey: '([^']+)'")         { $DuoIkey     = $Matches[1] }
        if (-not $DuoSkey     -and $yaml -match "skey: '([^']+)'")         { $DuoSkey     = $Matches[1] }
        if (-not $DuoHostname -and $yaml -match "hostname: '(api-[^']+)'") { $DuoHostname = $Matches[1] }
    }

    if (-not $DuoIkey -or -not $DuoSkey -or -not $DuoHostname) {
        Write-Host ""
        Write-Host "  Enter your Duo Admin API credentials." -ForegroundColor Yellow
        Write-Host "  (Admin Panel -> Applications -> your Admin API app)" -ForegroundColor Yellow
        Write-Host ""
        $DuoIkey     = Prompt-Required "Integration Key" $DuoIkey
        $DuoSkey     = Prompt-Required "Secret Key     " $DuoSkey $true
        $DuoHostname = Prompt-Required "API Hostname   " $DuoHostname
        Write-Host ""
    }

    # Forward slashes: Python on Windows accepts them and they are unambiguous in YAML
    $dlsLog      = ($LogDir + "\duologsync.log").Replace("\", "/")
    $dlsChkpts   = $CheckpointDir.Replace("\", "/")

    @"
# IMPORTANT!: Use single quotes (''), NOT double quotes ("")!
# Duo Log Sync configuration -- sends logs to duo-xdr-forwarder over local TCP.

version: '1.0.0'

dls_settings:
  log_filepath: '$dlsLog'
  log_format: 'JSON'
  api:
    offset: 180
    timeout: 120
  checkpointing:
    enabled: True
    directory: '$dlsChkpts'

servers:
  - id: 'xdr-forwarder'
    hostname: '127.0.0.1'
    port: 9999
    protocol: 'TCP'

account:
  ikey: '$DuoIkey'
  skey: '$DuoSkey'
  hostname: '$DuoHostname'

  endpoint_server_mappings:
    - endpoints: ['auth', 'activity']
      server: 'xdr-forwarder'
    # Uncomment to also collect telephony and trust monitor events:
    #- endpoints: ['telephony', 'trustmonitor']
    #  server: 'xdr-forwarder'

  is_msp: False
"@ | Set-Content -Path $DlsConfigFile -Encoding UTF8

    Write-Host "  Written: $DlsConfigFile"
}

# ---------------------------------------------------------------------------
# [6/7] State and log directories
# ---------------------------------------------------------------------------
Write-Host "[6/7] Creating data directories..." -ForegroundColor Cyan
foreach ($dir in @($StateDir, $LogDir, $CheckpointDir)) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Write-Host "  Created: $dir"
    } else {
        Write-Host "  Exists:  $dir"
    }
}

# ---------------------------------------------------------------------------
# [7/7] Register Windows services
# ---------------------------------------------------------------------------
Write-Host "[7/7] Registering Windows services..." -ForegroundColor Cyan

# --- Forwarder ---
if (-not (Test-Path $ForwarderScript)) {
    Write-Error "Forwarder script not found at '$ForwarderScript'. Verify the project directory is complete and re-run."
}
Write-Host "  Registering '$ForwarderSvcName'..."
Remove-ServiceIfExists $nssm $ForwarderSvcName
& $nssm install $ForwarderSvcName $PythonExe $ForwarderScript
& $nssm set     $ForwarderSvcName DisplayName    $ForwarderSvcDisplay
& $nssm set     $ForwarderSvcName Description    $ForwarderSvcDesc
& $nssm set     $ForwarderSvcName Start          SERVICE_AUTO_START
& $nssm set     $ForwarderSvcName AppDirectory   $ProjectDir
& $nssm set     $ForwarderSvcName AppStdout      (Join-Path $LogDir "forwarder-stdout.log")
& $nssm set     $ForwarderSvcName AppStderr      (Join-Path $LogDir "forwarder-stderr.log")
& $nssm set     $ForwarderSvcName AppRotateFiles 1
& $nssm set     $ForwarderSvcName AppRotateBytes 10485760

# Pass .env variables into the service environment.
# SECURITY NOTE: NSSM stores these values in the Windows Registry under
# HKLM\SYSTEM\CurrentControlSet\Services\DuoXdrForwarder. Restrict the ACL
# to SYSTEM and Administrators after installation.
$envVars = Read-EnvFile $EnvFile
if ($envVars.Count -gt 0) {
    $envString = ($envVars.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "`0"
    & $nssm set $ForwarderSvcName AppEnvironmentExtra $envString
}

# --- DLS ---
if (-not $SkipDls) {
    if (-not (Test-Path $DlsExe)) {
        Write-Host "  WARNING: duologsync.exe not found at $DlsExe" -ForegroundColor Yellow
        Write-Host "           DLS service not registered. Verify Step 2 succeeded." -ForegroundColor Yellow
    } else {
        Write-Host "  Registering '$DlsSvcName'..."
        Remove-ServiceIfExists $nssm $DlsSvcName
        & $nssm install $DlsSvcName $DlsExe $DlsConfigFile
        & $nssm set     $DlsSvcName DisplayName    $DlsSvcDisplay
        & $nssm set     $DlsSvcName Description    $DlsSvcDesc
        & $nssm set     $DlsSvcName Start          SERVICE_AUTO_START
        & $nssm set     $DlsSvcName AppDirectory   $ProjectDir
        & $nssm set     $DlsSvcName AppStdout      (Join-Path $LogDir "dls-stdout.log")
        & $nssm set     $DlsSvcName AppStderr      (Join-Path $LogDir "dls-stderr.log")
        & $nssm set     $DlsSvcName AppRotateFiles 1
        & $nssm set     $DlsSvcName AppRotateBytes 10485760
    }
}

# ---------------------------------------------------------------------------
# Start services - forwarder first so the TCP port is bound before DLS connects
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Starting services..." -ForegroundColor Cyan
& $nssm start $ForwarderSvcName

$dlsRegistered = (-not $SkipDls) -and (Test-Path $DlsExe)
if ($dlsRegistered) {
    Start-Sleep -Seconds 2
    & $nssm start $DlsSvcName
}

# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------
Start-Sleep -Seconds 2
$fwdSvc = Get-Service -Name $ForwarderSvcName -ErrorAction SilentlyContinue
$dlsSvc = if ($dlsRegistered) { Get-Service -Name $DlsSvcName -ErrorAction SilentlyContinue } else { $null }

Write-Host ""
Write-Host "=== Setup Complete ===" -ForegroundColor Green
Write-Host ""

$fwdColor = if ($fwdSvc -and $fwdSvc.Status -eq "Running") { "Green" } else { "Yellow" }
Write-Host "  $ForwarderSvcName : $(if ($fwdSvc) { $fwdSvc.Status } else { 'unknown' })" -ForegroundColor $fwdColor

if ($dlsRegistered) {
    $dlsColor = if ($dlsSvc -and $dlsSvc.Status -eq "Running") { "Green" } else { "Yellow" }
    Write-Host "  $DlsSvcName       : $(if ($dlsSvc) { $dlsSvc.Status } else { 'unknown' })" -ForegroundColor $dlsColor
} elseif ($SkipDls) {
    Write-Host "  $DlsSvcName       : skipped (-SkipDls)" -ForegroundColor Gray
}

Write-Host ""
Write-Host "  Logs : $LogDir"
Write-Host ""
Write-Host "NOTE: XDR API key is stored in the Windows Registry (NSSM AppEnvironmentExtra)." -ForegroundColor Yellow
Write-Host "      Restrict the registry ACL to SYSTEM + Administrators after install." -ForegroundColor Yellow
Write-Host ""
Write-Host "After DLS's first poll (~2 min), confirm records reached Cortex XDR:"
Write-Host "  XQL: dataset = duo_logs | limit 10"
Write-Host ""
