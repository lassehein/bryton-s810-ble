"""Ride download: framing against the capture of the app sync (2026-10-21) and a simulated S810."""
import asyncio
import gzip
import struct

import pytest

from bryton.protocol import (COMMAND_CHAR_UUID, CONTROL_CHAR_UUID, FILE_CHAR_UUID, MAX_READ, TransferReceiver,
                             parse_ride_list, read_request)
from tests.test_ble_fake import CH, FakeDevice, install_fake_bleak


def device_transfer(body, tag=b"\x11\x00\x01", chunk=148):
    """Notifications for one device -> phone transfer."""
    parts = [body[i:i + chunk] for i in range(0, len(body), chunk)]
    out = [b"\x00\x00" + struct.pack(">H", len(parts)) + bytes([len(parts[-1])]) + tag]
    out += [struct.pack(">H", i) + p for i, p in enumerate(parts, 1)]
    out.append(struct.pack(">HB", len(parts) + 1, 0) + struct.pack(">I", sum(body) & 0xFFFFFFFF))
    return out


def test_ride_list_entry_layout():
    body = b"\x13" + struct.pack(">III", 0x6553F100, 3600, 25000) + bytes(5) + b"\x02" + bytes(17)
    assert len(body) == 36
    assert parse_ride_list(body) == [{"id": 0x6553F100, "moving_s": 3600, "distance_m": 25000}]


def test_read_request_matches_capture():
    assert read_request(0x6553F100, 0x13560, 0xEBD).hex() == "11086553f100010001356000000ebd"
    assert read_request(0x6553F100, 0, 0x9AB0).hex() == "11086553f100010000000000009ab0"
    assert MAX_READ == 0x9AB0


def test_receiver_roundtrip_and_errors():
    body = bytes(range(256)) * 3
    rx, got = TransferReceiver(), None
    for v in device_transfer(body):
        got = rx.feed(v)
    assert got == body
    rx = TransferReceiver()
    pk = device_transfer(body)
    pk[-1] = pk[-1][:-1] + b"\x00"
    with pytest.raises(ValueError):
        for v in pk:
            rx.feed(v)


class RideDevice(FakeDevice):
    def __init__(self, fit, gz=True):
        super().__init__()
        self.gz = gzip.compress(fit) if gz else fit
        self.pending = None
        self.sent_33 = False

    def on_write(self, uuid, data):
        ch, h = CH[uuid], data.hex()
        self.writes.append((ch, h))
        if ch == "0x10" and h.startswith("0808"):
            self.notify(CONTROL_CHAR_UUID, bytes.fromhex("082802"))
        elif ch == "0x18" and h == "2f0433":
            self.notify(COMMAND_CHAR_UUID, bytes.fromhex("2f050700013f02faf90300000073"))
        elif ch == "0x18" and h.startswith("33"):
            self.sent_33 = True
        elif ch == "0x10" and h == "0608":
            self.notify(CONTROL_CHAR_UUID, bytes.fromhex("062802"))
        elif ch == "0x10" and h == "064803":
            self.send(b"UD0000000000000000\x00IV049\x00", b"\x0b\x06\x00")
        elif ch == "0x10" and h == "0b08":
            self.notify(CONTROL_CHAR_UUID, bytes.fromhex("0b2802"))
        elif ch == "0x10" and h == "0b4803":
            entry = b"\x13" + struct.pack(">III", 0x6553F100, 60, 1000) + bytes(5)
            self.send(entry + b"\x02" + bytes(17), b"\x12\x0b\x00\x00\x01")
        elif ch == "0x10" and h.startswith("1108"):
            _id, _one, off, n = struct.unpack(">IBII", data[2:])
            self.notify(CONTROL_CHAR_UUID, bytes.fromhex("112802"))
            if n == 4:
                self.pending = struct.pack(">I", len(self.gz)) + self.gz[4:18]
            else:
                self.pending = self.gz[off:off + n]
        elif ch == "0x10" and h == "11480501f4":
            self.send(self.pending, b"\x11\x00\x01")

    def send(self, body, tag):
        for v in device_transfer(body, tag):
            self.notify(FILE_CHAR_UUID, v)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    import bryton.ble as ble
    real_sleep = asyncio.sleep
    monkeypatch.setattr(ble.asyncio, "sleep", lambda s: real_sleep(0))


def test_list_and_download_two_chunks(monkeypatch):
    import os
    from bryton.ble import download_rides, list_rides
    fit = b"\x0e\x10\xf2\x03\x3e\x0e\x03\x00.FIT" + os.urandom(100000)  # incompressible: gzip stays > MAX_READ, so two reads are needed
    dev = RideDevice(fit)
    assert len(dev.gz) > MAX_READ
    install_fake_bleak(monkeypatch, dev)
    assert asyncio.run(list_rides(log=lambda line: None)) == [{"id": 0x6553F100, "moving_s": 60, "distance_m": 1000}]
    got = asyncio.run(download_rides(log=lambda line: None))
    assert [e["id"] for e, _ in got] == [0x6553F100] and got[0][1] == fit
    reads = [h for ch, h in dev.writes if ch == "0x10" and h.startswith("1108")]
    assert len(reads) >= 3  # size probe + 2 ranges
    assert not dev.sent_33  # never acknowledges: the ride stays for the app


def test_download_unknown_id_fetches_nothing(monkeypatch):
    from bryton.ble import download_rides
    dev = RideDevice(b"x" * 100)
    install_fake_bleak(monkeypatch, dev)
    assert asyncio.run(download_rides(ride_ids=[1], log=lambda line: None)) == []
    assert not [h for ch, h in dev.writes if h.startswith("1108")]


def test_preamble_plays_info_exchange_and_error_shows_replies(monkeypatch):
    from bryton.ble import UploadError, list_rides
    dev = RideDevice(b"x")
    install_fake_bleak(monkeypatch, dev)
    assert asyncio.run(list_rides(log=lambda line: None, preamble=True))[0]["id"] == 0x6553F100
    tx10 = [h for ch, h in dev.writes if ch == "0x10"]
    assert tx10.index("0608") < tx10.index("064803") < tx10.index("0b08")

    class Empty(RideDevice):
        def on_write(self, uuid, data):
            if CH[uuid] == "0x10" and data.hex() == "0b08":
                self.writes.append(("0x10", "0b08"))
                self.notify(CONTROL_CHAR_UUID, bytes.fromhex("0b2806"))
                return
            super().on_write(uuid, data)
    dev = Empty(b"x")
    install_fake_bleak(monkeypatch, dev)
    from bryton.ble import download_rides
    assert asyncio.run(list_rides(log=lambda line: None)) == []
    assert asyncio.run(download_rides(log=lambda line: None)) == []
    assert not [h for ch, h in dev.writes if h == "0b4803"]  # nothing requested after the "empty" answer

    class Silent(RideDevice):
        def on_write(self, uuid, data):
            if CH[uuid] == "0x10" and data.hex() == "0b08":
                self.writes.append(("0x10", "0b08"))
                self.notify(CONTROL_CHAR_UUID, bytes.fromhex("0b2801"))  # some other answer
                return
            super().on_write(uuid, data)
    install_fake_bleak(monkeypatch, Silent(b"x"))
    with pytest.raises(UploadError, match="0x10:0b2801"):
        asyncio.run(list_rides(log=lambda line: None))


def test_plain_fit_is_accepted_and_garbage_rejected(monkeypatch):
    from bryton.ble import UploadError, download_rides
    fit = b"\x0e\x10\xf2\x03\x3e\x0e\x03\x00.FIT" + bytes(100)
    install_fake_bleak(monkeypatch, RideDevice(fit, gz=False))
    assert asyncio.run(download_rides(log=lambda line: None))[0][1] == fit
    install_fake_bleak(monkeypatch, RideDevice(b"x" * 40, gz=False))
    with pytest.raises(UploadError, match="not a FIT file"):
        asyncio.run(download_rides(log=lambda line: None))


# --- rider profile -------------------------------------------------------------------------------
from bryton.protocol import (parse_body_item, parse_profile_scalar, parse_profile_zones)  # noqa: E402

PROFILE_FRAMES = {  # as captured: request -> reply
    "210425": "210501000128", "21020023": "210100dc00fe",
    "21020124": "210101000037004b005a00690078009600ffff74",
    "220426": "220501000129", "22020024": "220100bc00df",
    "22020125": "22010100003c00460050005a005d006000ffff0b",
    "230427": "23050100012a", "23020025": "2301009a00be",
    "23020126": "2301010000510058005d00640066006900ffff5c",
    "1e0422": "1e0501006f93", "1e02000020": "1e01000000002f43cdcc8942f5",
    "1e02000121": "1e01000100008c42cd4c1a4364",
}


def test_profile_parsers_match_capture():
    assert parse_profile_scalar(bytes.fromhex("210100dc00fe")) == 220
    assert parse_profile_zones(bytes.fromhex(PROFILE_FRAMES["21020124"])) == [0, 55, 75, 90, 105, 120, 150]
    assert parse_body_item(bytes.fromhex("1e01000100008c42cd4c1a4364")) == (70.0, 154.3)
    with pytest.raises(ValueError):
        parse_profile_scalar(bytes.fromhex("210100dc00ff"))  # wrong checksum


class ProfileDevice(FakeDevice):
    def on_write(self, uuid, data):
        ch, h = CH[uuid], data.hex()
        self.writes.append((ch, h))
        if ch == "0x10" and h.startswith("0808"):
            self.notify(CONTROL_CHAR_UUID, bytes.fromhex("082802"))
        elif ch == "0x18" and h == "2f0433":
            self.notify(COMMAND_CHAR_UUID, bytes.fromhex("2f050700013f02faf90300000073"))
        elif ch == "0x18" and h in PROFILE_FRAMES:
            self.notify(COMMAND_CHAR_UUID, bytes.fromhex(PROFILE_FRAMES[h]))


def test_read_profile_from_fake_device(monkeypatch):
    from bryton.ble import read_profile
    install_fake_bleak(monkeypatch, ProfileDevice(b"x"))
    p = asyncio.run(read_profile(log=lambda line: None))
    assert (p["ftp"], p["mhr"], p["lthr"], p["height_cm"], p["weight_kg"]) == (220, 188, 154, 175.0, 70.0)
    assert p["lthr_zones"] == [0, 81, 88, 93, 100, 102, 105]


def test_read_profile_reports_silence(monkeypatch):
    from bryton.ble import UploadError, read_profile

    class Mute(ProfileDevice):
        def on_write(self, uuid, data):
            if CH[uuid] == "0x18" and data.hex().startswith("22"):
                self.writes.append(("0x18", data.hex()))
                return
            super().on_write(uuid, data)
    install_fake_bleak(monkeypatch, Mute(b"x"))
    import bryton.ble as ble
    monkeypatch.setattr(ble.time, "time", iter(range(0, 100000, 2)).__next__)  # make the 3 s deadline pass fast
    with pytest.raises(UploadError, match="no reply to 2204"):
        asyncio.run(read_profile(log=lambda line: None))
