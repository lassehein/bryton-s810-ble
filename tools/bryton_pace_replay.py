"""Send a Pace Pilot route + plan to the S810 (replay of files captured from the app, or your own).

  PYTHONPATH=. python3 tools/bryton_pace_replay.py --name Test_Pace1 --route 01_type1a.bin --report 02_type1b.bin
  ... --send

--route is the gzip route file (kind 1a), --report the coach_report JSON (kind 1b), as written by
tools/pklg_extract.py. Only names starting with Test_ are accepted. Without --send nothing is transmitted.
"""
import argparse
import asyncio
import json
import sys

from bryton.ble import UploadError, upload_pace
from bryton.protocol import build_chunks

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--route", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--log", default="write.log")
    a = ap.parse_args()
    if not a.name.startswith("Test_") or len(a.name.encode()) > 32:
        sys.exit("name must start with Test_ and be at most 32 bytes")
    route, report = open(a.route, "rb").read(), open(a.report, "rb").read()
    if route[:2] != b"\x1f\x8b":
        sys.exit("route file must be gzip data")
    json.loads(report)  # must be valid JSON
    print(f"{a.name}: route {len(route)} B in {len(build_chunks(route))} chunk(s), "
          f"report {len(report)} B in {len(build_chunks(report))} chunk(s)")
    if not a.send:
        sys.exit("dry run OK, nothing sent (add --send to transmit)")
    with open(a.log, "w") as log:
        def line(text):
            print(text)
            log.write(text + "\n")
        try:
            asyncio.run(upload_pace(route, report, a.name, log=line))
        except UploadError as e:
            print(f"ABORT: {e}")
            sys.exit(1)
    print("done - check the Pace Pilot list on the device")
