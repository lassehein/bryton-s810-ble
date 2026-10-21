"""High-level workout service used by the MCP server: input normalisation, preview, name safety.

Step input (what Claude sends):
  {"kind": "warmup|active|rest|cooldown", "minutes": 5 | "seconds": 300,
   "power": [low, high]            # % FTP, omit for a cadence-only step
   "cadence": [low, high]          # rpm, optional
   "name": "Work"}                 # optional, shown on the device (max 9 characters)
  {"repeat": 4, "steps": [...]}    # expanded into plain steps
"""
import datetime
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from xml.etree.ElementTree import ParseError as ET_ParseError

from bryton import route as routelib
from bryton.fitgen import MAX_STEPS, PRIMARY_KEYS, SpecError, build_files, resolve_plan

STATE_DIR = Path(os.environ.get("BRYTON_S810_HOME", Path.home() / ".bryton_s810"))
KNOWN_FILE = STATE_DIR / "known_workouts.json"
KNOWN_ROUTES_FILE = STATE_DIR / "known_routes.json"
CONFIG_FILE = STATE_DIR / "config.json"
DEVICE_PROFILE_FILE = STATE_DIR / "device_profile.json"  # last profile read from the device (read_device_profile)
# Names of workouts already on the device can be listed in config.json ("known_workouts"); they are
# never overwritten without an explicit overwrite=True. The device list cannot be read over Bluetooth.
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
STEP_NAME_MAX = 9
DESCRIPTION_MAX = 200


class ServiceError(ValueError):
    pass


def ascii_fold(text):
    text = text.replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    text = text.replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def load_config():
    """config.json, with ftp / mhr / lthr / rider_kg replaced by the values last read from the device."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        cfg = {}
    cfg.update({k: v for k, v in load_device_profile().items() if k in ("ftp", "mhr", "lthr", "rider_kg", "resting_hr") and v})
    return cfg


def load_device_profile():
    try:
        return json.loads(DEVICE_PROFILE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_device_profile(profile):
    """profile as returned by bryton.ble.read_profile; stored with weight_kg also as rider_kg."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {**profile, "rider_kg": profile.get("weight_kg"), "read_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    DEVICE_PROFILE_FILE.write_text(json.dumps(data, indent=1))
    return data


def profile_text(profile):
    def z(key):
        return "/".join(str(v) for v in profile.get(key, [])[1:]) or "?"
    return (f"FTP {profile['ftp']} W, max HR {profile['mhr']} bpm, LTHR {profile['lthr']} bpm, "
            f"height {profile['height_cm']:g} cm, weight {profile['weight_kg']:g} kg"
            + (f", resting HR {profile['resting_hr']} bpm" if profile.get("resting_hr") else "") + " (from the device). "
            f"Zone limits FTP % {z('ftp_zones')}; max HR % {z('mhr_zones')}; LTHR % {z('lthr_zones')}.")


def known_names():
    try:
        stored = json.loads(KNOWN_FILE.read_text())
    except (OSError, ValueError):
        stored = []
    seed = load_config().get("known_workouts") or []
    return sorted(set(seed) | set(stored), key=str.lower)


def remember_name(name):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    names = set(known_names()) | {name}
    KNOWN_FILE.write_text(json.dumps(sorted(names, key=str.lower), indent=1))


def check_workout_name(name):
    if not NAME_RE.match(name or ""):
        raise ServiceError("name: 1-20 characters, only letters, digits, _ and - "
                           "(the device uses it as the file name)")


def _duration(item, where):
    """Returns {"seconds": n} or {"meters": n}."""
    given = [k for k in ("seconds", "minutes", "meters", "km") if k in item]
    if len(given) != 1:
        raise ServiceError(f"{where}: give exactly one of minutes, seconds, km, meters")
    key = given[0]
    value = item[key] * {"minutes": 60, "km": 1000}.get(key, 1)
    if not float(value).is_integer() or value <= 0:
        raise ServiceError(f"{where}: duration must be a whole number of "
                           f"{'meters' if key in ('meters', 'km') else 'seconds'} > 0")
    return {"meters" if key in ("meters", "km") else "seconds": int(value)}


def _pair(value, where, what, decimals=0):
    """[low, high], or a single number = fixed target (low == high)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = [value, value]
    if not (isinstance(value, (list, tuple)) and len(value) == 2):
        raise ServiceError(f"{where}: {what} must be a number or [low, high]")
    lo, hi = (round(v, decimals) if decimals else int(round(v)) for v in value)
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def normalize_steps(items, _depth=0, _path="steps"):
    """Expand repeats and convert Claude's step list into generator steps."""
    if not isinstance(items, list) or not items:
        raise ServiceError(f"{_path}: need a non-empty list")
    out = []
    for i, item in enumerate(items):
        where = f"{_path}[{i}]"
        if "repeat" in item:
            if _depth > 2:
                raise ServiceError(f"{where}: repeats nested too deep")
            count = int(item["repeat"])
            if not 1 <= count <= 30:
                raise ServiceError(f"{where}: repeat must be 1..30")
            inner = normalize_steps(item.get("steps"), _depth + 1, f"{where}.steps")
            out += [dict(s) for _ in range(count) for s in inner]
            continue
        kind = item.get("kind", "active")
        step = {"kind": kind, **_duration(item, where)}
        for key in PRIMARY_KEYS:
            if item.get(key) is not None:
                step[key] = _pair(item[key], where, key, 1 if key == "speed" else 0)
        if len([k for k in PRIMARY_KEYS if k in step]) > 1:
            raise ServiceError(f"{where}: only one of {', '.join(PRIMARY_KEYS)} per step")
        if item.get("cadence") is not None:
            step["cadence"] = _pair(item["cadence"], where, "cadence")
        if not any(k in step for k in PRIMARY_KEYS):
            if "cadence" not in step:
                raise ServiceError(f"{where}: needs one of {', '.join(PRIMARY_KEYS)} and/or cadence")
            step["target"] = "cadence"  # cadence-only step: FIT primary target (no device quirk)
        if item.get("name"):
            step["name"] = ascii_fold(str(item["name"]))[:STEP_NAME_MAX]
        out.append(step)
    return out


def resolve_date(date, today=None):
    """'' -> None (workout list); 'today', 'tomorrow', '+N' or YYYY-MM-DD -> YYYY-MM-DD."""
    if not date:
        return None
    date = {"tomorrow": "+1"}.get(date.strip().lower(), date.strip().lower())
    value = resolve_plan([date], today)[0]
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise ServiceError("date: use YYYY-MM-DD, 'today', 'tomorrow' or '+N'") from None
    return value


def build(name, steps, description="", date=None):
    """Validate and build the files. date (YYYY-MM-DD) = calendar day, None = workout list.
    Returns (fit, js, final_steps, warnings)."""
    check_workout_name(name)
    warnings = []
    description = ascii_fold(description or "")
    if len(description) > DESCRIPTION_MAX:
        description = description[:DESCRIPTION_MAX]
        warnings.append(f"description cut to {DESCRIPTION_MAX} characters")
    plain = normalize_steps(steps)
    if len(plain) + 1 > MAX_STEPS:
        raise ServiceError(f"{len(plain)} steps after expanding repeats; the limit is {MAX_STEPS - 1}")
    try:
        fit, js, final = build_files(name, plain, description=description, plan=[date] if date else None)
    except SpecError as e:
        raise ServiceError(str(e)) from None
    if len(final) > len(plain):
        warnings.append("a 5-second 'Start' step was added in front so that the first step shows its "
                        "cadence range correctly (device quirk)")
    if entry_key(name, date).lower() in {n.lower() for n in known_names()}:
        warnings.append(f"a workout named {name!r} is already known there; uploading replaces it")
    return fit, js, final, warnings


def preview_text(name, final_steps, description="", ftp=None, warnings=(), date=None, zones=None):
    lines = [f"Workout {name!r}" + (f" - {description}" if description else "")]
    lines.append(f"Destination: calendar {date}" if date else "Destination: workout list")
    total = 0
    distance = 0
    for i, s in enumerate(final_steps, 1):
        if "meters" in s:
            distance += s["meters"]
            length = f"{s['meters']:>5} m"
        else:
            total += s["seconds"]
            mins, secs = divmod(s["seconds"], 60)
            length = f"{mins:3d}:{secs:02d}"
        parts = [f"{i:2}. {s.get('name') or s['kind']:<10} {length}"]

        def rng(pair, unit):
            return f"{pair[0]:g}{unit}" if pair[0] == pair[1] else f"{pair[0]:g}-{pair[1]:g}{unit}"
        if s.get("target") == "cadence":
            parts.append(f"cadence {rng(s['cadence'], ' rpm')} (no other target)")
        else:
            if s.get("power"):
                lo, hi = s["power"]
                watts = f" = {rng((round(lo * ftp / 100), round(hi * ftp / 100)), ' W')}" if ftp else ""
                parts.append(f"{rng(s['power'], ' % FTP')}{watts}")
            elif s.get("mhr") or s.get("lthr"):
                key, label = ("mhr", "MHR") if s.get("mhr") else ("lthr", "LTHR")
                base = (zones or {}).get(key)
                bpm = (f" = {rng((int(s[key][0] * base / 100), int(s[key][1] * base / 100)), ' bpm')}"
                       if base else "")
                parts.append(f"{rng(s[key], f' % {label}')}{bpm}")
            elif s.get("speed"):
                parts.append(rng(s["speed"], " km/h"))
            if s.get("cadence"):
                parts.append(f"cadence {rng(s['cadence'], ' rpm')}")
        lines.append("  ".join(parts))
    h, rest = divmod(total, 3600)
    extra = f" + {distance / 1000:g} km (distance steps)" if distance else ""
    lines.append(f"Total {h}:{rest // 60:02d}:{rest % 60:02d}{extra}, {len(final_steps)} steps")
    lines += [f"Note: {w}" for w in warnings]
    return "\n".join(lines)


def entry_key(name, date=None):
    """Known-list key: plain name for the workout list, 'YYYY-MM-DD/name' for a calendar day."""
    return f"{date}/{name}" if date else name


def upload_refusal(name, name_confirmed_new, overwrite, date=None):
    """Return a reason not to upload, or None."""
    key = entry_key(name, date)
    exists = key.lower() in {n.lower() for n in known_names()}
    where = f"in the calendar on {date}" if date else "on the device"
    if exists and not overwrite:
        return (f"A workout named {name!r} is already {where}. Ask the user whether to replace it "
                "and call again with overwrite=true, or choose another name.")
    if not exists and not (name_confirmed_new or overwrite):
        return ("Uploading replaces a workout with the same name without warning, and the device's list "
                "cannot be read over Bluetooth. Ask the user to confirm that no workout named "
                f"{name!r} exists {where}, then call again with name_confirmed_new=true.")
    return None


# --- Calendar -------------------------------------------------------------------------------
# One calendar upload REPLACES the device calendar, so the server keeps its own copy
# (STATE_DIR/calendar/<YYYY-MM-DD>/<name>.fit/.json) and always sends every day from today on.
# Workouts planned in the Bryton app are not known here and disappear with the next upload.

def calendar_dir():
    return STATE_DIR / "calendar"


def calendar_entries(today=None):
    """[(date, name, fit, js)] from today on, sorted by date and name."""
    today = (today or datetime.date.today()).isoformat()
    out = []
    root = calendar_dir()
    if root.is_dir():
        for day in sorted(p for p in root.iterdir() if p.is_dir() and p.name >= today):
            for fit in sorted(day.glob("*.fit")):
                js = fit.with_suffix(".json")
                if js.exists():
                    out.append((day.name, fit.stem, fit.read_bytes(), js.read_bytes()))
    return out


def calendar_with(entries, date, name, fit, js):
    """Entries after adding/replacing (date, name)."""
    rest = [e for e in entries if not (e[0] == date and e[1].lower() == name.lower())]
    return sorted(rest + [(date, name, fit, js)], key=lambda e: (e[0], e[1].lower()))


def calendar_without(entries, date, name):
    return [e for e in entries if not (e[0] == date and e[1].lower() == name.lower())]


def calendar_save(entries, today=None):
    """Make the local copy equal to entries (called only after the device confirmed them)."""
    import shutil
    root = calendar_dir()
    if root.exists():
        shutil.rmtree(root)
    for date, name, fit, js in entries:
        day = root / date
        day.mkdir(parents=True, exist_ok=True)
        (day / f"{name}.fit").write_bytes(fit)
        (day / f"{name}.json").write_bytes(js)


def calendar_text(entries):
    if not entries:
        return "Calendar (from this server): empty"
    return "Calendar (from this server):\n" + "\n".join(f"  {d}  {n}" for d, n, _, _ in entries)


# --- Routes ---------------------------------------------------------------------------------
# The route list of the device cannot be read over Bluetooth either, so the server remembers the names it
# uploaded. config.json "known_routes" can list names that are already on the device.
ROUTE_NAME_MAX_BYTES = 32


def known_routes():
    try:
        stored = json.loads(KNOWN_ROUTES_FILE.read_text())
    except (OSError, ValueError):
        stored = []
    seed = load_config().get("known_routes") or []
    return sorted(set(seed) | set(stored), key=str.lower)


def remember_route(name):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    KNOWN_ROUTES_FILE.write_text(json.dumps(sorted(set(known_routes()) | {name}, key=str.lower), indent=1))


def check_route_name(name):
    if not name or not name.strip() or any(ord(c) < 32 for c in name):
        raise ServiceError("route name must not be empty or contain control characters")
    if len(name.encode("utf-8")) > ROUTE_NAME_MAX_BYTES:
        raise ServiceError(f"route name: at most {ROUTE_NAME_MAX_BYTES} bytes (UTF-8)")


def load_gpx(gpx_path="", gpx_text="", max_bytes=20 * 1024 * 1024):
    if bool(gpx_path) == bool(gpx_text):
        raise ServiceError("give exactly one of gpx_path (a .gpx file on this computer) or gpx_text")
    if gpx_path:
        path = Path(gpx_path).expanduser()
        if path.suffix.lower() != ".gpx" or not path.is_file():
            raise ServiceError(f"gpx_path must be an existing .gpx file: {gpx_path}")
        if path.stat().st_size > max_bytes:
            raise ServiceError("GPX file is larger than 20 MB")
        return path.read_text(encoding="utf-8", errors="replace")
    return gpx_text


def build_route(name, gpx_path="", gpx_text=""):
    """Returns (gzip payload for the device, summary dict). Raises ServiceError."""
    check_route_name(name)
    try:
        points = routelib.thin(routelib.parse_gpx(load_gpx(gpx_path, gpx_text)))
        if len(points) > routelib.MAX_POINTS:  # thin harder instead of refusing long tracks
            spacing = routelib.POINT_SPACING_M
            while len(points) > routelib.MAX_POINTS:
                spacing *= 1.5
                points = routelib.thin(points, spacing)
        fit = routelib.encode_fit(points, markers=("Start", "Ziel"))
    except (routelib.RouteError, ET_ParseError) as e:
        raise ServiceError(f"GPX: {e}") from None
    s = routelib.decode_fit(fit)[254][0]
    payload = routelib.to_transfer_payload(fit)
    return payload, {"points": len(points), "km": round(s[6] / 1000, 1), "ascent_m": s[9], "descent_m": s[10],
                     "bytes": len(payload), "flat": s[7] == s[8]}


def route_text(name, info, warnings=()):
    lines = [f"Route {name!r}: {info['km']} km, {info['points']} points, ascent {info['ascent_m']} m, "
             f"descent {info['descent_m']} m, {info['bytes']} bytes on the wire"]
    if info.get("flat"):
        warnings = [*warnings, "all altitudes are equal: the GPX file probably has no <ele> elevation data, "
                               "so the elevation profile on the device will be flat"]
    return "\n".join(lines + [f"Note: {w}" for w in warnings])


def route_refusal(name, name_confirmed_new, overwrite):
    exists = name.lower() in {n.lower() for n in known_routes()}
    if exists and not overwrite:
        return (f"A route named {name!r} is already on the device (or was uploaded before). Ask the user whether "
                "to add it anyway (it may appear twice, unverified) and call again with overwrite=true, or choose "
                "another name.")
    if not exists and not (name_confirmed_new or overwrite):
        return ("The route list of the device cannot be read over Bluetooth. Ask the user to confirm that no route "
                f"named {name!r} exists on the S810 (Courses > Route), then call again with name_confirmed_new=true.")
    return None


RIDES_DIR = STATE_DIR / "rides"


def ride_filename(ride_id):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ride_id, timezone.utc).strftime("%Y%m%d-%H%M%S") + f"_{ride_id}.fit"


def describe_ride(entry):
    from datetime import datetime, timezone
    start = datetime.fromtimestamp(entry["id"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"id {entry['id']}: {start}, {entry['moving_s'] // 60} min moving, {entry['distance_m'] / 1000:.1f} km"


def build_pace(name, gpx_path="", gpx_text="", effort="moderate", rider_kg=0.0, bike_kg=0.0, bike="road",
               surface="asphalt", language="de", main_field="", screen=(), wind_kmh=-1.0, wind_from_deg=-1.0,
               temperature_c=-99.0, ride_date="", start_time="10:00"):
    """Pace Pilot plan for a GPX route. Returns (route gzip payload, report JSON bytes, summary dict).
    FTP, max HR and LTHR come from the device profile or config.json; the weights must be given (parameter,
    device profile or config.json). Wind and temperature: given values (wind_kmh, wind_from_deg = where the wind blows
    FROM, temperature_c; negative / -99 = not given), or the forecast for ride_date + start_time."""
    from bryton import pace, weather
    check_route_name(name)
    cfg = load_config()
    rider_kg = rider_kg or cfg.get("rider_kg") or 0
    bike_kg = bike_kg or cfg.get("bike_kg") or 0
    if not rider_kg or not bike_kg:
        raise ServiceError("rider_kg and bike_kg are needed (parameters, or rider_kg / bike_kg in config.json): "
                           "ask the user (rider weight: read_device_profile), the plan depends on them")
    if not cfg.get("ftp"):
        raise ServiceError("ftp is missing in config.json (also mhr, lthr if known)")
    try:
        rider = pace.Rider(ftp=cfg["ftp"], mhr=cfg.get("mhr", 185), lthr=cfg.get("lthr", 0), rider_kg=rider_kg,
                           bike_kg=bike_kg, bike=bike, surface=surface, effort=effort, language=language,
                           main_field=main_field or pace.Rider.main_field,
                           screen=tuple(screen) if screen else pace.DEFAULT_SCREEN)
        rider.check()
        points = routelib.thin(routelib.parse_gpx(load_gpx(gpx_path, gpx_text)))
        spacing = routelib.POINT_SPACING_M
        while len(points) > routelib.MAX_POINTS:
            spacing *= 1.5
            points = routelib.thin(points, spacing)
        conditions = None
        if wind_kmh >= 0 or temperature_c > -99:
            conditions = {"source": "given by you"}
        if wind_kmh >= 0:
            rider.wind_ms, rider.wind_from_deg = wind_kmh / 3.6, max(wind_from_deg, 0.0)
        if temperature_c > -99:
            rider.temp_c = temperature_c
        if ride_date and (wind_kmh < 0 or temperature_c <= -99):
            hours = pace.build_plan(points, rider)[0]["coach_report"]["race_summary"]["total_est_time_sec"] / 3600
            mid = points[len(points) // 2]
            w = weather.fetch_weather(mid[0], mid[1], ride_date, start_time, hours)
            if wind_kmh < 0:
                rider.wind_ms, rider.wind_from_deg = w["wind_ms"], w["wind_from_deg"]
            if temperature_c <= -99:
                rider.temp_c = w["temp_c"]
            conditions = {"source": w["source"] + (" (the rest given by you)" if conditions else ""), "gust_ms": w["gust_ms"]}
        if conditions:
            conditions.update(wind_ms=rider.wind_ms, wind_from_deg=rider.wind_from_deg, temp_c=rider.temp_c)
        payload, report_json, report = pace.build_files(points, rider)
    except (ValueError, routelib.RouteError, ET_ParseError, weather.WeatherError) as e:
        raise ServiceError(f"Pace Pilot: {e}") from None
    r = report["coach_report"]
    return payload, report_json, {"report": r, "points": len(points), "route_bytes": len(payload),
                                  "report_bytes": len(report_json), "rider": rider, "conditions": conditions}


def pace_text(name, info):
    r, rider = info["report"], info["rider"]
    s = r["race_summary"]
    lines = [f"Pace Pilot plan {name!r}: {s['total_distance_km']} km, ascent {s['total_elevation_m']} m, "
             f"estimated {s['total_est_time']}, average {s['avg_power_w']} W (IF {s['intensity_factor']}), "
             f"{s['total_work_kj']} kJ, {len(r['tactical_segments'])} segments",
             f"Rider: FTP {rider.ftp} W, {rider.rider_kg:g} kg + bike {rider.bike_kg:g} kg, effort {rider.effort}, "
             f"{rider.bike}, {rider.surface}",
             f"Nutrition: {r['nutrition_and_energy']['carbohydrates']['energy_gels']['total_gels']} gels, "
             f"{r['nutrition_and_energy']['hydration']['total_ml']} ml; screen: main {rider.main_field}, "
             f"fields {', '.join(rider.screen)}"]
    c = info.get("conditions")
    if c:
        t = f", {c['temp_c']:g} C" if c.get("temp_c") is not None else ""
        gust = f", gusts up to {c['gust_ms'] * 3.6:.0f} km/h" if c.get("gust_ms") else ""
        lines.append(f"Conditions ({c['source']}): wind {c['wind_ms'] * 3.6:.0f} km/h from {c['wind_from_deg']:.0f} deg "
                     f"(0 = north, 90 = east){t}{gust}. Speeds and times include the wind; target watts do not change.")
    for g in r["tactical_segments"]:
        lines.append(f"  {g['segment']:2}  {g['distance_km']:6.2f} km  {g['avg_gradient_pct']:5.1f} %  "
                     f"{g['target_power_w_low']}-{g['target_power_w_high']} W  {g['est_speed_kmh']} km/h  {g['tactical_note']}")
    lines.append("Note: this is a physics model" + ("" if c else " (no weather, no wind)") + ". Only 'moderate' effort is calibrated against "
                 "the Bryton app's own plan; other levels are estimates.")
    return "\n".join(lines)
