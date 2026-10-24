# Copyright (c) 2021-2026 Littleton Robotics
# http://github.com/Mechanical-Advantage
#
# Use of this source code is governed by a BSD
# license that can be found in the LICENSE file
# at the root directory of this project.

"""Runs AdvantageKit log replay against robot code checked out at a specific commit."""

import glob
import mmap
import os
import re
import struct
import subprocess
import threading

GRADLE_TASK = "runReplay"
LAYOUT_FILE = "AdvantageScope.json"
SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")
WORKTREE_PATH = os.path.abspath("replay_worktree")

_lock = threading.Lock()
_status = {"state": "idle", "message": ""}


def read_git_sha(log_path):
    """Returns the "GitSHA" metadata value recorded by AdvantageKit in a WPILOG, or None."""
    with open(log_path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        if data[:6] != b"WPILOG":
            return None
        pos = 12 + struct.unpack_from("<I", data, 8)[0]
        sha_entry = None
        while pos < len(data):
            bits = data[pos]
            id_length = (bits & 0x3) + 1
            size_length = ((bits >> 2) & 0x3) + 1
            timestamp_length = ((bits >> 4) & 0x7) + 1
            pos += 1
            entry = int.from_bytes(data[pos : pos + id_length], "little")
            pos += id_length
            size = int.from_bytes(data[pos : pos + size_length], "little")
            pos += size_length + timestamp_length
            payload = data[pos : pos + size]
            pos += size

            if entry == 0:
                # Control record, only start records (type 0) matter here
                if len(payload) > 9 and payload[0] == 0:
                    new_entry, name_length = struct.unpack_from("<II", payload, 1)
                    name = payload[9 : 9 + name_length].decode("utf-8", "replace")
                    if "Metadata/" in name and name.endswith("GitSHA"):
                        sha_entry = new_entry
            elif entry == sha_entry:
                # Metadata is written once at startup, so stop at the first value
                return payload.decode("utf-8", "replace").strip()
    return None


def _find_java_home():
    """Returns JAVA_HOME, or the newest WPILib JDK since GradleRIO fails on newer system JDKs."""
    if os.environ.get("JAVA_HOME"):
        return os.environ["JAVA_HOME"]
    jdks = sorted(glob.glob(os.path.join(os.path.expanduser("~"), "wpilib", "*", "jdk")))
    return jdks[-1] if jdks else None


def get_status():
    with _lock:
        return dict(_status)


def _set_status(state, message):
    with _lock:
        _status["state"] = state
        _status["message"] = message


def start(repo, log_path, sha):
    """Starts a replay in the background. Returns an error message, or None if it started."""
    with _lock:
        if _status["state"] == "running":
            return "A replay is already running"
        _status["state"] = "running"
        _status["message"] = "Starting replay"
    threading.Thread(target=_run, args=(repo, log_path, sha), daemon=True).start()
    return None


def _git(*args, cwd, timeout=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)


def read_latest_layout(repo):
    """Returns the AdvantageScope layout from the remote's default branch, or None if not found."""
    try:
        _git("fetch", "--quiet", cwd=repo, timeout=15)
    except subprocess.TimeoutExpired:
        pass  # Offline, fall back to what's already fetched

    # Prefer the remote's default branch, then the local checkout
    for ref in ("origin/HEAD", "HEAD"):
        result = _git("show", f"{ref}:{LAYOUT_FILE}", cwd=repo)
        if result.returncode == 0:
            return result.stdout
    return None


def _run(repo, log_path, sha):
    try:
        if sha is None:
            sha = read_git_sha(log_path)
            if sha is None:
                _set_status("error", "No GitSHA found in log metadata, enter a hash manually")
                return
        if not SHA_PATTERN.match(sha):
            _set_status("error", f"Invalid Git hash: {sha}")
            return
        short_sha = sha[:7]

        # Fetch only when the commit isn't already available locally
        if _git("cat-file", "-e", sha + "^{commit}", cwd=repo).returncode != 0:
            _set_status("running", f"Fetching {short_sha}")
            _git("fetch", "--all", "--quiet", cwd=repo)
            if _git("cat-file", "-e", sha + "^{commit}", cwd=repo).returncode != 0:
                _set_status("error", f"Commit {short_sha} not found in robot repository")
                return

        # Check out into a separate worktree so the robot repository itself is untouched.
        # The worktree is reused between replays so Gradle builds stay incremental.
        _set_status("running", f"Checking out {short_sha}")
        if os.path.isdir(WORKTREE_PATH):
            result = _git("checkout", "--force", "--detach", sha, cwd=WORKTREE_PATH)
        else:
            _git("worktree", "prune", cwd=repo)
            result = _git("worktree", "add", "--force", "--detach", WORKTREE_PATH, sha, cwd=repo)
        if result.returncode != 0:
            _set_status("error", f"Checkout failed: {result.stderr.strip()}")
            return

        # Run replay, reporting the latest Gradle output line as progress
        gradlew = "gradlew.bat" if os.name == "nt" else "./gradlew"
        env = dict(os.environ, REPLAYENABLED="1", AKIT_LOG_PATH=log_path)
        java_home = _find_java_home()
        if java_home is not None:
            env["JAVA_HOME"] = java_home
        process = subprocess.Popen(
            [gradlew, GRADLE_TASK, "--console=plain"],
            cwd=WORKTREE_PATH,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # Keep the compiler errors or Gradle's "What went wrong" section for the failure message
        errors = []
        capture_lines = 0
        for line in process.stdout:
            line = line.strip()
            if len(line) == 0:
                continue
            if capture_lines > 0:
                errors.append(line)
                capture_lines -= 1
            if "What went wrong" in line:
                capture_lines = 3
            elif "error:" in line:
                errors.append(line)
            _set_status("running", f"Replaying ({short_sha}): {line}")
        if process.wait() != 0:
            _set_status("error", f"Replay failed ({short_sha}): {' | '.join(errors[:4]) or 'see server output'}")
            return

        stem, extension = os.path.splitext(os.path.basename(log_path))
        _set_status("done", f"Replay finished: {stem}_sim{extension}")
    except Exception as e:
        _set_status("error", f"Replay failed: {e}")
