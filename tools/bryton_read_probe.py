"""Read-only BLE probe for the Bryton Rider S810.

Connects, subscribes to the three notify characteristics, sends only the
handshake on the control characteristic, and logs every notification for a
fixed time. Writes nothing to the file channel -> cannot touch Plan files.

usage: python3 tools/bryton_read_probe.py [seconds] [logfile] [--name SUBSTR] [--address ADDR]
  --name     case-insensitive substring of the advertised name (default "rider")
  --address  connect by address/macOS UUID from bryton_scan.py --show-addr
"""
import argparse
import asyncio
import sys
import time

from bleak import BleakClient, BleakScanner

from bryton.protocol import COMMAND_CHAR_UUID, CONTROL_CHAR_UUID, FILE_CHAR_UUID, HANDSHAKE



async def find(name, address):
    if address:
        return await BleakScanner.find_device_by_address(address, timeout=15)
    needle = name.lower()
    return await BleakScanner.find_device_by_filter(
        lambda d, adv: needle in (adv.local_name or d.name or "").lower(), timeout=15)


async def main(seconds, logpath, name, address):
    log = open(logpath, "w")
    t0 = time.time()

    def emit(tag, data):
        line = f"{time.time() - t0:8.3f} {tag} {data.hex()}  {data!r}"
        print(line)
        log.write(line + "\n")
        log.flush()

    device = await find(name, address)
    if device is None:
        sys.exit("S810 not found: not advertising. It is probably still connected to a phone/tablet "
                 "(Bryton Active) or in USB mode. Run tools/bryton_scan.py --all to compare.")
    emit("INFO", (device.name or "").encode())

    async with BleakClient(device) as client:
        for tag, uuid in (("0x0d", FILE_CHAR_UUID), ("0x10", CONTROL_CHAR_UUID), ("0x18", COMMAND_CHAR_UUID)):
            await client.start_notify(uuid, lambda _c, d, tag=tag: emit(f"RX {tag}", bytes(d)))
        emit("TX 0x10", HANDSHAKE)
        await client.write_gatt_char(CONTROL_CHAR_UUID, HANDSHAKE, response=True)
        await asyncio.sleep(seconds)
    log.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("seconds", nargs="?", type=float, default=15)
    ap.add_argument("logfile", nargs="?", default="probe.log")
    ap.add_argument("--name", default="rider")
    ap.add_argument("--address")
    a = ap.parse_args()
    asyncio.run(main(a.seconds, a.logfile, a.name, a.address))
