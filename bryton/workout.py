"""Build safe test workouts from the captured reference files."""
import json
from pathlib import Path

from bryton.fit import set_workout_name, workout_name

REF = Path(__file__).resolve().parent.parent / "reference"
TEST_PREFIX = "Test_"


class UnsafeName(ValueError):
    pass


FIT_NAME_MAX = 15  # wkt_name field is 16 bytes incl. terminator
NAME_MAX = 20


def check_name(name):
    """Only `Test_…` names may be written. This protects 4x4.* and every real workout."""
    if not name.startswith(TEST_PREFIX) or len(name) <= len(TEST_PREFIX):
        raise UnsafeName(f"refusing to write workout {name!r}: name must start with {TEST_PREFIX!r}")
    if len(name) > NAME_MAX:
        raise UnsafeName(f"refusing to write workout {name!r}: longer than {NAME_MAX} characters")
    if not all(c.isalnum() or c in "_-" for c in name):
        raise UnsafeName(f"refusing to write workout {name!r}: only letters, digits, _ and -")


def make_test_fit(name, source=REF / "ble_transfer_FTP_workout.fit"):
    check_name(name)
    return set_workout_name(Path(source).read_bytes(), name[:FIT_NAME_MAX])


def make_test_json(name, source=REF / "ble_transfer_FTP_workout.json"):
    check_name(name)
    doc = json.loads(Path(source).read_bytes())
    doc["name"] = name
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()


def verify(fit, js):
    """Return the JSON name or raise; the FIT name must be its 15-character prefix, both Test_…"""
    fit_name, json_name = workout_name(fit), json.loads(js)["name"]
    check_name(json_name)
    check_name(fit_name)
    if fit_name != json_name[:FIT_NAME_MAX]:
        raise UnsafeName(f"FIT name {fit_name!r} does not match JSON name {json_name!r}")
    return json_name


def reference_files():
    """The captured app workout, byte-exact (used only for the verbatim replay)."""
    return (REF / "ble_transfer_FTP_workout.fit").read_bytes(), (REF / "ble_transfer_FTP_workout.json").read_bytes()
