import hashlib
import json
import re
from pathlib import Path

from bryton.protocol import build_transfer, plan_upload
from bryton.workout import make_test_fit, make_test_json, reference_files, verify

REF = Path(__file__).resolve().parent.parent / "reference"


def capture_tx_frames():
    """TX frames of the iPhone upload, from the capture window (data frames show as 'data')."""
    frames = []
    for line in (REF / "capture_transfer_window.txt").read_text().splitlines():
        m = re.match(r"\s*[\d.]+ TX \S+\s+0x([0-9a-f]{4}) len=\s*\d+ (\S+)", line)
        if m:
            ch = {"0018": "0x18", "0010": "0x10", "000d": "0x0d"}[m.group(1)]
            frames.append((ch, m.group(2)))
    return frames


def planned(transfers):
    out = []
    for ch, data, _resp, _exp in plan_upload(transfers):
        out.append((ch, "data" if ch == "0x0d" and len(data) > 16 else data.hex()))
    return out


def replay_transfers():
    fit, js = reference_files()
    return {"fit": build_transfer(fit, 0x63), "json": build_transfer(js, 0x1E)}


def test_plan_matches_capture_except_reactive_acks():
    captured = [f for f in capture_tx_frames() if not (f[0] == "0x18" and len(f[1]) == 4 and f[1].endswith("0a"))]  # acks are reactive
    assert planned(replay_transfers()) == captured


def test_replay_reference_is_what_is_on_the_device():
    fit, js = reference_files()
    assert hashlib.sha256(fit).hexdigest() == "42f32597ccdee36df368e392d33b550112bb9bdf724fbe1eeee3c84fa188eb2a"
    assert hashlib.sha256(js).hexdigest() == "fbcb62f294680d30606cfe35386400f6677bf74e51d64900ad2d5c81cf9fb37c"


def test_test_name_has_same_sizes_as_the_capture():
    # 17-character name -> JSON stays 466 bytes, FIT stays 683 bytes, so length-dependent bytes match
    fit, js = make_test_fit("Test_BLE_Interv01"), make_test_json("Test_BLE_Interv01")
    assert len(fit) == 683 and len(js) == 466
    assert verify(fit, js) == "Test_BLE_Interv01"
    assert json.loads(js)["name"] == "Test_BLE_Interv01"


def test_reply_seen_catches_reply_that_arrived_during_the_write():
    from bryton.protocol import reply_seen
    seen = [("0x10", bytes.fromhex("1040030000"))]
    mark = len(seen)                              # taken before the write
    seen.append(("0x10", bytes.fromhex("102002")))  # reply arrives while write() is still awaited
    assert reply_seen(seen, mark, "prep")
    assert not reply_seen(seen, len(seen), "prep")  # a mark taken too late would miss it (the bug)
    assert not reply_seen(seen, mark, "done")       # stale replies before the mark do not count


def test_several_workouts_in_one_calendar_session():
    from bryton.ble import transfer_pairs
    fit, js = reference_files()
    steps = plan_upload(transfer_pairs([(fit, js), (fit, js)]), "calendar")
    begins = [d.hex() for ch, d, _r, _e in steps if ch == "0x18"]
    assert begins == ["46044a", "4601010149", "4601000148"]
    assert [d.hex() for ch, d, _r, _e in steps if ch == "0x10" and len(d) == 3] == ["100009", "10000f"] * 2
