import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import pklg_analyze as pa  # noqa: E402

FILE_UUID = "454d1188-a122-058d-9b42-9d0f3772af82"


def rec(typ, payload, endian="<"):
    body = struct.pack(endian + "II", 1, 0) + bytes([typ]) + payload
    return struct.pack(endian + "I", len(body)) + body


def acl(att, typ):
    l2 = struct.pack("<HH", len(att), 4) + att
    return rec(typ, struct.pack("<HH", 0x0040, len(l2)) + l2)


def adv_event(addr, ad, rssi=-60):
    report = bytes([0x00, 0x01]) + addr + bytes([len(ad)]) + ad + struct.pack("b", rssi)
    params = bytes([0x02, 0x01]) + report
    return bytes([0x3E, len(params)]) + params


def test_endian_detection():
    for e in "<>":
        recs = list(pa.read_records(rec(1, b"\x00", e) + rec(2, b"\x01\x02", e)))
        assert [r[2] for r in recs] == [1, 2]


def test_adv_name_and_service():
    uuid_le = bytes(reversed(bytes.fromhex(FILE_UUID.replace("-", ""))))
    ad = bytes([10, 0x09]) + b"RiderS810" + bytes([17, 0x07]) + uuid_le + bytes([5, 0xFF, 0x34, 0x12, 0xAA, 0xBB])
    reports = list(pa.adv_reports(adv_event(b"\x01\x02\x03\x04\x05\x06", ad)))
    assert len(reports) == 1
    parsed = pa.parse_ad(reports[0][1])
    assert parsed["name"] == "RiderS810"
    assert parsed["services"] == [FILE_UUID]
    assert parsed["manufacturer"] == [(0x1234, "aabb")]


def test_gatt_table():
    svc_uuid = bytes(range(16))
    group = bytes([0x11, 20]) + struct.pack("<HH", 0x000A, 0x0012) + svc_uuid
    uuid_le = bytes(reversed(bytes.fromhex(FILE_UUID.replace("-", ""))))
    char = bytes([0x09, 21]) + struct.pack("<HBH", 0x000C, 0x1C, 0x000D) + uuid_le
    data = acl(group, 3) + acl(char, 3)
    services, chars = pa.gatt_table(pa.att_frames(pa.read_records(data)))
    assert services == [(0x0A, 0x12, pa.uuid_from_le(svc_uuid))]
    assert chars == [(0x0C, 0x0D, 0x1C, FILE_UUID)]


def test_transfer_window(capsys, tmp_path, monkeypatch):
    def write_cmd(handle, value, typ=2):
        return acl(bytes([0x52]) + struct.pack("<H", handle) + value, typ)

    def at(t, raw):
        # re-stamp the record time (seconds) for ordering/window tests
        body = struct.pack("<II", t, 0) + raw[12:]
        return struct.pack("<I", len(body)) + body

    blob = (at(1, write_cmd(0x10, b"\x01")) + at(100, write_cmd(0x18, b"\x46\x04\x4a"))
            + at(101, write_cmd(0x0D, bytes.fromhex("00000005631008")))
            + at(101, write_cmd(0x0D, b"\x00\x01" + b"x" * 148))
            + at(101, write_cmd(0x0D, bytes.fromhex("00060000009ad2")))
            + at(102, write_cmd(0x18, bytes.fromhex("46010100" "48"))) + at(200, write_cmd(0x10, b"\x02")))
    path = tmp_path / "t.pklg"
    path.write_bytes(blob)
    monkeypatch.setattr("sys.argv", ["x", "att", str(path), "--transfer-window"])
    pa.main()
    out = capsys.readouterr().out
    assert "46044a" in out and "46010100" in out and "00000005631008" in out
    assert out.count("data") == 1                      # long data frame collapsed
    assert "0x0010" not in out                         # outside the window


def test_cmds_marks_new_commands(capsys, tmp_path, monkeypatch):
    def tx(value):
        return acl(bytes([0x52]) + struct.pack("<H", 0x18) + value, 2)

    base = tmp_path / "base.pklg"
    base.write_bytes(tx(bytes.fromhex("2f0433")) + tx(bytes.fromhex("2f0a0b")))
    new = tmp_path / "new.pklg"
    new.write_bytes(tx(bytes.fromhex("2f0433")) + tx(bytes.fromhex("7a0100112233")))
    monkeypatch.setattr("sys.argv", ["x", "cmds", str(new), "--baseline", str(base)])
    pa.main()
    out = capsys.readouterr().out
    assert "cmd=0x7a sub=0x01 TX n=1" in out and "NEW COMMAND" in out and "7a0100112233" in out
    assert "cmd=0x2f sub=0x04" in out and out.count("NEW") == 1
