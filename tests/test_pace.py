import gzip
import json
import math

import pytest

from bryton import pace, route
from tools.bryton_pace_test import synthetic_hilly

REPORT_KEYS = {"race_summary", "main_target", "nutrition_and_energy", "tactical_segments", "auxiliary_fields_black"}
SEGMENT_KEYS = {"segment", "index", "distance_km", "avg_gradient_pct", "elevation_m", "est_speed_kmh", "estimated_time",
                "target_power_w_low", "target_power_w_high", "target_power_pct_ftp", "target_cadence_rpm_low",
                "target_cadence_rpm_high", "hr_zone_low", "hr_zone_high", "w_prime_balance_pct", "tactical_note",
                "troubleshooting"}  # the keys of the app's report (capture 2026-10-17)
SUMMARY_KEYS = {"intensity_factor", "normalized_power_w", "environmental_factor_applied", "avg_power_w", "total_work_kj",
                "total_distance_km", "max_downhill_speed_limit", "total_est_time", "tss", "total_est_time_sec",
                "total_elevation_m"}


def test_speed_physics():
    kw = dict(mass_kg=83, crr=0.004, cda=0.32)
    flat = pace.speed_ms(200, 0, **kw)
    assert 7 < flat < 9.5  # 25-34 km/h for 200 W
    assert pace.speed_ms(200, 6, **kw) < 0.5 * flat  # a 6 % climb is much slower
    assert pace.speed_ms(200, -8, **kw) == pace.MAX_SPEED_MS  # capped
    assert pace.speed_ms(150, 0, **kw) < flat < pace.speed_ms(250, 0, **kw)


def test_plan_shape_and_consistency():
    pts = synthetic_hilly()
    rider = pace.Rider(ftp=220, mhr=188, lthr=154)
    report, ends = pace.build_plan(pts, rider)
    cr = report["coach_report"]
    assert set(report) == {"coach_report"} and set(cr) == REPORT_KEYS and set(cr["race_summary"]) == SUMMARY_KEYS
    segs = cr["tactical_segments"]
    assert 2 <= len(segs) <= pace.MAX_SEGMENTS and all(set(s) == SEGMENT_KEYS for s in segs)
    assert segs[0]["index"]["start"] == 0 and segs[-1]["index"]["end"] == len(pts) - 1 == ends[-1]
    assert all(a["index"]["end"] == b["index"]["start"] for a, b in zip(segs, segs[1:]))  # no gaps
    assert abs(sum(s["distance_km"] for s in segs) - float(cr["race_summary"]["total_distance_km"])) < 0.1 * len(segs)
    s = cr["race_summary"]
    assert abs(s["avg_power_w"] - 0.68 * 220) < 3  # time-weighted mean on target
    assert s["tss"] == round(s["total_est_time_sec"] / 3600 * 0.68 ** 2 * 100)
    assert all(x["target_power_w_low"] < x["target_power_w_high"] and x["hr_zone_low"] < x["hr_zone_high"] for x in segs)
    climbs = [x for x in segs if x["avg_gradient_pct"] > 1.5]
    flats = [x for x in segs if abs(x["avg_gradient_pct"]) <= 1.5]
    assert climbs and flats and min(c["target_power_w_low"] for c in climbs) > max(f["target_power_w_low"] for f in flats)
    assert max(f["est_speed_kmh"] for f in flats) > max(c["est_speed_kmh"] for c in climbs)


def test_effort_levels_are_ordered():
    pts = synthetic_hilly()
    power = [pace.build_plan(pts, pace.Rider(ftp=220, effort=e))[0]["coach_report"]["race_summary"]["avg_power_w"]
             for e in pace.EFFORT_IF]
    assert power == sorted(power)


def test_files_roundtrip_and_format():
    pts = synthetic_hilly()
    payload, report_json, report = pace.build_files(pts, pace.Rider(ftp=220, language="en"))
    assert json.loads(report_json) == report and b'"race_summary" : {' in report_json  # the app's style
    d = route.decode_fit(gzip.decompress(payload))
    assert 248 not in d and len(d[249]) == len(pts)
    ends = [r[1] for r in d[250] if r[2] == 150]
    assert ends == [s["index"]["end"] for s in report["coach_report"]["tactical_segments"]]
    assert report["coach_report"]["tactical_segments"][0]["tactical_note"] in pace.NOTES["en"].values()


def test_rider_validation_and_custom_notes():
    with pytest.raises(ValueError):
        pace.Rider(effort="nope").check()
    rep, _ = pace.build_plan(synthetic_hilly(), pace.Rider(notes=["Eins", "Zwei"]))
    assert [s["tactical_note"] for s in rep["coach_report"]["tactical_segments"][:2]] == ["Eins", "Zwei"]


def test_short_route_has_one_segment_and_no_gels():
    loop = [(47 + 0.003 * math.sin(2 * math.pi * i / 120), 8 + 0.004 * math.cos(2 * math.pi * i / 120), 420) for i in range(121)]
    rep, ends = pace.build_plan(loop, pace.Rider())
    cr = rep["coach_report"]
    assert len(cr["tactical_segments"]) >= 1 and cr["nutrition_and_energy"]["carbohydrates"]["energy_gels"]["total_gels"] == 0


def test_report_variants_keep_size_and_change_one_thing():
    from tools.pace_variants import variants
    rep, _ = pace.build_plan(synthetic_hilly(), pace.Rider(ftp=220))
    raw = pace.report_bytes(rep)
    v = variants(raw)
    assert set(v) == {"A", "B", "D"} and all(len(x) == len(raw) for x in v.values())
    a, d, b = (json.loads(v[k])["coach_report"] for k in "ADB")
    base = rep["coach_report"]
    assert a["tactical_segments"][0]["target_power_w_low"] == base["tactical_segments"][0]["target_power_w_low"] + 1
    assert a["tactical_segments"][1:] == base["tactical_segments"][1:]
    assert d["tactical_segments"][0]["tactical_note"] != base["tactical_segments"][0]["tactical_note"]
    fields = {f["data_field"]: f for f in b["auxiliary_fields_black"]}
    assert fields["Segment_Distance"]["position"] == "Data_6_Bottom_Right" and fields["Segment_Distance"]["priority_score"] == 6
    assert fields["Grade"]["position"] == "Data_1_Top_Left" and fields["Grade"]["priority_score"] == 1


def test_screen_fields_are_configurable_and_paired():
    rider = pace.Rider(screen=("HeartRate", "Power", "Cadence", "Grade", "Average_Power", "FTP_Zone"), main_field="Power_target")
    cr = pace.build_plan(synthetic_hilly(), rider)[0]["coach_report"]
    assert cr["main_target"]["data_field"] == "Power_target"
    fields = cr["auxiliary_fields_black"]
    assert [f["data_field"] for f in fields][:3] == ["HeartRate", "Power", "Cadence"]
    assert [(f["position"], f["priority_score"]) for f in fields] == [(p, i) for i, p in enumerate(pace.POSITIONS, 1)]
    assert pace.build_plan(synthetic_hilly(), pace.Rider())[0]["coach_report"]["auxiliary_fields_black"][5]["data_field"] == "Grade"
    for bad in (dict(screen=("HeartRate",) * 5), dict(screen=("Nope",) * 6), dict(main_field="Nope")):
        with pytest.raises(ValueError):
            pace.Rider(**bad).check()
