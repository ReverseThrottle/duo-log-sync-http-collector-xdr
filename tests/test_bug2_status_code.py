"""
Bug 2 Reproducer: resp.status.code AttributeError on XDR non-200 response.

The script uses resp.status.code but requests.Response exposes resp.status_code.
This raises AttributeError on any non-200 response, killing the flush task.

Run:
    python test_bug2_status_code.py
"""

import requests
from unittest.mock import MagicMock, patch
import traceback


def flush_xdr_broken(resp):
    """Buggy version — crashes on non-200."""
    if resp.status_code != 200:
        print(f"CRITICAL XDR REJECTION! HTTP {resp.status.code}")  # BUG: .status.code
    else:
        print(f"Accepted: HTTP {resp.status_code}")


def flush_xdr_fixed(resp):
    """Fixed version."""
    if resp.status_code != 200:
        print(f"CRITICAL XDR REJECTION! HTTP {resp.status_code}: {resp.text}")
    else:
        print(f"Accepted: HTTP {resp.status_code}")


def make_mock_response(status_code, body=""):
    r = MagicMock(spec=requests.Response)
    r.status_code = status_code
    r.text = body
    # NOTE: real requests.Response does NOT have .status — only .status_code
    del r.status  # remove mock's auto-generated attribute to match real behavior
    return r


if __name__ == "__main__":
    print("=" * 60)
    print("Test 1: 200 OK response (both versions)")
    resp_200 = make_mock_response(200, '{"accepted":1}')

    try:
        flush_xdr_broken(resp_200)
        print("  Broken version: PASS (no error on 200)")
    except AttributeError as e:
        print(f"  Broken version: FAIL — {e}")

    flush_xdr_fixed(resp_200)
    print()

    print("=" * 60)
    print("Test 2: 400 Bad Request (the crash scenario)")
    resp_400 = make_mock_response(400, "Invalid payload")

    print("  Broken version:")
    try:
        flush_xdr_broken(resp_400)
        print("    No error (unexpected)")
    except AttributeError as e:
        print(f"    AttributeError raised — this kills the flush task!")
        print(f"    Error: {e}")
        print(f"    Traceback would be swallowed by asyncio on Windows.")

    print()
    print("  Fixed version:")
    flush_xdr_fixed(resp_400)

    print()
    print("=" * 60)
    print("Test 3: Confirm requests.Response has no .status attribute")
    try:
        real_resp = requests.Response()
        real_resp.status_code = 400
        _ = real_resp.status.code  # This is what the buggy code does
        print("  .status.code exists (unexpected)")
    except AttributeError as e:
        print(f"  Confirmed: requests.Response has no .status — {e}")
