"""Bryton Rider S810 file-transfer framing on the file characteristic (0x0d).

Layout (see HANDOFF.md):
  start packet : 00 00 00 <n_data_packets> <unknown> 10 08        (7 bytes)
  data packet  : <seq u16 BE, from 1> <up to 148 bytes>   (150 bytes on air)
                 body = 00 00 00 00 <file_len u32 BE> + file, split into chunks
  end packet   : <n_data_packets+1 u16 BE> 00 <sum of body bytes u32 BE>  (7 bytes)

Verified byte-exact against the iPhone capture (FIT and JSON).
Start byte 5 = payload length of the last data packet (verified by a rejected upload with the wrong value).

Command channel (0x18) messages end with a checksum = sum of all
previous bytes mod 256; two-byte acks (<cmd> 0a) carry none.
"""
import struct

FILE_CHAR_UUID = "454d1188-a122-058d-9b42-9d0f3772af82"
CONTROL_CHAR_UUID = "454d2288-a122-058d-9b42-9d0f3772af82"
COMMAND_CHAR_UUID = "50b81188-c2de-a994-094d-340090726877"
HANDSHAKE = bytes.fromhex("0808") + b"12345678ios"

DEFAULT_CHUNK = 148  # payload per data packet; + 2 bytes seq = 150 on air


def build_transfer(data, start_byte5=None, chunk=DEFAULT_CHUNK):
    """Return (start_packet, [data_packets], end_packet) for one file.

    start_byte5 defaults to the payload length of the LAST data packet (found by comparing
    the capture: FIT 691-4*148=99=0x63, JSON 474-3*148=30=0x1e; the device rejects a wrong value).
    """
    body = b"\x00\x00\x00\x00" + struct.pack(">I", len(data)) + data
    chunks = [body[i:i + chunk] for i in range(0, len(body), chunk)]
    n = len(chunks)
    if start_byte5 is None:
        start_byte5 = len(chunks[-1])
    if n > 255:
        raise ValueError("file too large for one-byte packet count")
    start = bytes([0, 0, 0, n, start_byte5, 0x10, 0x08])
    packets = [struct.pack(">H", i) + c for i, c in enumerate(chunks, 1)]
    end = struct.pack(">H", n + 1) + b"\x00" + struct.pack(">I", sum(body) & 0xFFFFFFFF)
    return start, packets, end


def reassemble(start, packets, end):
    """Inverse of build_transfer; validates framing, returns the file bytes."""
    n = start[3]
    if len(start) != 7 or start[:3] != b"\x00\x00\x00" or start[5:] != b"\x10\x08":
        raise ValueError("bad start packet")
    if len(packets) != n:
        raise ValueError(f"expected {n} data packets, got {len(packets)}")
    for i, p in enumerate(packets, 1):
        if struct.unpack(">H", p[:2])[0] != i:
            raise ValueError(f"bad sequence number in packet {i}")
    if len(end) != 7 or struct.unpack(">H", end[:2])[0] != n + 1:
        raise ValueError("bad end packet")
    body = b"".join(p[2:] for p in packets)
    if struct.unpack(">I", end[3:])[0] != sum(body) & 0xFFFFFFFF:
        raise ValueError("end packet checksum mismatch")
    if body[:4] != b"\x00\x00\x00\x00":
        raise ValueError("bad prefix")
    length = struct.unpack(">I", body[4:8])[0]
    data = body[8:]
    if len(data) != length:
        raise ValueError(f"length mismatch: header {length}, payload {len(data)}")
    return data


def command(*payload):
    """Frame a 0x18 command: payload bytes + sum checksum."""
    b = bytes(payload)
    return b + bytes([sum(b) & 0xFF])


# Info queries seen in the capture as "XX 04 <sum>"; only these may be sent by the query probe.
# Excluded on purpose: 0x46 (followed by begin/end markers 46 01 01 / 46 01 00), 0x18, 0x27.
QUERY_WHITELIST = frozenset({0x2F, 0x34, 0x47, 0x41, 0x5B, 0x29, 0x48, 0x25, 0x53,
                             0x1D, 0x1E, 0x21, 0x22, 0x23, 0x5A})


def build_query(cmd):
    """Return the 0x18 query frame `cmd 04 <sum>`; refuses commands outside the whitelist."""
    if cmd not in QUERY_WHITELIST:
        raise ValueError(f"command 0x{cmd:02x} is not whitelisted for querying")
    return command(cmd, 0x04)


def ack_for(notification):
    """Frame to send after receiving a 0x18 notification, or None.

    The app answers every notification longer than 2 bytes with `cmd 0a`;
    the 2-byte `cmd 0a` frames are the device's own acks and get no reply.
    """
    if len(notification) <= 2:
        return None
    return bytes([notification[0], 0x0A])


# --- Session framing seen around a file upload (capture, 2026-10-16) -----------------------
# 0x18: 46 04 4a -> 46 05 ..; ack 46 0a; 46 01 01 00 48 (begin) -> 46 0a;
#   per file: 0x10 `10 00 <k>` -> `10 20 02`; 0x10 `10 40 05 01 f4`; 0x0d start/data/end;
#             later 0x10 notify `10 40 03 00 00`
#   46 01 00 00 47 (end) -> 46 0a
# k = 0x09 for the FIT, 0x0f for the JSON in the capture; meaning unknown.
QUERY_46 = command(0x46, 0x04)
# Last payload byte of begin/end = destination (capture-calendar, 2026-10-16):
# 0 = workout list (System/Plan/Cycling), 1 = calendar (Workout/calendar/<date of JSON "plan">).
TARGETS = {"plan": 0x00, "calendar": 0x01}


def begin_transfer(target="plan"):
    return command(0x46, 0x01, 0x01, TARGETS[target])


def end_transfer(target="plan"):
    return command(0x46, 0x01, 0x00, TARGETS[target])


BEGIN_TRANSFER, END_TRANSFER = begin_transfer(), end_transfer()
PREPARE = {"fit": bytes.fromhex("100009"), "json": bytes.fromhex("10000f")}
PARAMS = bytes.fromhex("10400501f4")
PREPARE_REPLY = bytes.fromhex("102002")
DONE_REPLY = bytes.fromhex("1040030000")


def plan_upload(transfers, target="plan"):
    """Ordered steps for uploading {kind: (start, packets, end)} or a list of (kind, transfer)
    pairs (several workouts in one session: the calendar is replaced by what one session sends).

    A step is (channel, data, with_response, expect) where expect is None or
    (channel, predicate-name) resolved by the writer: 'q46' | 'ack46' | 'prep' | 'done'.
    """
    steps = [("0x18", QUERY_46, True, "q46"), ("0x18", begin_transfer(target), True, "ack46")]
    pairs = transfers.items() if isinstance(transfers, dict) else transfers
    for kind, (start, packets, end) in pairs:
        steps.append(("0x10", PREPARE[kind], True, "prep"))
        steps.append(("0x10", PARAMS, True, None))
        steps.append(("0x0d", start, False, None))
        steps += [("0x0d", pk, False, None) for pk in packets]
        steps.append(("0x0d", end, True, "done"))
    steps.append(("0x18", end_transfer(target), True, "ack46"))
    return steps


# Expected replies by name: (channel, predicate)
EXPECT = {
    "q46": ("0x18", lambda d: d[:2] == b"\x46\x05"),
    "ack46": ("0x18", lambda d: d == b"\x46\x0a"),
    "prep": ("0x10", lambda d: d == PREPARE_REPLY),
    "done": ("0x10", lambda d: d == DONE_REPLY),
}


def reply_seen(seen, mark, name):
    """True if a notification matching `name` is in seen[mark:].

    `mark` must be taken BEFORE the request is written: the device can answer while the
    write call is still awaiting its own response (this raced in the first replay run).
    """
    channel, pred = EXPECT[name]
    return any(c == channel and pred(d) for c, d in seen[mark:])
