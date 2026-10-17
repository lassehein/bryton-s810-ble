"""MCP server (Claude Desktop / Claude Code): turn a training description into a Bryton workout.

The client starts this module over stdio (`bryton-mcp`). Bluetooth runs in a separate local agent
(`bryton-agent`, bryton/agent.py). On macOS, processes started by Claude Desktop have no Bluetooth
permission, so start the agent yourself in a Terminal. The server tries to start it if it is missing.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from bryton.service import (STATE_DIR, ServiceError, build, calendar_entries, calendar_save,
                            calendar_text, calendar_with, calendar_without, known_names, load_config,
                            preview_text, remember_name, resolve_date, upload_refusal)

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

AGENT_URL = os.environ.get("BRYTON_AGENT_URL", "http://127.0.0.1:8765")
INSTRUCTIONS = """Bryton Rider S810 workouts. Workflow: turn the user's plan into steps, call
preview_workout, show the preview, then ask before upload_workout. Power targets are % FTP. Cadence
can be added to any step; a step with only cadence becomes a cadence-only step. Use repeat blocks for
intervals. The device replaces a workout with the same name silently and its list cannot be read, so
always ask the user whether the name is new (name_confirmed_new) or may be replaced (overwrite).
To put a workout into the device calendar for a day, pass date (YYYY-MM-DD, "today", "tomorrow"
or "+N"); one call per training day. Every calendar upload replaces the WHOLE device calendar with
this server's calendar (all days from today on, see list_calendar): workouts planned in the Bryton
app disappear. Tell the user once and pass calendar_replace_ok=true after they agree. Without date
the workout goes into the workout list.
Never upload without the user's explicit go-ahead."""

STEPS_HELP = """steps: list of {"kind": "warmup|active|rest|cooldown", duration: one of "minutes", "seconds",
"km", "meters"; target: exactly one of "power" (% FTP), "mhr" (% max HR), "lthr" (% LTHR), "speed" (km/h),
each a number (fixed target) or [low, high] (range); optional "cadence" (rpm, number or [low, high]) on top;
a step with only cadence is a cadence-only step; "name": up to 9 characters (optional)}
or {"repeat": n, "steps": [...]}. Absolute watts or bpm are not supported."""

server = _Server("bryton-s810", instructions=INSTRUCTIONS)


def _ftp():
    return load_config().get("ftp")


def _zones():
    cfg = load_config()
    return {k: cfg[k] for k in ("mhr", "lthr") if cfg.get(k)}


@server.tool(description="Build a workout and show it as text (no device access). " + STEPS_HELP)
def preview_workout(name: str, steps: list, description: str = "", date: str = "") -> str:
    try:
        day = resolve_date(date)
        _, _, final, warnings = build(name, steps, description, day)
    except ServiceError as e:
        return f"Error: {e}"
    return preview_text(name, final, description, _ftp(), warnings, day, _zones())


def _start_agent():
    """Start the Bluetooth agent as a detached child (it outlives this server)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(STATE_DIR / "agent.log", "ab")
    subprocess.Popen([sys.executable, "-m", "bryton.agent"], stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, start_new_session=True)
    for _ in range(40):
        time.sleep(0.25)
        try:
            return _raw_agent("/health", timeout=2).get("ok", False)
        except (urllib.error.URLError, OSError):
            pass
    return False


def _agent(path, payload=None, timeout=150):
    """Call the agent; if it is not running, start it once and retry."""
    try:
        return _raw_agent(path, payload, timeout)
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, OSError):
        if os.environ.get("BRYTON_NO_AUTOSTART") or not _start_agent():
            raise
        return _raw_agent(path, payload, timeout)


def _raw_agent(path, payload=None, timeout=150):
    url = AGENT_URL + path
    headers = {}
    data = None
    if payload is not None:
        token = (STATE_DIR / "agent_token").read_text().strip()
        headers = {"Content-Type": "application/json", "X-Bryton-Token": token}
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


AGENT_HINT = ("The Bluetooth agent is not running and could not be started automatically (see "
              "~/.bryton_s810/agent.log; macOS may deny Bluetooth to processes started by Claude Desktop). "
              "Ask the user to start it in the macOS Terminal: cd <repo> && PYTHONPATH=. python3 "
              "tools/bryton_agent.py")


@server.tool(description="Upload a workout to the S810 over Bluetooth. Requires the user's go-ahead. "
                         "name_confirmed_new=true only after the user confirmed the name is not on the "
                         "device; overwrite=true only if the user agreed to replace it. The phone's "
                         "Bluetooth must be off and the S810 not connected via USB. " + STEPS_HELP)
def upload_workout(name: str, steps: list, description: str = "", date: str = "",
                   name_confirmed_new: bool = False, overwrite: bool = False,
                   calendar_replace_ok: bool = False) -> str:
    try:
        day = resolve_date(date)
        fit, js, final, warnings = build(name, steps, description, day)
    except ServiceError as e:
        return f"Error: {e}"
    if day:
        return _upload_calendar(name, day, fit, js, final, description, warnings, overwrite,
                                calendar_replace_ok)
    reason = upload_refusal(name, name_confirmed_new, overwrite)
    if reason:
        return "Not uploaded. " + reason
    try:
        result = _agent("/upload", {"name": name, "fit": base64.b64encode(fit).decode(),
                                    "json": base64.b64encode(js).decode(),
                                    "target": "plan"})
    except (urllib.error.URLError, OSError):
        return "Not uploaded. " + AGENT_HINT
    if not result.get("ok"):
        return f"Upload failed: {result.get('message')}. Nothing was changed on the device."
    remember_name(name)
    return f"{result['message']}.\n" + preview_text(name, final, description, _ftp(), warnings, zones=_zones())


def _send_calendar(entries):
    """Send the whole calendar in one session. Returns None on success or an error text."""
    if not entries:
        return "the calendar would be empty; clearing it over Bluetooth is not supported yet"
    try:
        result = _agent("/upload", {"name": f"calendar ({len(entries)} entries)", "target": "calendar",
                                    "workouts": [{"fit": base64.b64encode(f).decode(),
                                                  "json": base64.b64encode(j).decode()}
                                                 for _d, _n, f, j in entries]}, timeout=300)
    except (urllib.error.URLError, OSError):
        return AGENT_HINT
    if not result.get("ok"):
        return f"upload failed: {result.get('message')}"
    calendar_save(entries)
    return None


def _upload_calendar(name, day, fit, js, final, description, warnings, overwrite, replace_ok):
    current = calendar_entries()
    if any(d == day and n.lower() == name.lower() for d, n, _, _ in current) and not overwrite:
        return (f"Not uploaded. {name!r} is already planned on {day}. Ask the user whether to replace "
                "it and call again with overwrite=true, or choose another name.")
    if not replace_ok:
        return ("Not uploaded. A calendar upload replaces the whole device calendar with this server's "
                "calendar; workouts planned in the Bryton app disappear. Ask the user, then call again "
                "with calendar_replace_ok=true.\n" + calendar_text(current))
    entries = calendar_with(current, day, name, fit, js)
    error = _send_calendar(entries)
    if error:
        return f"Not uploaded: {error}. The device calendar is unchanged or as before."
    return (f"Planned {name!r} on {day}; device confirmed {len(entries)} calendar entries.\n"
            + preview_text(name, final, description, _ftp(), warnings, day, _zones()) + "\n" + calendar_text(entries))


@server.tool(description="Show the calendar this server last sent to the S810 (from today on).")
def list_calendar() -> str:
    return calendar_text(calendar_entries())


@server.tool(description="Remove a planned workout from the calendar and resend the remaining calendar "
                         "to the S810. Requires the user's go-ahead (calendar_replace_ok=true).")
def remove_calendar_entry(date: str, name: str, calendar_replace_ok: bool = False) -> str:
    try:
        day = resolve_date(date)
    except ServiceError as e:
        return f"Error: {e}"
    current = calendar_entries()
    entries = calendar_without(current, day, name)
    if len(entries) == len(current):
        return f"{name!r} is not planned on {day}.\n" + calendar_text(current)
    if not calendar_replace_ok:
        return "Not changed. Ask the user, then call again with calendar_replace_ok=true."
    error = _send_calendar(entries)
    if error:
        return f"Not changed: {error}."
    return f"Removed {name!r} on {day}.\n" + calendar_text(entries)


@server.tool(description="Workout names known to be on the S810 (seed list plus everything uploaded "
                         "through this server). Not read from the device.")
def list_known_workouts() -> str:
    return "\n".join(known_names())


@server.tool(description="Check whether the Bluetooth agent is running.")
def agent_status() -> str:
    try:
        r = _agent("/health", timeout=5)
        return "Agent running" + (" (busy)" if r.get("busy") else "")
    except (urllib.error.URLError, OSError):
        return AGENT_HINT


def main():
    server.run()


if __name__ == "__main__":
    main()
