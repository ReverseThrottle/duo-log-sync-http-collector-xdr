# duo-log-sync-http-collector-xdr

A lightweight, production-ready bridge that forwards **Duo Security authentication and activity logs** to the **Cortex XDR HTTP Log Collector** in real time.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Data Flow](#data-flow)
4. [What Data Is Collected](#what-data-is-collected)
5. [Prerequisites](#prerequisites)
6. [Installation — Linux](#installation--linux)
7. [Installation — Windows](#installation--windows)
8. [Configuration Reference](#configuration-reference)
9. [Security Model](#security-model)
10. [Operational Behavior](#operational-behavior)
11. [Troubleshooting](#troubleshooting)

---

## Overview

[Duo Log Sync (DLS)](https://github.com/duosecurity/duo_log_sync) is Duo Security's official utility for fetching authentication and activity logs from the Duo Admin API. It outputs those logs as newline-delimited JSON (NDJSON) over a TCP connection to a receiving server.

This project provides that receiving server. It:

- Listens on a local TCP port for DLS connections
- Parses incoming NDJSON log records
- Enriches each record with fields required by Cortex XDR (`_dataset`, `_time`)
- Batch-POSTs records to the [Cortex XDR HTTP Log Collector](https://docs-cortex.paloaltonetworks.com/r/Cortex-XDR/Cortex-XDR-API-Reference/Send-Logs-to-Cortex-XDR) endpoint over HTTPS
- Retries failed deliveries with exponential backoff
- Runs as a persistent daemon (systemd on Linux, NSSM Windows Service on Windows)

---

## Architecture

```
┌─────────────────────────────────────────┐
│              This Host                  │
│                                         │
│  ┌─────────────┐    TCP (localhost)     │
│  │  Duo Log    │──────────────────────► │
│  │  Sync (DLS) │       port 9999        │
│  └─────────────┘                        │
│         │                               │
│         │  Duo Admin API (HTTPS)        │
│         ▼                               │
│  ┌──────────────────┐                   │
│  │  duo_xdr_        │                   │
│  │  forwarder.py    │                   │
│  │  (TCP listener)  │                   │
│  └──────────────────┘                   │
│         │                               │
└─────────┼───────────────────────────────┘
          │  HTTPS (TLS 1.2+)
          ▼
┌─────────────────────────────────────────┐
│         Cortex XDR                      │
│  HTTP Log Collector endpoint            │
│  /logs/v1/event                         │
└─────────────────────────────────────────┘
```

Both DLS and the forwarder run on the same host. The TCP connection between them is loopback-only (`127.0.0.1`) by default — log data never traverses the network unencrypted. All communication with Cortex XDR is over HTTPS.

---

## Data Flow

1. **DLS polls the Duo Admin API** every 120 seconds (Duo's minimum), fetching new `auth` and `activity` log records since its last checkpoint.
2. **DLS sends records over TCP** to the forwarder's listener on `127.0.0.1:9999` as NDJSON (one JSON object per line).
3. **The forwarder parses each line**, validates it as JSON, and places it on an internal queue.
4. **The sender loop** drains the queue in batches (up to `BATCH_SIZE` records, or after `FLUSH_INTERVAL_SECONDS` — whichever comes first).
5. **Each batch is POST'd** to the Cortex XDR HTTP Log Collector endpoint with the required `Authorization` and `Content-Type: text/plain` headers.
6. **On success (HTTP 2xx)**, the batch is cleared. On retriable failures (HTTP 429, 5xx, network errors), the batch is retried up to `MAX_RETRIES` times with exponential backoff. On permanent failures (HTTP 4xx other than 429), an error is logged and the batch is discarded.
7. **DLS writes checkpoints** to disk after each successful API poll so it can resume from the correct offset after a restart — preventing duplicate records.

---

## What Data Is Collected

The forwarder receives whatever log endpoints DLS is configured to collect. By default the included `config.yml` enables:

| Endpoint | Content |
|---|---|
| `auth` | Authentication events — user, result (success/denied/fraud), factor (push/OTP/etc.), IP address, device, timestamp |
| `activity` | Admin Panel activity — admin actions, policy changes, user management events |

Additional endpoints can be enabled in `duo_log_sync/config.yml`:

| Endpoint | Content |
|---|---|
| `telephony` | Phone call and SMS log records |
| `trustmonitor` | Risk-based trust assessments |

All records are forwarded verbatim to Cortex XDR with two added fields:

| Field | Value |
|---|---|
| `_dataset` | Configured via `XDR_DATASET` (default: `duo_logs`) — routes records to the correct dataset for XQL queries |
| `_time` | Epoch milliseconds derived from the record's `timestamp` field; falls back to ingestion time if missing or out of range |

---

## Prerequisites

### Duo Security

- **Duo Advantage or Premier plan** — the Admin API with log access (`Grant read log`) is not available on the free tier.
- An **Admin API application** created in the Duo Admin Panel with only the `Grant read log` permission enabled.
  - Admin Panel → Applications → Protect an Application → Admin API
  - Under Permissions: enable **Grant read log** only
  - Note your **Integration Key**, **Secret Key**, and **API Hostname**

### Cortex XDR

- An active Cortex XDR tenant.
- An **HTTP Log Collector** configured in your XDR tenant (Settings → Configurations → HTTP Log Collector).
- The collector's **endpoint URL** and **API key**.

### Host

- Python 3.8 or later (`python3 --version`)
- `pip` or `venv` support (`python3 -m venv`)
- Outbound HTTPS (port 443) to your Cortex XDR tenant endpoint
- Network access from the host to `api-<your-tenant>.duosecurity.com` (HTTPS, port 443)

---

## Installation — Linux

### 1. Clone and set up the Python environment

```bash
git clone https://github.com/ReverseThrottle/duo-log-sync-http-collector-xdr.git
cd duo-log-sync-http-collector-xdr

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Install Duo Log Sync

```bash
git clone https://github.com/duosecurity/duo_log_sync.git
cd duo_log_sync
# Install into the same venv
../.venv/bin/pip install setuptools
../.venv/bin/pip install -e .
cd ..
```

### 3. Configure the forwarder

```bash
cp .env.example .env
```

Edit `.env` and fill in your values (see [Configuration Reference](#configuration-reference)):

```bash
XDR_COLLECTOR_URL=https://api-<tenant>.xdr.us.paloaltonetworks.com/logs/v1/event
XDR_API_KEY=<your-xdr-api-key>
LISTEN_HOST=127.0.0.1
LISTEN_PORT=9999
```

### 4. Configure Duo Log Sync

Create `duo_log_sync/config.yml` based on the template below. Fill in your Duo Admin API credentials:

```yaml
version: '1.0.0'

dls_settings:
  log_filepath: '/var/log/duologsync/duologsync.log'
  log_format: 'JSON'
  api:
    offset: 180       # Days of history to fetch on first run
    timeout: 120      # Seconds between API polls (minimum enforced by DLS)
  checkpointing:
    enabled: True
    directory: '/var/log/duologsync/checkpoints'

servers:
  - id: 'xdr-forwarder'
    hostname: '127.0.0.1'
    port: 9999
    protocol: 'TCP'

account:
  ikey: '<your-duo-integration-key>'
  skey: '<your-duo-secret-key>'
  hostname: '<your-duo-api-hostname>'   # e.g. api-XXXXXXXX.duosecurity.com

  endpoint_server_mappings:
    - endpoints: ['auth', 'activity']
      server: 'xdr-forwarder'

  is_msp: False
```

Create the log directories:

```bash
sudo mkdir -p /var/log/duologsync/checkpoints
sudo chown -R $USER /var/log/duologsync
```

### 5. Test manually before deploying as a service

Open two terminals:

**Terminal 1 — start the forwarder:**
```bash
cd duo-log-sync-http-collector-xdr
.venv/bin/python3 duo_xdr_forwarder.py
```
Expected output:
```
2026-01-01T00:00:00Z INFO Starting duo-xdr-forwarder: listen=127.0.0.1:9999 ...
2026-01-01T00:00:00Z INFO Record queue max size: 20000 records
2026-01-01T00:00:00Z INFO Listening for DLS connections on 127.0.0.1:9999
```

**Terminal 2 — start DLS:**
```bash
cd duo-log-sync-http-collector-xdr
.venv/bin/duologsync duo_log_sync/config.yml
```

After ~2 minutes (DLS's first API poll), the forwarder should log:
```
2026-01-01T00:02:00Z INFO DLS connected: 127.0.0.1:XXXXX
2026-01-01T00:02:05Z INFO Sent 42 records to XDR
```

Verify records appear in Cortex XDR under your configured dataset via XQL:
```
dataset = duo_logs | limit 10
```

### 6. Deploy as a systemd service

```bash
# Create a dedicated service user
sudo useradd -r -s /sbin/nologin duo-xdr-forwarder

# Copy files to the deployment directory
sudo mkdir -p /opt/duo-xdr-forwarder
sudo cp -r .venv duo_xdr_forwarder.py .env /opt/duo-xdr-forwarder/
sudo chown -R duo-xdr-forwarder:duo-xdr-forwarder /opt/duo-xdr-forwarder
sudo chmod 600 /opt/duo-xdr-forwarder/.env  # Restrict .env to service user only

# Install and enable the systemd unit
sudo cp systemd/duo-xdr-forwarder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now duo-xdr-forwarder

# Check status
sudo systemctl status duo-xdr-forwarder
sudo journalctl -u duo-xdr-forwarder -f
```

DLS should be deployed and managed separately (also as a systemd service or alongside this one), with `config.yml` pointing to `127.0.0.1:9999`.

---

## Installation — Windows

### Prerequisites

- [Python 3.8+](https://www.python.org/downloads/) — check **"Add Python to PATH"** during install; do **not** use the Microsoft Store version
- [Git for Windows](https://git-scm.com/download/win)
- [NSSM](https://nssm.cc/download) (Non-Sucking Service Manager) — place `nssm.exe` in `C:\nssm\` or add it to `PATH`
- PowerShell 5.1+ (run **as Administrator**)

> **Important — install location:** Do not install under `C:\Program Files\` or any path containing spaces. NSSM does not reliably quote space-containing script paths when registering a service, causing Python to receive a truncated argument and exit immediately with `can't open file 'C:\\Program'`. Use `C:\ProgramData\` — it has no spaces and is writable without elevation.

### 1. Clone the repository

Clone directly into `C:\ProgramData\` to avoid path-quoting issues with NSSM:

```powershell
git clone https://github.com/ReverseThrottle/duo-log-sync-http-collector-xdr.git C:\ProgramData\dls-http-collector-xdr-main
cd C:\ProgramData\dls-http-collector-xdr-main
```

### 2. Set up the Python environment

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 3. Install Duo Log Sync

Clone DLS and install it into the same venv as the forwarder — one Python environment to manage:

```powershell
git clone https://github.com/duosecurity/duo_log_sync.git C:\ProgramData\duo_log_sync
cd C:\ProgramData\duo_log_sync
C:\ProgramData\dls-http-collector-xdr-main\.venv\Scripts\pip install setuptools
C:\ProgramData\dls-http-collector-xdr-main\.venv\Scripts\pip install -e .
cd C:\ProgramData\dls-http-collector-xdr-main
```

### 4. Configure Duo Log Sync

Create `C:\ProgramData\duo_log_sync\config.yml`. Fill in your Duo Admin API credentials:

```yaml
version: '1.0.0'

dls_settings:
  log_filepath: 'C:\ProgramData\duologsync\duologsync.log'
  log_format: 'JSON'
  api:
    offset: 180       # Days of history to fetch on first run
    timeout: 120      # Seconds between API polls (minimum enforced by DLS)
  checkpointing:
    enabled: True
    directory: 'C:\ProgramData\duologsync\checkpoints'

servers:
  - id: 'xdr-forwarder'
    hostname: '127.0.0.1'
    port: 9999
    protocol: 'TCP'

account:
  ikey: '<your-duo-integration-key>'
  skey: '<your-duo-secret-key>'
  hostname: '<your-duo-api-hostname>'   # e.g. api-XXXXXXXX.duosecurity.com

  endpoint_server_mappings:
    - endpoints: ['auth', 'activity']
      server: 'xdr-forwarder'

  is_msp: False
```

Create the DLS log and checkpoint directories:

```powershell
New-Item -ItemType Directory -Force "C:\ProgramData\duologsync\checkpoints"
```

> **Important:** DLS will exit immediately at startup if the checkpoint directory does not exist — it does not create it automatically.

### 5. Configure the forwarder

```powershell
Copy-Item .env.example .env
notepad .env
```

Only `XDR_COLLECTOR_URL` and `XDR_API_KEY` are required — everything else has sensible defaults (see [Configuration Reference](#configuration-reference)):

```ini
XDR_COLLECTOR_URL=https://api-<tenant>.xdr.us.paloaltonetworks.com/logs/v1/event
XDR_API_KEY=<your-xdr-api-key>
```

The `DUO_IKEY`, `DUO_SKEY`, and `DUO_HOSTNAME` fields in `.env.example` are only used by `windows\setup.ps1` to generate DLS's `config.yml`. The forwarder itself never reads them — leave them blank or remove them.

### 6. Test manually before deploying as a service

Open two Administrator PowerShell windows:

**Window 1 — start the forwarder:**
```powershell
cd C:\ProgramData\dls-http-collector-xdr-main
.venv\Scripts\python duo_xdr_forwarder.py
```
Expected output:
```
2026-01-01T00:00:00Z INFO Starting duo-xdr-forwarder: listen=127.0.0.1:9999 ...
2026-01-01T00:00:00Z INFO Record queue max size: 20000 records
2026-01-01T00:00:00Z INFO Listening for DLS connections on 127.0.0.1:9999
```

**Window 2 — start DLS** (if not already running as a service):
```powershell
C:\ProgramData\dls-http-collector-xdr-main\.venv\Scripts\duologsync C:\ProgramData\duo_log_sync\config.yml
```

After ~2 minutes (DLS's first API poll), the forwarder should log:
```
2026-01-01T00:02:00Z INFO DLS connected: 127.0.0.1:XXXXX
2026-01-01T00:02:05Z INFO Sent 42 records to XDR
```

Verify records in Cortex XDR via XQL:
```
dataset = duo_logs | limit 10
```

### 7. Register the forwarder as a Windows service

Run the following from an **Administrator** PowerShell prompt:

```powershell
$ProjectDir = "C:\ProgramData\dls-http-collector-xdr-main"
$PythonExe  = "$ProjectDir\.venv\Scripts\python.exe"
$Script     = "$ProjectDir\duo_xdr_forwarder.py"
$LogDir     = "C:\ProgramData\duo-xdr-forwarder\logs"

New-Item -ItemType Directory -Force $LogDir

nssm install DuoXdrForwarder $PythonExe $Script
nssm set DuoXdrForwarder DisplayName    "Duo XDR Forwarder"
nssm set DuoXdrForwarder Description    "Forwards Duo log records to Cortex XDR."
nssm set DuoXdrForwarder Start          SERVICE_AUTO_START
nssm set DuoXdrForwarder AppDirectory   $ProjectDir
nssm set DuoXdrForwarder AppStdout      "$LogDir\forwarder-stdout.log"
nssm set DuoXdrForwarder AppStderr      "$LogDir\forwarder-stderr.log"
nssm set DuoXdrForwarder AppRotateFiles 1
nssm set DuoXdrForwarder AppRotateBytes 10485760

nssm start DuoXdrForwarder
Get-Service DuoXdrForwarder
```

`AppDirectory` is required — it sets the working directory so that `python-dotenv` can locate the `.env` file at startup. Without it the service exits immediately with a missing credentials error.

Verify the service started correctly:

```powershell
Get-Content "$LogDir\forwarder-stderr.log" -Tail 20
```

> **Note on log files:** All forwarder output — info, warnings, and errors alike — goes to `forwarder-stderr.log`. This is expected: Python's `logging` module writes to `sys.stderr` by default, and NSSM captures stderr separately from stdout. `forwarder-stdout.log` will be empty under normal operation.

To uninstall:
```powershell
nssm stop DuoXdrForwarder
nssm remove DuoXdrForwarder confirm
```

### 8. Register DLS as a Windows service

Run the following from an **Administrator** PowerShell prompt:

```powershell
$DlsDir    = "C:\ProgramData\duo_log_sync"
$DlsExe    = "C:\ProgramData\dls-http-collector-xdr-main\.venv\Scripts\duologsync.exe"
$ConfigYml = "$DlsDir\config.yml"
$LogDir    = "C:\ProgramData\duo-xdr-forwarder\logs"

nssm install DuoLogSync $DlsExe $ConfigYml
nssm set DuoLogSync DisplayName      "Duo Log Sync"
nssm set DuoLogSync Description      "Polls the Duo Admin API and forwards logs to the XDR forwarder."
nssm set DuoLogSync Start            SERVICE_AUTO_START
nssm set DuoLogSync AppDirectory     $DlsDir
nssm set DuoLogSync AppStdout        "$LogDir\dls-stdout.log"
nssm set DuoLogSync AppStderr        "$LogDir\dls-stderr.log"
nssm set DuoLogSync AppRotateFiles   1
nssm set DuoLogSync AppRotateBytes   10485760
nssm set DuoLogSync DependOnService  DuoXdrForwarder

nssm start DuoLogSync
Get-Service DuoLogSync
```

`DependOnService DuoXdrForwarder` ensures DLS waits for the forwarder to be running before attempting its TCP connection to `127.0.0.1:9999`. Without it, DLS exits immediately on first start (before the forwarder is ready) and NSSM enters a restart loop.

`AppDirectory` sets the working directory to the DLS clone root so DLS can locate its checkpoint files correctly.

Verify DLS started correctly:

```powershell
Get-Content "$LogDir\dls-stderr.log" -Tail 20
```

To uninstall:
```powershell
nssm stop DuoLogSync
nssm remove DuoLogSync confirm
```

---

### Automated installer (work in progress)

`windows\setup.ps1` is an all-in-one script that automates the steps above, including fetching and configuring DLS. It is still being refined — the manual steps above are recommended for production deployments.

---

## Setting Up Duo Log Sync (DLS) — Windows

This section covers installing and running [Duo Log Sync](https://github.com/duosecurity/duo_log_sync) on the same Windows host as the forwarder, reusing the forwarder's existing venv.

### Prerequisites

Three values from the Duo Admin Panel before you start:

- **Integration Key** (ikey)
- **Secret Key** (skey)
- **API Hostname** — `api-XXXXXXXX.duosecurity.com`

Get them from **Admin Panel → Applications → your Admin API application**. If one doesn't exist: Protect an Application → Admin API → enable **Grant read log** only.

### 1. Get the DLS source

Download the zip from GitHub and extract to `C:\ProgramData\`, or clone:

```powershell
git clone https://github.com/duosecurity/duo_log_sync.git C:\ProgramData\duo_log_sync-master
```

> If you downloaded the zip, the folder will already be named `duo_log_sync-master` — use that name as-is throughout.

### 2. Install DLS into the forwarder's venv

```powershell
cd C:\ProgramData\duo_log_sync-master
C:\ProgramData\dls-http-collector-xdr-main\.venv\Scripts\pip install setuptools
C:\ProgramData\dls-http-collector-xdr-main\.venv\Scripts\pip install -e .
```

### 3. Fix the cerberus / setuptools 81+ incompatibility

DLS pins `cerberus==1.3.2`, which imports `pkg_resources` — a module removed in `setuptools` 81+. Upgrade cerberus to a version that uses `importlib.metadata` instead:

```powershell
C:\ProgramData\dls-http-collector-xdr-main\.venv\Scripts\pip install "cerberus>=1.3.5"
```

pip will warn that this conflicts with DLS's pin. The warning is expected and harmless — cerberus 1.3.5+ is a drop-in replacement for everything DLS uses.

### 4. Create directories

DLS exits immediately at startup if the checkpoint directory does not exist:

```powershell
New-Item -ItemType Directory -Force "C:\ProgramData\duologsync\checkpoints"
New-Item -ItemType Directory -Force "C:\ProgramData\duologsync\logs"
```

### 5. Create config.yml

Create `C:\ProgramData\duo_log_sync-master\config.yml` and fill in your Duo credentials.

> **Important — use single quotes, not double quotes.** YAML double-quoted strings process backslashes as escape sequences, so Windows paths like `C:\ProgramData\duo_log_sync-master` will cause a parse error. Single-quoted strings are literal.

```yaml
version: '1.0.0'

dls_settings:
  log_filepath: 'C:\ProgramData\duologsync\logs\duologsync.log'
  log_format: 'JSON'
  api:
    offset: 180
    timeout: 120
  checkpointing:
    enabled: True
    directory: 'C:\ProgramData\duologsync\checkpoints'

servers:
  - id: 'xdr-forwarder'
    hostname: '127.0.0.1'
    port: 9999
    protocol: 'TCP'

account:
  ikey: '<your-duo-integration-key>'
  skey: '<your-duo-secret-key>'
  hostname: '<your-duo-api-hostname>'

  endpoint_server_mappings:
    - endpoints: ['auth', 'activity']
      server: 'xdr-forwarder'

  is_msp: False
```

### 6. Smoke test

With the forwarder service already running, test DLS manually first:

```powershell
C:\ProgramData\dls-http-collector-xdr-main\.venv\Scripts\duologsync.exe C:\ProgramData\duo_log_sync-master\config.yml
```

After ~2 minutes (DLS's first API poll) the forwarder log should show:

```
INFO DLS connected: 127.0.0.1:XXXXX
INFO Sent 42 records to XDR
```

Verify records in Cortex XDR:

```
dataset = duo_logs | limit 10
```

When confirmed, stop DLS with **Ctrl-C** — it flushes cleanly before exiting.

### 7. Register DLS as a Windows service

Run from an **Administrator** PowerShell prompt:

```powershell
$DlsDir    = "C:\ProgramData\duo_log_sync-master"
$DlsExe    = "C:\ProgramData\dls-http-collector-xdr-main\.venv\Scripts\duologsync.exe"
$ConfigYml = "$DlsDir\config.yml"
$LogDir    = "C:\ProgramData\duo-xdr-forwarder\logs"

nssm install DuoLogSync $DlsExe $ConfigYml
nssm set DuoLogSync DisplayName      "Duo Log Sync"
nssm set DuoLogSync Description      "Polls the Duo Admin API and forwards logs to the XDR forwarder."
nssm set DuoLogSync Start            SERVICE_AUTO_START
nssm set DuoLogSync AppDirectory     $DlsDir
nssm set DuoLogSync AppStdout        "$LogDir\dls-stdout.log"
nssm set DuoLogSync AppStderr        "$LogDir\dls-stderr.log"
nssm set DuoLogSync AppRotateFiles   1
nssm set DuoLogSync AppRotateBytes   10485760
nssm set DuoLogSync DependOnService  DuoXdrForwarder

nssm start DuoLogSync
Get-Service DuoLogSync
```

`DependOnService DuoXdrForwarder` ensures DLS waits for the forwarder to be up before connecting to `127.0.0.1:9999`. Without it, DLS exits immediately on first start and NSSM enters a restart loop.

Verify it came up:

```powershell
Get-Content "$LogDir\dls-stderr.log" -Tail 20
```

To uninstall:

```powershell
nssm stop DuoLogSync
nssm remove DuoLogSync confirm
```

---

## Configuration Reference

All configuration is via environment variables, loaded from a `.env` file in the working directory (via `python-dotenv`) or from the system environment.

| Variable | Required | Default | Description |
|---|---|---|---|
| `XDR_COLLECTOR_URL` | **Yes** | — | Full HTTPS URL of the Cortex XDR HTTP Log Collector endpoint. Must begin with `https://` — the script will refuse to start if HTTP is specified, to prevent API key exposure. |
| `XDR_API_KEY` | **Yes** | — | Cortex XDR API key value. |
| `XDR_API_KEY_ID` | No | — | API key ID (integer string). Only required for some XDR tenant configurations. Omit if not needed. |
| `LISTEN_HOST` | No | `127.0.0.1` | IP address for the TCP listener to bind on. Defaults to loopback — **do not change to `0.0.0.0` unless you fully understand the security implications** (see [Security Model](#security-model)). |
| `LISTEN_PORT` | No | `9999` | TCP port to listen on. Must match the `port` in DLS `config.yml`. Valid range: 1–65535. |
| `XDR_DATASET` | No | `duo_logs` | Dataset name used in Cortex XDR for routing and XQL queries. |
| `BATCH_SIZE` | No | `100` | Number of records per HTTP POST to XDR. Must be ≥ 1. |
| `FLUSH_INTERVAL_SECONDS` | No | `5` | Maximum seconds to wait before flushing a partial batch. Ensures timely delivery when records trickle in slowly. |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity. Valid values: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `MAX_RETRIES` | No | `3` | Maximum retry attempts per batch on transient failures (HTTP 429, 5xx, network errors) before discarding the batch. |
| `RETRY_BACKOFF_SECONDS` | No | `5` | Base backoff in seconds between retries. Doubles with each attempt (exponential backoff). |

---

## Security Model

### What the script does

- **Listens on TCP** for connections from DLS. By default this is `127.0.0.1` (loopback only) — no external host can connect.
- **Reads NDJSON records** from DLS and parses them as JSON. Non-JSON lines are silently discarded (logged at WARNING level without reproducing their content).
- **Enriches records** by adding `_dataset` and `_time` fields, then batch-POSTs them to Cortex XDR over **HTTPS with TLS certificate verification enabled** (Python `requests` default).
- **Retries** transient failures and discards batches that permanently fail, logging errors.
- **Never writes Duo log content to disk** — all processing is in memory.

### What the script does not do

- Does not modify, filter, redact, or sample Duo log records — all records received from DLS are forwarded as-is.
- Does not store credentials beyond the lifetime of the process (loaded from environment at startup).
- Does not open any outbound connection other than to `XDR_COLLECTOR_URL`.
- Does not accept inbound connections from any host other than those that can reach `LISTEN_HOST:LISTEN_PORT`.

### Credential storage

| Credential | Where stored | How to protect |
|---|---|---|
| Duo `skey` | `duo_log_sync/config.yml` | Restrict file permissions: `chmod 600 config.yml`. This file must **never** be committed to git (it is gitignored by this repo). |
| `XDR_API_KEY` | `.env` file | Restrict file permissions: `chmod 600 .env`. This file is gitignored. |
| `XDR_API_KEY` (Windows) | `.env` file in the project directory (read at service startup via `AppDirectory`) | Restrict file ACL to `SYSTEM` and `Administrators`: right-click → Properties → Security. The file is gitignored. |

### Network security

- All traffic between this host and Cortex XDR is **HTTPS (TLS 1.2+)**. The `XDR_COLLECTOR_URL` is validated to begin with `https://` at startup — the process will exit immediately if an HTTP URL is configured.
- The TCP listener between DLS and the forwarder is **plaintext**. This is intentional and safe as long as `LISTEN_HOST` remains `127.0.0.1` (loopback). If you change `LISTEN_HOST` to a network-reachable address, the forwarder logs a WARNING — any host that can reach that address could inject arbitrary records into your XDR dataset.
- The forwarder enforces a **1 MB receive buffer limit** per connection. A connection that sends more than 1 MB without a newline is closed immediately to prevent memory exhaustion.

### Least-privilege deployment (Linux)

The systemd unit runs the process as a dedicated `duo-xdr-forwarder` user with no shell and no home directory. The unit applies the following hardening:

| Setting | Effect |
|---|---|
| `ProtectSystem=strict` | Filesystem is read-only except for explicitly listed paths |
| `ProtectHome=true` | Home directories are inaccessible |
| `PrivateTmp=true` | Private `/tmp` namespace |
| `NoNewPrivileges=true` | Process cannot gain elevated privileges via setuid/capabilities |
| `MemoryMax=512M` | OOM killed before it can consume unbounded memory |
| `TasksMax=64` | Limits thread count (one thread per DLS connection) |

### Dependency supply chain

Dependencies are pinned to major version ranges in `requirements.txt` to prevent unexpected breaking changes from future releases:

```
requests>=2.31.0,<3.0.0
python-dotenv>=1.0.0,<2.0.0
```

For high-security environments, consider adding hash verification:
```bash
pip install --require-hashes -r requirements.txt
```
Generate a hash-pinned requirements file with:
```bash
pip-compile --generate-hashes requirements.in
```

---

## Operational Behavior

### Startup validation

The script validates all configuration at startup and exits immediately (`sys.exit(1)`) with a descriptive error message if:

- Any required environment variable is missing (`XDR_COLLECTOR_URL`, `XDR_API_KEY`)
- `XDR_COLLECTOR_URL` does not begin with `https://`
- `LISTEN_PORT` is outside the range 1–65535
- `BATCH_SIZE` is less than 1
- `LOG_LEVEL` is not one of the recognized Python logging levels

### Batching and delivery guarantees

- Records are accumulated in an in-memory queue (maximum 200× `BATCH_SIZE` records) and sent in batches.
- A batch is flushed when it reaches `BATCH_SIZE` records **or** after `FLUSH_INTERVAL_SECONDS` seconds, whichever comes first.
- The queue is **bounded** — if XDR is unreachable for an extended period and the queue fills, incoming records are dropped with a WARNING log rather than consuming unbounded memory.
- Delivery is **at-least-once** from DLS's perspective (DLS checkpoints prevent re-sending already-fetched records on restart) but **best-effort** from the forwarder to XDR (failed batches after all retries are discarded).

### Graceful shutdown

On `SIGTERM` or `SIGINT` (Ctrl-C), the forwarder:
1. Stops accepting new TCP connections
2. Finishes processing any records already in the queue
3. Sends a final flush batch to XDR
4. Exits cleanly with code 0

### Listener health monitoring

The main sender loop monitors the TCP listener thread. If the listener thread dies unexpectedly (e.g., due to an OS-level socket error), the process logs a CRITICAL message and shuts down so that systemd can restart it cleanly, rather than silently running in a state where DLS cannot connect.

### Log rotation

DLS handles its own log rotation via checkpoints. The forwarder itself has no local log files — all output goes to stdout/stderr, captured by systemd journal or NSSM log files.

---

## Troubleshooting

### Forwarder exits immediately at startup

Check the error message — it will be one of the startup validation failures described above. Most commonly:
- Missing required env vars → ensure `.env` is present and complete
- HTTP URL → ensure `XDR_COLLECTOR_URL` starts with `https://`

### DLS shows `403 Access forbidden`

The Duo Admin API is returning a 403. Check:
1. Your Duo account is on the **Advantage or Premier plan** — the Admin API is not available on the free tier.
2. The Admin API application in the Duo Admin Panel has **Grant read log** enabled under Permissions.
3. The `ikey`, `skey`, and `hostname` in `config.yml` match the correct application.
4. If the Admin API app has **Networks for API Access** configured, ensure this host's IP is in the allowlist.

### DLS connects but no records appear in XDR

1. Check the forwarder log for HTTP errors from the XDR endpoint.
2. Verify the `XDR_COLLECTOR_URL` matches the endpoint URL shown in your XDR tenant's HTTP Log Collector configuration.
3. Verify the `XDR_API_KEY` is correct and has not expired.
4. In Cortex XDR, query: `dataset = duo_logs | limit 10` — if the dataset name was changed via `XDR_DATASET`, use that name instead.

### `Record queue full — dropping record` warnings

The forwarder cannot deliver records to XDR as fast as DLS is sending them, causing the internal queue to fill. This typically means:
- XDR is temporarily unreachable or slow → records will resume flowing once connectivity is restored.
- The XDR endpoint is rate-limiting requests → reduce `BATCH_SIZE` or increase `FLUSH_INTERVAL_SECONDS` to reduce request frequency.

### Large number of records on first run

DLS is configured to fetch up to 180 days of history on first run (`offset: 180` in `config.yml`). This is normal. After the first successful poll, DLS will only fetch new records. You can reduce the initial backfill by lowering `offset` in `config.yml` before the first run.

### Checking logs

**Linux (systemd):**
```bash
journalctl -u duo-xdr-forwarder -f          # Forwarder logs (live)
tail -f /var/log/duologsync/duologsync.log   # DLS application logs
```

**Running manually (Linux):**
```bash
LOG_LEVEL=DEBUG .venv/bin/python3 duo_xdr_forwarder.py
```

**Windows (service logs):**
```powershell
# All forwarder output goes to stderr.log — stdout.log will be empty
Get-Content "C:\ProgramData\duo-xdr-forwarder\logs\forwarder-stderr.log" -Tail 50 -Wait
```

**Windows (running manually with verbose output):**
```powershell
cd C:\ProgramData\dls-http-collector-xdr-main
$env:LOG_LEVEL = "DEBUG"
.venv\Scripts\python duo_xdr_forwarder.py
```


