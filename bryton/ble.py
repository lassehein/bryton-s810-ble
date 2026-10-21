"""BLE upload of workouts (FIT + JSON) and routes to the Bryton Rider S810.

Sequences as in the iPhone captures (bryton/protocol.py: plan_upload, plan_route_upload).
Raises UploadError on any failure; never leaves the device in transfer mode (46 01 00).
"""
import asyncio
import contextlib
import time
import types

from bryton.protocol import (COMMAND_CHAR_UUID, CONTROL_CHAR_UUID, FILE_CHAR_UUID, HANDSHAKE, MAX_READ,
                             BODY_CMD, RESTING_CMD, INFO_GO, INFO_REQUEST, PROFILE_SCALARS, READ_GO, RIDE_LIST_EMPTY, RIDE_LIST_GO, RIDE_LIST_REQUEST, TransferReceiver, ack_for,
                             begin_transfer, build_chunks, build_query, command, build_transfer, end_transfer,
                             parse_body_item, parse_profile_scalar, parse_resting_hr, profile_write_frames, resting_write_frame, parse_profile_zones, parse_ride_list, plan_pace_upload, plan_route_upload, plan_upload, read_request,
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
    await run_steps(plan_upload(transfer_pairs(workouts), target), devname, address, log,
                    begin=begin_transfer(target), end=end_transfer(target))


async def upload_route(payload, name, devname="rider", address=None, log=print):
    """Send a route (gzip-compressed route FIT, bryton/route.py) and its name. No begin/end frames."""
    await run_steps(plan_route_upload(build_chunks(payload), name), devname, address, log)


async def upload_pace(route_payload, report_json, name, devname="rider", address=None, log=print):
    """Send a route with a Pace Pilot plan: route (gzip FIT, kind 1a), the name, the coach report (JSON, kind 1b)."""
    steps = plan_pace_upload(build_chunks(route_payload), build_chunks(report_json), name)
    await run_steps(steps, devname, address, log)


@contextlib.asynccontextmanager
async def _session(devname, address, log, preamble=False):
    """Connect, enable notifications, handshake, `2f 04`. Yields an object with write(), expect(), seen, rx0d.

    preamble=True also plays the app's `06 08` / `06 48 03` device-info exchange before returning."""
    from bleak import BleakClient
    t0 = time.time()
    disconnected, handshake_ok = asyncio.Event(), asyncio.Event()
    seen = []
    rx0d = asyncio.Queue()

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
                if ch == "0x0d":
                    rx0d.put_nowait(data)
                    if len(data) < 20:  # packet bodies are not logged
                        emit(f"RX {ch}", data)
                    return
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
                if disconnected.is_set():
                    return False
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
        s = types.SimpleNamespace(write=write, expect=expect, seen=seen, rx0d=rx0d, disconnected=disconnected)
        if preamble:
            await _receive(s, INFO_REQUEST, "info_ack", INFO_GO, timeout=10)
        yield s


async def run_steps(steps, devname, address, log, begin=None, end=None):
    """Connect, handshake and play `steps`; if begin was sent but not end, send end before leaving."""
    BEGIN_TRANSFER, END_TRANSFER = begin, end
    async with _session(devname, address, log) as s:
        begun, end_sent, failed = False, False, None
        try:
            for ch, data, response, exp in steps:
                mark = len(s.seen)  # before the write: the reply can arrive while the write is awaited
                await s.write(ch, data, response)
                begun = begun or (BEGIN_TRANSFER is not None and data == BEGIN_TRANSFER)
                end_sent = end_sent or (END_TRANSFER is not None and data == END_TRANSFER)
                if not response:
                    await asyncio.sleep(PACE)
                if exp and not await s.expect(exp, 6 if exp == "done" else 3, mark):
                    failed = f"no reply '{exp}' after {data.hex()}"
                    break
        finally:
            if begun and not end_sent and not s.disconnected.is_set():
                mark = len(s.seen)
                await s.write("0x18", END_TRANSFER, True)
                await s.expect("ack46", 3, mark)
        await asyncio.sleep(2)
    if failed:
        raise UploadError(f"device rejected or did not answer: {failed}")


async def _receive(s, request, reply, go, timeout=45, empty=None):
    """Send `request` on 0x10, wait for `reply`, send `go`, collect one transfer from 0x0d; returns its body.
    If the device answered with `empty` instead, nothing is requested and None is returned."""
    while not s.rx0d.empty():  # leftovers of an earlier, failed transfer
        s.rx0d.get_nowait()
    mark = len(s.seen)
    await s.write("0x10", request, True)
    if not await s.expect(reply, 3, mark):
        got = ", ".join(f"{ch}:{d.hex()}" for ch, d in s.seen[mark:][:6]) or "nothing"
        raise UploadError(f"no reply '{reply}' after {request.hex()} (received: {got})")
    if empty is not None and any(c == "0x10" and d == empty for c, d in s.seen[mark:]):
        return None
    await s.write("0x10", go, True)
    rx = TransferReceiver()
    deadline = time.time() + timeout
    while True:
        try:
            value = await asyncio.wait_for(s.rx0d.get(), max(0.1, deadline - time.time()))
        except asyncio.TimeoutError:
            raise UploadError("transfer from the device timed out") from None
        try:
            body = rx.feed(value)
        except ValueError as e:
            raise UploadError(f"invalid transfer from the device: {e}") from None
        if body is not None:
            return body


async def list_rides(devname="rider", address=None, log=print, preamble=False):
    """Read-only: rides recorded on the device that the app has not fetched yet. [{"id", "moving_s", "distance_m"}]"""
    async with _session(devname, address, log, preamble) as s:
        body = await _receive(s, RIDE_LIST_REQUEST, "list_ack", RIDE_LIST_GO, empty=RIDE_LIST_EMPTY)
        return parse_ride_list(body) if body else []


async def download_rides(ride_ids=None, devname="rider", address=None, log=print, progress=None, preamble=False):
    """Read-only: fetch rides as FIT files. ride_ids=None fetches every ride in the device list. Never sends
    the app's `33` acknowledgement, so the rides stay in the device list for the app's own sync.
    Returns [(entry, fit_bytes)]. progress(done_bytes, total_bytes) is optional."""
    import gzip
    out = []
    async with _session(devname, address, log, preamble) as s:
        body = await _receive(s, RIDE_LIST_REQUEST, "list_ack", RIDE_LIST_GO, empty=RIDE_LIST_EMPTY)
        entries = parse_ride_list(body) if body else []
        if ride_ids is not None:
            entries = [e for e in entries if e["id"] in set(ride_ids)]
        for e in entries:
            first = await _receive(s, read_request(e["id"], 0, 4), "read_ack", READ_GO)
            if len(first) < 4:
                raise UploadError(f"ride {e['id']}: size reply too short")
            total = int.from_bytes(first[:4], "big")
            data = b""
            while len(data) < total:
                n = min(MAX_READ, total - len(data))
                part = await _receive(s, read_request(e["id"], len(data), n), "read_ack", READ_GO)
                if len(part) != n:
                    raise UploadError(f"ride {e['id']}: expected {n} bytes at offset {len(data)}, got {len(part)}")
                data += part
                if progress:
                    progress(len(data), total)
            if data[:2] == b"\x1f\x8b":  # long rides arrive gzip-compressed, a short one (1858 bytes) as a plain FIT file
                try:
                    data = gzip.decompress(data)
                except (OSError, EOFError) as err:
                    raise UploadError(f"ride {e['id']}: not valid gzip data ({err})") from None
            if data[8:12] != b".FIT":
                raise UploadError(f"ride {e['id']}: not a FIT file (starts with {data[:12].hex()})")
            out.append((e, data))
        await asyncio.sleep(1)
    return out


async def _ask(s, frame, match, timeout=3):
    """Write a 0x18 frame and return the first reply on 0x18 for which match(reply) holds."""
    mark = len(s.seen)
    await s.write("0x18", frame, True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for ch, d in s.seen[mark:]:
            if ch == "0x18" and match(d):
                await asyncio.sleep(0.1)  # let the reactive ack go out first
                return d
        if s.disconnected.is_set():
            break
        await asyncio.sleep(0.02)
    got = ", ".join(f"{ch}:{d.hex()}" for ch, d in s.seen[mark:][:6]) or "nothing"
    raise UploadError(f"no reply to {frame.hex()} (received: {got})")


async def _read_profile(s):
    out = {}
    for cmd, key in PROFILE_SCALARS.items():
        await _ask(s, command(cmd, 0x04), lambda d, c=cmd: d[:2] == bytes([c, 0x05]))
        first = await _ask(s, command(cmd, 0x02, 0), lambda d, c=cmd: d[:3] == bytes([c, 0x01, 0]))
        zones = await _ask(s, command(cmd, 0x02, 1), lambda d, c=cmd: d[:3] == bytes([c, 0x01, 1]))
        try:
            out[key], out[key + "_zones"] = parse_profile_scalar(first), parse_profile_zones(zones)
        except ValueError as e:
            raise UploadError(str(e)) from None
    await _ask(s, command(BODY_CMD, 0x04), lambda d: d[:2] == bytes([BODY_CMD, 0x05]))
    for item, key in ((0, "height_cm"), (1, "weight_kg")):
        reply = await _ask(s, command(BODY_CMD, 0x02, 0, item),
                           lambda d, i=item: d[:4] == bytes([BODY_CMD, 0x01, 0, i]) and len(d) >= 13)
        try:
            out[key] = parse_body_item(reply)[0]
        except ValueError as e:
            raise UploadError(str(e)) from None
    try:  # resting heart rate (command 5a); optional: a device without it only costs the short wait
        await _ask(s, command(RESTING_CMD, 0x04), lambda d: d[:2] == bytes([RESTING_CMD, 0x05]), timeout=2)
        reply = await _ask(s, command(RESTING_CMD, 0x02), lambda d: d[:2] == bytes([RESTING_CMD, 0x01]), timeout=2)
        out["resting_hr"] = parse_resting_hr(reply)[0]
    except (UploadError, ValueError):
        out["resting_hr"] = None
    return out


async def read_profile(devname="rider", address=None, log=print):
    """Read-only: FTP, max HR, LTHR (with zone limits), height and weight stored on the device.
    -> {"ftp", "mhr", "lthr", "ftp_zones", "mhr_zones", "lthr_zones", "height_cm", "weight_kg"}"""
    async with _session(devname, address, log) as s:
        out = await _read_profile(s)
        await asyncio.sleep(0.5)
    return out


PROFILE_LIMITS = {"ftp": (50, 600), "mhr": (120, 230), "lthr": (80, 220), "resting_hr": (30, 120)}


async def write_profile(changes, devname="rider", address=None, log=print):
    """Change FTP / max HR / LTHR / resting HR on the device. changes = {"ftp": 230, ...}. In one session: read the profile,
    send the two frames of the app per changed value (zone limits unchanged), read back and check.
    Returns {"before": profile, "after": profile}. Raises UploadError if the readback differs."""
    for key, value in changes.items():
        if key not in PROFILE_LIMITS or not PROFILE_LIMITS[key][0] <= value <= PROFILE_LIMITS[key][1]:
            raise UploadError(f"{key}={value}: only ftp {PROFILE_LIMITS['ftp']}, mhr {PROFILE_LIMITS['mhr']}, "
                              f"lthr {PROFILE_LIMITS['lthr']}, resting_hr {PROFILE_LIMITS['resting_hr']} can be written")
    async with _session(devname, address, log) as s:
        before = await _read_profile(s)
        if {**before, **changes}["lthr"] >= {**before, **changes}["mhr"]:
            raise UploadError("lthr must be below max HR")
        todo = {key: v for key, v in changes.items() if v != before.get(key)}
        for cmd, key in PROFILE_SCALARS.items():
            if key in todo:
                for frame in profile_write_frames(cmd, todo[key], before[key + "_zones"]):
                    await _ask(s, frame, lambda d, c=cmd: d == bytes([c, 0x0A]))
        if "resting_hr" in todo:
            await _ask(s, resting_write_frame(todo["resting_hr"], time.time()),
                       lambda d: d == bytes([RESTING_CMD, 0x0A]))
        after = await _read_profile(s) if todo else before
        await asyncio.sleep(0.5)
    for key, value in todo.items():
        if after[key] != value:
            raise UploadError(f"readback of {key} is {after[key]}, expected {value}")
    return {"before": before, "after": after}
