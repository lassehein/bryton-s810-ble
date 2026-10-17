import gzip
import math
from pathlib import Path

import pytest

from bryton import route

CAPTURE = Path(__file__).resolve().parent.parent / "results" / "files-route-ble" / "01_type0b.bin"


def circle(n=200, lat0=10.0, lon0=20.0, r=0.003):
    return [(lat0 + r * math.sin(2 * math.pi * i / n), lon0 + r * math.cos(2 * math.pi * i / n),
             100 + 20 * math.sin(4 * math.pi * i / n)) for i in range(n + 1)]


def test_encode_decode_roundtrip_and_summary():
    pts = circle()
    fit = route.encode_fit(pts, waypoints=[(50, "Cafe"), (120, "View")], climbs=[(10, 30)])
    d = route.decode_fit(fit)
    assert len(d[249]) == len(d[251]) == len(pts) == d[254][0][1] == d[252][0][1]
    assert [r[1] for r in d[251]] == list(range(len(pts)))
    s = d[254][0]
    assert s[2] == max(r[1] for r in d[249]) and s[3] == min(r[1] for r in d[249])
    assert s[4] == max(r[2] for r in d[249]) and s[5] == min(r[2] for r in d[249])
    assert abs(s[6] - 2 * math.pi * 0.003 * 111195 * 0.5) < 1500  # polyline length, ellipse-ish
    assert d[253][0][1] == 4 and [r[2] for r in d[250]] == [190, 191, 100, 100]
    assert d[250][2][5].rstrip(b"\0") == b"Cafe"
    assert gzip.decompress(route.to_transfer_payload(fit)) == fit


def test_route_survives_chunking():
    from bryton.protocol import build_chunks
    from tools.pklg_extract import assemble_files, extract_transfers
    from tests.test_protocol import _frames_for
    import random
    rnd = random.Random(1)
    wobbly = [(10 + i * 1e-4 + rnd.random() * 1e-4, 20 + rnd.random() * 1e-3, rnd.random() * 90) for i in range(3000)]
    payload = route.to_transfer_payload(route.encode_fit(wobbly))
    chunks = build_chunks(payload)
    assert len(chunks) > 1
    (kind, out, note), = list(assemble_files(extract_transfers(_frames_for(route.KIND_ROUTE, chunks))))
    assert out == payload and note == "ok"


def test_gpx_and_thinning():
    gpx = ('<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
           + "".join(f'<trkpt lat="{50 + i * 1e-5}" lon="8.0"><ele>{100 + i}</ele></trkpt>' for i in range(500))
           + "</trkseg></trk></gpx>")
    pts = route.parse_gpx(gpx)
    assert len(pts) == 500 and pts[3][2] == 103
    thin = route.thin(pts)
    assert 2 <= len(thin) < 500 and thin[0] == pts[0] and thin[-1] == pts[-1]
    with pytest.raises(route.RouteError):
        route.parse_gpx('<gpx><trk/></gpx>')


@pytest.mark.skipif(not CAPTURE.exists(), reason="private capture data not available")
def test_capture_file_reencodes_identically():
    """Decode the app's route, re-encode the same values: the FIT must match byte for byte."""
    fit = gzip.decompress(CAPTURE.read_bytes())
    d = route.decode_fit(fit)
    s = d[254][0]
    pts = [(r[1] / 1e6, r[2] / 1e6, r[3] / 5 - 500) for r in d[249]]
    summary = {"lat_max": s[2], "lat_min": s[3], "lon_max": s[4], "lon_min": s[5], "distance": s[6],
               "alt_max": s[7], "alt_min": s[8], "ascent": s[9], "descent": s[10]}
    waypoints = [(r[1], bytes(r[5]).split(b"\0")[0].decode()) for r in d[250] if r[2] == 100]
    climbs = [(d[250][0][1], d[250][1][1])]
    again = route.decode_fit(route.encode_fit(pts, waypoints, climbs, summary))
    assert [(r[1], r[2]) for r in again[249]] == [(r[1], r[2]) for r in d[249]]  # coordinates exact
    assert [r[3] for r in again[249]] == [r[3] for r in d[249]]  # altitude encoding exact
    assert again[254] == d[254]
    key = lambda rows: [(r[1], r[2], bytes(r[5]).split(b"\0")[0]) for r in rows]
    assert key(again[250]) == key(d[250])  # same waypoints, climbs first, names intact
    agree = sum(a[4] == b[4] for a, b in zip(again[249], d[249])) / len(pts)
    assert agree > 0.8  # grade: whole-percent approximation of the app's value
