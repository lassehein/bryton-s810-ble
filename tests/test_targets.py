"""Generated targets must match the files the Bryton app wrote (reference/app_targets/Ref_*.fit)."""
from pathlib import Path

from bryton.fit import read_messages
from bryton.fitgen import encode_fit
from bryton.service import ServiceError, normalize_steps, preview_text

REF = Path(__file__).resolve().parent.parent / "reference" / "app_targets"


def steps_of(fit):
    out = []
    for num, fields in read_messages(fit):
        if num == 27:
            v = {k: int.from_bytes(b, "little") for k, b in fields.items() if k != 0}
            out.append((v[1], v[2], v[3], v[5], v[6]))  # duration_type, duration, target_type, low, high
    return out


def mine(*steps):
    return steps_of(encode_fit("X", normalize_steps(list(steps))))


def test_mhr_lthr_ftp_fixed_targets_match_app():
    t = lambda **k: dict(kind="active", seconds=20, **k)
    for name, key, vals in (("FTP", "power", (75, 95)), ("MHR", "mhr", (65, 80)), ("LTHR", "lthr", (90, 100))):
        app = steps_of((REF / f"Ref_{name}.fit").read_bytes())
        assert mine(t(**{key: vals[0]}), t(**{key: vals[1]})) == app, name


def test_speed_fixed_targets_match_app():
    app = steps_of((REF / "Ref_Speed.fit").read_bytes())
    assert app[0][2] == 0 and app[0][3] == app[0][4] == 7500
    speeds = [27, 33, 27, 33, 27, 33, 27]
    assert mine(*({"kind": "active", "seconds": 20, "speed": s} for s in speeds)) == app


def test_distance_step_layout():
    app = steps_of((REF / "Ref_Dist.fit").read_bytes())
    assert [a[0] for a in app] == [1, 1]  # duration_type distance
    ours = mine({"kind": "warmup", "km": 5, "power": 75}, {"kind": "active", "km": 10, "power": 90})
    assert ours == app  # the app steps were 5 km and 10 km -> duration in cm


def test_input_rules():
    import pytest
    with pytest.raises(ServiceError):
        normalize_steps([{"seconds": 20, "power": 70, "mhr": 60}])
    with pytest.raises(ServiceError):
        normalize_steps([{"seconds": 20, "meters": 100, "power": 70}])
    text = preview_text("X", normalize_steps([{"seconds": 20, "lthr": [90, 100]}, {"meters": 500, "speed": 27.5}]))
    assert "% LTHR" in text and "km/h" in text and "500 m" in text


def test_preview_shows_bpm_with_zones():
    steps = normalize_steps([{"seconds": 20, "mhr": [60, 70]}, {"seconds": 20, "lthr": [85, 95]}, {"seconds": 20, "mhr": 65}])
    text = preview_text("X", steps, zones={"mhr": 188, "lthr": 154})  # the device floors
    assert "112-131 bpm" in text and "130-146 bpm" in text and "122 bpm" in text  # as shown on the S810
