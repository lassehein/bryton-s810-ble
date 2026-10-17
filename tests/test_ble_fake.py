"""Upload logic against a simulated S810 (no Bluetooth needed)."""
import asyncio
import sys
import types

import pytest

from bryton.fitgen import build_files
from bryton.protocol import COMMAND_CHAR_UUID, CONTROL_CHAR_UUID, FILE_CHAR_UUID

CH = {FILE_CHAR_UUID: "0x0d", CONTROL_CHAR_UUID: "0x10", COMMAND_CHAR_UUID: "0x18"}


class FakeDevice:
    def __init__(self, reject_file=False, silent_handshake=False):
        self.reject_file, self.silent_handshake = reject_file, silent_handshake
        self.callbacks, self.writes, self.in_transfer = {}, [], False

    def notify(self, uuid, data):
        self.callbacks[uuid](None, bytearray(data))

    def on_write(self, uuid, data):
        ch = CH[uuid]
        self.writes.append((ch, data.hex()))
        h = data.hex()
        if ch == "0x10" and h.startswith("0808") and not self.silent_handshake:
            self.notify(CONTROL_CHAR_UUID, bytes.fromhex("082802"))
        elif ch == "0x18" and h == "2f0433":
            self.notify(COMMAND_CHAR_UUID, bytes.fromhex("2f050700013f02faf90300000073"))
        elif ch == "0x18" and h == "46044a":
            self.notify(COMMAND_CHAR_UUID, bytes.fromhex("46050100014d"))
        elif ch == "0x18" and h in ("4601010048", "4601000047"):
            self.in_transfer = h == "4601010048"
            self.notify(COMMAND_CHAR_UUID, bytes.fromhex("460a"))
        elif ch == "0x10" and h in ("100009", "10000f"):
            self.notify(CONTROL_CHAR_UUID, bytes.fromhex("102002"))  # arrives during the write
        elif ch == "0x0d" and len(data) == 7 and data[:3] != b"\x00\x00\x00":
            self.notify(CONTROL_CHAR_UUID, bytes.fromhex("1040010000" if self.reject_file else "1040030000"))


def install_fake_bleak(monkeypatch, device):
    class Client:
        def __init__(self, dev, disconnected_callback=None):
            self.mtu_size = 185

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def start_notify(self, uuid, cb):
            device.callbacks[uuid] = cb

        async def write_gatt_char(self, uuid, data, response=True):
            device.on_write(uuid, bytes(data))

    class Scanner:
        @staticmethod
        async def find_device_by_filter(f, timeout=15):
            return object()

    monkeypatch.setitem(sys.modules, "bleak", types.SimpleNamespace(BleakClient=Client, BleakScanner=Scanner))


def files():
    steps = [{"kind": "warmup", "seconds": 20, "power": (50, 60)},
             {"kind": "active", "seconds": 20, "power": (70, 80), "cadence": (85, 95)}]
    fit, js, _ = build_files("CC_Fake", steps)
    return fit, js


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    import bryton.ble as ble
    real_sleep = asyncio.sleep
    monkeypatch.setattr(ble.asyncio, "sleep", lambda s: real_sleep(0))


def test_upload_success_sequence(monkeypatch):
    from bryton.ble import upload
    dev = FakeDevice()
    install_fake_bleak(monkeypatch, dev)
    asyncio.run(upload(*files(), log=lambda line: None))
    tx18 = [h for ch, h in dev.writes if ch == "0x18"]
    assert tx18[0] == "2f0433" and "4601010048" in tx18 and tx18[-1] == "4601000047"
    assert not dev.in_transfer


def test_upload_rejected_file_still_closes_transfer(monkeypatch):
    from bryton.ble import UploadError, upload
    dev = FakeDevice(reject_file=True)
    install_fake_bleak(monkeypatch, dev)
    with pytest.raises(UploadError):
        asyncio.run(upload(*files(), log=lambda line: None))
    assert dev.writes[-1] == ("0x18", "4601000047") and not dev.in_transfer


def test_no_handshake_sends_nothing(monkeypatch):
    from bryton.ble import UploadError, upload
    dev = FakeDevice(silent_handshake=True)
    install_fake_bleak(monkeypatch, dev)
    import bryton.ble as ble
    monkeypatch.setattr(ble.asyncio, "wait_for", lambda aw, t: _raise_timeout(aw))
    with pytest.raises(UploadError):
        asyncio.run(upload(*files(), log=lambda line: None))
    assert all(ch == "0x10" for ch, _ in dev.writes)


async def _raise_timeout(aw):
    aw.close()
    raise asyncio.TimeoutError


class RouteDevice(FakeDevice):
    """Answers like the S810 did in the route captures (kind 0b, no 46 frames, name via 0x31)."""

    def on_write(self, uuid, data):
        ch = CH[uuid]
        self.writes.append((ch, data.hex()))
        h = data.hex()
        if ch == "0x10" and h.startswith("0808"):
            self.notify(CONTROL_CHAR_UUID, bytes.fromhex("082802"))
        elif ch == "0x10" and h == "10000b":
            self.notify(CONTROL_CHAR_UUID, bytes.fromhex("102002"))
        elif ch == "0x0d" and len(data) == 7 and data[:3] != b"\x00\x00\x00":
            self.notify(CONTROL_CHAR_UUID, bytes.fromhex("1040030000"))
        elif ch == "0x18" and h == "310435":
            self.notify(COMMAND_CHAR_UUID, bytes.fromhex("310501000138"))
        elif ch == "0x18" and h.startswith("3101"):
            self.notify(COMMAND_CHAR_UUID, bytes.fromhex("310a"))
            if data[2] + 1 < data[3]:  # asks for the next name frame
                self.notify(COMMAND_CHAR_UUID, bytes([0x31, 0x02, data[2] + 1, (0x33 + data[2] + 1) & 0xFF]))


def test_route_upload_sequence(monkeypatch):
    import random
    from bryton import route
    from bryton.ble import upload_route
    from bryton.protocol import name_frames
    rnd = random.Random(2)
    pts = [(10 + i * 1e-4 + rnd.random() * 1e-4, 20 + rnd.random() * 1e-3, rnd.random() * 90) for i in range(2500)]
    payload = route.to_transfer_payload(route.encode_fit(pts))
    dev = RouteDevice()
    install_fake_bleak(monkeypatch, dev)
    asyncio.run(upload_route(payload, "A route name that is longer than sixteen", log=lambda line: None))
    tx18 = [h for ch, h in dev.writes if ch == "0x18"]
    assert not any(h.startswith("46") for h in tx18)  # no begin/end frames for routes
    assert [h for ch, h in dev.writes if ch == "0x10" and h == "10000b"] and len(
        [1 for ch, h in dev.writes if ch == "0x10" and h == "10000b"]) >= 2  # several chunks
    assert [h for h in tx18 if h.startswith("3101")] == [f.hex() for f in name_frames("A route name that is longer than sixteen")]


def test_name_frames_match_capture():
    from bryton.protocol import name_frames
    frames = name_frames("Ref_Route2")
    assert frames[0].hex() == "310100020a3e"  # as sent by the app
    assert frames[1][:13].hex() == "3101015265665f526f7574653200"[:26]
    assert len(name_frames("x" * 40)) == 3 and name_frames("x" * 40)[0].hex().startswith("31010003")
