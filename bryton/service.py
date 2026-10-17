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
import unicodedata
from pathlib import Path

from bryton.fitgen import MAX_STEPS, PRIMARY_KEYS, SpecError, build_files, resolve_plan

STATE_DIR = Path(os.environ.get("BRYTON_S810_HOME", Path.home() / ".bryton_s810"))
KNOWN_FILE = STATE_DIR / "known_workouts.json"
CONFIG_FILE = STATE_DIR / "config.json"
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
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


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
