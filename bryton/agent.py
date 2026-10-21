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

from bryton.ble import UploadError, download_rides, list_rides, read_profile, upload_many, upload_pace, upload_route
from bryton.service import RIDES_DIR, STATE_DIR, ride_filename

AGENT_API = 5  # 1: workouts and calendar, 2: + routes, 3: + reading rides (/rides), 4: + Pace Pilot plans, 5: + reading the rider profile (/profile). The MCP server refuses an older running agent.
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


def run_upload(workouts, name, uploader=upload_many, target="plan", route_payload=None, route_uploader=upload_route,
               pace=None, pace_uploader=upload_pace):
    """workouts: [(fit, js)], all sent in one session; or route_payload (gzip bytes) for a route; or pace =
    (route gzip bytes, report JSON bytes) for a route with a Pace Pilot plan."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{name}.log"
    lines = []

    def log(line):
        lines.append(line)
        with open(path, "a") as f:
            f.write(line + "\n")
    try:
        if pace is not None:
            asyncio.run(pace_uploader(pace[0], pace[1], name, log=log))
            return {"ok": True, "message": f"route {name} with its Pace Pilot plan uploaded, device confirmed both files",
                    "log": lines[-60:]}
        if route_payload is not None:
            asyncio.run(route_uploader(route_payload, name, log=log))
            return {"ok": True, "message": f"route {name} uploaded, device confirmed the file and the name", "log": lines[-60:]}
        asyncio.run(uploader(workouts, log=log, target=target))
        where = "calendar" if target == "calendar" else "workout list"
        return {"ok": True, "message": f"{name} uploaded to the {where}, device confirmed {len(workouts)} workout(s)", "log": lines[-60:]}
    except UploadError as e:
        return {"ok": False, "message": str(e), "log": lines[-60:]}
    except Exception as e:  # bleak/CoreBluetooth errors
        return {"ok": False, "message": f"{type(e).__name__}: {e}", "log": lines[-60:]}


def run_rides(action, ids, lister=list_rides, downloader=download_rides):
    """Read-only: list the rides waiting on the device, or download them into RIDES_DIR."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_rides.log"
    lines = []

    def log(line):
        lines.append(line)
        with open(path, "a") as f:
            f.write(line + "\n")
    try:
        if action == "list":
            return {"ok": True, "rides": asyncio.run(lister(log=log))}
        got = asyncio.run(downloader(ids or None, log=log))
        RIDES_DIR.mkdir(parents=True, exist_ok=True)
        rides = []
        for entry, fit in got:
            target = RIDES_DIR / ride_filename(entry["id"])
            target.write_bytes(fit)
            rides.append({**entry, "path": str(target), "bytes": len(fit)})
        return {"ok": True, "rides": rides}
    except UploadError as e:
        return {"ok": False, "message": str(e), "log": lines[-40:]}
    except Exception as e:  # bleak/CoreBluetooth errors
        return {"ok": False, "message": f"{type(e).__name__}: {e}", "log": lines[-40:]}


def run_profile(reader=read_profile):
    """Read-only: FTP, max HR, LTHR, height and weight stored on the device."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_profile.log"
    lines = []

    def log(line):
        lines.append(line)
        with open(path, "a") as f:
            f.write(line + "\n")
    try:
        return {"ok": True, "profile": asyncio.run(reader(log=log))}
    except UploadError as e:
        return {"ok": False, "message": str(e), "log": lines[-40:]}
    except Exception as e:  # bleak/CoreBluetooth errors
        return {"ok": False, "message": f"{type(e).__name__}: {e}", "log": lines[-40:]}


def make_handler(token, uploader=upload_many, route_uploader=upload_route, ride_lister=list_rides,
                 ride_downloader=download_rides, pace_uploader=upload_pace, profile_reader=read_profile):
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
                self._send(200, {"ok": True, "busy": _lock.locked(), "api": AGENT_API})
            else:
                self._send(404, {"ok": False, "message": "not found"})

        def do_POST(self):
            if self.path not in ("/upload", "/rides", "/profile"):
                return self._send(404, {"ok": False, "message": "not found"})
            if not secrets.compare_digest(self.headers.get("X-Bryton-Token", ""), token):
                return self._send(403, {"ok": False, "message": "bad token"})
            if self.path == "/profile":
                if not _lock.acquire(blocking=False):
                    return self._send(409, {"ok": False, "message": "another transfer is running"})
                try:
                    return self._send(200, run_profile(profile_reader))
                finally:
                    _lock.release()
            if self.path == "/rides":
                try:
                    req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    action = req["action"]
                    ids = [int(i) for i in req.get("ids") or []]
                    if action not in ("list", "download"):
                        raise ValueError(f"unknown action {action!r}")
                except (ValueError, KeyError) as e:
                    return self._send(400, {"ok": False, "message": f"bad request: {e}"})
                if not _lock.acquire(blocking=False):
                    return self._send(409, {"ok": False, "message": "another transfer is running"})
                try:
                    return self._send(200, run_rides(action, ids, ride_lister, ride_downloader))
                finally:
                    _lock.release()
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                name = str(req["name"])
                route_payload, pace = None, None
                if req.get("kind") == "pace":
                    pace = (base64.b64decode(req["payload"]), base64.b64decode(req["report"]))
                    workouts = []
                    if len(pace[0]) < 18 or pace[0][:2] != b"\x1f\x8b" or not pace[1].lstrip().startswith(b"{"):
                        raise ValueError("pace payload must be gzip route data plus a JSON report")
                elif req.get("kind") == "route":
                    route_payload = base64.b64decode(req["payload"])
                    workouts = []
                    if len(route_payload) < 18 or route_payload[:2] != b"\x1f\x8b":
                        raise ValueError("route payload must be gzip data")
                else:
                    items = req["workouts"] if "workouts" in req else [req]  # one or several (calendar)
                    workouts = [(base64.b64decode(w["fit"]), base64.b64decode(w["json"])) for w in items]
                    if not workouts:
                        raise ValueError("no workouts")
                target = req.get("target", "plan")
                if target not in ("plan", "calendar"):
                    raise ValueError(f"unknown target {target!r}")
            except (ValueError, KeyError) as e:
                return self._send(400, {"ok": False, "message": f"bad request: {e}"})
            if not _lock.acquire(blocking=False):
                return self._send(409, {"ok": False, "message": "another upload is running"})
            try:
                self._send(200, run_upload(workouts, name, uploader, target, route_payload, route_uploader, pace, pace_uploader))
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
