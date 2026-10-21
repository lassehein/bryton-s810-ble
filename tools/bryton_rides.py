"""List and download recorded rides, or read the rider profile, from the S810 over Bluetooth (read-only).

  PYTHONPATH=. python3 tools/bryton_rides.py list
  PYTHONPATH=. python3 tools/bryton_rides.py profile      # FTP, max HR, LTHR, height, weight stored on the device
  PYTHONPATH=. python3 tools/bryton_rides.py profile --set ftp=225 [--set mhr=188 --set lthr=154] --write   # WRITES
  PYTHONPATH=. python3 tools/bryton_rides.py download --out ~/bryton_rides [--id 1792588357] [--log read.log]

Only rides the Bryton app has not fetched yet are in the device list. The app's acknowledgement (command 33)
is never sent, so the rides stay available for the app. Files are named <UTC start>_<id>.fit and contain
heart rate, power and position data: keep them out of repositories.
"""
import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

from bryton.ble import UploadError, download_rides, list_rides, read_profile, write_profile


def describe(e):
    start = datetime.fromtimestamp(e["id"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"{e['id']}  {start}  {e['moving_s'] // 60} min moving  {e['distance_m'] / 1000:.1f} km"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "download", "profile"])
    ap.add_argument("--out", default="rides")
    ap.add_argument("--id", type=int, action="append")
    ap.add_argument("--log", default=None)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="profile: ftp, mhr or lthr")
    ap.add_argument("--write", action="store_true", help="profile --set: really write (otherwise only show the change)")
    ap.add_argument("--preamble", action="store_true", help="play the app's 06 08 / 06 48 03 device-info exchange first")
    a = ap.parse_args()
    logf = open(a.log, "w", buffering=1) if a.log else None

    def line(text):
        if logf:
            logf.write(text + "\n")
    try:
        if a.cmd == "profile":
            changes = {k: int(v) for k, v in (item.split("=") for item in a.set)}
            if changes and a.write:
                r = asyncio.run(write_profile(changes, log=line))
                print("before:", {k: r["before"][k] for k in changes}, "after (read back):", {k: r["after"][k] for k in changes})
            else:
                p = asyncio.run(read_profile(log=line))
                print("\n".join(f"{k}: {v}" for k, v in p.items()))
                if changes:
                    print("would change:", {k: f"{p[k]} -> {v}" for k, v in changes.items()}, "(add --write)")
        elif a.cmd == "list":
            rides = asyncio.run(list_rides(log=line, preamble=a.preamble))
            print("\n".join(describe(e) for e in rides) or "no rides in the device list")
        else:
            rides = asyncio.run(download_rides(a.id, log=line, preamble=a.preamble, progress=lambda d, t: print(f"  {d}/{t} bytes", end="\r")))
            out = Path(a.out).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            for e, fit in rides:
                name = datetime.fromtimestamp(e["id"], timezone.utc).strftime("%Y%m%d-%H%M%S") + f"_{e['id']}.fit"
                (out / name).write_bytes(fit)
                print(f"{describe(e)} -> {out / name} ({len(fit)} bytes, FIT header {fit[8:12]!r})")
            if not rides:
                print("no rides downloaded (list empty or id not found)")
    except UploadError as e:
        sys.exit(f"ABORT: {e}")
