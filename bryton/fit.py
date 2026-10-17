"""Minimal FIT helpers: CRC, message reader, workout-name rewrite."""
import struct

FIT_CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
                 0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def fit_crc(data, crc=0):
    for byte in data:
        for nibble in (byte & 0xF, byte >> 4):
            tmp = FIT_CRC_TABLE[crc & 0xF]
            crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ FIT_CRC_TABLE[nibble]
    return crc


def _walk(data):
    """Yield (global_msg_num, {field_num: (offset, size)}) for normal data records."""
    header_size = data[0]
    definitions = {}
    pos = header_size
    end = len(data) - 2
    while pos < end:
        rec_hdr = data[pos]
        pos += 1
        assert not rec_hdr & 0x80, "compressed timestamp headers not supported"
        local = rec_hdr & 0x0F
        if rec_hdr & 0x40:  # definition message
            has_dev = bool(rec_hdr & 0x20)
            arch = data[pos + 1]
            fmt = '<' if arch == 0 else '>'
            global_num = struct.unpack(fmt + 'H', data[pos + 2:pos + 4])[0]
            n_fields = data[pos + 4]
            pos += 5
            fields = []
            for _ in range(n_fields):
                fields.append((data[pos], data[pos + 1]))
                pos += 3
            dev_size = 0
            if has_dev:
                n_dev = data[pos]
                pos += 1
                for _ in range(n_dev):
                    dev_size += data[pos + 1]
                    pos += 3
            definitions[local] = (global_num, fields, dev_size, fmt)
        else:  # data message
            global_num, fields, dev_size, _ = definitions[local]
            size = sum(s for _, s in fields) + dev_size
            spans = {}
            offset = pos
            for num, s in fields:
                spans[num] = (offset, s)
                offset += s
            yield global_num, spans
            pos += size
    assert pos == end, f"record parsing ended at {pos}, expected {end}"


def read_messages(data):
    """Return list of (global_msg_num, {field_num: raw_bytes}) for normal data records."""
    return [(n, {f: data[o:o + sz] for f, (o, sz) in spans.items()}) for n, spans in _walk(data)]


WORKOUT_MSG, WKT_NAME_FIELD = 26, 8


def workout_name(data):
    for n, values in read_messages(data):
        if n == WORKOUT_MSG and WKT_NAME_FIELD in values:
            return values[WKT_NAME_FIELD].split(b"\x00")[0].decode("ascii", "replace")
    raise ValueError("no workout name in FIT")


def set_workout_name(data, name):
    """Return a copy of the FIT with workout.wkt_name replaced and the file CRC fixed."""
    raw = name.encode("ascii")
    out = bytearray(data)
    for n, spans in _walk(data):
        if n == WORKOUT_MSG and WKT_NAME_FIELD in spans:
            off, size = spans[WKT_NAME_FIELD]
            if len(raw) >= size:
                raise ValueError(f"name too long: max {size - 1} characters")
            out[off:off + size] = raw + b"\x00" * (size - len(raw))
            out[-2:] = struct.pack("<H", fit_crc(bytes(out[:-2])))
            return bytes(out)
    raise ValueError("no workout name in FIT")
