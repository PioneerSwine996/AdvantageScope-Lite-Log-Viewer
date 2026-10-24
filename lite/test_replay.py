# Copyright (c) 2021-2026 Littleton Robotics
# http://github.com/Mechanical-Advantage
#
# Use of this source code is governed by a BSD
# license that can be found in the LICENSE file
# at the root directory of this project.

"""
Checks for the /replay endpoint, using a throwaway robot repository whose
"gradlew" imitates AdvantageKit replay. Run from this directory with the same
dependencies as the server:

    python3 test_replay.py
"""

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PORT = 5810  # Not 5808, so a running server doesn't collide
HERE = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = tempfile.mkdtemp(prefix="ascope_replay_")
LOG_DIR = os.path.join(TEMP_DIR, "logs")
REPO_DIR = os.path.join(TEMP_DIR, "robot")

failures = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got {actual!r}, want {expected!r}")
        failures.append(label)


def write_wpilog(path, metadata):
    """Writes a minimal WPILOG with one string entry per metadata key."""

    def record(entry, payload):
        return bytes([0x7F]) + struct.pack("<IIQ", entry, len(payload), 0) + payload

    def string(value):
        return struct.pack("<I", len(value)) + value.encode()

    data = b"WPILOG" + struct.pack("<HI", 0x0100, 0)
    for i, (key, value) in enumerate(metadata.items(), start=1):
        data += record(0, b"\x00" + struct.pack("<I", i) + string(key) + string("string") + string(""))
        data += record(i, value.encode())
    with open(path, "wb") as f:
        f.write(data)


def git(*args):
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def request(path, body=None):
    """Returns (status, parsed JSON or reason)."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as response:
            text = response.read().decode()
            return response.status, json.loads(text) if text else None
    except urllib.error.HTTPError as error:
        return error.code, error.reason


def wait_for_replay():
    for _ in range(100):
        status = request("/replayStatus")[1]
        if status["state"] != "running":
            return status
        time.sleep(0.1)
    return status


# Fake robot repository: "gradlew runReplay" copies the log to "<name>_sim.wpilog"
os.makedirs(LOG_DIR)
os.makedirs(REPO_DIR)
with open(os.path.join(REPO_DIR, "gradlew"), "w") as f:
    f.write(
        '#!/bin/sh\n[ "$1" = runReplay ] && [ "$REPLAYENABLED" = 1 ] || exit 1\n'
        'echo "> Task :simulateJava"\ncp "$AKIT_LOG_PATH" "${AKIT_LOG_PATH%.wpilog}_sim.wpilog"\n'
    )
os.chmod(os.path.join(REPO_DIR, "gradlew"), 0o755)
LAYOUT = {"hubs": [{"state": {"tabs": []}}], "satellites": [], "version": "26.0.0"}
with open(os.path.join(REPO_DIR, "AdvantageScope.json"), "w") as f:
    json.dump(LAYOUT, f)
git("init", "--quiet")
git("add", ".")
git("commit", "--quiet", "-m", "Initial")
SHA = git("rev-parse", "HEAD")

write_wpilog(os.path.join(LOG_DIR, "match.wpilog"), {"/RealMetadata/* ProjectName": "robot", "/RealMetadata/* GitSHA": SHA})
write_wpilog(os.path.join(LOG_DIR, "nosha.wpilog"), {"/RealMetadata/* ProjectName": "robot"})

# Run a copy of the server on the test port, with replay.py importable and its
# worktree created inside the temp folder
server_path = os.path.join(TEMP_DIR, "lite_server_test.py")
with open(os.path.join(HERE, "lite_server.py")) as source:
    with open(server_path, "w") as dest:
        dest.write(source.read().replace("PORT = 5808", f"PORT = {PORT}"))
os.symlink(os.path.join(HERE, "static"), os.path.join(TEMP_DIR, "static"))
server = subprocess.Popen(
    [sys.executable, server_path, "--enable-file-access", "--log-dir", LOG_DIR, "--robot-repo", REPO_DIR],
    cwd=TEMP_DIR,
    env=dict(os.environ, PYTHONPATH=HERE),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.STDOUT,
)
time.sleep(1.5)

try:
    check("layout served from repo", request("/robotLayout"), (200, LAYOUT))

    check("idle before replay", request("/replayStatus")[1]["state"], "idle")

    check("automatic hash accepted", request("/replay", {"name": "match.wpilog", "sha": None})[0], 202)
    status = wait_for_replay()
    check("automatic hash replay finished", status, {"state": "done", "message": "Replay finished: match_sim.wpilog"})
    check("output written to log folder", os.path.isfile(os.path.join(LOG_DIR, "match_sim.wpilog")), True)
    check("worktree left repo untouched", git("status", "--porcelain"), "")

    check("manual hash accepted", request("/replay", {"name": "nosha.wpilog", "sha": SHA[:10]})[0], 202)
    check("manual hash replay finished", wait_for_replay()["state"], "done")

    request("/replay", {"name": "nosha.wpilog", "sha": None})
    check("missing metadata reported", wait_for_replay()["message"].startswith("No GitSHA"), True)

    request("/replay", {"name": "match.wpilog", "sha": "deadbeef"})
    check("unknown commit reported", wait_for_replay()["message"], "Commit deadbee not found in robot repository")

    check("malformed hash rejected", request("/replay", {"name": "match.wpilog", "sha": "--force"})[0], 400)
    check("non-wpilog rejected", request("/replay", {"name": "match.rlog", "sha": None})[0], 400)
    check("traversal rejected", request("/replay", {"name": "../robot/gradlew", "sha": None})[0], 400)
finally:
    server.terminate()
    server.wait(timeout=5)
    shutil.rmtree(TEMP_DIR, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} check(s) failed: {failures}")
    sys.exit(1)
print("All checks passed.")
