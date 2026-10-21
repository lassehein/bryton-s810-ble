"""Build a Pace Pilot plan for a route and send it to the S810 (route + coach report).

  PYTHONPATH=. python3 tools/bryton_pace_test.py --name Test_PaceGen                 # synthetic hilly route, dry run
  PYTHONPATH=. python3 tools/bryton_pace_test.py --name Test_PaceGen --gpx my.gpx --send

Rider values come from ~/.bryton_s810/config.json (ftp, mhr, lthr, rider_kg, bike_kg); options override them.
Only names starting with Test_ are accepted. Without --send nothing is transmitted.
"""
import argparse
import asyncio
import json
import math
import sys

from bryton import pace, route
from bryton.ble import UploadError, upload_pace
from bryton.protocol import build_chunks
from bryton.service import load_config


def synthetic_hilly(n=1200, lat0=47.0, lon0=8.0):
    """About 30 km of gently winding road with two climbs and a descent (test data, not a real place)."""
    return [(lat0 + i * 25 / 111195 * 0.8, lon0 + math.sin(i / 90) * 0.01,
             400 + 90 * math.sin(i / 160) + 0.004 * i * 25) for i in range(n)]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--gpx")
    ap.add_argument("--effort", default="moderate", choices=list(pace.EFFORT_IF))
    ap.add_argument("--screen", help="six data fields, comma separated (top left to bottom right), see bryton/pace.py")
    ap.add_argument("--main", default="3Sec_Power_target", help="main field of the big bar")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--log", default="write.log")
    a = ap.parse_args()
    if not a.name.startswith("Test_") or len(a.name.encode()) > 32:
        sys.exit("name must start with Test_ and be at most 32 bytes")
    cfg = load_config()
    rider = pace.Rider(ftp=cfg.get("ftp", 200), mhr=cfg.get("mhr", 185), lthr=cfg.get("lthr", 0),
                       rider_kg=cfg.get("rider_kg", 75), bike_kg=cfg.get("bike_kg", 8), effort=a.effort,
                       main_field=a.main, screen=tuple(a.screen.split(",")) if a.screen else pace.DEFAULT_SCREEN)
    rider.check()
    pts = route.thin(route.parse_gpx(open(a.gpx).read())) if a.gpx else synthetic_hilly()
    payload, report_json, report = pace.build_files(pts, rider)
    s = report["coach_report"]["race_summary"]
    print(f"{a.name}: {len(pts)} points, {s['total_distance_km']} km, {len(report['coach_report']['tactical_segments'])} "
          f"segments, {s['total_est_time']}, avg {s['avg_power_w']} W (IF {s['intensity_factor']}), "
          f"route {len(payload)} B in {len(build_chunks(payload))} chunk(s), report {len(report_json)} B in "
          f"{len(build_chunks(report_json))} chunk(s)")
    for seg in report["coach_report"]["tactical_segments"]:
        print(f"  {seg['segment']:2} {seg['distance_km']:6.2f} km {seg['avg_gradient_pct']:5.2f} % "
              f"{seg['target_power_w_low']}-{seg['target_power_w_high']} W {seg['est_speed_kmh']} km/h {seg['tactical_note']}")
    if not a.send:
        sys.exit("dry run OK, nothing sent (add --send to transmit)")
    with open(a.log, "w") as log:
        def line(text):
            print(text)
            log.write(text + "\n")
        try:
            asyncio.run(upload_pace(payload, report_json, a.name, log=line))
        except UploadError as e:
            print(f"ABORT: {e}")
            sys.exit(1)
    print("done - check the Pace Pilot list on the device")
