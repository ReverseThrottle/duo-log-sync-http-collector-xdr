#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install or uninstall duo-xdr-forwarder as a Windows Service via NSSM.

.DESCRIPTION
    Registers duo_xdr_forwarder.py as a Windows Service using NSSM (Non-Sucking Service Manager).
    Environment variables are loaded from a .env file and passed to NSSM.

.PARAMETER Action
    "install" (default) or "uninstall"

.PARAMETER ScriptPath
    Full path to duo_xdr_forwarder.py. Defaults to the directory containing this script.

.PARAMETER PythonPath
    Full path to python.exe. Defaults to auto-detection via 'where python'.

.PARAMETER EnvFile
    Full path to the .env file containing configuration. Defaults to .env in the script directory.

.EXAMPLE
    .\install-service.ps1
    .\install-service.ps1 -Action uninstall
    .\install-service.ps1 -ScriptPath "C:\tools\duo-xdr-forwarder\duo_xdr_forwarder.py" -EnvFile "C:\tools\duo-xdr-forwarder\.env"
#>

param(
    [ValidateSet("install", "uninstall")]
    [string]$Action = "install",

    [string]$ScriptPath = "",
    [string]$PythonPath = "",
    [string]$EnvFile = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ServiceName = "DuoXdrForwarder"
$ServiceDisplay = "Duo Log Sync - Cortex XDR Forwarder"
$ServiceDesc = "Tails Duo Log Sync output and forwards records to the Cortex XDR HTTP Log Collector."
$StateDir = "C:\ProgramData\duo-xdr-forwarder"
$LogDir = "C:\ProgramData\duo-xdr-forwarder\logs"

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
# Guard against dot-sourcing, where MyCommand.Path is empty
$ScriptDir = if ($MyInvocation.MyCommand.Path) {
    Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
    $PWD.Path
}
$ProjectDir = Split-Path -Parent $ScriptDir

if (-not $ScriptPath) {
    $ScriptPath = Join-Path $ProjectDir "duo_xdr_forwarder.py"
}
if (-not $EnvFile) {
    $EnvFile = Join-Path $ProjectDir ".env"
}
if (-not $PythonPath) {
    try {
        $PythonPath = (Get-Command python -ErrorAction Stop).Source
    } catch {
        Write-Error "python.exe not found in PATH. Specify -PythonPath explicitly."
    }
}

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
        try {
            $resolved = (Get-Command $c -ErrorAction Stop).Source
            return $resolved
        } catch {}
    }
    return $null
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
        # Strip surrounding quotes
        if (($val.StartsWith('"') -and $val.EndsWith('"')) -or
            ($val.StartsWith("'") -and $val.EndsWith("'"))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        $vars[$key] = $val
    }
    return $vars
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
if ($Action -eq "uninstall") {
    $nssm = Find-NSSM
    if (-not $nssm) {
        Write-Error "NSSM not found. Cannot uninstall service."
    }
    Write-Host "Stopping and removing service '$ServiceName'..."
    & $nssm stop $ServiceName
    # Wait for the service to reach a stopped state before removing
    $waited = 0
    while ($waited -lt 15) {
        $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if (-not $svc -or $svc.Status -eq "Stopped") { break }
        Start-Sleep -Seconds 1
        $waited++
    }
    & $nssm remove $ServiceName confirm
    Write-Host "Service '$ServiceName' removed."
    exit 0
}

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
Write-Host "=== Duo XDR Forwarder - Windows Service Installer ==="
Write-Host ""

# Validate files
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found: $ScriptPath"
}
if (-not (Test-Path $EnvFile)) {
    Write-Error ".env file not found: $EnvFile`nCopy .env.example to .env and fill in your values."
}
if (-not (Test-Path $PythonPath)) {
    Write-Error "Python executable not found: $PythonPath"
}

# Find or prompt for NSSM
$nssm = Find-NSSM
if (-not $nssm) {
    Write-Host ""
    Write-Host "NSSM (Non-Sucking Service Manager) is required but was not found." -ForegroundColor Yellow
    Write-Host "Download from: https://nssm.cc/download" -ForegroundColor Yellow
    Write-Host "Install nssm.exe to C:\nssm\ or add it to your PATH, then re-run this script." -ForegroundColor Yellow
    exit 1
}

Write-Host "NSSM found: $nssm"
Write-Host "Python:     $PythonPath"
Write-Host "Script:     $ScriptPath"
Write-Host "Env file:   $EnvFile"
Write-Host ""

# Create state/log directories
foreach ($dir in @($StateDir, $LogDir)) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Write-Host "Created directory: $dir"
    }
}

# Remove existing service if present
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing service '$ServiceName'..."
    & $nssm stop $ServiceName 2>$null
    & $nssm remove $ServiceName confirm
}

# Register the service
Write-Host "Registering service '$ServiceName'..."
& $nssm install $ServiceName $PythonPath $ScriptPath
& $nssm set $ServiceName DisplayName $ServiceDisplay
& $nssm set $ServiceName Description $ServiceDesc
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName AppStdout (Join-Path $LogDir "stdout.log")
& $nssm set $ServiceName AppStderr (Join-Path $LogDir "stderr.log")
& $nssm set $ServiceName AppRotateFiles 1
& $nssm set $ServiceName AppRotateBytes 10485760  # 10 MB

# Load environment variables from .env and pass them to NSSM.
# SECURITY NOTE: NSSM stores these values (including XDR_API_KEY) in the Windows
# Registry under HKLM\SYSTEM\CurrentControlSet\Services\$ServiceName in plaintext.
# Restrict registry access to SYSTEM and Administrators only after installation.
$envVars = Read-EnvFile $EnvFile
if ($envVars.Count -gt 0) {
    Write-Host "Configuring $($envVars.Count) environment variable(s) from $EnvFile..."
    Write-Host "NOTE: Secrets are stored in the Windows Registry. Ensure registry ACLs restrict access." -ForegroundColor Yellow
    $envString = ($envVars.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "`0"
    & $nssm set $ServiceName AppEnvironmentExtra $envString
}

# Start the service
Write-Host "Starting service..."
& $nssm start $ServiceName

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Write-Host ""
    Write-Host "Service '$ServiceName' is running." -ForegroundColor Green
    Write-Host "Logs: $LogDir"
    Write-Host "State: $StateDir\state.json"
} else {
    Write-Host ""
    Write-Host "Service registered but may not have started. Check logs in: $LogDir" -ForegroundColor Yellow
    Write-Host "You can also run: nssm start $ServiceName"
}
