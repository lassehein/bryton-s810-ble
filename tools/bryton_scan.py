"""Passive BLE scan: lists advertisers, never connects.

Marks candidates for the S810: name contains "rider"/"bryton", or an
advertised service UUID shares the Bryton base (454d....-a122-058d-...).

usage: python3 tools/bryton_scan.py [seconds] [--all] [--show-addr]
Without --all only candidates and unnamed devices with service UUIDs are shown.
macOS shows a per-Mac UUID instead of the MAC address; it is masked by default.
"""
import argparse
import asyncio

from bleak import BleakScanner

NAME_HINTS = ("rider", "bryton")
UUID_HINTS = ("-a122-058d-", "-c2de-a994-")


def is_candidate(name, services):
    n = (name or "").lower()
    return any(h in n for h in NAME_HINTS) or any(h in s.lower() for s in services for h in UUID_HINTS)


async def main(seconds, show_all, show_addr):
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = sorted(found.values(), key=lambda da: -(da[1].rssi or -999))
    for dev, adv in rows:
        name = adv.local_name or dev.name
        services = list(adv.service_uuids or [])
        cand = is_candidate(name, services)
        if not (show_all or cand or (not name and services)):
            continue
        addr = dev.address if show_addr else "…" + dev.address[-4:]
        mfr = " ".join(f"0x{cid:04x}:{data.hex()}" for cid, data in (adv.manufacturer_data or {}).items())
        print(f"{'*' if cand else ' '} {addr} rssi={adv.rssi} name={name!r} services={services} mfr={mfr}")
    print(f"{len(rows)} devices seen, * = S810 candidate")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("seconds", nargs="?", type=float, default=15)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--show-addr", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.seconds, a.all, a.show_addr))
