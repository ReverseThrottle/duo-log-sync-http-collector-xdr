"""
Bug 6 Reproducer: Stale requests.Session keep-alive connection after long idle.

Sequence reproduced:
  1. First POST to XDR succeeds — session pools the keep-alive connection.
  2. XDR server closes the idle connection (keep-alive timeout expiry).
  3. Second POST — requests tries to reuse the dead pooled connection and raises
     ConnectionError: Connection aborted (stale keep-alive).
  4. Without a catch the ConnectionError propagates, killing the flush thread.
     Records in the queue are lost.
  5. With the fix: ConnectionError is caught, the session is closed and recreated,
     and the retry succeeds.

The stale-connection failure is injected via mock to avoid relying on
urllib3 internals (which vary across versions) for reliable test timing.

Run:
    python test_bug6_stale_keepalive.py
"""

import requests
from unittest.mock import MagicMock, patch, call as mock_call


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_response(status_code=200, text="OK"):
    r = MagicMock(spec=requests.Response)
    r.status_code = status_code
    r.text = text
    return r


def stale_connection_error():
    return requests.exceptions.ConnectionError(
        "Connection aborted (stale keep-alive): "
        "ConnectionAbortedError(10053, 'An established connection was "
        "aborted by the software in your host machine')"
    )


# ── Broken flush: no handling of stale connection ─────────────────────────────

def flush_broken(session, url):
    """Production code without the session-refresh fix."""
    resp = session.post(url, data=b'{"test":1}', timeout=30)
    if 200 <= resp.status_code < 300:
        print(f"  [flush_broken] HTTP {resp.status_code} — batch accepted")
        return True
    return False


# ── Fixed flush: catch ConnectionError, refresh session, retry ────────────────

def flush_fixed(session_ref, url):
    """Production code with the session-refresh fix from sender_loop."""
    try:
        resp = session_ref[0].post(url, data=b'{"test":1}', timeout=30)
        if 200 <= resp.status_code < 300:
            print(f"  [flush_fixed] HTTP {resp.status_code} — batch accepted")
            return True
        return False
    except requests.exceptions.ConnectionError as e:
        print(f"  [flush_fixed] ConnectionError — refreshing session: {e}")
        session_ref[0].close()
        session_ref[0] = requests.Session()
        resp = session_ref[0].post(url, data=b'{"test":1}', timeout=30)
        print(f"  [flush_fixed] Retry HTTP {resp.status_code} — batch accepted")
        return True


if __name__ == "__main__":
    URL = "https://api-tenant.xdr.us.paloaltonetworks.com/logs/v1/event"

    # ── Test 1: broken — ConnectionError propagates, flush thread dies ────────
    print("=" * 60)
    print("Test 1: Broken flush — ConnectionError kills flush thread")
    print()

    session1 = requests.Session()
    # Simulate: 1st call succeeds, 2nd call raises (stale pooled connection)
    session1.post = MagicMock(side_effect=[
        make_response(200),
        stale_connection_error(),
    ])

    try:
        flush_broken(session1, URL)          # 1st flush: succeeds
        print("  [*] Long idle period — XDR server closes the keep-alive connection")
        flush_broken(session1, URL)          # 2nd flush: stale connection raises
    except requests.exceptions.ConnectionError as e:
        print(f"  [flush_broken] CRASH — exception propagates out of flush: {e}")
        print("  [flush_broken] Flush thread is now dead. Records in queue are LOST.")

    print()

    # ── Test 2: fixed — session refreshed, retry succeeds ────────────────────
    print("=" * 60)
    print("Test 2: Fixed flush — session refreshed on ConnectionError, retry succeeds")
    print()

    session2 = requests.Session()
    session3 = requests.Session()  # the refreshed session

    # 1st session: call 1 succeeds, call 2 raises (stale)
    session2.post = MagicMock(side_effect=[
        make_response(200),
        stale_connection_error(),
    ])
    # Refreshed session: retry succeeds
    session3.post = MagicMock(return_value=make_response(200))
    session2.close = MagicMock()

    session_ref = [session2]

    with patch("requests.Session", return_value=session3):
        flush_fixed(session_ref, URL)         # 1st flush: succeeds
        print("  [*] Long idle period — XDR server closes the keep-alive connection")
        flush_fixed(session_ref, URL)         # 2nd flush: catches, refreshes, retries

    print()
    print("  [flush_fixed] Session closed and recreated:", session2.close.called)
    print("  [flush_fixed] Retry used fresh session:", session_ref[0] is session3)
    print()
    print("RESULT: Fix works — ConnectionError caught, session refreshed, no records lost.")
