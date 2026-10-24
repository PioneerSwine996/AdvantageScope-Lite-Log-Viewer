# Copyright (c) 2021-2026 Littleton Robotics
# http://github.com/Mechanical-Advantage
#
# Use of this source code is governed by a BSD
# license that can be found in the LICENSE file
# at the root directory of this project.

"""
Checks for the /uploadLog endpoint, covering filename sanitization in particular.
Run from this directory with the same dependencies as the server:

    python3 test_upload.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

PORT = 5809  # Not 5808, so a running server doesn't collide
LOG_DIR = tempfile.mkdtemp(prefix="ascope_logs_")
OUTSIDE_DIR = tempfile.mkdtemp(prefix="ascope_outside_")
SERVER_DIR = tempfile.mkdtemp(prefix="ascope_server_")

failures = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got {actual!r}, want {expected!r}")
        failures.append(label)


def post(filename, body=b"payload", field="file"):
    """Posts a single multipart file, returning (status, response body or reason)."""
    boundary = uuid.uuid4().hex
    data = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
        + body
        + f"\r\n--{boundary}--\r\n".encode()
    )
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/uploadLog",
        data=data,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.reason


def get(path):
    """Returns the response body, or None on an HTTP error."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}") as response:
            return response.read().decode()
    except urllib.error.HTTPError:
        return None


# Run a copy of the server on the test port. Its working directory stays here so
# that "static" and the other relative paths still resolve.
server_path = os.path.join(SERVER_DIR, "lite_server_test.py")
with open("lite_server.py") as source:
    with open(server_path, "w") as dest:
        dest.write(source.read().replace("PORT = 5808", f"PORT = {PORT}"))
server = subprocess.Popen(
    [sys.executable, server_path, "--enable-file-access", "--log-dir", LOG_DIR],
    cwd=os.path.dirname(os.path.abspath(__file__)),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.STDOUT,
)
time.sleep(1.5)

try:
    status, body = post("match_01.wpilog", b"x" * 5000)
    check("valid log accepted", status, 200)
    check("destination reported", body, LOG_DIR)
    check("file written intact", os.path.getsize(os.path.join(LOG_DIR, "match_01.wpilog")), 5000)

    # Directory components must be stripped rather than honored
    escape = "../../../../" + os.path.basename(OUTSIDE_DIR) + "/pwned.wpilog"
    check("traversal accepted", post(escape)[0], 200)
    check("traversal did not escape", os.path.exists(os.path.join(OUTSIDE_DIR, "pwned.wpilog")), False)
    check("traversal landed in log dir", os.path.isfile(os.path.join(LOG_DIR, "pwned.wpilog")), True)

    post("..\\..\\win.wpilog")
    check("backslash traversal flattened", os.path.isfile(os.path.join(LOG_DIR, "win.wpilog")), True)

    check("wrong extension rejected", post("notes.txt")[0], 400)
    check("dotfile rejected", post(".hidden.wpilog")[0], 400)
    check("list shows upload", "match_01.wpilog" in get("/logs"), True)
    check("file served", len(get("/logs/match_01.wpilog?folder=/etc")), 5000)
    check("served path cannot escape", get("/logs/..%2F..%2Fetc%2Fpasswd.log"), None)
    check("missing file rejected", post("", field="nofile")[0], 400)

    check(
        "only accepted files present",
        sorted(os.listdir(LOG_DIR)),
        ["match_01.wpilog", "pwned.wpilog", "win.wpilog"],
    )
finally:
    server.terminate()
    server.wait(timeout=5)
    for path in (LOG_DIR, OUTSIDE_DIR, SERVER_DIR):
        shutil.rmtree(path, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} check(s) failed: {failures}")
    sys.exit(1)
print("All checks passed.")
