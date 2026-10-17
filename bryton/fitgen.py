"""Generate Bryton workout files (FIT + JSON) from a simple step list.

Layout copied from files the Bryton app produced (reference/…fit, 4x4.fit): file_id, workout,
workout_step messages; power targets as % FTP (target_type 245, 100000 + percent); cadence
lives only in the JSON (`interval[i]` belongs to step i, `{"target_type":""}` = none).

step = {"kind": "warmup|active|rest|cooldown", "seconds": int, "power": (lo, hi) in % FTP,
        "cadence": (lo, hi) optional, "name": optional (max 9 characters)}
"""
import datetime
import json
import re
import struct
import time

from bryton.fit import fit_crc

FIT_EPOCH = 631065600  # 1989-12-31T00:00:00Z in unix seconds
INTENSITY = {"active": 0, "rest": 1, "warmup": 2, "cooldown": 3}
DEFAULT_STEP_NAME = {"warmup": "Warm up", "active": "Work", "rest": "Rest", "cooldown": "Cool down"}
POWER_PERCENT_TARGET = 245
CADENCE_TARGET = 3  # FIT wkt_step_target cadence
# Primary targets seen in the app's own files (Ref_FTP/MHR/LTHR/Speed, 2026-10-16): fixed target =
# low == high; percent targets are 100000 + percent; speed is mm/s (27 km/h -> 7500), target_type 0.
PERCENT_TARGETS = {"power": 245, "mhr": 249, "lthr": 248}
SPEED_TARGET = 0
PRIMARY_KEYS = ("power", "mhr", "lthr", "speed")
DISTANCE_CM_PER_M = 100  # distance duration (duration_type 1) is in cm: 5 km = 500000 (Ref_Dist)
PROFILE_VERSION = 0x0877  # as in the app's files
STEP_NAME_BYTES, WKT_NAME_BYTES = 10, 16  # app layout (longer names are cut)
MAX_STEPS = 50


class SpecError(ValueError):
    pass


def validate(steps):
    if not 1 <= len(steps) <= MAX_STEPS:
        raise SpecError(f"need 1..{MAX_STEPS} steps, got {len(steps)}")
    for i, step in enumerate(steps):
        if step.get("kind") not in INTENSITY:
            raise SpecError(f"step {i}: kind must be one of {sorted(INTENSITY)}")
        if ("seconds" in step) == ("meters" in step):
            raise SpecError(f"step {i}: give exactly one of seconds or meters")
        if "seconds" in step and (not isinstance(step["seconds"], int) or not 1 <= step["seconds"] <= 6 * 3600):
            raise SpecError(f"step {i}: seconds must be an integer 1..21600")
        if "meters" in step and (not isinstance(step["meters"], int) or not 10 <= step["meters"] <= 200000):
            raise SpecError(f"step {i}: meters must be an integer 10..200000")
        if step.get("target") == "cadence":
            lo, hi = step["cadence"]
            if not (20 <= lo <= hi <= 250):
                raise SpecError(f"step {i}: primary cadence must satisfy 20 <= low <= high <= 250")
            continue
        given = [k for k in PRIMARY_KEYS if step.get(k)]
        if len(given) != 1:
            raise SpecError(f"step {i}: need exactly one of {', '.join(PRIMARY_KEYS)} (or target cadence)")
        lo, hi = step[given[0]]
        limit = 60 if given[0] == "speed" else 300
        if not (1 <= lo <= hi <= limit):
            unit = "km/h" if given[0] == "speed" else "%"
            raise SpecError(f"step {i}: {given[0]} {unit} must satisfy 1 <= low <= high <= {limit}")
        if step.get("cadence"):
            lo, hi = step["cadence"]
            if not (20 <= lo <= hi <= 250):
                raise SpecError(f"step {i}: cadence must satisfy 20 <= low <= high <= 250")


def _text(value, size):
    raw = value.encode("ascii", "replace")[:size - 1]
    return raw + b"\x00" * (size - len(raw))


def _definition(local, global_num, fields):
    body = bytes([0x40 | local, 0, 0]) + struct.pack("<H", global_num) + bytes([len(fields)])
    return body + b"".join(bytes(f) for f in fields)


def primary_values(step):
    """(low, high, FIT target_type) of a validated step."""
    if step.get("target") == "cadence":  # FIT target_type 3, custom values in rpm (Bryton web app: woParser)
        lo, hi = step["cadence"]
        return lo, hi, CADENCE_TARGET
    if step.get("speed"):
        lo, hi = step["speed"]
        return int(lo / 3.6 * 1000), int(hi / 3.6 * 1000), SPEED_TARGET  # app truncates (33 -> 9166)
    key = next(k for k in PERCENT_TARGETS if step.get(k))
    lo, hi = step[key]
    return 100000 + lo, 100000 + hi, PERCENT_TARGETS[key]


def encode_fit(wkt_name, steps, time_created=None):
    validate(steps)
    created = int(time.time() - FIT_EPOCH) if time_created is None else time_created
    out = bytearray()
    out += _definition(0, 0, [(0, 1, 0), (1, 2, 132), (4, 4, 134)])
    out += bytes([0, 5]) + struct.pack("<HI", 255, created)
    out += _definition(0, 26, [(8, WKT_NAME_BYTES, 7), (6, 2, 132), (4, 1, 0)])
    out += _definition(2, 27, [(0, STEP_NAME_BYTES, 7), (2, 4, 134), (4, 4, 134), (5, 4, 134),
                               (6, 4, 134), (254, 2, 132), (1, 1, 0), (3, 1, 0), (7, 1, 0)])
    # record order as in the app's files: both definitions first, then the workout, then the steps
    out += bytes([0]) + _text(wkt_name, WKT_NAME_BYTES) + struct.pack("<H", len(steps)) + bytes([2])
    for i, step in enumerate(steps):
        lo, hi, target_type = primary_values(step)
        if "meters" in step:
            duration_type, duration = 1, step["meters"] * DISTANCE_CM_PER_M
        else:
            duration_type, duration = 0, step["seconds"] * 1000
        out += bytes([2]) + _text(step.get("name") or DEFAULT_STEP_NAME[step["kind"]], STEP_NAME_BYTES)
        out += struct.pack("<IIII", duration, 0, lo, hi)
        out += struct.pack("<H", i) + bytes([duration_type, target_type, INTENSITY[step["kind"]]])
    header = bytes([14, 0x10]) + struct.pack("<HI", PROFILE_VERSION, len(out)) + b".FIT"
    header += struct.pack("<H", fit_crc(header))
    body = header + bytes(out)
    return body + struct.pack("<H", fit_crc(body))


def encode_json(name, steps, description="", create_time_ms=None, leading=None, shift_highs=False,
                plan=None, high_first=False):
    """plan: list of dates "YYYY-MM-DD" the workout is scheduled for. In Bryton's own data the
    schedule lives only here (see intervalssync, bryton/workout.py: build_workout_info).
    leading=(low, high): extra cadence entry in front of the step entries (experiment).
    high_first: write target_high before target_low. The firmware reader (firmware analysis, confirmed on the device)
    advances its slot counter after target_low, so the device expects target_high first; with the
    app's order each step shows its own range and no shift correction is needed."""
    validate(steps)
    plan = list(plan or [])
    for d in plan:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            raise SpecError(f"plan date must be YYYY-MM-DD, got {d!r}")
    interval = []
    if leading:
        interval.append({"target_low": leading[0], "target_high": leading[1], "target_type": "cadence"})
    def entry(low, high):
        keys = ({"target_high": high, "target_low": low} if high_first
                else {"target_low": low, "target_high": high})
        return {**keys, "target_type": "cadence"}

    def secondary(step):
        return step.get("cadence") if step.get("target") != "cadence" else None

    for i, step in enumerate(steps):
        own = secondary(step)
        nxt = secondary(steps[i + 1]) if i + 1 < len(steps) else None
        if not shift_highs:
            interval.append(entry(own[0], own[1]) if own else {"target_type": ""})
        elif own or nxt:
            # The S810 pairs a step's lower value with the upper value of the PREVIOUS entry, and only
            # takes it from an entry typed "cadence" (Test_CadFix, Test_CadMix). So each entry carries
            # its own lower value (0 = none) and the NEXT step's upper value (0 = none).
            interval.append(entry(own[0] if own else 0, nxt[1] if nxt else 0))
        else:
            interval.append({"target_type": ""})
    doc = {"name": name, "description": description, "plan": plan, "provider": "bryton",
           "interval": interval,
           "create_time": int(time.time() * 1000) if create_time_ms is None else create_time_ms,
           "ver": 5}
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()


def with_dummy_first_step(steps, seconds=5, low=None):
    """Prepend a short step so the first real step gets a displayable upper cadence value.

    The device reads a step's upper cadence value from the previous entry, so the first step of a
    workout always shows 0 there. Only needed when the first step has a cadence target.
    """
    first = steps[0]
    if not first.get("cadence") or first.get("target") == "cadence":
        return list(steps)
    dummy = {"kind": first["kind"], "seconds": seconds, "name": "Start"}
    dummy.update({k: first[k] for k in PRIMARY_KEYS if first.get(k)})
    if low is not None:  # only for the old experiment Test_CadFix (start step showed low/0)
        dummy["cadence"] = (low, low)
    return [dummy] + list(steps)


def device_view(interval):
    """Model of the S810 cadence display per step, from the observations:
    entry typed "" -> no cadence; entry typed "cadence" -> (own target_low, target_high of the
    previous entry if that one is typed "cadence", else 0)."""
    view = []
    for i, entry in enumerate(interval):
        if entry.get("target_type") != "cadence":
            view.append(None)
            continue
        prev = interval[i - 1] if i > 0 else {}
        high = prev.get("target_high", 0) if prev.get("target_type") == "cadence" else 0
        view.append((entry.get("target_low", 0), high))
    return view


def resolve_plan(entries, today=None):
    """Turn spec plan entries ("today", "+2", "2026-10-20") into YYYY-MM-DD strings."""
    today = today or datetime.date.today()
    out = []
    for e in entries or []:
        if e == "today":
            out.append(today.isoformat())
        elif re.fullmatch(r"\+\d+", e):
            out.append((today + datetime.timedelta(days=int(e[1:]))).isoformat())
        else:
            out.append(e)
    return out


START_STEP_SECONDS = 5


def build_files(name, steps, description="", plan=None, correct_cadence=False,
                start_seconds=START_STEP_SECONDS, start_low=None, high_first=True):
    """FIT + JSON for one workout.

    - Step with only a cadence goal: give it {"target": "cadence"} -> FIT primary target.
    - Step with a power/HR/speed target AND cadence: cadence goes into the JSON. The device reads
      `target_high` before `target_low` (firmware analysis, confirmed with Test_CadOrder), so with
      high_first=True (default) every step shows its own range and no workaround is needed.
    - Legacy: high_first=False, correct_cadence=True reproduces the old workaround (upper values moved one
      entry forward plus a short "Start" step; verified with Test_CadFix) for the app's key order.
    Returns (fit_bytes, json_bytes, final_steps).
    """
    if correct_cadence:
        steps = with_dummy_first_step(steps, start_seconds, start_low)
    fit = encode_fit(name[:WKT_NAME_BYTES - 1], steps)
    js = encode_json(name, steps, description=description, plan=plan, shift_highs=correct_cadence,
                     high_first=high_first)
    return fit, js, steps
