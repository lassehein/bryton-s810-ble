"""Query probe for the Bryton Rider S810 command channel (0x18).

Sends ONLY whitelisted info queries `XX 04 <sum>` and acks (`XX 0a`) the answers,
exactly like the app does. Never sends `XX 01` / `XX 02` frames, never touches
the file channel (0x0d). Aborts on the first error or disconnect.

usage: PYTHONPATH=. python3 tools/bryton_query_probe.py [--cmds 2f] [--wait 6] [--log probe18.log]
The log stays local: later queries answer with profile data (size, weight, birth date, zones).
"""
import argparse
import asyncio
import sys
import time

from bleak import BleakClient, BleakScanner

from bryton.protocol import (COMMAND_CHAR_UUID, CONTROL_CHAR_UUID, FILE_CHAR_UUID, HANDSHAKE,
                             ack_for, build_query)


async def find(name, address):
    if address:
        return await BleakScanner.find_device_by_address(address, timeout=15)
    needle = name.lower()
    return await BleakScanner.find_device_by_filter(
        lambda d, adv: needle in (adv.local_name or d.name or "").lower(), timeout=15)


async def main(cmds, wait, logpath, name, address):
    frames = [build_query(c) for c in cmds]  # validates the whitelist before connecting
    log = open(logpath, "w")
    t0 = time.time()
    disconnected = asyncio.Event()

    def emit(tag, data):
        line = f"{time.time() - t0:8.3f} {tag} {data.hex()}"
        print(line)
        log.write(line + "\n")
        log.flush()

    device = await find(name, address)
    if device is None:
        sys.exit("S810 not found: not advertising (still connected to phone/tablet, or USB mode?).")

    async with BleakClient(device, disconnected_callback=lambda _c: disconnected.set()) as client:
        async def send(uuid, tag, data):
            emit(f"TX {tag}", data)
            await client.write_gatt_char(uuid, data, response=True)

        def on_command(_c, data):
            data = bytes(data)
            emit("RX 0x18", data)
            ack = ack_for(data)
            if ack:
                asyncio.get_running_loop().create_task(send(COMMAND_CHAR_UUID, "0x18", ack))

        await client.start_notify(COMMAND_CHAR_UUID, on_command)
        await client.start_notify(CONTROL_CHAR_UUID, lambda _c, d: emit("RX 0x10", bytes(d)))
        await client.start_notify(FILE_CHAR_UUID, lambda _c, d: emit("RX 0x0d", bytes(d)))

        await send(CONTROL_CHAR_UUID, "0x10", HANDSHAKE)
        await asyncio.sleep(1)
        for frame in frames:
            if disconnected.is_set():
                sys.exit("disconnected, aborting")
            await send(COMMAND_CHAR_UUID, "0x18", frame)
            await asyncio.sleep(wait)
    log.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmds", default="2f", help="comma-separated hex command bytes, default 2f")
    ap.add_argument("--wait", type=float, default=6)
    ap.add_argument("--log", default="probe18.log")
    ap.add_argument("--name", default="rider")
    ap.add_argument("--address")
    a = ap.parse_args()
    asyncio.run(main([int(x, 16) for x in a.cmds.split(",")], a.wait, a.log, a.name, a.address))
