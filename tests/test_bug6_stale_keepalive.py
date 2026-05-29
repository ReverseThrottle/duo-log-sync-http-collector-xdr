"""
Bug 6 Reproducer: Stale requests.Session keep-alive connection after long idle.

The XDR server closes idle TCP connections after its keep-alive timeout.
If the forwarder then calls session.post() on the dead connection, requests
raises ConnectionError. Without a catch, this kills the flush task.

This test spins up a local HTTPS-like TCP server that closes the connection
after the first response, then verifies the broken vs. fixed behavior.

Run:
    python test_bug6_stale_keepalive.py
"""

import socket
import threading
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ── Minimal HTTP server that closes connection after first response ────────────

def run_drop_server(port, ready_event, stop_event):
    """Accepts connections, sends 200 then immediately closes each one.
    Simulates a server whose keep-alive timeout has expired."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(10)
    srv.settimeout(0.5)
    ready_event.set()
    while not stop_event.is_set():
        try:
            conn, _ = srv.accept()
            conn.recv(8192)  # consume request
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: 2\r\n"
                b"Connection: close\r\n"  # always close to simulate stale pool
                b"\r\n"
                b"OK"
            )
            conn.close()
        except socket.timeout:
            continue
        except Exception:
            break
    srv.close()


# ── Broken flush: no retry, session reuse raises ConnectionError ──────────────

def flush_broken(session, url):
    resp = session.post(url, data=b'{"test":1}', timeout=3)
    print(f"  [flush_broken] HTTP {resp.status_code}")


# ── Fixed flush: catch ConnectionError, refresh session ──────────────────────

def make_session():
    s = requests.Session()
    adapter = HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST"]
    ))
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def flush_fixed(session_holder, url):
    try:
        resp = session_holder[0].post(url, data=b'{"test":1}', timeout=3)
        print(f"  [flush_fixed] HTTP {resp.status_code}")
    except requests.exceptions.ConnectionError as e:
        print(f"  [flush_fixed] ConnectionError caught — refreshing session: {e}")
        session_holder[0] = make_session()
        resp = session_holder[0].post(url, data=b'{"test":1}', timeout=3)
        print(f"  [flush_fixed] Retry HTTP {resp.status_code}")


if __name__ == "__main__":
    PORT = 18888
    ready = threading.Event()
    stop = threading.Event()
    t = threading.Thread(target=run_drop_server, args=(PORT, ready, stop), daemon=True)
    t.start()
    ready.wait()
    url = f"http://127.0.0.1:{PORT}/logs/v1/event"

    print("=" * 60)
    print("Test 1: Broken session — no retry on ConnectionError")
    broken_session = make_session()
    try:
        flush_broken(broken_session, url)      # first call: succeeds
        print("  [flush_broken] First call succeeded (connection open)")
        time.sleep(0.2)
        flush_broken(broken_session, url)      # second call: server closed it
    except requests.exceptions.ConnectionError as e:
        print(f"  [flush_broken] CRASH — ConnectionError kills flush task: {e}")
        print("  Records in queue are now LOST.")

    print()
    print("=" * 60)
    print("Test 2: Fixed session — catches ConnectionError and retries")
    fixed_session = [make_session()]
    try:
        flush_fixed(fixed_session, url)        # first call: succeeds
        print("  [flush_fixed] First call succeeded")
        time.sleep(0.2)
        flush_fixed(fixed_session, url)        # second call: recovers
        print("  [flush_fixed] Second call recovered — no records lost.")
    except Exception as e:
        print(f"  [flush_fixed] Unexpected error: {e}")
    finally:
        stop.set()
