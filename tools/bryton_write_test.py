"""Upload a workout (FIT + JSON) to the S810 over BLE, exactly like the app does.

Modes
  --replay          send the captured app workout byte-exact (name "Intervalltraining").
                    The files are identical to what is already on the device, so success shows up
                    as new modification times, not as new content.
  --name Test_XXXX  send the reference workout renamed; only Test_… names are accepted.
  --spec FILE       generate FIT+JSON from a step list (bryton/fitgen.py); only Test_… names for now.

Nothing is sent without --send (dry run: builds, verifies and prints the plan, no Bluetooth).
The sequence is the one seen in the iPhone capture (reference/capture_transfer_window.txt):
  46 04 -> 46 begin -> per file: 10 00 <k>, 10 40 05 01 f4, 0x0d start/data/end -> 46 end.
If a reply is missing the script stops and still sends the 46 end frame.

  python3 tools/bryton_write_test.py --replay
  PYTHONPATH=. python3 tools/bryton_write_test.py --replay --send
  PYTHONPATH=. python3 tools/bryton_write_test.py --name Test_BLE_Interv01 --send
"""
import argparse
import asyncio
import hashlib
import json
import sys

from bryton.ble import UploadError, transfers_for, upload
from bryton.protocol import plan_upload
from bryton.fitgen import build_files, encode_fit, encode_json, resolve_plan
from bryton.workout import check_name, make_test_fit, make_test_json, reference_files, verify

REPLAY_SHA256 = {  # hashes of the files already on the device (Intervalltraining.*)
    "fit": "42f32597ccdee36df368e392d33b550112bb9bdf724fbe1eeee3c84fa188eb2a",
    "json": "fbcb62f294680d30606cfe35386400f6677bf74e51d64900ad2d5c81cf9fb37c",
}


def build(args):
    if args.replay:
        fit, js = reference_files()
        for kind, data in (("fit", fit), ("json", js)):
            if hashlib.sha256(data).hexdigest() != REPLAY_SHA256[kind]:
                sys.exit(f"reference {kind} differs from the file on the device, refusing to replay")
        label = "replay Intervalltraining"
    elif args.spec:
        spec = json.load(open(args.spec))
        steps = [dict(st, **{k: tuple(st[k]) for k in ("power", "cadence") if st.get(k)})
                 for st in spec["steps"]]
        check_name(spec["name"])
        if spec.get("json_leading"):  # old experiment (Test_CadPre), no correction
            fit = encode_fit(spec["name"][:15], steps)
            js = encode_json(spec["name"], steps, description=spec.get("description", ""),
                             leading=tuple(spec["json_leading"]), plan=resolve_plan(spec.get("plan")))
        else:
            fix = spec.get("cadence_fix") or {}
            fit, js, steps = build_files(spec["name"], steps, description=spec.get("description", ""),
                                         plan=resolve_plan(spec.get("plan")),
                                         correct_cadence=spec.get("correct_cadence", False),
                                         high_first=spec.get("high_first", True),
                                         start_seconds=fix.get("dummy_seconds", 5),
                                         start_low=fix.get("dummy_low"))
        label = verify(fit, js)
        print(f"generated {label}: FIT {len(fit)} B, JSON {len(js)} B, {len(steps)} steps, plan {json.loads(js)['plan']}")
    else:
        fit, js = make_test_fit(args.name), make_test_json(args.name)
        label = verify(fit, js)
    return label, fit, js


def send(fit, js, logpath, devname, address, target="plan"):
    with open(logpath, "w") as log:
        def line(text):
            print(text)
            log.write(text + "\n")
            log.flush()
        try:
            asyncio.run(upload(fit, js, devname=devname, address=address, log=line, target=target))
        except UploadError as e:
            print(f"ABORT: {e}")
            print("ABORTED - check the device over USB")
            sys.exit(1)
    print("done - check the device over USB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--replay", action="store_true")
    g.add_argument("--name", help="reference workout renamed; name must start with Test_")
    g.add_argument("--spec", help="JSON workout spec (see reference/specs/); name must start with Test_")
    ap.add_argument("--send", action="store_true", help="actually transmit (default: dry run)")
    ap.add_argument("--log", default="write.log")
    ap.add_argument("--devname", default="rider")
    ap.add_argument("--address")
    a = ap.parse_args()
    label, fit, js = build(a)
    plan = json.loads(js).get("plan") or []
    target = "calendar" if plan else "plan"  # JSON with a date -> calendar (capture-calendar)
    if len(plan) > 1:
        sys.exit("calendar upload: one date per upload")
    transfers = transfers_for(fit, js)
    steps = plan_upload(transfers, target)
    for kind, (start, packets, end) in transfers.items():
        print(f"{kind}: start={start.hex()} packets={len(packets)} end={end.hex()}")
    print(f"{label}: {len(steps)} planned frames, target {target}")
    if a.send:
        send(fit, js, a.log, a.devname, a.address, target)
    else:
        print("dry run OK, nothing sent (add --send to transmit)")
