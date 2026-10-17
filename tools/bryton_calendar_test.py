"""Upload several calendar workouts in ONE session (a calendar session replaces the whole calendar).

  PYTHONPATH=. python3 tools/bryton_calendar_test.py SPEC [SPEC ...]          # dry run
  PYTHONPATH=. python3 tools/bryton_calendar_test.py SPEC [SPEC ...] --send
Each spec (reference/specs/) needs a Test_ name and exactly one plan date.
"""
import argparse
import asyncio
import json
import sys

from bryton.ble import UploadError, transfer_pairs, upload_many
from bryton.fitgen import build_files, resolve_plan
from bryton.protocol import plan_upload
from bryton.workout import check_name, verify


def build(path):
    spec = json.load(open(path))
    check_name(spec["name"])
    plan = resolve_plan(spec.get("plan"))
    if len(plan) != 1:
        sys.exit(f"{path}: need exactly one plan date")
    steps = [dict(st, **{k: tuple(st[k]) for k in ("power", "cadence") if st.get(k)}) for st in spec["steps"]]
    fix = spec.get("cadence_fix") or {}
    fit, js, _ = build_files(spec["name"], steps, description=spec.get("description", ""), plan=plan,
                             start_seconds=fix.get("dummy_seconds", 5))
    print(f"{verify(fit, js)}: plan {plan[0]}, FIT {len(fit)} B, JSON {len(js)} B")
    return fit, js


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("specs", nargs="+")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--log", default="write.log")
    a = ap.parse_args()
    workouts = [build(p) for p in a.specs]
    print(f"{len(plan_upload(transfer_pairs(workouts), 'calendar'))} planned frames, target calendar")
    if not a.send:
        sys.exit(print("dry run OK, nothing sent (add --send to transmit)"))
    with open(a.log, "w") as log:
        def line(text):
            print(text)
            log.write(text + "\n")
        try:
            asyncio.run(upload_many(workouts, log=line, target="calendar"))
        except UploadError as e:
            print(f"ABORT: {e}")
            sys.exit(1)
    print("done - check the device over USB")
