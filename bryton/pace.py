"""Pace Pilot plans for the Bryton Rider S810, generated from a route.

The Bryton app sends two files for a route with Pace Pilot (docs/file-formats.md): the route (kind 1a, with a
waypoint of type 150 at the end of every segment) and a JSON `coach_report` (kind 1b) with the pacing plan. The
app computes that plan in the cloud, with weather. This module computes a plan of the same shape from a
physical model; the numbers are ours, not Bryton's:

- target power = intensity factor x FTP (moderate = 0.68 as seen in the app; the other levels are guesses),
  raised on climbs and lowered on descents, scaled so that the average stays on target;
- speed and time from the power equation (gravity, rolling resistance, air drag, no wind);
- heart-rate band around LTHR, cadence band, nutrition and hydration rules modelled on the app's report.
"""
import json
import math
from dataclasses import dataclass, field

from bryton import route as routelib

EFFORT_IF = {"very_light": 0.50, "light": 0.60, "moderate": 0.68, "hard": 0.78, "maximal": 0.90}
CRR = {"asphalt": 0.004, "gravel": 0.008, "offroad": 0.015}
CDA = {"road": 0.32, "gravel": 0.36, "mtb": 0.40}
G, RHO, DRIVETRAIN = 9.81, 1.2, 0.975
MAX_SEGMENTS = 12  # the app's example has 12
MAX_SPEED_MS = 18.0  # 65 km/h: descents are capped

NOTES = {
    "de": {"flat": "Gleichmäßiges Tempo halten", "climb": "Stabilität im leichten Anstieg",
           "steep": "Anstieg kontrolliert fahren", "descent": "Im Gefälle erholen", "last": "Endspurt finalisieren"},
    "en": {"flat": "Hold a steady pace", "climb": "Stay smooth on the climb",
           "steep": "Ride the climb under control", "descent": "Recover on the descent", "last": "Finish strong"},
}
TROUBLE = {
    "de": {"cadence_low": "Erhöhe Kadenz um Rhythmus zu sichern", "cadence_high": "Verringere Kadenz leicht um Kraft zu sparen",
           "hr_low": "Steigere Herzfrequenz durch kleine Intensitätssteigerung",
           "hr_high": "Senke Herzfrequenz durch kontrollierte Atmung",
           "power_low": "Erhöhe Kraft durch gleichmäßiges Antreten", "power_high": "Reduziere Kraft durch lockerere Sitzposition"},
    "en": {"cadence_low": "Raise your cadence to keep a rhythm", "cadence_high": "Lower your cadence slightly to save energy",
           "hr_low": "Raise your heart rate with a small effort increase",
           "hr_high": "Lower your heart rate with controlled breathing",
           "power_low": "Add power with a smooth pedal stroke", "power_high": "Ease off with a relaxed position"},
}
# Data fields known to the firmware (field-name table in the R049 image, found by the firmware analysis).
# Only the ones in the app's own report are confirmed on the device; swapping two of them worked (Test_Pace4).
MAIN_FIELDS = ("HeartRate_target", "Power_target", "Cadence_target", "3Sec_Power_target")
SCREEN_CHOICES = ("HeartRate", "Power", "Cadence", "3Sec_Power", "Segment_Normalized_Power", "FTP_Zone",
                  "Segment_Distance", "Grade", "Average_Power", "Segment_Average_Power", "Anaerobic_Reserve")
POSITIONS = ("Data_1_Top_Left", "Data_2_Top_Right", "Data_3_Mid_Left", "Data_4_Mid_Right", "Data_5_Bottom_Left",
             "Data_6_Bottom_Right")  # position and priority_score must stay paired in this order (Test_Pace2)
DEFAULT_SCREEN = ("Segment_Distance", "Segment_Normalized_Power", "Anaerobic_Reserve", "3Sec_Power", "FTP_Zone", "Grade")


def screen_fields(names):
    return [{"data_field": n, "position": p, "priority_score": i} for i, (n, p) in enumerate(zip(names, POSITIONS), 1)]


@dataclass
class Rider:
    ftp: int = 200
    mhr: int = 185
    lthr: int = 0  # 0 = 92 % of mhr
    rider_kg: float = 75.0
    bike_kg: float = 8.0
    bike: str = "road"
    surface: str = "asphalt"
    effort: str = "moderate"
    cadence: tuple = (89, 98)
    gel_g: int = 50
    language: str = "de"
    notes: list = field(default_factory=list)  # optional: one text per segment (overrides the templates)
    main_field: str = "3Sec_Power_target"  # the big bar in the middle of the screen
    screen: tuple = DEFAULT_SCREEN  # the six data fields, top left to bottom right

    def check(self):
        if self.effort not in EFFORT_IF or self.surface not in CRR or self.bike not in CDA or self.language not in NOTES:
            raise ValueError("effort/surface/bike/language not one of " + ", ".join(
                [*EFFORT_IF, *CRR, *CDA, *NOTES]))
        if self.main_field not in MAIN_FIELDS or len(self.screen) != 6 or any(f not in SCREEN_CHOICES for f in self.screen):
            raise ValueError("main_field must be one of " + ", ".join(MAIN_FIELDS) + "; screen needs 6 of "
                             + ", ".join(SCREEN_CHOICES))
        if not (50 <= self.ftp <= 600 and 120 <= self.mhr <= 230 and 30 <= self.rider_kg + self.bike_kg <= 250):
            raise ValueError("ftp, mhr or weights out of range")


def speed_ms(power_w, grade_pct, mass_kg, crr, cda):
    """Steady speed (m/s) for a wheel power; bisection on the power equation, capped at MAX_SPEED_MS."""
    slope = math.atan(grade_pct / 100)
    force0 = mass_kg * G * (crr * math.cos(slope) + math.sin(slope))
    watts = power_w * DRIVETRAIN

    def need(v):
        return v * (force0 + 0.5 * RHO * cda * v * v)

    if need(MAX_SPEED_MS) <= watts:
        return MAX_SPEED_MS
    lo, hi = 0.3, MAX_SPEED_MS
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if need(mid) < watts else (lo, mid)
    return (lo + hi) / 2


def _profile(points):
    """Cumulative distance (m) and smoothed altitude (m) per point."""
    cum = [0.0]
    for a, b in zip(points, points[1:]):
        cum.append(cum[-1] + routelib._distance_m((a[0] * 1e6, a[1] * 1e6), (b[0] * 1e6, b[1] * 1e6)))
    alt = []
    for i in range(len(points)):  # +-100 m moving average
        j0, j1 = i, i
        while j0 > 0 and cum[i] - cum[j0 - 1] <= 100:
            j0 -= 1
        while j1 < len(points) - 1 and cum[j1 + 1] - cum[i] <= 100:
            j1 += 1
        alt.append(sum(p[2] for p in points[j0:j1 + 1]) / (j1 - j0 + 1))
    return cum, alt


def _grade_class(g):
    return 3 if g > 5 else 2 if g > 1.5 else 0 if g >= -1.5 else -1


def split_segments(points, cum, alt, max_segments=MAX_SEGMENTS):
    """[(start_index, end_index)]: cut where the grade class changes, merge short parts, split very long ones."""
    n, total = len(points), cum[-1]
    min_len = max(800.0, total / 25)
    cls = []
    for i in range(n):
        j = i
        while j < n - 1 and cum[j] - cum[i] < 150:
            j += 1
        span = cum[j] - cum[i]
        cls.append(_grade_class((alt[j] - alt[i]) / span * 100) if span > 30 else (cls[-1] if cls else 0))
    cuts = [0] + [i for i in range(1, n) if cls[i] != cls[i - 1]] + [n - 1]
    segs = [(a, b) for a, b in zip(cuts, cuts[1:]) if b > a]

    def length(s):
        return cum[s[1]] - cum[s[0]]

    while len(segs) > 1:
        short = min(range(len(segs)), key=lambda k: length(segs[k]))
        if length(segs[short]) >= min_len and len(segs) <= max_segments:
            break
        k = short
        nb = k - 1 if k > 0 and (k == len(segs) - 1 or length(segs[k - 1]) <= length(segs[k + 1])) else k + 1
        a, b = sorted((k, nb))
        segs[a:b + 1] = [(segs[a][0], segs[b][1])]
    out = []
    for a, b in segs:  # split segments longer than 15 km into equal parts (nutrition and pacing anchors)
        parts = max(1, math.ceil(length((a, b)) / 15000))
        ends = [a] + [min(range(a, b + 1), key=lambda i: abs(cum[i] - (cum[a] + (cum[b] - cum[a]) * p / parts)))
                      for p in range(1, parts)] + [b]
        out += [(x, y) for x, y in zip(ends, ends[1:]) if y > x]
    while len(out) > max_segments:  # merge the shortest neighbours again
        k = min(range(len(out) - 1), key=lambda k: length(out[k]) + length(out[k + 1]))
        out[k:k + 2] = [(out[k][0], out[k + 1][1])]
    return out


def _hms(seconds):
    s = round(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def build_plan(points, rider):
    """(coach_report dict, segment end indices). points: [(lat, lon, alt_m)]."""
    rider.check()
    cum, alt = _profile(points)
    segs = split_segments(points, cum, alt)
    lthr = rider.lthr or round(rider.mhr * 0.92)
    mass, crr, cda = rider.rider_kg + rider.bike_kg, CRR[rider.surface], CDA[rider.bike]
    target = EFFORT_IF[rider.effort] * rider.ftp
    info = []
    for a, b in segs:
        d = cum[b] - cum[a]
        grade = (alt[b] - alt[a]) / d * 100 if d > 0 else 0.0
        info.append({"a": a, "b": b, "d": d, "grade": grade,
                     "factor": 1.08 if grade > 3 else 1.04 if grade > 1.5 else 0.7 if grade < -3 else 0.9 if grade < -1.5 else 1.0})
    scale = 1.0
    for _ in range(3):  # time-weighted mean power on target
        for s in info:
            s["p"] = target * s["factor"] * scale
            s["v"] = speed_ms(s["p"], s["grade"], mass, crr, cda)
            s["t"] = s["d"] / s["v"]
        mean = sum(s["p"] * s["t"] for s in info) / sum(s["t"] for s in info)
        scale *= target / mean
    total_t = sum(s["t"] for s in info)
    avg_p = sum(s["p"] * s["t"] for s in info) / total_t
    work_kj = avg_p * total_t / 1000
    ascent = sum(max(alt[i + 1] - alt[i], 0) for i in range(len(alt) - 1))
    minutes = total_t / 60
    hr_center = lthr * (0.7 + 0.45 * EFFORT_IF[rider.effort])
    notes, trouble = NOTES[rider.language], TROUBLE[rider.language]
    segments = []
    for k, s in enumerate(info, 1):
        g = s["grade"]
        text = (rider.notes[k - 1] if k <= len(rider.notes) else
                notes["last"] if k == len(info) and len(info) > 1 else
                notes["steep"] if g > 5 else notes["climb"] if g > 1.5 else notes["descent"] if g < -1.5 else notes["flat"])
        segments.append({
            "segment": k, "index": {"start": s["a"], "end": s["b"]}, "distance_km": round(s["d"] / 1000, 2),
            "avg_gradient_pct": round(g, 2), "elevation_m": round(sum(alt[s["a"]:s["b"] + 1]) / (s["b"] - s["a"] + 1), 1),
            "est_speed_kmh": round(s["v"] * 3.6, 1), "estimated_time": _hms(s["t"]),
            "target_power_w_low": round(s["p"] * 0.952), "target_power_w_high": round(s["p"] * 1.056),
            "target_power_pct_ftp": round(s["p"] / rider.ftp * 100),
            "target_cadence_rpm_low": rider.cadence[0], "target_cadence_rpm_high": rider.cadence[1],
            "hr_zone_low": round(hr_center * 0.95), "hr_zone_high": round(hr_center * 1.045),
            "w_prime_balance_pct": 100, "tactical_note": text, "troubleshooting": dict(trouble)})
    report = {"coach_report": {
        "race_summary": {
            "intensity_factor": EFFORT_IF[rider.effort], "normalized_power_w": round(avg_p),
            "environmental_factor_applied": 1, "avg_power_w": round(avg_p), "total_work_kj": round(work_kj),
            "total_distance_km": f"{cum[-1] / 1000:.2f}", "max_downhill_speed_limit": "13.1",
            "total_est_time": _hms(total_t), "tss": round(total_t / 3600 * EFFORT_IF[rider.effort] ** 2 * 100),
            "total_est_time_sec": round(total_t), "total_elevation_m": f"{ascent:.1f}"},
        "main_target": {"data_field": rider.main_field, "position": "Center_Large_Bar"},
        "nutrition_and_energy": {
            "carbohydrates": {"raw_grams": {"total_kcal": int(round(minutes / 30 * 180, -1)),
                                            "interval_min": 30, "segment_kcal": 180},
                              "energy_gels": {"rules": {"min_alert_interval_min": 33, "take_one_gel_every_kj": 200,
                                                        "skip_if_accumulated_kj_lte": 300, "max_gels_per_hour": 1,
                                                        "max_carbohydrate_g_per_hour": 90},
                                              "total_gels": int(minutes // 33), "gel_size_g": rider.gel_g}},
            "hydration": {"total_ml": round(minutes / 20 * 216.5), "interval_min": 20, "segment_ml": 216}},
        "tactical_segments": segments,
        "auxiliary_fields_black": screen_fields(rider.screen)}}
    return report, [b for _a, b in segs]


def report_bytes(report):
    """The coach report as the app writes it: indent 2, `"key" : value`, UTF-8."""
    return json.dumps(report, indent=2, separators=(",", " : "), ensure_ascii=False).encode("utf-8")


def build_files(points, rider, name_markers=None):
    """(route payload kind 1a: gzip FIT, report bytes kind 1b, report dict)."""
    report, ends = build_plan(points, rider)
    fit = routelib.encode_fit(points, markers=name_markers, segment_ends=ends, version=False)
    return routelib.to_transfer_payload(fit), report_bytes(report), report
