"""
Bug 1 Reproducer: Silent asyncio task death on Windows ProactorEventLoop.

Demonstrates how an unhandled exception inside an asyncio Task is silently
swallowed on Windows, leaving the server socket alive but the flush worker dead.

Run on Windows as:
    python test_bug1_silent_task_death.py

Expected output WITHOUT fix:
    [SERVER] Listening on 127.0.0.1:9999
    [SERVER] DLS connected
    [SERVER] DLS disconnected
    [SERVER] Flush worker DIED silently (no log output from worker after this)
    [CLIENT] Sent 1 record
    [CLIENT] Sent 1 record       <-- server still accepts but never flushes

Expected output WITH fix (--fix flag):
    ... all flushes succeed ...
"""

import asyncio
import json
import sys
import time
import threading
import argparse

HOST = "127.0.0.1"
PORT = 19999  # avoid conflict with real service

record_queue = asyncio.Queue()
flush_count = 0
APPLY_FIX = False


# ── Broken flush worker (no exception guard) ──────────────────────────────────

async def flush_worker_broken():
    """Simulates the buggy flush loop that dies silently on first error."""
    global flush_count
    call_count = 0
    while True:
        await asyncio.sleep(0.5)
        batch = []
        while not record_queue.empty():
            batch.append(await record_queue.get())
        if batch:
            call_count += 1
            if call_count == 1:
                # Simulate what happens on first XDR 4xx: resp.status.code raises
                # AttributeError which propagates uncaught out of the task.
                raise AttributeError("'int' object has no attribute 'code'")
            flush_count += len(batch)
            print(f"[FLUSH] Sent {len(batch)} records (total={flush_count})")


# ── Fixed flush worker (exception guard keeps it alive) ───────────────────────

async def flush_worker_fixed():
    """Flush loop that survives exceptions."""
    global flush_count
    while True:
        try:
            await asyncio.sleep(0.5)
            batch = []
            while not record_queue.empty():
                batch.append(await record_queue.get())
            if batch:
                # Simulate resp.status.code bug but catch it
                try:
                    raise AttributeError("'int' object has no attribute 'code'")
                except AttributeError as e:
                    print(f"[FLUSH] AttributeError caught, using status_code instead: {e}")
                flush_count += len(batch)
                print(f"[FLUSH] Sent {len(batch)} records (total={flush_count})")
        except Exception as e:
            print(f"[FLUSH] Error (loop continues): {e}")


# ── DLS connection handler ─────────────────────────────────────────────────────

async def handle_dls(reader, writer):
    peer = writer.get_extra_info("peername")
    print(f"[SERVER] DLS connected: {peer}")
    buf = b""
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    record = json.loads(line)
                    await record_queue.put(record)
    except Exception as e:
        print(f"[SERVER] Handler error: {e}")
    finally:
        print(f"[SERVER] DLS disconnected: {peer}")
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def server_main():
    loop = asyncio.get_event_loop()
    policy_name = type(loop).__name__

    print(f"[SERVER] Event loop: {policy_name}")
    print(f"[SERVER] Listening on {HOST}:{PORT}")
    print(f"[SERVER] Fix applied: {APPLY_FIX}")
    print()

    server = await asyncio.start_server(handle_dls, HOST, PORT)

    if APPLY_FIX:
        worker = asyncio.create_task(flush_worker_fixed())
    else:
        worker = asyncio.create_task(flush_worker_broken())

    # Monitor the task after 2s, then shut down after 10s total
    async def monitor_and_stop():
        await asyncio.sleep(2)
        if worker.done():
            exc = worker.exception() if not worker.cancelled() else None
            print(f"[SERVER] !! Flush worker DIED. Exception: {exc}")
            print(f"[SERVER] !! Server socket still alive but flush is dead.")
        else:
            print(f"[SERVER] Flush worker still running (good).")
        await asyncio.sleep(8)
        server.close()

    asyncio.create_task(monitor_and_stop())

    async with server:
        await server.serve_forever()


def run_client():
    """Send 3 fake DLS log records in separate connections, 1 second apart."""
    import socket, time
    time.sleep(0.5)  # let server start
    for i in range(3):
        time.sleep(1.5)
        try:
            with socket.create_connection((HOST, PORT), timeout=5) as s:
                record = json.dumps({"event": f"auth_{i}", "user": "test@example.com"})
                s.sendall((record + "\n").encode())
                time.sleep(0.2)
            print(f"[CLIENT] Sent record {i+1}")
        except Exception as e:
            print(f"[CLIENT] Error: {e}")
    time.sleep(1)
    print(f"[CLIENT] Done. Total records flushed to XDR: {flush_count}")
    print()
    if not APPLY_FIX and flush_count == 0:
        print("RESULT: BUG REPRODUCED — flush worker died, 0 records forwarded")
    elif APPLY_FIX and flush_count > 0:
        print("RESULT: FIX WORKS — all records forwarded despite AttributeError")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true", help="Apply the fix")
    args = parser.parse_args()
    APPLY_FIX = args.fix

    if sys.platform == "win32" and APPLY_FIX:
        # Fix: use SelectorEventLoop which handles exceptions more predictably
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    client_thread = threading.Thread(target=run_client, daemon=True)
    client_thread.start()

    try:
        asyncio.run(server_main())
    except KeyboardInterrupt:
        pass
