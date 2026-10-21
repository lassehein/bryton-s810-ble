"""Route files for the Bryton Rider S810 (Navigation > Route).

A route is a FIT file with Bryton-specific message numbers, compressed with gzip, and sent over the file
channel as file kind 0x0b in chunks (bryton/protocol.py: build_chunks). The name is not part of the file;
the app sends it afterwards with command 0x31 (see docs/file-formats.md). Layout decoded from one app
capture (2026-10-17):

  248  file version        u16 x2                                  (0, 1)
  254  summary             points, lat max, lat min, lon max, lon min (micro-degrees, sint32),
                           distance (m, u32), altitude max, altitude min, ascent (m), descent (m)
  251  point index         u16, one per point (0, 1, 2, ...)
  253  waypoint count      u16
  250  waypoint            point index u16, type u8 (100 = named point, 190/191 = climb start/end,
                           250 = start/end marker of a planned route, 1/2/5/33 = turn entries seen in an
                           app-planned route), distance along the route (m, u32), reserved u32, name (32 bytes)
  252  point count         u16
  249  point               lat, lon (micro-degrees, sint32), altitude u16 (= (m + 500) * 5),
                           grade sint16 (percent * 100, whole percents, seen within -4..+4)

Altitudes, grades and the point spacing limits are only checked against that one capture.
"""
import gzip
import math
import re
import struct
import xml.etree.ElementTree as ET

from bryton.fit import fit_crc

PROFILE_VERSION = 0x0877
KIND_ROUTE = 0x0B
NAME_BYTES = 32
POINT_SPACING_M = 30  # the capture has a median spacing of 36 m; denser tracks are thinned (unverified limit)
MAX_POINTS = 5000  # unverified; the capture has 2480 points

# global message number -> (local number, [(field, size, base type)])
LAYOUT = {
    248: (7, [(1, 2, 132), (2, 2, 132)]),
    254: (1, [(1, 2, 132), (2, 4, 133), (3, 4, 133), (4, 4, 133), (5, 4, 133), (6, 4, 134),
              (7, 2, 132), (8, 2, 132), (9, 2, 132), (10, 2, 132)]),
    251: (2, [(1, 2, 132)]),
    253: (3, [(1, 2, 132)]),
    250: (4, [(1, 2, 132), (2, 1, 0), (3, 4, 134), (4, 4, 134), (5, NAME_BYTES, 7)]),
    252: (5, [(1, 2, 132)]),
    249: (6, [(1, 4, 133), (2, 4, 133), (3, 2, 132), (4, 2, 131)]),
}
FORMAT = {132: "<H", 133: "<i", 134: "<I", 131: "<h", 0: "<B"}


class RouteError(ValueError):
    pass


def _definition(num):
    local, fields = LAYOUT[num]
    body = bytes([0x40 | local, 0, 0]) + struct.pack("<H", num) + bytes([len(fields)])
    return body + b"".join(bytes(f) for f in fields)


def _record(num, *values):
    local, fields = LAYOUT[num]
    out = bytearray([local])
    for (_n, size, base), value in zip(fields, values):
        if base == 7:
            raw = value.encode("utf-8")[:size - 1].decode("utf-8", "ignore").encode("utf-8")  # names are UTF-8
            out += raw + b"\x00" * (size - len(raw))
        else:
            out += struct.pack(FORMAT[base], value)
    return bytes(out)


def encode_altitude(meters):
    return max(0, min(0xFFFF, round((meters + 500) * 5)))


def _distance_m(a, b):
    mid = math.radians((a[0] + b[0]) / 2e6)
    return math.hypot((b[0] - a[0]) * 0.111195, (b[1] - a[1]) * 0.111195 * math.cos(mid))


def encode_fit(points, waypoints=(), climbs=(), summary=None, markers=None, segment_ends=(), version=True):
    """points: [(lat_deg, lon_deg, alt_m)]; waypoints: [(point_index, name)]; climbs: [(start, end)] indices.

    markers: (start name, end name): adds the two type-250 entries the app writes for planned routes.
    segment_ends: point indices where a Pace Pilot segment ends (waypoint type 150); with version=False the
    file has no message 248, as in the Pace Pilot route (kind 1a).
    summary overrides the computed values (used by the round-trip test)."""
    if not 2 <= len(points) <= MAX_POINTS:
        raise RouteError(f"need 2..{MAX_POINTS} points, got {len(points)}")
    pts = [(round(la * 1e6), round(lo * 1e6), encode_altitude(al)) for la, lo, al in points]
    cum = [0.0]
    for i in range(len(pts) - 1):
        cum.append(cum[-1] + _distance_m(pts[i], pts[i + 1]))
    grades = []
    for i in range(len(pts)):  # forward window of about 50 m, whole percent, clamped to +-4 (capture)
        j = i
        while j < len(pts) - 1 and cum[j] - cum[i] < 50:
            j += 1
        span = cum[j] - cum[i]
        g = 0 if span < 25 else (pts[j][2] - pts[i][2]) / 5 / span * 100
        grades.append(max(-4, min(4, round(g))) * 100)
    alts = [p[2] for p in pts]
    ascent = descent = 0.0
    for a, b in zip(alts, alts[1:]):
        step = (b - a) / 5
        ascent += max(step, 0)
        descent += max(-step, 0)
    s = summary or {
        "lat_max": max(p[0] for p in pts), "lat_min": min(p[0] for p in pts),
        "lon_max": max(p[1] for p in pts), "lon_min": min(p[1] for p in pts),
        "distance": round(cum[-1]), "alt_max": max(alts), "alt_min": min(alts),
        "ascent": round(ascent), "descent": round(descent)}
    waypoint_rows = []  # the app lists climbs first, then named points
    for start, end in climbs:
        waypoint_rows += [(start, 190, 0, 0, ""), (end, 191, 0, 0, "")]
    waypoint_rows += [(idx, 100, round(cum[idx]), 0, name) for idx, name in waypoints]
    if markers:
        waypoint_rows += [(0, 250, 0, 0, markers[0]), (len(pts) - 1, 250, round(cum[-1]), 0, markers[1])]
    waypoint_rows += [(idx, 150, 0, 0, "") for idx in segment_ends]
    out = bytearray()
    if version:
        out += _definition(248) + _record(248, 0, 1)
    out += _definition(254) + _record(254, len(pts), s["lat_max"], s["lat_min"], s["lon_max"], s["lon_min"],
                                       s["distance"], s["alt_max"], s["alt_min"], s["ascent"], s["descent"])
    out += _definition(251) + b"".join(_record(251, i) for i in range(len(pts)))
    out += _definition(253) + _record(253, len(waypoint_rows))
    out += _definition(250) + b"".join(_record(250, *row) for row in waypoint_rows)
    out += _definition(252) + _record(252, len(pts))
    out += _definition(249) + b"".join(_record(249, lat, lon, alt, g) for (lat, lon, alt), g in zip(pts, grades))
    header = bytes([14, 0x10]) + struct.pack("<HI", PROFILE_VERSION, len(out)) + b".FIT"
    header += struct.pack("<H", fit_crc(header))
    body = header + bytes(out)
    return body + struct.pack("<H", fit_crc(body))


def to_transfer_payload(fit):
    """gzip as sent by the app (fixed header, no timestamp, so the output is reproducible)."""
    return gzip.compress(fit, compresslevel=9, mtime=0)


def decode_fit(data):
    """{global message number: [{field: value}]}; strings stay bytes. Raises RouteError on a bad file."""
    if data[8:12] != b".FIT" or fit_crc(data[:-2]) != struct.unpack("<H", data[-2:])[0]:
        raise RouteError("not a valid FIT file")
    pos, defs, out = data[0], {}, {}
    while pos < len(data) - 2:
        h = data[pos]
        pos += 1
        if h & 0x40:
            n = data[pos + 4]
            fields = [tuple(data[pos + 5 + 3 * i:pos + 8 + 3 * i]) for i in range(n)]
            defs[h & 0x0F] = (struct.unpack("<H", data[pos + 2:pos + 4])[0], fields)
            pos += 5 + 3 * n
        else:
            num, fields = defs[h & 0x0F]
            row = {}
            for fnum, size, base in fields:
                raw = data[pos:pos + size]
                pos += size
                row[fnum] = raw if base == 7 else struct.unpack(FORMAT[base], raw)[0]
            out.setdefault(num, []).append(row)
    return out


def parse_gpx(text):
    """[(lat, lon, ele)] from the first track (or route) in a GPX document."""
    root = ET.fromstring(text)
    ns = re.match(r"\{.*\}", root.tag)
    q = (lambda t: f"{ns.group(0)}{t}") if ns else (lambda t: t)
    nodes = root.findall(f".//{q('trkpt')}") or root.findall(f".//{q('rtept')}")
    out = []
    for n in nodes:
        ele = n.find(q("ele"))
        out.append((float(n.get("lat")), float(n.get("lon")), float(ele.text) if ele is not None else 0.0))
    if len(out) < 2:
        raise RouteError("GPX needs at least 2 track or route points")
    return out


def thin(points, spacing=POINT_SPACING_M):
    """Drop points closer than `spacing` meters to the previous kept one (keeps first and last)."""
    kept = [points[0]]
    for p in points[1:-1]:
        if _distance_m((kept[-1][0] * 1e6, kept[-1][1] * 1e6), (p[0] * 1e6, p[1] * 1e6)) >= spacing:
            kept.append(p)
    return kept + [points[-1]]
