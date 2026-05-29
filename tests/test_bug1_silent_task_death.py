"""
Bug 1 Reproducer: Silent flush-thread death.

The production forwarder uses threads, not asyncio. This test demonstrates
the thread-based failure mode: an unhandled exception in the flush worker
thread kills it silently while the TCP listener thread keeps accepting DLS
connections. Records accumulate in the queue but are never forwarded to XDR —
matching the 2.5-day dead period seen in the customer logs (May 15-18).

Run:
    python test_bug1_silent_task_death.py           # bug: 0 records forwarded
    python test_bug1_silent_task_death.py --fix     # fix: all records forwarded
"""

import argparse
import json
import queue
import socket
import sys
import threading
import time

HOST = "127.0.0.1"
PORT = 19999  # avoid conflict with real service

APPLY_FIX = False
record_queue: queue.Queue = queue.Queue(maxsize=1000)
flush_count = 0
shutdown = threading.Event()
flush_alive_before_shutdown = True  # updated by run_client() before setting shutdown


# ── Broken flush thread (no exception guard) ──────────────────────────────────

def flush_worker_broken():
    """
    Simulates the buggy flush loop. On the first batch it raises AttributeError
    (the resp.status.code bug). The exception propagates out of the thread
    function, killing the thread. Python prints the traceback to stderr but the
    TCP listener keeps running — callers see DLS connected/disconnected in the
    log but 'Sent X records' never appears again.
    """
    global flush_count
    call_count = 0
    while not shutdown.is_set():
        time.sleep(0.5)
        batch = []
        while True:
            try:
                batch.append(record_queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            call_count += 1
            if call_count == 1:
                # resp.status.code raises AttributeError — propagates uncaught,
                # thread exits, flush is dead for the lifetime of the process.
                raise AttributeError("'int' object has no attribute 'code'")
            flush_count += len(batch)
            print(f"[FLUSH] Sent {len(batch)} records (total={flush_count})")


# ── Fixed flush thread (exception guard keeps it alive) ───────────────────────

def flush_worker_fixed():
    """Flush loop that survives exceptions — the loop never exits on error."""
    global flush_count
    while not shutdown.is_set():
        try:
            time.sleep(0.5)
            batch = []
            while True:
                try:
                    batch.append(record_queue.get_nowait())
                except queue.Empty:
                    break
            if batch:
                try:
                    raise AttributeError("'int' object has no attribute 'code'")
                except AttributeError as e:
                    print(f"[FLUSH] AttributeError caught (use status_code): {e}")
                flush_count += len(batch)
                print(f"[FLUSH] Sent {len(batch)} records (total={flush_count})")
        except Exception as e:
            print(f"[FLUSH] Error (loop continues): {e}")


# ── TCP connection handler ─────────────────────────────────────────────────────

def handle_connection(conn: socket.socket, addr: tuple):
    peer = f"{addr[0]}:{addr[1]}"
    print(f"[SERVER] DLS connected: {peer}")
    buf = b""
    try:
        conn.settimeout(2.0)
        while not shutdown.is_set():
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    try:
                        record_queue.put_nowait(json.loads(line))
                    except queue.Full:
                        pass
    except Exception as e:
        print(f"[SERVER] Handler error: {e}")
    finally:
        print(f"[SERVER] DLS disconnected: {peer}")
        conn.close()


# ── TCP listener thread ────────────────────────────────────────────────────────

def tcp_listener():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, PORT))
        srv.listen(5)
        srv.settimeout(1.0)
        print(f"[SERVER] Listening on {HOST}:{PORT}")
        while not shutdown.is_set():
            try:
                conn, addr = srv.accept()
                threading.Thread(
                    target=handle_connection, args=(conn, addr), daemon=True
                ).start()
            except socket.timeout:
                continue


# ── Test client ────────────────────────────────────────────────────────────────

def run_client():
    time.sleep(0.5)  # let listener bind
    for i in range(3):
        time.sleep(1.5)
        try:
            with socket.create_connection((HOST, PORT), timeout=5) as s:
                record = json.dumps({"event": f"auth_{i}", "user": "test@example.com"})
                s.sendall((record + "\n").encode())
                time.sleep(0.1)
            print(f"[CLIENT] Sent record {i + 1}")
        except Exception as e:
            print(f"[CLIENT] Error: {e}")
    time.sleep(1)
    # Check liveness BEFORE signalling shutdown so a clean exit of the fixed
    # worker (which also stops when shutdown is set) isn't misread as a crash.
    global flush_alive_before_shutdown
    flush_alive_before_shutdown = flush_thread.is_alive()
    shutdown.set()

    print(f"[CLIENT] Done. Total records flushed to XDR: {flush_count}")
    print()
    if not APPLY_FIX and flush_count == 0:
        print("RESULT: BUG REPRODUCED — flush thread died, 0 records forwarded")
    elif APPLY_FIX and flush_count > 0:
        print("RESULT: FIX WORKS — all records forwarded despite AttributeError")
    else:
        print(f"RESULT: unexpected state (fix={APPLY_FIX}, flushed={flush_count})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true", help="Apply the exception guard fix")
    args = parser.parse_args()
    APPLY_FIX = args.fix

    print(f"[SERVER] Fix applied: {APPLY_FIX}")
    print()

    listener_thread = threading.Thread(target=tcp_listener, daemon=True, name="tcp-listener")
    listener_thread.start()

    worker_fn = flush_worker_fixed if APPLY_FIX else flush_worker_broken
    flush_thread = threading.Thread(target=worker_fn, daemon=True, name="flush-worker")
    flush_thread.start()

    client_thread = threading.Thread(target=run_client)
    client_thread.start()
    client_thread.join()

    if not flush_alive_before_shutdown:
        print("[SERVER] !! Flush thread DIED before shutdown — listener still running, records lost.")
    else:
        print("[SERVER] Flush thread was alive until shutdown (good).")
