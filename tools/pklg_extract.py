"""Reassemble the files the phone app sends over the file characteristic (0x0d) from a capture.

  python3 tools/pklg_extract.py capture.pklg OUTDIR

Writes OUTDIR/NN_typeKK.bin for every transfer (KK = argument of the preceding `10 00 KK` prepare
command on 0x10) and prints a summary. The output contains the transferred data (routes include
coordinates!): keep it out of public repositories.

A file can be split over several transfers. Each transfer body starts with `offset (4 B, BE)`
and `total length (4 B, BE)`, then its data chunk; the chunks are joined at their offsets until
`total length` bytes are present.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _body(start, packets, end):
    """Check sequence numbers and end checksum of one transfer; return its body bytes."""
    n = start[3]
    for i, p in enumerate(packets, 1):
        if struct.unpack(">H", p[:2])[0] != i:
            raise ValueError(f"bad sequence number in packet {i}")
    if struct.unpack(">H", end[:2])[0] != n + 1:
        raise ValueError("bad end packet")
    body = b"".join(p[2:] for p in packets)
    if struct.unpack(">I", end[3:])[0] != sum(body) & 0xFFFFFFFF:
        raise ValueError("end packet checksum mismatch")
    return body


def extract_transfers(frames):
    """frames: iterable of (t, direction, att_pdu). Yields (kind_byte, body_bytes or None, note)."""
    kind = None
    start, packets = None, []
    for _t, direction, pdu in frames:
        if direction != "TX" or len(pdu) < 4 or pdu[0] not in (0x12, 0x52):
            continue
        handle, value = struct.unpack("<H", pdu[1:3])[0], pdu[3:]
        if handle == 0x10 and len(value) == 3 and value[:2] == b"\x10\x00":
            kind = value[2]
        elif handle == 0x0D:
            if start is None and len(value) == 7 and value[:3] == b"\x00\x00\x00" and value[5:] == b"\x10\x08":
                start, packets = value, []
            elif start is not None:
                if len(packets) < start[3]:
                    packets.append(value)
                else:  # the end packet
                    try:
                        yield kind, _body(start, packets, value), "ok"
                    except ValueError as e:
                        yield kind, None, f"invalid: {e}"
                    start = None


def assemble_files(transfers):
    """Join chunks into files. Yields (kind_byte, file_bytes or None, note)."""
    cur = None  # [kind, total, bytearray]
    for kind, body, note in transfers:
        if body is None:
            yield kind, None, note
            continue
        offset, total = struct.unpack(">II", body[:8])
        chunk = body[8:]
        if offset == 0:
            if cur is not None:
                yield cur[0], None, f"incomplete: {len(cur[2])} of {cur[1]} bytes"
            cur = [kind, total, bytearray()]
        if cur is None or offset != len(cur[2]) or total != cur[1]:
            yield kind, None, f"invalid: chunk offset {offset}, total {total}"
            cur = None
            continue
        cur[2].extend(chunk)
        if len(cur[2]) > cur[1]:
            yield cur[0], None, f"invalid: {len(cur[2])} bytes for total {cur[1]}"
            cur = None
        elif len(cur[2]) == cur[1]:
            yield cur[0], bytes(cur[2]), "ok"
            cur = None
    if cur is not None:
        yield cur[0], None, f"incomplete: {len(cur[2])} of {cur[1]} bytes"


def main():
    from tools.pklg_analyze import att_frames, read_records
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    frames = list(att_frames(read_records(open(sys.argv[1], "rb").read())))
    for i, (kind, data, note) in enumerate(assemble_files(extract_transfers(frames)), 1):
        kk = "??" if kind is None else f"{kind:02x}"
        if data is None:
            print(f"{i:02d} type {kk}: {note}")
            continue
        (out / f"{i:02d}_type{kk}.bin").write_bytes(data)
        print(f"{i:02d} type {kk}: {len(data)} bytes, head {data[:16].hex()}")


if __name__ == "__main__":
    main()
