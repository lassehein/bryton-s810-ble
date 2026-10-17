import json
import struct

import pytest

from bryton.fit import fit_crc, read_messages, workout_name
from bryton.protocol import build_transfer, reassemble
from bryton.workout import UnsafeName, check_name, make_test_fit, make_test_json, verify


def test_fit_rename_keeps_structure_and_crc():
    fit = make_test_fit("Test_BLE1")
    assert workout_name(fit) == "Test_BLE1"
    assert struct.unpack("<H", fit[-2:])[0] == fit_crc(fit[:-2])
    assert len(fit) == 683
    assert len([1 for n, _ in read_messages(fit) if n == 27]) == 18


def test_json_rename_only_changes_name():
    js = json.loads(make_test_json("Test_BLE1"))
    ref = json.loads(make_test_json("Test_BLE1", source=make_ref()))
    assert js["name"] == "Test_BLE1"
    assert js["provider"] == "bryton" and js["ver"] == 5 and len(js["interval"]) == len(ref["interval"])


def make_ref():
    from bryton.workout import REF
    return REF / "ble_transfer_FTP_workout.json"


@pytest.mark.parametrize("bad", ["4x4", "Power_Test_3x8", "Intervalltraining", "test_x", "Test_", "Test_a/b", "Test_..", ""])
def test_unsafe_names_refused(bad):
    with pytest.raises(UnsafeName):
        check_name(bad)
    with pytest.raises(UnsafeName):
        make_test_fit(bad)


def test_name_too_long_refused():
    with pytest.raises(ValueError):
        make_test_fit("Test_" + "x" * 20)


def test_verify_requires_matching_names():
    assert verify(make_test_fit("Test_BLE1"), make_test_json("Test_BLE1")) == "Test_BLE1"
    with pytest.raises(UnsafeName):
        verify(make_test_fit("Test_BLE1"), make_test_json("Test_BLE2"))


def test_transfer_roundtrip_for_test_files():
    for data, b5 in ((make_test_fit("Test_BLE1"), 0x63), (make_test_json("Test_BLE1"), 0x1E)):
        assert reassemble(*build_transfer(data, b5)) == data
