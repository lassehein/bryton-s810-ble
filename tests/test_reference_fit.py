import struct
from pathlib import Path

from bryton.fit import fit_crc, read_messages

FIT = Path(__file__).resolve().parent.parent / "reference" / "ble_transfer_FTP_workout.fit"
FILE_ID = 0
WORKOUT_STEP = 27


def load():
    return FIT.read_bytes()


def test_header_is_valid():
    data = load()
    assert data[0] == 14
    assert data[8:12] == b".FIT"
    assert struct.unpack("<I", data[4:8])[0] == len(data) - 14 - 2
    assert struct.unpack("<H", data[12:14])[0] == fit_crc(data[:12])


def test_file_crc_is_valid():
    data = load()
    assert struct.unpack("<H", data[-2:])[0] == fit_crc(data[:-2])


def test_file_is_workout():
    msgs = read_messages(load())
    file_ids = [v for n, v in msgs if n == FILE_ID]
    assert len(file_ids) == 1
    assert file_ids[0][0] == b"\x05", "file_id.type should be 5 (workout)"


def steps():
    return [v for n, v in read_messages(load()) if n == WORKOUT_STEP]


def test_has_18_steps():
    # 1 warmup + 16 alternating active/rest + 1 cooldown
    assert len(steps()) == 18


def test_step_structure():
    s = steps()
    assert s[0][7] == b"\x02", "first step intensity should be warmup (2)"
    assert s[-1][7] == b"\x03", "last step intensity should be cooldown (3)"
    assert [x[7] for x in s[1:-1]] == [b"\x00", b"\x01"] * 8


def test_power_percent_target_type():
    # target_type 245 = power in % FTP (see HANDOFF.md)
    assert all(x[3] == b"\xf5" for x in steps())
