"""Send a test route to the S810 over Bluetooth (route list: Courses > Route on the device).

  PYTHONPATH=. python3 tools/bryton_route_test.py --name Test_Route1            # dry run, synthetic loop
  PYTHONPATH=. python3 tools/bryton_route_test.py --name Test_Route1 --gpx my.gpx --send

Only names starting with Test_ are accepted. Without --send nothing is transmitted.
"""
import argparse
import asyncio
import math
import sys

from bryton import route
from bryton.ble import UploadError, upload_route
from bryton.protocol import build_chunks


def synthetic_loop(n=120, lat0=47.0, lon0=8.0, radius_m=300):
    """A small circular loop; lat/lon only used for testing, hilly enough to exercise grade and altitude."""
    dlat = radius_m / 111195
    dlon = radius_m / (111195 * math.cos(math.radians(lat0)))
    return [(lat0 + dlat * math.sin(2 * math.pi * i / n), lon0 + dlon * math.cos(2 * math.pi * i / n),
             420 + 12 * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--gpx")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--log", default="write.log")
    a = ap.parse_args()
    if not a.name.startswith("Test_") or len(a.name.encode()) > 32:
        sys.exit("name must start with Test_ and be at most 32 bytes")
    pts = route.thin(route.parse_gpx(open(a.gpx).read())) if a.gpx else synthetic_loop()
    fit = route.encode_fit(pts, markers=("Start", "Ziel"))
    payload = route.to_transfer_payload(fit)
    d = route.decode_fit(fit)
    chunks = build_chunks(payload)
    print(f"{a.name}: {len(pts)} points, {d[254][0][6]} m, FIT {len(fit)} B, gzip {len(payload)} B, {len(chunks)} chunk(s)")
    if not a.send:
        sys.exit("dry run OK, nothing sent (add --send to transmit)")
    with open(a.log, "w") as log:
        def line(text):
            print(text)
            log.write(text + "\n")
        try:
            asyncio.run(upload_route(payload, a.name, log=line))
        except UploadError as e:
            print(f"ABORT: {e}")
            sys.exit(1)
    print("done - check Courses > Route on the device")
