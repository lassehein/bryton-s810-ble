"""Analyse a PacketLogger capture (.pklg) without Bluetooth hardware.

  adv   LE advertising reports: name, service UUIDs, manufacturer data
  gatt  primary services and characteristics discovered by the phone
  att   ATT traffic (writes/notifies), optionally for one handle

usage:
  python3 tools/pklg_analyze.py adv  capture.pklg [--name Rider] [--show-addr]
  python3 tools/pklg_analyze.py gatt capture.pklg
  python3 tools/pklg_analyze.py att  capture.pklg [--handle 0x0d] [--from-start]
  python3 tools/pklg_analyze.py cmds capture.pklg [--baseline base.pklg]

cmds summarises the 0x18 command channel by (command, sub-command, direction). With --baseline,
combinations missing in the baseline are marked NEW and shown with sample payloads (profile data of
already known commands is never printed).

--transfer-window prints every ATT frame (all handles) from 2 s before the first file-transfer
start packet to 2 s after the last end packet; long 0x0d data frames are shown as "data" only.
--from-start skips everything before the first file-transfer start packet
(7 bytes, 00 00 00 ..), so the device info packet (contains the device UUID)
is not printed.

Addresses are masked by default so the output can be pasted into the repo.
"""
import argparse
import struct
import uuid as uuidlib

PKLG_HCI_CMD, PKLG_HCI_EVT, PKLG_ACL_TX, PKLG_ACL_RX = 0x00, 0x01, 0x02, 0x03

ATT_NAMES = {
    0x01: "ErrRsp", 0x02: "MTUReq", 0x03: "MTURsp", 0x08: "ReadByTypeReq", 0x09: "ReadByTypeRsp",
    0x0A: "ReadReq", 0x0B: "ReadRsp", 0x10: "ReadByGroupReq", 0x11: "ReadByGroupRsp",
    0x12: "WriteReq", 0x13: "WriteRsp", 0x1B: "Notify", 0x1D: "Indicate", 0x52: "WriteCmd",
}


def read_records(data):
    """Yield (seconds, usecs, type, payload). Detects little- or big-endian files."""
    endian = "<"
    if len(data) >= 4:
        le = struct.unpack("<I", data[:4])[0]
        be = struct.unpack(">I", data[:4])[0]
        if not (9 <= le <= len(data) - 4) and 9 <= be <= len(data) - 4:
            endian = ">"
    off = 0
    while off + 4 <= len(data):
        length = struct.unpack(endian + "I", data[off:off + 4])[0]
        if length < 9 or off + 4 + length > len(data):
            break
        body = data[off + 4:off + 4 + length]
        secs, usecs = struct.unpack(endian + "II", body[:8])
        yield secs, usecs, body[8], body[9:]
        off += 4 + length


def uuid_from_le(raw):
    if len(raw) == 2:
        return f"{struct.unpack('<H', raw)[0]:04x}"
    if len(raw) == 4:
        return f"{struct.unpack('<I', raw)[0]:08x}"
    return str(uuidlib.UUID(bytes=bytes(reversed(raw))))


def parse_ad(data):
    """Parse advertising data into a dict of readable fields."""
    out = {"name": None, "services": [], "manufacturer": [], "service_data": [], "other": []}
    i = 0
    while i < len(data):
        n = data[i]
        if n == 0 or i + 1 + n > len(data):
            break
        typ, val = data[i + 1], data[i + 2:i + 1 + n]
        if typ in (0x08, 0x09):
            out["name"] = val.decode("utf-8", "replace")
        elif typ in (0x02, 0x03):
            out["services"] += [uuid_from_le(val[j:j + 2]) for j in range(0, len(val) - 1, 2)]
        elif typ in (0x06, 0x07):
            out["services"] += [uuid_from_le(val[j:j + 16]) for j in range(0, len(val) - 15, 16)]
        elif typ == 0xFF and len(val) >= 2:
            out["manufacturer"].append((struct.unpack("<H", val[:2])[0], val[2:].hex()))
        elif typ in (0x16, 0x20, 0x21):
            size = {0x16: 2, 0x20: 4, 0x21: 16}[typ]
            out["service_data"].append((uuid_from_le(val[:size]), val[size:].hex()))
        elif typ != 0x01:  # flags are uninteresting
            out["other"].append((typ, val.hex()))
        i += 1 + n
    return out


def adv_reports(event):
    """Yield (addr_bytes, adv_data, rssi) from one HCI LE Meta event (legacy or extended)."""
    if len(event) < 4 or event[0] != 0x3E:
        return
    sub, params = event[2], event[3:]
    if sub == 0x02:  # LE Advertising Report
        num, pos = params[0], 1
        for _ in range(num):
            addr = params[pos + 2:pos + 8]
            dlen = params[pos + 8]
            data = params[pos + 9:pos + 9 + dlen]
            rssi = struct.unpack("b", params[pos + 9 + dlen:pos + 10 + dlen])[0]
            yield addr, data, rssi
            pos += 10 + dlen
    elif sub == 0x0D:  # LE Extended Advertising Report
        num, pos = params[0], 1
        for _ in range(num):
            addr = params[pos + 3:pos + 9]
            rssi = struct.unpack("b", params[pos + 13:pos + 14])[0]
            dlen = params[pos + 23]
            data = params[pos + 24:pos + 24 + dlen]
            yield addr, data, rssi
            pos += 24 + dlen


def att_frames(records):
    """Yield (t, direction, att_pdu) for ATT (CID 4) frames in ACL records."""
    for secs, usecs, typ, p in records:
        if typ not in (PKLG_ACL_TX, PKLG_ACL_RX) or len(p) < 8:
            continue
        hlen = struct.unpack("<H", p[2:4])[0]
        acl = p[4:4 + hlen]
        if len(acl) < 4:
            continue
        l2len, cid = struct.unpack("<HH", acl[:4])
        if cid == 4:
            yield secs + usecs / 1e6, "TX" if typ == PKLG_ACL_TX else "RX", acl[4:4 + l2len]


def gatt_table(frames):
    """Return (services, characteristics) seen in discovery responses."""
    services, chars = [], []
    for _, _, pdu in frames:
        if not pdu:
            continue
        if pdu[0] == 0x11 and len(pdu) > 2:  # ReadByGroupRsp: start, end, uuid
            size = pdu[1]
            for j in range(2, len(pdu) - size + 1, size):
                start, end = struct.unpack("<HH", pdu[j:j + 4])
                services.append((start, end, uuid_from_le(pdu[j + 4:j + size])))
        elif pdu[0] == 0x09 and len(pdu) > 2:  # ReadByTypeRsp: char declarations
            size = pdu[1]
            if size not in (7, 21):
                continue
            for j in range(2, len(pdu) - size + 1, size):
                decl, props, value_handle = struct.unpack("<HBH", pdu[j:j + 5])
                chars.append((decl, value_handle, props, uuid_from_le(pdu[j + 5:j + size])))
    return sorted(set(services)), sorted(set(chars))


def command_frames(records):
    """Yield (t, direction, value) for frames on the 0x18 command characteristic."""
    for t, direction, pdu in att_frames(records):
        if len(pdu) >= 5 and pdu[0] in (0x12, 0x52, 0x1B) and struct.unpack("<H", pdu[1:3])[0] == 0x18:
            yield t, direction, pdu[3:]


def summarize_commands(records):
    """{(cmd, sub, dir): [values]} for all 0x18 frames."""
    out = {}
    for _, direction, value in command_frames(records):
        out.setdefault((value[0], value[1], direction), []).append(value)
    return out


def mask(addr, show):
    s = ":".join(f"{b:02x}" for b in reversed(addr))
    return s if show else "xx:xx:xx:xx:" + s[-5:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["adv", "gatt", "att", "cmds"])
    ap.add_argument("pklg")
    ap.add_argument("--name", help="only adv reports whose name contains this (case-insensitive)")
    ap.add_argument("--show-addr", action="store_true")
    ap.add_argument("--handle", type=lambda s: int(s, 0))
    ap.add_argument("--from-start", action="store_true")
    ap.add_argument("--transfer-window", action="store_true")
    ap.add_argument("--baseline", help="cmds: compare against this capture")
    a = ap.parse_args()
    records = list(read_records(open(a.pklg, "rb").read()))

    if a.cmd == "adv":
        seen = {}
        for _, _, typ, p in records:
            if typ != PKLG_HCI_EVT:
                continue
            for addr, data, rssi in adv_reports(p):
                ad = parse_ad(data)
                entry = seen.setdefault(addr, {"names": set(), "services": set(), "mfr": set(),
                                               "sdata": set(), "count": 0, "rssi": rssi})
                entry["count"] += 1
                if ad["name"]:
                    entry["names"].add(ad["name"])
                entry["services"].update(ad["services"])
                entry["mfr"].update(ad["manufacturer"])
                entry["sdata"].update(ad["service_data"])
        for addr, e in seen.items():
            if a.name and not any(a.name.lower() in n.lower() for n in e["names"]):
                continue
            print(f"{mask(addr, a.show_addr)} n={e['count']} rssi={e['rssi']} names={sorted(e['names'])}")
            for s in sorted(e["services"]):
                print(f"    service {s}")
            for cid, hexdata in sorted(e["mfr"]):
                print(f"    manufacturer 0x{cid:04x} {hexdata}")
            for s, hexdata in sorted(e["sdata"]):
                print(f"    service-data {s} {hexdata}")
        print(f"{len(records)} records, {len(seen)} advertisers")

    elif a.cmd == "cmds":
        mine = summarize_commands(records)
        base = None
        if a.baseline:
            base = summarize_commands(list(read_records(open(a.baseline, "rb").read())))
        base_cmds = {k[0] for k in base} if base is not None else set()
        for (cmd, sub, direction), values in sorted(mine.items()):
            new_cmd = base is not None and cmd not in base_cmds
            new_combo = base is not None and (cmd, sub, direction) not in base
            tag = " NEW COMMAND" if new_cmd else (" NEW" if new_combo else "")
            print(f"cmd=0x{cmd:02x} sub=0x{sub:02x} {direction} n={len(values)} lens={sorted({len(v) for v in values})}{tag}")
            if new_cmd or new_combo:
                for v in values[:3]:
                    print(f"    {v[:40].hex()}")
        print(f"{len(mine)} combinations, {sum(len(v) for v in mine.values())} frames")

    elif a.cmd == "gatt":
        services, chars = gatt_table(att_frames(records))
        for start, end, u in services:
            print(f"service 0x{start:04x}-0x{end:04x} {u}")
            for decl, vh, props, cu in chars:
                if start <= decl <= end:
                    print(f"    char value 0x{vh:04x} props 0x{props:02x} {cu}")

    elif a.transfer_window:
        frames = list(att_frames(records))
        marks = [(t, pdu) for t, d, pdu in frames if d == "TX" and len(pdu) == 10 and pdu[0] in (0x12, 0x52)
                 and struct.unpack("<H", pdu[1:3])[0] == 0x0D and pdu[3:6] == b"\x00\x00\x00"]
        if not marks:
            raise SystemExit("no file-transfer packets found")
        lo, hi = marks[0][0] - 2.0, marks[-1][0] + 2.0
        t0 = None
        for t, direction, pdu in frames:
            if not lo <= t <= hi or not pdu:
                continue
            op = pdu[0]
            handle = struct.unpack("<H", pdu[1:3])[0] if op in (0x12, 0x52, 0x1B, 0x1D, 0x0A) and len(pdu) >= 3 else None
            value = pdu[3:] if handle is not None else pdu[1:]
            t0 = t if t0 is None else t0
            h = f"0x{handle:04x}" if handle is not None else "     -"
            shown = "data" if handle == 0x0D and len(value) > 16 else value.hex()
            print(f"{t - t0:9.3f} {direction} {ATT_NAMES.get(op, hex(op)):14} {h} len={len(value):3} {shown}")

    else:
        t0 = None
        started = not a.from_start
        for t, direction, pdu in att_frames(records):
            if not pdu:
                continue
            op = pdu[0]
            handle = struct.unpack("<H", pdu[1:3])[0] if op in (0x12, 0x52, 0x1B, 0x1D, 0x0A) and len(pdu) >= 3 else None
            if a.handle is not None and handle != a.handle:
                continue
            value = pdu[3:] if handle is not None else pdu[1:]
            if not started:
                if op in (0x12, 0x52) and len(value) == 7 and value[:3] == b"\x00\x00\x00":
                    started = True
                else:
                    continue
            t0 = t if t0 is None else t0
            h = f"0x{handle:04x}" if handle is not None else "     -"
            print(f"{t - t0:9.3f} {direction} {ATT_NAMES.get(op, hex(op)):14} {h} len={len(value):3} {value.hex()}")


if __name__ == "__main__":
    main()
