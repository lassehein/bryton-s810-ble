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

from bryton.service import (STATE_DIR, ServiceError, build, build_pace, build_route, calendar_entries, calendar_save,
                            calendar_text, calendar_with, calendar_without, describe_ride, known_names, known_routes,
                            load_config, pace_text, preview_text, profile_text, save_device_profile, remember_name, remember_route, resolve_date,
                            route_refusal, route_text, upload_refusal)

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
Routes: preview_route / upload_route take a GPX file (gpx_path); they appear on the device under Courses > Route and
the route list cannot be read, so ask whether the name is new (name_confirmed_new) before uploading.
Pace Pilot: preview_pace_plan / upload_pace_plan calculate a pacing plan (target watts per segment, nutrition) for a GPX route and
send it as route + plan. read_device_profile fetches FTP, max HR, LTHR and weight from the device first. Ask the user for bike weight, bike weight, effort and bike/surface; the plan is a model without weather.
Rides: list_rides / download_rides read finished rides the Bryton app has not fetched yet (read-only, saved as FIT files).
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


AGENT_API_REQUIRED = 5  # bryton.agent.AGENT_API: 2 = routes, 3 = reading rides, 4 = Pace Pilot, 5 = rider profile


class AgentOutdated(OSError):
    pass


def _check_agent_version(timeout=5):
    api = _raw_agent("/health", timeout=timeout).get("api", 1)
    if api < AGENT_API_REQUIRED:
        raise AgentOutdated("an old Bluetooth agent is still running on this computer")


def _agent(path, payload=None, timeout=150):
    """Call the agent; if it is not running, start it once and retry. Refuses an outdated running agent."""
    try:
        if payload is not None:
            _check_agent_version()
        return _raw_agent(path, payload, timeout)
    except (urllib.error.HTTPError, AgentOutdated):
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


OUTDATED_HINT = ("An old Bluetooth agent (started before the last update) is still running and cannot do this. "
                 "Stop it (Ctrl+C in its Terminal, or `pkill -f bryton.agent; pkill -f bryton_agent`) and try again; "
                 "the server then starts a fresh one. Nothing was sent to the device.")


def _agent_hint(error):
    return OUTDATED_HINT if isinstance(error, AgentOutdated) else AGENT_HINT


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
    except (urllib.error.URLError, OSError) as e:
        return "Not uploaded. " + _agent_hint(e)
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
    except (urllib.error.URLError, OSError) as e:
        return _agent_hint(e)
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


ROUTE_HELP = ("A route comes from a GPX file: gpx_path = path of a .gpx file on this computer (preferred), or gpx_text "
              "= the GPX document as text. Tracks (trk) and routes (rte) work; very dense tracks are thinned out.")


@server.tool(description="Check a GPX file and show what would be sent as a route (no device access). " + ROUTE_HELP)
def preview_route(name: str, gpx_path: str = "", gpx_text: str = "") -> str:
    try:
        _, info = build_route(name, gpx_path, gpx_text)
    except ServiceError as e:
        return f"Error: {e}"
    return route_text(name, info)


@server.tool(description="Upload a route (GPX) to the S810 over Bluetooth; it appears under Courses > Route. Requires "
                         "the user's go-ahead. name_confirmed_new=true only after the user confirmed that no route of "
                         "that name is on the device. The phone's Bluetooth must be off and the S810 not connected via "
                         "USB. " + ROUTE_HELP)
def upload_route(name: str, gpx_path: str = "", gpx_text: str = "", name_confirmed_new: bool = False,
                 overwrite: bool = False) -> str:
    try:
        payload, info = build_route(name, gpx_path, gpx_text)
    except ServiceError as e:
        return f"Error: {e}"
    reason = route_refusal(name, name_confirmed_new, overwrite)
    if reason:
        return "Not uploaded. " + reason
    try:
        result = _agent("/upload", {"kind": "route", "name": name, "payload": base64.b64encode(payload).decode()},
                        timeout=300)
    except (urllib.error.URLError, OSError) as e:
        return "Not uploaded. " + _agent_hint(e)
    if not result.get("ok"):
        return f"Upload failed: {result.get('message')}. Nothing was changed on the device."
    remember_route(name)
    return f"{result['message']}.\n" + route_text(name, info)


@server.tool(description="Read FTP, max HR, LTHR, height and weight from the S810 (read-only) and remember them: "
                         "workout previews and Pace Pilot plans then use the device values instead of config.json. "
                         "Call it once before planning, and again after the user changed the profile on the device. "
                         "The phone's Bluetooth must be off and the S810 not connected via USB.")
def read_device_profile() -> str:
    try:
        result = _agent("/profile", {}, timeout=60)
    except (urllib.error.URLError, OSError) as e:
        return "Not read. " + _agent_hint(e)
    if not result.get("ok"):
        return f"Could not read the profile: {result.get('message')}."
    return "Saved. " + profile_text(save_device_profile(result["profile"]))


PACE_HELP = ("gpx_path (a .gpx file on this computer) or gpx_text; effort: very_light|light|moderate|hard|maximal (only "
             "moderate is calibrated); rider_kg (default: the weight from read_device_profile, else config.json, else ask) and bike_kg (ask unless in config.json); bike: "
             "road|gravel|mtb; surface: asphalt|gravel|offroad; language: de|en (text of the segment hints); optional "
             "main_field (HeartRate_target|Power_target|Cadence_target|3Sec_Power_target) and screen = exactly 6 of "
             "HeartRate, Power, Cadence, 3Sec_Power, Segment_Normalized_Power, FTP_Zone, Segment_Distance, Grade, "
             "Average_Power, Segment_Average_Power, Anaerobic_Reserve (top left to bottom right). FTP, max HR and LTHR "
             "come from the device profile (read_device_profile) or config.json. Wind and weather: either wind_kmh, "
             "wind_from_deg (where the wind blows FROM, 0 = north, 90 = east) and temperature_c, or ride_date "
             "(YYYY-MM-DD, within 15 days) + start_time (HH:MM) for the Open-Meteo forecast at the route (needs internet); "
             "given values win over the forecast. Wind changes speeds and times (and so nutrition), not the target watts.")


@server.tool(description="Calculate a Pace Pilot plan for a GPX route and show it (no device access). " + PACE_HELP)
def preview_pace_plan(name: str, gpx_path: str = "", gpx_text: str = "", effort: str = "moderate", rider_kg: float = 0,
                      bike_kg: float = 0, bike: str = "road", surface: str = "asphalt", language: str = "de",
                      main_field: str = "", screen: list[str] = [], wind_kmh: float = -1,
                      wind_from_deg: float = -1, temperature_c: float = -99, ride_date: str = "",
                      start_time: str = "10:00") -> str:
    try:
        _, _, info = build_pace(name, gpx_path, gpx_text, effort, rider_kg, bike_kg, bike, surface, language,
                                main_field, screen, wind_kmh, wind_from_deg, temperature_c, ride_date, start_time)
    except ServiceError as e:
        return f"Error: {e}"
    return pace_text(name, info)


@server.tool(description="Upload a route with a calculated Pace Pilot plan to the S810 over Bluetooth (route under "
                         "Courses > Route, plan under Pace Pilot). Requires the user's go-ahead after preview_pace_plan. "
                         "name_confirmed_new=true only after the user confirmed that no route of that name is on the "
                         "device. The phone's Bluetooth must be off and the S810 not connected via USB. " + PACE_HELP)
def upload_pace_plan(name: str, gpx_path: str = "", gpx_text: str = "", effort: str = "moderate", rider_kg: float = 0,
                     bike_kg: float = 0, bike: str = "road", surface: str = "asphalt", language: str = "de",
                     main_field: str = "", screen: list[str] = [], name_confirmed_new: bool = False,
                     overwrite: bool = False, wind_kmh: float = -1, wind_from_deg: float = -1,
                     temperature_c: float = -99, ride_date: str = "", start_time: str = "10:00") -> str:
    try:
        payload, report_json, info = build_pace(name, gpx_path, gpx_text, effort, rider_kg, bike_kg, bike, surface,
                                                language, main_field, screen, wind_kmh, wind_from_deg, temperature_c,
                                                ride_date, start_time)
    except ServiceError as e:
        return f"Error: {e}"
    reason = route_refusal(name, name_confirmed_new, overwrite)
    if reason:
        return "Not uploaded. " + reason
    try:
        result = _agent("/upload", {"kind": "pace", "name": name, "payload": base64.b64encode(payload).decode(),
                                    "report": base64.b64encode(report_json).decode()}, timeout=300)
    except (urllib.error.URLError, OSError) as e:
        return "Not uploaded. " + _agent_hint(e)
    if not result.get("ok"):
        return f"Upload failed: {result.get('message')}. Nothing was changed on the device."
    remember_route(name)
    return f"{result['message']}.\n" + pace_text(name, info)


RIDES_HELP = ("Rides recorded on the S810 that the Bryton app has not fetched yet. Read-only: nothing is changed or "
              "deleted on the device, and the app can still sync them afterwards. The phone's Bluetooth must be "
              "off and the S810 not connected via USB.")


@server.tool(description="List the rides waiting on the S810 (start time, moving time, distance). " + RIDES_HELP)
def list_rides() -> str:
    try:
        result = _agent("/rides", {"action": "list"}, timeout=120)
    except (urllib.error.URLError, OSError) as e:
        return "Not read. " + _agent_hint(e)
    if not result.get("ok"):
        return f"Could not read the ride list: {result.get('message')}."
    return "\n".join(describe_ride(r) for r in result["rides"]) or "No rides are waiting on the device."


@server.tool(description="Download the rides waiting on the S810 as FIT files into ~/.bryton_s810/rides (all, or only "
                         "the ride_ids given, ids from list_rides). Returns the file paths. " + RIDES_HELP)
def download_rides(ride_ids: list[int] = []) -> str:
    try:
        result = _agent("/rides", {"action": "download", "ids": ride_ids}, timeout=600)
    except (urllib.error.URLError, OSError) as e:
        return "Not downloaded. " + _agent_hint(e)
    if not result.get("ok"):
        return f"Download failed: {result.get('message')}."
    if not result["rides"]:
        return "No matching rides are waiting on the device."
    return "\n".join(f"{describe_ride(r)} -> {r['path']} ({r['bytes']} bytes)" for r in result["rides"])


@server.tool(description="Route names this server knows to be on the S810 (uploaded through it, or listed in "
                         "config.json as known_routes). Not read from the device.")
def list_known_routes() -> str:
    return "\n".join(known_routes())


@server.tool(description="Workout names known to be on the S810 (seed list plus everything uploaded "
                         "through this server). Not read from the device.")
def list_known_workouts() -> str:
    return "\n".join(known_names())


@server.tool(description="Check whether the Bluetooth agent is running.")
def agent_status() -> str:
    try:
        r = _agent("/health", timeout=5)
        if r.get("api", 1) < AGENT_API_REQUIRED:
            return OUTDATED_HINT
        return "Agent running" + (" (busy)" if r.get("busy") else "")
    except (urllib.error.URLError, OSError) as e:
        return _agent_hint(e)


def main():
    server.run()


if __name__ == "__main__":
    main()
