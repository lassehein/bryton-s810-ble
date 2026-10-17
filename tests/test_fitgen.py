import json
from pathlib import Path

import pytest

from bryton.fit import fit_crc, read_messages, workout_name
from bryton.fitgen import SpecError, encode_fit, encode_json

REF = Path(__file__).resolve().parent.parent / "reference"
KIND = {0: "active", 1: "rest", 2: "warmup", 3: "cooldown"}


def ref_steps():
    fit = (REF / "ble_transfer_FTP_workout.fit").read_bytes()
    steps = []
    for n, v in read_messages(fit):
        if n == 27:
            steps.append({"kind": KIND[v[7][0]],
                          "seconds": int.from_bytes(v[2], "little") // 1000,
                          "power": (int.from_bytes(v[5], "little") - 100000,
                                    int.from_bytes(v[6], "little") - 100000),
                          "name": v[0].split(b"\0")[0].decode()})
    created = int.from_bytes([v for n, v in read_messages(fit) if n == 0][0][4], "little")
    return fit, steps, created


def test_fit_reproduces_the_app_file_byte_exact():
    fit, steps, created = ref_steps()
    assert encode_fit("Intervalltraini", steps, time_created=created) == fit


def test_json_reproduces_the_app_file_byte_exact():
    js = (REF / "ble_transfer_FTP_workout.json").read_bytes()
    _, steps, _ = ref_steps()
    assert encode_json("Intervalltraining", steps, create_time_ms=1570053600000) == js


def test_generated_fit_is_valid():
    steps = [{"kind": "warmup", "seconds": 300, "power": (50, 60)},
             {"kind": "active", "seconds": 480, "power": (95, 100), "cadence": (90, 100)},
             {"kind": "cooldown", "seconds": 300, "power": (40, 50)}]
    fit = encode_fit("CC_Demo", steps)
    assert workout_name(fit) == "CC_Demo"
    assert fit[-2:] == fit_crc(fit[:-2]).to_bytes(2, "little")
    assert len([1 for n, _ in read_messages(fit) if n == 27]) == 3
    js = json.loads(encode_json("CC_Demo", steps, description="Test"))
    assert js["interval"][1] == {"target_low": 90, "target_high": 100, "target_type": "cadence"}
    assert js["interval"][0] == {"target_type": ""} and js["description"] == "Test"


@pytest.mark.parametrize("bad", [
    [], [{"kind": "x", "seconds": 60, "power": (50, 60)}],
    [{"kind": "active", "seconds": 0, "power": (50, 60)}],
    [{"kind": "active", "seconds": 60, "power": (70, 60)}],
    [{"kind": "active", "seconds": 60, "power": (50, 60), "cadence": (300, 310)}],
])
def test_invalid_specs_rejected(bad):
    with pytest.raises(SpecError):
        encode_fit("CC_X", bad)


def test_leading_entry_is_prepended():
    steps = [{"kind": "active", "seconds": 60, "power": (50, 60), "cadence": (80, 90)}] * 2
    doc = json.loads(encode_json("CC_X", steps, leading=(11, 101)))
    assert doc["interval"][0] == {"target_low": 11, "target_high": 101, "target_type": "cadence"}
    assert len(doc["interval"]) == 3
    assert len(json.loads(encode_json("CC_X", steps))["interval"]) == 2


def test_model_reproduces_device_observations():
    from bryton.fitgen import device_view
    # Test_CadMap (app format): 41/0, 52/111, 63/122 ...
    cadmap = [{"target_low": l, "target_high": h, "target_type": "cadence"}
              for l, h in [(41, 111), (52, 122), (63, 133)]]
    assert device_view(cadmap) == [(41, 0), (52, 111), (63, 122)]
    # Test_CadMix (first try): "" entries carrying target_high are ignored -> 85/0, 100/0
    cadmix = [{"target_high": 95, "target_type": ""},
              {"target_low": 85, "target_high": 95, "target_type": "cadence"},
              {"target_high": 110, "target_type": ""},
              {"target_low": 100, "target_high": 110, "target_type": "cadence"},
              {"target_type": ""}]
    assert device_view(cadmix) == [None, (85, 0), None, (100, 0), None]


def test_corrected_encoding_shows_every_range():
    from bryton.fitgen import build_files, device_view
    c = lambda lo, hi: {"kind": "active", "seconds": 20, "power": (70, 80), "cadence": (lo, hi)}
    p = {"kind": "rest", "seconds": 20, "power": (50, 60)}
    steps = [c(85, 95), p, c(100, 110), c(70, 80), p]
    _, js, final = build_files("CC_All", steps, start_seconds=20, correct_cadence=True, high_first=False)  # legacy workaround
    assert final[0]["name"] == "Start" and len(final) == 6
    view = device_view(json.loads(js)["interval"])
    assert view == [(0, 0), (85, 95), (0, 0), (100, 110), (70, 80), None]


def test_no_dummy_needed_without_first_cadence():
    from bryton.fitgen import with_dummy_first_step
    steps = [{"kind": "warmup", "seconds": 60, "power": (50, 60)},
             {"kind": "active", "seconds": 60, "power": (50, 60), "cadence": (80, 90)}]
    assert with_dummy_first_step(steps) == steps


def test_plan_dates():
    import datetime
    from bryton.fitgen import resolve_plan
    steps = [{"kind": "active", "seconds": 30, "power": (50, 60)}]
    today = datetime.date(2026, 10, 16)
    plan = resolve_plan(["today", "+2", "2026-10-30"], today=today)
    assert plan == ["2026-10-16", "2026-10-18", "2026-10-30"]
    assert json.loads(encode_json("CC_X", steps, plan=plan))["plan"] == plan
    with pytest.raises(SpecError):
        encode_json("CC_X", steps, plan=["16.10.2026"])


def test_primary_cadence_target_in_fit():
    steps = [{"kind": "warmup", "seconds": 30, "power": (50, 60)},
             {"kind": "active", "seconds": 30, "target": "cadence", "cadence": (95, 105)}]
    fit = encode_fit("CC_X", steps)
    st = [v for n, v in read_messages(fit) if n == 27]
    assert st[1][3] == b"\x03"                                   # target_type cadence
    assert int.from_bytes(st[1][5], "little") == 95 and int.from_bytes(st[1][6], "little") == 105
    assert st[0][3] == b"\xf5"                                   # power % FTP stays 245
    assert json.loads(encode_json("CC_X", steps))["interval"][1] == {"target_type": ""}


def test_build_files_without_correction_is_app_format():
    from bryton.fitgen import build_files
    steps = [{"kind": "active", "seconds": 20, "power": (50, 60), "cadence": (80, 90)}] * 3
    _, js, final = build_files("CC_App", steps, correct_cadence=False)
    assert len(final) == 3
    assert json.loads(js)["interval"] == [{"target_low": 80, "target_high": 90, "target_type": "cadence"}] * 3


def test_high_first_key_order_and_no_shift():
    import json
    from bryton.fitgen import build_files
    steps = [{"kind": "active", "seconds": 20, "power": (70, 80)},
             {"kind": "active", "seconds": 20, "power": (70, 80), "cadence": (85, 95)}]
    fit, js, final = build_files("Test_Order", steps, correct_cadence=False, high_first=True)
    assert len(final) == 2  # no Start step
    assert json.loads(js)["interval"] == [{"target_type": ""},
                                          {"target_high": 95, "target_low": 85, "target_type": "cadence"}]
    assert js.decode().index('"target_high"') < js.decode().index('"target_low"')


def test_default_is_high_first_without_workaround():
    import json
    from bryton.fitgen import build_files, device_view
    c = lambda lo, hi: {"kind": "active", "seconds": 20, "power": (70, 80), "cadence": (lo, hi)}
    p = {"kind": "rest", "seconds": 20, "power": (50, 60)}
    steps = [c(85, 95), p, c(100, 110), c(70, 80), p]
    _, js, final = build_files("CC_All", steps)
    assert len(final) == 5  # no Start step
    interval = json.loads(js)["interval"]
    assert [list(e)[:2] for e in interval if e.get("target_type") == "cadence"] == [["target_high", "target_low"]] * 3
    assert [e.get("target_low") for e in interval] == [85, None, 100, 70, None]
