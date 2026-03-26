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
12. [Security Review Summary](#security-review-summary)

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

- [Python 3.8+](https://www.python.org/downloads/) installed and on `PATH`
- [NSSM](https://nssm.cc/download) (Non-Sucking Service Manager) installed — place `nssm.exe` in `C:\nssm\` or add to `PATH`
- PowerShell 5.1+ (run as Administrator)

### 1. Set up the environment

```powershell
git clone https://github.com/ReverseThrottle/duo-log-sync-http-collector-xdr.git
cd duo-log-sync-http-collector-xdr

python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 2. Configure

```powershell
Copy-Item .env.example .env
notepad .env  # Fill in your XDR and listener settings
```

Update `STATE_FILE_PATH` in `.env` for Windows:
```
STATE_FILE_PATH=C:\ProgramData\duo-xdr-forwarder\state.json
```

### 3. Install Duo Log Sync and configure it

Follow [Duo's Windows installation instructions](https://github.com/duosecurity/duo_log_sync), configuring the server block to point to `127.0.0.1:9999`.

### 4. Install as a Windows Service

```powershell
# Run as Administrator
.\windows\install-service.ps1
```

To uninstall:
```powershell
.\windows\install-service.ps1 -Action uninstall
```

Service logs are written to `C:\ProgramData\duo-xdr-forwarder\logs\`.

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
| `XDR_API_KEY` (Windows) | Windows Registry (via NSSM) | Restrict registry key ACL to `SYSTEM` and `Administrators` only after service installation. The installer script prints a reminder. |

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

**Running manually:**
```bash
LOG_LEVEL=DEBUG .venv/bin/python3 duo_xdr_forwarder.py   # Verbose forwarder output
```

**Windows:**
```
C:\ProgramData\duo-xdr-forwarder\logs\stdout.log
C:\ProgramData\duo-xdr-forwarder\logs\stderr.log
```

---

## Security Review Summary

This codebase was reviewed for security and reliability issues prior to release. The following categories of issues were identified and addressed:

### Addressed before release

| Severity | Issue | Resolution |
|---|---|---|
| High | No HTTPS enforcement — HTTP URLs would expose API key in transit | Startup validation rejects any non-HTTPS URL with `sys.exit(1)` |
| High | Unbounded TCP receive buffer — malformed connection could exhaust memory | 1 MB buffer cap per connection; connection closed if exceeded |
| High | Unbounded record queue — XDR outage could exhaust memory | Queue capped at 200× `BATCH_SIZE`; records dropped with WARNING when full |
| High | Listener thread death invisible to main process — silent failure | Main thread monitors listener thread; shuts down cleanly if it dies |
| Medium | Non-loopback bind address not warned | Warning logged at startup if `LISTEN_HOST` is not a loopback address |
| Medium | JSON parse error logged raw line content — potential PII exposure | Parse errors log byte count only, never log record content |
| Medium | `BATCH_SIZE=0` caused sender loop to spin and never send | Validated ≥ 1 at startup |
| Medium | Invalid `LISTEN_PORT` silently killed listener thread | Validated 1–65535 at startup |
| Medium | `LOG_LEVEL` accepted arbitrary logging module attributes | Validated against known level names at startup |
| Medium | Windows: NSSM stores secrets in Registry plaintext | Documented clearly in installer output and this README |
| Low | Timestamps not range-checked — already-millisecond values sent as seconds would be far in the future | ±5-year sanity check applied; out-of-range timestamps fall back to ingestion time |
| Low | `StartLimitIntervalSec` placed in wrong systemd unit section | Moved to `[Unit]` section |
| Low | systemd unit had no memory or task limits | `MemoryMax=512M` and `TasksMax=64` added |
| Low | `requirements.txt` had no upper version bounds | Pinned to major version ranges |
| Low | PowerShell installer had no guard for dot-source invocation | `$MyInvocation.MyCommand.Path` null guard added |
| Low | PowerShell uninstall had no wait between stop and remove | Polls service status before issuing remove |

### Known limitations / accepted risks

| Item | Notes |
|---|---|
| Best-effort delivery to XDR | Records that fail all retry attempts are discarded. DLS checkpoints prevent re-sending from Duo, so these records are permanently lost. Alert on `ERROR` level log messages to detect this condition. |
| In-memory queue only | There is no persistent write-ahead log between DLS and XDR. A process crash after DLS delivers records but before XDR accepts them will result in those records being lost. DLS's own checkpoint means it will not re-deliver them. |
| Plaintext TCP between DLS and forwarder | Acceptable because the connection is loopback-only by default. If your deployment requires DLS and the forwarder to run on different hosts, configure DLS with `TCPSSL` protocol and update the forwarder to use `ssl.wrap_socket`. |
| No authentication on TCP listener | Any process on the local host that can connect to `LISTEN_HOST:LISTEN_PORT` can inject records. Mitigate with OS-level firewall rules restricting the port to the DLS process user, or by running DLS and the forwarder as the same OS user. |
