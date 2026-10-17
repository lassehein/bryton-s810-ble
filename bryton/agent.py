"""Local BLE agent for the MCP server. Start it in a Terminal that may use Bluetooth (macOS!):

    bryton-agent            # or: python3 -m bryton.agent

Listens on 127.0.0.1 only. Every upload request must carry the token from
~/.bryton_s810/agent_token (written at start, readable only by you). One upload at a time.
"""
import asyncio
import base64
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bryton.ble import UploadError, upload_many
from bryton.service import STATE_DIR

HOST, PORT = "127.0.0.1", int(os.environ.get("BRYTON_AGENT_PORT", "8765"))
TOKEN_FILE = STATE_DIR / "agent_token"
LOG_DIR = STATE_DIR / "logs"
_lock = threading.Lock()


def write_token():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    TOKEN_FILE.write_text(token)
    os.chmod(TOKEN_FILE, 0o600)
    return token


def run_upload(workouts, name, uploader=upload_many, target="plan"):
    """workouts: [(fit, js)], all sent in one session."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{name}.log"
    lines = []

    def log(line):
        lines.append(line)
        with open(path, "a") as f:
            f.write(line + "\n")
    try:
        asyncio.run(uploader(workouts, log=log, target=target))
        where = "calendar" if target == "calendar" else "workout list"
        return {"ok": True, "message": f"{name} uploaded to the {where}, device confirmed {len(workouts)} workout(s)", "log": lines[-60:]}
    except UploadError as e:
        return {"ok": False, "message": str(e), "log": lines[-60:]}
    except Exception as e:  # bleak/CoreBluetooth errors
        return {"ok": False, "message": f"{type(e).__name__}: {e}", "log": lines[-60:]}


def make_handler(token, uploader=upload_many):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # quiet
            pass

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"ok": True, "busy": _lock.locked()})
            else:
                self._send(404, {"ok": False, "message": "not found"})

        def do_POST(self):
            if self.path != "/upload":
                return self._send(404, {"ok": False, "message": "not found"})
            if not secrets.compare_digest(self.headers.get("X-Bryton-Token", ""), token):
                return self._send(403, {"ok": False, "message": "bad token"})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                items = req["workouts"] if "workouts" in req else [req]  # one or several (calendar)
                workouts = [(base64.b64decode(w["fit"]), base64.b64decode(w["json"])) for w in items]
                if not workouts:
                    raise ValueError("no workouts")
                name = str(req["name"])
                target = req.get("target", "plan")
                if target not in ("plan", "calendar"):
                    raise ValueError(f"unknown target {target!r}")
            except (ValueError, KeyError) as e:
                return self._send(400, {"ok": False, "message": f"bad request: {e}"})
            if not _lock.acquire(blocking=False):
                return self._send(409, {"ok": False, "message": "another upload is running"})
            try:
                self._send(200, run_upload(workouts, name, uploader, target))
            finally:
                _lock.release()
    return Handler


def main():
    token = write_token()
    server = ThreadingHTTPServer((HOST, PORT), make_handler(token))
    print(f"Bryton BLE agent on http://{HOST}:{PORT} (token in {TOKEN_FILE}). Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
