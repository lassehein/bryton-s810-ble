"""BLE upload of one workout (FIT + JSON) to the Bryton Rider S810.

Sequence as in the iPhone capture (bryton/protocol.py: plan_upload), verified on the device.
Raises UploadError on any failure; never leaves the device in transfer mode (46 01 00).
"""
import asyncio
import time

from bryton.protocol import (COMMAND_CHAR_UUID, CONTROL_CHAR_UUID, FILE_CHAR_UUID, HANDSHAKE, ack_for,
                             begin_transfer, build_query, build_transfer, end_transfer, plan_upload,
                             reassemble, reply_seen)

PACE = 0.002   # capture: ~1 ms between write-without-response packets
MIN_MTU = 153  # 150-byte packets + 3 ATT header bytes
UUIDS = {"0x0d": FILE_CHAR_UUID, "0x10": CONTROL_CHAR_UUID, "0x18": COMMAND_CHAR_UUID}


class UploadError(RuntimeError):
    pass


def transfers_for(fit, js):
    return dict(transfer_pairs([(fit, js)]))


def transfer_pairs(workouts):
    """[(fit, js), ...] -> [("fit", transfer), ("json", transfer), ...] in upload order."""
    out = []
    for fit, js in workouts:
        for kind, data in (("fit", fit), ("json", js)):
            t = build_transfer(data)
            if reassemble(*t) != data:
                raise UploadError(f"internal error: {kind} framing does not round-trip")
            out.append((kind, t))
    return out


async def find_device(devname="rider", address=None, timeout=15):
    from bleak import BleakScanner
    if address:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)
    needle = devname.lower()
    return await BleakScanner.find_device_by_filter(
        lambda d, adv: needle in (adv.local_name or d.name or "").lower(), timeout=timeout)


async def upload(fit, js, devname="rider", address=None, log=print, target="plan"):
    """Send FIT + JSON. target "plan" = workout list, "calendar" = calendar day from the JSON "plan"
    field. log(line) receives every TX/RX line. Returns on success, raises otherwise."""
    await upload_many([(fit, js)], devname, address, log, target)


async def upload_many(workouts, devname="rider", address=None, log=print, target="plan"):
    """Send several (fit, js) pairs in one session. A calendar session REPLACES the whole device
    calendar, so it must carry every planned day."""
    from bleak import BleakClient
    BEGIN_TRANSFER, END_TRANSFER = begin_transfer(target), end_transfer(target)
    steps = plan_upload(transfer_pairs(workouts), target)
    t0 = time.time()
    disconnected, handshake_ok = asyncio.Event(), asyncio.Event()
    seen = []

    def emit(tag, data):
        log(f"{time.time() - t0:8.3f} {tag} {data.hex()}")

    device = await find_device(devname, address)
    if device is None:
        raise UploadError("S810 not found: not advertising (connected to phone/tablet, or USB mode?)")

    async with BleakClient(device, disconnected_callback=lambda _c: disconnected.set()) as client:
        mtu = getattr(client, "mtu_size", None)
        log(f"MTU {mtu}")
        if mtu is not None and mtu < MIN_MTU:
            raise UploadError(f"ATT MTU {mtu} < {MIN_MTU}: nothing sent")

        async def write(ch, data, response):
            if disconnected.is_set():
                raise UploadError("disconnected")
            emit(f"TX {ch}", data)
            await client.write_gatt_char(UUIDS[ch], data, response=response)

        def on_notify(ch):
            def cb(_c, data):
                data = bytes(data)
                emit(f"RX {ch}", data)
                seen.append((ch, data))
                if ch == "0x10" and data == bytes.fromhex("082802"):
                    handshake_ok.set()
                if ch == "0x18" and (ack := ack_for(data)):
                    asyncio.get_running_loop().create_task(write("0x18", ack, True))
            return cb

        async def expect(name, timeout, mark):
            deadline = time.time() + timeout
            while time.time() < deadline:
                if reply_seen(seen, mark, name):
                    await asyncio.sleep(0.1)  # let the reactive ack go out first
                    return True
                await asyncio.sleep(0.02)
            return False

        for ch in ("0x18", "0x10", "0x0d"):
            await client.start_notify(UUIDS[ch], on_notify(ch))
        await write("0x10", HANDSHAKE, True)
        try:
            await asyncio.wait_for(handshake_ok.wait(), 5)
        except asyncio.TimeoutError:
            raise UploadError("no handshake reply (08 28 02), nothing sent") from None
        await write("0x18", build_query(0x2F), True)
        await asyncio.sleep(1.5)

        begun, end_sent, failed = False, False, None
        try:
            for ch, data, response, exp in steps:
                mark = len(seen)  # before the write: the reply can arrive while the write is awaited
                await write(ch, data, response)
                begun = begun or data == BEGIN_TRANSFER
                end_sent = end_sent or data == END_TRANSFER
                if not response:
                    await asyncio.sleep(PACE)
                if exp and not await expect(exp, 6 if exp == "done" else 3, mark):
                    failed = f"no reply '{exp}' after {data.hex()}"
                    break
        finally:
            if begun and not end_sent and not disconnected.is_set():
                mark = len(seen)
                await write("0x18", END_TRANSFER, True)
                await expect("ack46", 3, mark)
        await asyncio.sleep(2)
    if failed:
        raise UploadError(f"device rejected or did not answer: {failed}")
