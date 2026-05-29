"""
Bug 3 Reproducer: Mixed tabs/spaces causing TabError/IndentationError.

Python 3 does not allow mixing tabs and spaces in the same block.
Windows editors (Notepad, some IDEs with wrong settings) default to tabs
while the original file uses spaces, creating mixed indentation on edit.

Run:
    python test_bug3_indentation.py

This script creates a temporary .py file with the same mixed indentation
pattern seen at lines 155 and 211, then attempts to compile it.
"""

import py_compile
import tempfile
import os
import subprocess
import sys


# Simulates what the customer's edited script looked like around line 155.
# The original code uses 4-space indentation; the customer's edit introduced
# a tab character before the print statement.
BROKEN_CODE_LINE_155 = '''\
def flush_batch(resp):
    if resp.status_code != 200:
        # Original code (spaces):
        print("Rejection!")
    \t\tprint(f"\\nCRITICAL XDR REJECTION! HTTP {resp.status.code}")
'''

# Simulates what line 211 looked like (IndentationError variant).
BROKEN_CODE_LINE_211 = '''\
async def handle_dls(reader, writer):
    buf = b""
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            break
        buf += chunk
    while b"\\n" in buf:
                       ^
        line, buf = buf.split(b"\\n", 1)
'''

# What the fixed code should look like (all spaces, correct attribute).
FIXED_CODE = '''\
def flush_batch(resp):
    if resp.status_code != 200:
        print(f"CRITICAL XDR REJECTION! HTTP {resp.status_code}")

async def handle_dls(reader, writer):
    buf = b""
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\\n" in buf:
            line, buf = buf.split(b"\\n", 1)
            if line.strip():
                pass  # process record
'''


def test_compile(label, code):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                     delete=False, encoding="utf-8") as f:
        f.write(code)
        fname = f.name
    try:
        py_compile.compile(fname, doraise=True)
        print(f"  {label}: COMPILED OK")
        return True
    except py_compile.PyCompileError as e:
        print(f"  {label}: COMPILE ERROR — {e}")
        return False
    finally:
        os.unlink(fname)


if __name__ == "__main__":
    print("=" * 60)
    print("Test 1: Mixed tabs+spaces (line 155 pattern)")
    # Write actual tab character into the broken snippet
    broken_155 = BROKEN_CODE_LINE_155.replace("\\t", "\t")
    test_compile("Broken (tabs+spaces)", broken_155)

    print()
    print("=" * 60)
    print("Test 2: Fixed version (all spaces)")
    test_compile("Fixed (spaces only)", FIXED_CODE)

    print()
    print("=" * 60)
    print("How to fix an existing file:")
    print()
    print("  Option A — autopep8:")
    print("    pip install autopep8")
    print("    autopep8 --in-place --aggressive duo_xdr_forwarder.py")
    print()
    print("  Option B — expand tabs manually:")
    print("    python -c \"")
    print("    with open('duo_xdr_forwarder.py') as f: src = f.read()")
    print("    with open('duo_xdr_forwarder.py', 'w') as f: f.write(src.expandtabs(4))")
    print("    \"")
    print()
    print("  Always verify after:")
    print("    python -m py_compile duo_xdr_forwarder.py && echo OK")
