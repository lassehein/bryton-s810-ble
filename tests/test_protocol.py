from pathlib import Path

import pytest

from bryton.protocol import HANDSHAKE, build_transfer, command, reassemble

REF = Path(__file__).resolve().parent.parent / "reference"
FIT = REF / "ble_transfer_FTP_workout.fit"
JSON = REF / "ble_transfer_FTP_workout.json"


def captured():
    rows = [bytes.fromhex(line.split()[-1]) for line in (REF / "capture_0x0d_transfer.txt").open()]
    return (rows[0], rows[1:6], rows[6]), (rows[7], rows[8:12], rows[12])


def test_handshake_bytes():
    assert HANDSHAKE.hex() == "08083132333435363738696f73"


@pytest.mark.parametrize("path,idx,byte5", [(FIT, 0, 0x63), (JSON, 1, 0x1E)])
def test_build_matches_capture_byte_exact(path, idx, byte5):
    assert build_transfer(path.read_bytes(), byte5) == captured()[idx]


@pytest.mark.parametrize("idx,path", [(0, FIT), (1, JSON)])
def test_reassemble_capture(idx, path):
    assert reassemble(*captured()[idx]) == path.read_bytes()


def test_reassemble_rejects_bad_sequence():
    start, packets, end = build_transfer(FIT.read_bytes(), 0x63)
    packets[1], packets[2] = packets[2], packets[1]
    with pytest.raises(ValueError):
        reassemble(start, packets, end)


def test_reassemble_rejects_bad_checksum():
    start, packets, end = build_transfer(FIT.read_bytes(), 0x63)
    with pytest.raises(ValueError):
        reassemble(start, packets, end[:-1] + b"\x00")


def test_command_checksum_matches_capture():
    assert command(0x2F, 0x04).hex() == "2f0433"
    assert command(0x46, 0x01, 0x01, 0x00).hex() == "4601010048"


def test_build_query_matches_capture():
    from bryton.protocol import build_query
    assert build_query(0x2F).hex() == "2f0433"
    assert build_query(0x47).hex() == "47044b"


def test_build_query_refuses_non_whitelisted():
    from bryton.protocol import build_query
    for cmd in (0x46, 0x18, 0x27, 0x01):
        with pytest.raises(ValueError):
            build_query(cmd)


def test_ack_rules_match_capture():
    from bryton.protocol import ack_for
    assert ack_for(bytes.fromhex("2f050700013f02faf90300000073")) == bytes.fromhex("2f0a")
    assert ack_for(bytes.fromhex("2f0a")) is None


def test_start_byte5_is_last_packet_payload_length():
    # derived from the capture, confirmed by the device rejecting 0x63 for a 395-byte FIT
    for path, expected in ((FIT, 0x63), (JSON, 0x1E)):
        start, packets, _ = build_transfer(path.read_bytes())
        assert start[4] == expected == len(packets[-1]) - 2
    from bryton.fitgen import encode_fit
    steps = [{"kind": "active", "seconds": 60, "power": (50, 60)}] * 9
    start, packets, _ = build_transfer(encode_fit("CC_X", steps))
    assert start[4] == len(packets[-1]) - 2


def test_transfer_target_bytes_match_calendar_capture():
    from bryton.protocol import begin_transfer, end_transfer
    assert begin_transfer().hex() == "4601010048" and end_transfer().hex() == "4601000047"
    assert begin_transfer("calendar").hex() == "4601010149"
    assert end_transfer("calendar").hex() == "4601000148"


def _att(handle, value, op=0x52):
    import struct
    return (0.0, "TX", bytes([op]) + struct.pack("<H", handle) + value)


def _frames_for(kind, transfers):
    frames = []
    for start, packets, end in transfers:
        frames.append(_att(0x10, bytes([0x10, 0x00, kind]), 0x12))
        frames.append(_att(0x0D, start))
        frames += [_att(0x0D, p) for p in packets]
        frames.append(_att(0x0D, end, 0x12))
    return frames


def test_extract_single_chunk_files():
    from bryton.protocol import build_transfer
    from tools.pklg_extract import assemble_files, extract_transfers
    frames = []
    for payload in (bytes(range(256)) * 3, b"short"):
        frames += _frames_for(0x0B, [build_transfer(payload)])
    got = [(k, d) for k, d, _ in assemble_files(extract_transfers(frames))]
    assert got == [(0x0B, bytes(range(256)) * 3), (0x0B, b"short")]


def test_large_file_is_split_into_chunks_and_rejoined():
    from bryton.protocol import MAX_CHUNK_PAYLOAD, build_chunks
    from tools.pklg_extract import assemble_files, extract_transfers
    data = bytes(range(256)) * 90  # 23040 bytes
    chunks = build_chunks(data)
    assert len(chunks) == 4 and all(len(c[1]) <= 47 for c in chunks)
    assert MAX_CHUNK_PAYLOAD == 6922
    (kind, out, note), = list(assemble_files(extract_transfers(_frames_for(0x0B, chunks))))
    assert (kind, out, note) == (0x0B, data, "ok")


def test_pace_upload_plan_order():
    from bryton.protocol import build_chunks, plan_pace_upload
    steps = plan_pace_upload(build_chunks(b"\x1f\x8b" + bytes(range(256)) * 60), build_chunks(b'{"a": 1}'), "Test_Pace")
    kinds = [d.hex() for ch, d, _r, _e in steps if ch == "0x10" and len(d) == 3]
    assert kinds == ["10001a"] * 3 + ["10001b"]  # route chunks, then the report
    assert [i for i, (ch, d, _r, _e) in enumerate(steps) if ch == "0x18" and d.hex() == "310435"][0] < \
        [i for i, (ch, d, _r, _e) in enumerate(steps) if d.hex() == "10001b"][0]  # the name goes before the report
