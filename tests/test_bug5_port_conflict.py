"""
Bug 5 Reproducer: Dual-instance startup race / port 9999 already in use.

From the log (May 15 16:56:40 and 16:56:43): two forwarder instances started
3 seconds apart. The second one hits [WinError 10048] when binding port 9999.
If this OSError isn't caught, the second instance crashes silently.

Run:
    python test_bug5_port_conflict.py
"""

import asyncio
import sys

HOST = "127.0.0.1"
PORT = 19998


async def start_server_broken():
    """No error handling — crashes on port conflict."""
    server = await asyncio.start_server(lambda r, w: None, HOST, PORT)
    print(f"[Instance] Listening on {HOST}:{PORT}")
    return server


async def start_server_fixed():
    """Handles port conflict gracefully."""
    try:
        server = await asyncio.start_server(lambda r, w: None, HOST, PORT)
        print(f"[Instance] Listening on {HOST}:{PORT}")
        return server
    except OSError as e:
        print(f"[Instance] FATAL: Cannot bind {HOST}:{PORT} — "
              f"another instance may be running. ({e})")
        print(f"[Instance] Exiting cleanly to avoid corrupt state.")
        sys.exit(1)


async def main():
    print("=" * 60)
    print("Simulating two instances starting within 3 seconds")
    print()

    # First instance — grabs the port
    print("[Instance 1] Starting...")
    server1 = await start_server_fixed()

    # Second instance — port already taken
    print("[Instance 2] Starting...")
    try:
        server2 = await start_server_broken()
    except OSError as e:
        print(f"[Instance 2] OSError not caught inside coroutine: {e}")
        print("[Instance 2] This exception propagates to asyncio.run() and")
        print("             appears as an unhandled exception — hard to diagnose.")

    print()
    print("With the fix, instance 2 logs a clear FATAL message and exits.")

    server1.close()
    await server1.wait_closed()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
