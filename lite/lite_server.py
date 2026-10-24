# Copyright (c) 2021-2026 Littleton Robotics
# http://github.com/Mechanical-Advantage
#
# Use of this source code is governed by a BSD
# license that can be found in the LICENSE file
# at the root directory of this project.

import http.server
import mimetypes
import shutil
import socketserver
import os
import sys
import json
import urllib
import gzip
import zipfile

import replay
from multipart import parse_options_header, MultipartParser

PORT = 5808
ROOT = os.path.abspath("static")
IS_SYSTEMCORE = hasattr(os, "uname") and os.uname().nodename == "robot"
EXTRA_ASSETS_PATH = "/home/systemcore/ascope_assets" if IS_SYSTEMCORE else os.path.abspath("ascope_assets")
BUNDLED_ASSETS_PATH = os.path.join(ROOT, "bundledAssets")
ALLOWED_LOG_SUFFIXES = [".wpilog", ".rlog", ".log"]
ENABLE_FILESYSTEM_ACCESS = IS_SYSTEMCORE or "--enable-file-access" in sys.argv
WEBROOT = ""


def parse_arg(argv, name):
    """Reads the value of a "--name value" or "--name=value" argument, or None."""
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg[len(name) + 1:]
    return None


# All logs are listed from, served from, and uploaded to this folder. The client's
# "folder" query parameter is ignored, since it is untrusted.
LOG_DIR = os.path.abspath(parse_arg(sys.argv, "--log-dir") or ("/U/logs" if IS_SYSTEMCORE else "logs"))

# Robot code repository used for log replay, which is disabled when unset
ROBOT_REPO = parse_arg(sys.argv, "--robot-repo")
if ROBOT_REPO is not None:
    ROBOT_REPO = os.path.abspath(ROBOT_REPO)


class Handler(http.server.SimpleHTTPRequestHandler):
    def _send_response_with_compression(self, status_code, content_type, source_data):
        """Sends a response, compressing it with gzip if the client supports it."""
        use_gzip = "gzip" in self.headers.get("Accept-Encoding", "")

        if use_gzip:
            data = gzip.compress(source_data)
            content_length = len(data)
        else:
            data = source_data
            content_length = len(source_data)

        self.send_response(status_code)
        self.send_header("Content-type", content_type)
        self.send_header("Content-Length", str(content_length))
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()

        self.wfile.write(data)

    def do_GET(self):
        request = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(request.query)

        # Don't serve directly since all assets are available under "/assets"
        if request.path.startswith(WEBROOT + "/bundledAssets"):
            self.send_error(404, f"File not found: {request.path}")

        # Serve list of asset files
        elif request.path == WEBROOT + "/assets" or request.path == WEBROOT + "/assets/":
            asset_file_list = {}
            for root in [BUNDLED_ASSETS_PATH, EXTRA_ASSETS_PATH]:
                for dirpath, _, filenames in os.walk(root):
                    for filename in filenames:
                        if not filename.startswith("."):
                            path = os.path.join(dirpath, filename)
                            contents = None
                            if filename == "config.json":
                                try:
                                    with open(path, "r") as f:
                                        contents = json.load(f)
                                except:
                                    pass
                            asset_file_list[os.path.relpath(path, root)] = contents
            json_string = json.dumps(asset_file_list, separators=(',', ':'))
            self._send_response_with_compression(200, "application/json", json_string.encode('utf-8'))

        # Serve asset files (bundled or extra)
        elif request.path.startswith(WEBROOT + "/assets"):
            asset_path = urllib.parse.unquote(self.path[len(WEBROOT + "/assets/"):])
            extra_asset_path = os.path.join(EXTRA_ASSETS_PATH, asset_path)
            bundled_asset_path = os.path.join(BUNDLED_ASSETS_PATH, asset_path)
            for path in [extra_asset_path, bundled_asset_path]:
                if os.path.exists(path) and os.path.isfile(path):
                    try:
                        with open(path, 'rb') as f:
                            file_content = f.read()
                            mimetype, _ = mimetypes.guess_type(path)
                            if not mimetype:
                                mimetype = 'application/octet-stream'
                            self._send_response_with_compression(200, mimetype, file_content)
                    except Exception as e:
                        self.send_error(500, f"Error serving file: {e}")
                    return
            self.send_error(404, f"File not found: {request.path}")

        # Serve replay status
        elif request.path == WEBROOT + "/replayStatus":
            body = json.dumps(replay.get_status()).encode("utf-8")
            self._send_response_with_compression(200, "application/json", body)

        # Serve list of logs
        elif request.path == WEBROOT + "/logs" or request.path == WEBROOT + "/logs/":
            files = []
            if ENABLE_FILESYSTEM_ACCESS:
                if not os.path.isdir(LOG_DIR):
                    self.send_error(404, "Log folder does not exist")
                    return
                for filename in [x for x in os.listdir(LOG_DIR) if not x.startswith(".")]:
                    for suffix in ALLOWED_LOG_SUFFIXES:
                        if filename.endswith(suffix):
                            files.append({
                                "name": filename,
                                "size": os.path.getsize(os.path.join(LOG_DIR, filename))
                            })
                            break
            json_string = json.dumps(files, separators=(',', ':'))
            self._send_response_with_compression(200, "application/json", json_string.encode("utf-8"))

        # Serve log file
        elif request.path.startswith(WEBROOT + "/logs"):
            if ENABLE_FILESYSTEM_ACCESS:
                # basename() keeps requests inside LOG_DIR
                filename = os.path.basename(urllib.parse.unquote(request.path[len(WEBROOT + "/logs/"):]))
                if any(filename.endswith(suffix) for suffix in ALLOWED_LOG_SUFFIXES):
                    full_path = os.path.join(LOG_DIR, filename)
                    if os.path.exists(full_path):
                        try:
                            with open(full_path, 'rb') as f:
                                file_content = f.read()
                                self._send_response_with_compression(200, "application/octet-stream", file_content)
                        except Exception as e:
                            self.send_error(500, f"Error serving file: {e}")
                        return
            self.send_error(404, f"File not found: {request.path}")

        # Serve everything else
        else:
            filepath = self.translate_path(self.path.removeprefix(WEBROOT))
            if os.path.isdir(filepath):
                index_path = os.path.join(filepath, "index.html")
                if os.path.exists(index_path):
                    filepath = index_path
                else:
                    self.send_error(404, f"File not found: {self.path}")
                    return

            if os.path.exists(filepath) and os.path.isfile(filepath):
                try:
                    with open(filepath, "rb") as f:
                        file_content = f.read()
                        mimetype, _ = mimetypes.guess_type(filepath)
                        if not mimetype:
                            mimetype = "application/octet-stream"
                        self._send_response_with_compression(200, mimetype, file_content)
                except Exception as e:
                    self.send_error(500, f"Error serving file: {e}")
            else:
                self.send_error(404, f"File not found: {self.path}")

    def _send_status(self, code, message=None):
        """Sends a bare response, using "message" as the HTTP reason phrase."""
        self.send_response(code, message)
        self.end_headers()

    def _upload_logs(self):
        """Saves uploaded log files to LOG_DIR."""
        if not os.path.isdir(LOG_DIR):
            self._send_status(500, "Configured log directory does not exist")
            return

        content_type, options = parse_options_header(self.headers.get("Content-Type", ""))
        if content_type != "multipart/form-data" or "boundary" not in options:
            self._send_status(400, "Expected multipart/form-data")
            return

        parser = MultipartParser(
            self.rfile, options["boundary"], content_length=int(self.headers.get("Content-Length", -1))
        )
        parts = []
        saved = 0
        try:
            for part in parser:
                parts.append(part)
                if not part.filename:
                    continue

                # Discard any directory components supplied by the client so an upload
                # cannot escape LOG_DIR. Backslashes are normalized first, since
                # os.path.basename does not treat them as separators on POSIX.
                filename = os.path.basename(part.filename.replace("\\", "/"))
                if filename.startswith("."):
                    # The file listing skips dotfiles, so these would save but never appear
                    self._send_status(400, "Log name cannot start with a period")
                    return
                if not any(filename.endswith(suffix) for suffix in ALLOWED_LOG_SUFFIXES):
                    self._send_status(400, "Unsupported log file type")
                    return

                part.save_as(os.path.join(LOG_DIR, filename))
                saved += 1
        except Exception as e:
            self._send_status(500, f"Error saving log: {e}")
            return
        finally:
            # Only close what was actually read, so this never re-parses the stream
            for part in parts:
                part.close()

        if saved == 0:
            self._send_status(400, "No files provided")
            return

        # Report the destination so the client can show where the log went
        body = LOG_DIR.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _start_replay(self):
        """Starts replaying a log from LOG_DIR. Expects JSON with "name" and an optional "sha"."""
        if ROBOT_REPO is None:
            self._send_status(400, "Server was started without --robot-repo")
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            filename = os.path.basename(str(body["name"]))
            sha = body.get("sha")
        except Exception:
            self._send_status(400, "Invalid replay request")
            return
        log_path = os.path.join(LOG_DIR, filename)
        if not filename.endswith(".wpilog") or not os.path.isfile(log_path):
            self._send_status(400, "Replay requires a WPILOG file from the log folder")
            return
        if sha is not None and not replay.SHA_PATTERN.match(str(sha)):
            self._send_status(400, "Invalid Git hash")
            return

        error = replay.start(ROBOT_REPO, log_path, sha)
        self._send_status(409 if error else 202, error)

    def do_POST(self):
        request = urllib.parse.urlparse(self.path)
        if request.path.startswith(WEBROOT + "/uploadLog") and ENABLE_FILESYSTEM_ACCESS:
            self._upload_logs()
        elif request.path == WEBROOT + "/replay" and ENABLE_FILESYSTEM_ACCESS:
            self._start_replay()
        elif request.path.startswith(WEBROOT + "/uploadAsset") & ENABLE_FILESYSTEM_ACCESS:
            content_type, options = parse_options_header(self.headers.get("Content-Type", ""))

            if content_type == "multipart/form-data" and 'boundary' in options:
                stream = self.rfile
                boundary = options["boundary"]
                parser = MultipartParser(stream, boundary, content_length=int(self.headers.get("Content-Length", -1)))

                for part in parser:
                    if part.filename:
                        if part.filename.lower().endswith(".zip"):
                            asset_zip = f"{EXTRA_ASSETS_PATH}/{part.filename}"
                            part.save_as(asset_zip)
                            temp_path = EXTRA_ASSETS_PATH + "/AS_TEMP/"
                            asset_path = temp_path + part.filename[:-len(".zip")]  # remove .zip ending

                            with zipfile.ZipFile(asset_zip, "r") as zip_ref:
                                zip_ref.extractall(asset_path)
                                os.remove(asset_zip)

                            # recursively extract .zips
                            for dirpath, _, filenames in os.walk(asset_path):
                                for filename in filenames:
                                    if filename.lower().endswith(".zip"):
                                        with zipfile.ZipFile(f"{dirpath}/{filename}", "r") as zip_ref:
                                            filename_no_ext = filename[:-4]  # remove .zip (4 char)
                                            zip_ref.extractall(f"{dirpath}/{filename_no_ext}")
                                            os.remove(f"{dirpath}/{filename}")
                            # find config folders
                            for dirpath, _, filenames in os.walk(asset_path):
                                for filename in filenames:
                                    if filename == "config.json":
                                        # recursively copy the parent directory to extra assets, overwriting existing files
                                        dirname = os.path.basename(dirpath)
                                        shutil.copytree(dirpath, f"{EXTRA_ASSETS_PATH}/{dirname}", dirs_exist_ok=True)
                            shutil.rmtree(temp_path)

                            self.send_response(200)
                            self.end_headers()
                        else:
                            self.send_response(400, "Uploaded asset must be a zip")
                            self.end_headers()

                # Free up resources after use
                for part in parser.parts():
                    part.close()
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    # Create extra assets folder
    if not os.path.exists(EXTRA_ASSETS_PATH):
        os.mkdir(EXTRA_ASSETS_PATH)
        print(f"Created folder for extra assets: {EXTRA_ASSETS_PATH}")

    # Warn if filesystem access disabled
    if not ENABLE_FILESYSTEM_ACCESS:
        print("Log downloads and custom asset uploads are currently disabled. Pass \"--enable-file-access\" to override.\nWARNING: When enabled, AdvantageScope Lite provides read and upload access to the log folder and upload access to the ascope_assets folder.\n")

    # Create log folder
    if ENABLE_FILESYSTEM_ACCESS:
        os.makedirs(LOG_DIR, exist_ok=True)
        print(f"Logs are stored in: {LOG_DIR} (override with \"--log-dir <path>\")\n")
        if ROBOT_REPO is None:
            print("Log replay is disabled. Pass \"--robot-repo <path>\" to enable it.\n")
        else:
            print(f"Log replay uses robot code from: {ROBOT_REPO}\n")

    # Start server
    os.chdir(ROOT)
    httpd = socketserver.ThreadingTCPServer(("", PORT), Handler, bind_and_activate=False)
    httpd.allow_reuse_address = True
    httpd.daemon_threads = True
    print(f"Serving AdvantageScope Lite on port {PORT}: http://localhost:{PORT}{WEBROOT}")
    try:
        httpd.server_bind()
        httpd.server_activate()
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
        httpd.server_close()
        print("\nServer stopped")
