#!/usr/bin/env python3
"""
duo_xdr_forwarder.py — TCP listener that receives Duo Log Sync (DLS) NDJSON output
and forwards records to the Cortex XDR HTTP Log Collector.

Architecture:
  DLS  ──TCP──►  [this script]  ──HTTPS──►  Cortex XDR HTTP Log Collector

DLS is configured to send logs over TCP to LISTEN_HOST:LISTEN_PORT. This script
accepts those connections, parses NDJSON records, enriches them, and batch-POSTs
them to the XDR endpoint. Multiple simultaneous DLS connections are supported.
"""

import ipaddress
import json
import logging
import os
import queue
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load and validate all environment variables. Exits on missing required vars."""
    required = ["XDR_COLLECTOR_URL", "XDR_API_KEY"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    def _int(name, default):
        val = os.environ.get(name, str(default))
        try:
            return int(val)
        except ValueError:
            print(f"ERROR: {name} must be an integer, got: {val!r}", file=sys.stderr)
            sys.exit(1)

    def _float(name, default):
        val = os.environ.get(name, str(default))
        try:
            return float(val)
        except ValueError:
            print(f"ERROR: {name} must be a number, got: {val!r}", file=sys.stderr)
            sys.exit(1)

    url = os.environ["XDR_COLLECTOR_URL"].rstrip("/")
    if not url.lower().startswith("https://"):
        print(f"ERROR: XDR_COLLECTOR_URL must use HTTPS to protect the API key in transit, got: {url!r}", file=sys.stderr)
        sys.exit(1)

    listen_port = _int("LISTEN_PORT", 9999)
    if not (1 <= listen_port <= 65535):
        print(f"ERROR: LISTEN_PORT must be between 1 and 65535, got: {listen_port}", file=sys.stderr)
        sys.exit(1)

    batch_size = _int("BATCH_SIZE", 100)
    if batch_size < 1:
        print(f"ERROR: BATCH_SIZE must be >= 1, got: {batch_size}", file=sys.stderr)
        sys.exit(1)

    max_connections = _int("MAX_CONNECTIONS", 10)
    if max_connections < 1:
        print(f"ERROR: MAX_CONNECTIONS must be >= 1, got: {max_connections}", file=sys.stderr)
        sys.exit(1)

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if log_level not in valid_levels:
        print(f"ERROR: LOG_LEVEL must be one of {sorted(valid_levels)}, got: {log_level!r}", file=sys.stderr)
        sys.exit(1)

    return {
        "url": url,
        "api_key": os.environ["XDR_API_KEY"],
        "api_key_id": os.environ.get("XDR_API_KEY_ID"),  # optional — only needed for some tenants
        "dataset": os.environ.get("XDR_DATASET", "duo_logs"),
        "listen_host": os.environ.get("LISTEN_HOST", "127.0.0.1"),
        "listen_port": listen_port,
        "batch_size": batch_size,
        "max_connections": max_connections,
        "flush_interval": _float("FLUSH_INTERVAL_SECONDS", 5),
        "log_level": log_level,
        "max_retries": _int("MAX_RETRIES", 3),
        "backoff": _float("RETRY_BACKOFF_SECONDS", 5),
    }


# ---------------------------------------------------------------------------
# Record enrichment
# ---------------------------------------------------------------------------

def enrich_record(record: dict, dataset: str) -> dict:
    """Add _dataset and _time (epoch ms) fields required by XDR HTTP Log Collector."""
    enriched = dict(record)
    enriched["_dataset"] = dataset
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    ts = record.get("timestamp")
    if ts is not None:
        try:
            _time = int(float(ts) * 1000)
            # Sanity check: reject timestamps outside a ±5 year window from now.
            # Catches already-millisecond timestamps (13-digit) passed as seconds,
            # which would produce dates far in the future.
            _five_years_ms = 5 * 365 * 24 * 3600 * 1000
            if not (now_ms - _five_years_ms <= _time <= now_ms + _five_years_ms):
                logging.warning(
                    "Record timestamp %r is outside a ±5-year window — using current time", ts
                )
                _time = now_ms
            enriched["_time"] = _time
        except (TypeError, ValueError):
            enriched["_time"] = now_ms
    else:
        enriched["_time"] = now_ms
    return enriched


# ---------------------------------------------------------------------------
# HTTP sending
# ---------------------------------------------------------------------------

def send_batch(records: list, url: str, headers: dict, max_retries: int, backoff: float) -> bool:
    """
    POST a batch of enriched records to XDR as NDJSON.

    Retries on 429, 5xx, and network errors with exponential backoff.
    Returns True on 2xx success, False on permanent failure.
    """
    body = "\n".join(json.dumps(r) for r in records)
    attempt = 0
    while attempt <= max_retries:
        if attempt > 0:
            wait = backoff * (2 ** (attempt - 1))
            logging.info("Retry %d/%d in %.1fs", attempt, max_retries, wait)
            time.sleep(wait)
        attempt += 1
        try:
            resp = requests.post(url, data=body, headers=headers, timeout=30)
            if 200 <= resp.status_code < 300:
                logging.debug("Batch of %d records accepted (HTTP %d)", len(records), resp.status_code)
                return True
            elif resp.status_code == 429 or resp.status_code >= 500:
                logging.warning(
                    "HTTP %d — will retry (attempt %d/%d)", resp.status_code, attempt, max_retries + 1
                )
            else:
                logging.error(
                    "HTTP %d — no retry (check credentials/config): %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
        except requests.RequestException as e:
            logging.warning("Network error — will retry (attempt %d/%d): %s", attempt, max_retries + 1, e)

    logging.error("All %d retry attempts exhausted for batch of %d records", max_retries, len(records))
    return False


# Max bytes buffered per connection before closing it (protects against OOM from
# a client that sends data continuously with no newlines).
_MAX_BUF_BYTES = 1 * 1024 * 1024  # 1 MB

# ---------------------------------------------------------------------------
# TCP connection handler (one thread per DLS connection)
# ---------------------------------------------------------------------------

def handle_connection(conn: socket.socket, addr: tuple, record_queue: queue.Queue, shutdown_event: threading.Event, conn_sem: threading.Semaphore = None):
    """
    Read newline-delimited JSON from a single DLS TCP connection.
    Each complete line is parsed and pushed onto record_queue.
    Partial lines are buffered until the next recv().
    """
    peer = f"{addr[0]}:{addr[1]}"
    logging.info("DLS connected: %s", peer)
    buf = b""
    try:
        conn.settimeout(5.0)
        while not shutdown_event.is_set():
            try:
                data = conn.recv(65536)
            except socket.timeout:
                continue
            if not data:
                logging.info("DLS disconnected: %s", peer)
                break
            buf += data
            if len(buf) > _MAX_BUF_BYTES:
                logging.error(
                    "Receive buffer exceeded %d bytes on connection %s — closing connection",
                    _MAX_BUF_BYTES, peer,
                )
                break
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    try:
                        record_queue.put_nowait(record)
                    except queue.Full:
                        logging.warning("Record queue full — dropping record from %s", peer)
                except json.JSONDecodeError:
                    logging.warning("JSON parse error from %s — skipping %d bytes", peer, len(line))
    except Exception as e:
        logging.error("Unexpected error on connection %s: %s", peer, e)
    finally:
        if conn_sem is not None:
            conn_sem.release()
        try:
            conn.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# TCP listener thread
# ---------------------------------------------------------------------------

def tcp_listener(config: dict, record_queue: queue.Queue, shutdown_event: threading.Event):
    """
    Accept incoming TCP connections from DLS. Spawns a daemon thread per connection.
    Runs until shutdown_event is set.
    """
    host = config["listen_host"]
    port = config["listen_port"]
    max_conn = config["max_connections"]
    conn_sem = threading.Semaphore(max_conn)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(16)
        server.settimeout(1.0)
        logging.info("Listening for DLS connections on %s:%d (max %d concurrent)", host, port, max_conn)
        try:
            if not ipaddress.ip_address(host).is_loopback:
                logging.warning(
                    "LISTEN_HOST=%s is not a loopback address — any host that can reach "
                    "this port can inject records into the XDR pipeline", host
                )
        except ValueError:
            logging.warning("LISTEN_HOST=%s is not a valid IP address", host)

        while not shutdown_event.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue

            if not conn_sem.acquire(blocking=False):
                logging.warning(
                    "Connection limit (%d) reached — rejecting connection from %s:%s",
                    max_conn, addr[0], addr[1],
                )
                try:
                    conn.close()
                except OSError:
                    pass
                continue

            t = threading.Thread(
                target=handle_connection,
                args=(conn, addr, record_queue, shutdown_event, conn_sem),
                daemon=True,
            )
            t.start()


# ---------------------------------------------------------------------------
# Sender loop (runs on main thread)
# ---------------------------------------------------------------------------

def sender_loop(config: dict, record_queue: queue.Queue, shutdown_event: threading.Event,
                listener_thread: threading.Thread = None):
    """
    Drain record_queue in batches and POST to Cortex XDR.

    Batches are sent when they reach BATCH_SIZE or after FLUSH_INTERVAL_SECONDS,
    whichever comes first. On XDR failure, the batch is retried with exponential
    backoff; records are not dropped unless all retries are exhausted.

    Monitors listener_thread health — sets shutdown_event if it dies unexpectedly.
    """
    headers = {
        "Content-Type": "text/plain",
        "Authorization": config["api_key"],
    }
    if config["api_key_id"]:
        headers["x-xdr-auth-id"] = str(config["api_key_id"])
    batch = []
    last_flush = time.monotonic()

    while not shutdown_event.is_set() or not record_queue.empty():
        # Check if the listener thread has died unexpectedly
        if listener_thread is not None and not listener_thread.is_alive() and not shutdown_event.is_set():
            logging.critical("TCP listener thread died unexpectedly — shutting down")
            shutdown_event.set()
            break

        # Accumulate records up to batch_size or flush_interval
        flush_deadline = last_flush + config["flush_interval"]
        while len(batch) < config["batch_size"]:
            remaining = flush_deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                record = record_queue.get(timeout=min(remaining, 1.0))
                batch.append(enrich_record(record, config["dataset"]))
            except queue.Empty:
                if time.monotonic() >= flush_deadline:
                    break

        if batch:
            ok = send_batch(batch, config["url"], headers, config["max_retries"], config["backoff"])
            if ok:
                logging.info("Sent %d records to XDR", len(batch))
                batch = []
            else:
                logging.error(
                    "Batch of %d records failed after all retries — records dropped", len(batch)
                )
                batch = []
            last_flush = time.monotonic()
        else:
            last_flush = time.monotonic()

    # Final flush on shutdown
    if batch:
        logging.info("Flushing %d remaining records on shutdown", len(batch))
        send_batch(batch, config["url"], headers, config["max_retries"], config["backoff"])


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_shutdown_event = threading.Event()


def _handle_signal(signum, frame):
    # Set the event only — do NOT call logging here.
    # logging acquires a lock; calling it from a signal handler risks deadlock
    # if the lock is already held by the interrupted thread.
    # The main thread logs the shutdown message after returning from the event wait.
    _shutdown_event.set()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    config = load_config()

    logging.basicConfig(
        level=getattr(logging, config["log_level"], logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    # On Windows, SIGTERM is defined but its handler is never invoked — it is not a real
    # OS signal. NSSM stops processes by sending GenerateConsoleCtrlEvent: first
    # CTRL_C (SIGINT), then CTRL_BREAK (SIGBREAK) if the process is still running.
    # Registering SIGBREAK ensures the graceful shutdown path (including final flush)
    # is reached even when SIGINT is not delivered (e.g. no console attached).
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, _handle_signal)

    _sigbreak_note = ', SIGBREAK' if hasattr(signal, 'SIGBREAK') else ''
    logging.info("Signal handlers registered (SIGTERM, SIGINT%s)", _sigbreak_note)

    logging.info(
        "Starting duo-xdr-forwarder: listen=%s:%d dataset=%s batch_size=%d flush_interval=%ss",
        config["listen_host"],
        config["listen_port"],
        config["dataset"],
        config["batch_size"],
        config["flush_interval"],
    )

    # Bounded queue — protects against OOM if XDR is unreachable for an extended period.
    # Records are dropped (with a WARNING) when full rather than buffering indefinitely.
    # Sized at 200× batch_size to allow generous headroom before backpressure kicks in.
    queue_max = config["batch_size"] * 200
    record_queue: queue.Queue = queue.Queue(maxsize=queue_max)
    logging.info("Record queue max size: %d records", queue_max)

    # TCP listener runs in a daemon thread; sender loop runs on main thread
    listener_thread = threading.Thread(
        target=tcp_listener,
        args=(config, record_queue, _shutdown_event),
        daemon=True,
        name="tcp-listener",
    )
    listener_thread.start()

    try:
        sender_loop(config, record_queue, _shutdown_event, listener_thread)
    except Exception:
        logging.exception("Unhandled exception in sender loop")
        _shutdown_event.set()

    logging.info("Shutdown signal received — shutdown complete")
    sys.exit(0)


if __name__ == "__main__":
    main()
