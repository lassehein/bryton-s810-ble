import asyncio
import base64
import json
import threading
import urllib.request

import pytest


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    import bryton.service as svc
    monkeypatch.setattr(svc, "STATE_DIR", tmp_path)
    monkeypatch.setattr(svc, "KNOWN_FILE", tmp_path / "known_workouts.json")
    monkeypatch.setattr(svc, "KNOWN_ROUTES_FILE", tmp_path / "known_routes.json")
    monkeypatch.setattr(svc, "CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({"ftp": 220, "known_workouts": ["4x4"]}))
    return tmp_path


FOUR_BY_FOUR = [{"kind": "warmup", "minutes": 10, "power": [50, 60]},
                {"repeat": 4, "steps": [{"kind": "active", "minutes": 4, "power": [110, 120], "cadence": [90, 100]},
                                        {"kind": "rest", "minutes": 4, "power": [40, 50]}]},
                {"kind": "cooldown", "minutes": 10, "power": [40, 50]}]


def test_normalize_expands_repeats_and_cadence_only():
    from bryton.service import normalize_steps
    steps = normalize_steps(FOUR_BY_FOUR + [{"kind": "active", "seconds": 60, "cadence": [110, 120]}])
    assert len(steps) == 1 + 8 + 1 + 1
    assert steps[1] == {"kind": "active", "seconds": 240, "power": (110, 120), "cadence": (90, 100)}
    assert steps[-1]["target"] == "cadence" and "power" not in steps[-1]


@pytest.mark.parametrize("bad", [[], [{"kind": "active", "minutes": 5}], [{"kind": "active", "power": [50, 60]}],
                                 [{"repeat": 99, "steps": [{"kind": "active", "minutes": 1, "power": [50, 60]}]}]])
def test_normalize_rejects(bad):
    from bryton.service import ServiceError, normalize_steps
    with pytest.raises(ServiceError):
        normalize_steps(bad)


def test_build_and_preview():
    from bryton.service import build, preview_text
    fit, js, final, warnings = build("CC_4x4_Neu", FOUR_BY_FOUR, "4x4 VO2max, Trittfrequenz 90-100 in den Intervallen")
    text = preview_text("CC_4x4_Neu", final, "", 220, warnings)
    assert "110-120 % FTP = 242-264 W" in text and "cadence 90-100 rpm" in text
    assert json.loads(js)["description"].startswith("4x4 VO2max")
    assert not any("Start" in w for w in warnings)  # first step has no cadence -> no Start step


def test_name_rules_and_refusals():
    from bryton.service import ServiceError, build, remember_name, upload_refusal
    with pytest.raises(ServiceError):
        build("4x4 neu!", FOUR_BY_FOUR)
    assert "already on the device" in upload_refusal("4x4", True, False)
    assert upload_refusal("4x4", False, True) is None
    assert "confirm" in upload_refusal("CC_Neu", False, False)
    assert upload_refusal("CC_Neu", True, False) is None
    remember_name("CC_Neu")
    assert "already on the device" in upload_refusal("cc_neu", True, False)


def _start_agent(uploader):
    from http.server import ThreadingHTTPServer
    from bryton import agent
    srv = ThreadingHTTPServer(("127.0.0.1", 0), agent.make_handler("secret", uploader))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _post(url, payload, token):
    req = urllib.request.Request(url + "/upload", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "X-Bryton-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_agent_token_and_upload(state):
    got = {}

    async def fake_upload(workouts, log=print, target="plan"):
        (got["fit"], got["json"]), = workouts
        log("TX fake")

    srv, url = _start_agent(fake_upload)
    try:
        payload = {"name": "CC_X", "fit": base64.b64encode(b"F").decode(), "json": base64.b64encode(b"J").decode()}
        assert _post(url, payload, "wrong")[0] == 403
        code, body = _post(url, payload, "secret")
        assert code == 200 and body["ok"] and got == {"fit": b"F", "json": b"J"}
    finally:
        srv.shutdown()


def test_agent_reports_failure(state):
    from bryton.ble import UploadError

    async def failing(workouts, log=print, target="plan"):
        raise UploadError("S810 not found")

    srv, url = _start_agent(failing)
    try:
        payload = {"name": "CC_X", "fit": "", "json": ""}
        code, body = _post(url, payload, "secret")
        assert code == 200 and not body["ok"] and "not found" in body["message"]
    finally:
        srv.shutdown()


def test_mcp_tools_registered_and_guarded(monkeypatch, state):
    monkeypatch.setenv("BRYTON_NO_AUTOSTART", "1")
    from bryton import mcp_server as mod
    tools = asyncio.run(mod.server.list_tools())
    names = {t.name for t in tools}
    assert {"preview_workout", "upload_workout", "list_known_workouts", "agent_status"} <= names
    # without confirmation nothing is sent
    called = []
    monkeypatch.setattr(mod, "_agent", lambda *a, **k: called.append(a) or {"ok": True, "message": "x"})
    out = mod.upload_workout("CC_New", FOUR_BY_FOUR)
    assert out.startswith("Not uploaded") and not called
    out = mod.upload_workout("CC_New", FOUR_BY_FOUR, name_confirmed_new=True)
    assert called and "CC_New" in mod.list_known_workouts()


def test_resolve_date_and_calendar_json():
    import datetime
    import json
    from bryton.service import build, entry_key, resolve_date
    today = datetime.date(2026, 10, 16)
    assert resolve_date("", today) is None
    assert resolve_date("tomorrow", today) == "2026-10-17"
    assert resolve_date("+3", today) == "2026-10-19"
    assert resolve_date("2026-11-01", today) == "2026-11-01"
    _, js, _, _ = build("Cal", [{"kind": "active", "seconds": 20, "power": [70, 80]}], date="2026-10-17")
    assert json.loads(js)["plan"] == ["2026-10-17"]
    assert entry_key("Cal", "2026-10-17") == "2026-10-17/Cal"


def test_mcp_calendar_sends_whole_calendar(monkeypatch, state):
    monkeypatch.setenv("BRYTON_NO_AUTOSTART", "1")
    from bryton import mcp_server as mod
    sent = []
    monkeypatch.setattr(mod, "_agent", lambda path, payload=None, **k: sent.append(payload) or {"ok": True, "message": "x"})
    # guarded without calendar_replace_ok
    assert mod.upload_workout("Cal_A", FOUR_BY_FOUR, date="2099-01-01").startswith("Not uploaded") and not sent
    out = mod.upload_workout("Cal_A", FOUR_BY_FOUR, date="2099-01-01", calendar_replace_ok=True)
    assert "Planned" in out and len(sent[-1]["workouts"]) == 1 and sent[-1]["target"] == "calendar"
    mod.upload_workout("Cal_B", FOUR_BY_FOUR, date="2099-01-02", calendar_replace_ok=True)
    assert len(sent[-1]["workouts"]) == 2  # second call resends the first day too
    assert "already planned" in mod.upload_workout("Cal_B", FOUR_BY_FOUR, date="2099-01-02", calendar_replace_ok=True)
    assert "2099-01-01  Cal_A" in mod.list_calendar()
    mod.remove_calendar_entry("2099-01-01", "Cal_A", calendar_replace_ok=True)
    assert len(sent[-1]["workouts"]) == 1 and "Cal_A" not in mod.list_calendar()
    # failed upload leaves the local copy untouched
    monkeypatch.setattr(mod, "_agent", lambda *a, **k: {"ok": False, "message": "boom"})
    assert "Not uploaded" in mod.upload_workout("Cal_C", FOUR_BY_FOUR, date="2099-01-03", calendar_replace_ok=True)
    assert "Cal_C" not in mod.list_calendar()


GPX = ('<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
       + "".join(f'<trkpt lat="{47 + i * 3e-4}" lon="8.0"><ele>{400 + (i % 40)}</ele></trkpt>' for i in range(300))
       + "</trkseg></trk></gpx>")


def test_route_service_and_mcp_tools(monkeypatch, tmp_path):
    import gzip
    from bryton import mcp_server as mod
    from bryton import route
    from bryton.service import ServiceError, build_route, check_route_name, route_refusal
    payload, info = build_route("Loop", gpx_text=GPX)
    assert info["points"] > 10 and 8 < info["km"] < 11 and route.decode_fit(gzip.decompress(payload))
    gpx_file = tmp_path / "r.gpx"
    gpx_file.write_text(GPX)
    assert build_route("Loop", gpx_path=str(gpx_file))[1] == info
    for bad in ({"gpx_path": str(tmp_path / "nope.gpx")}, {"gpx_path": str(gpx_file), "gpx_text": GPX}, {}):
        with pytest.raises(ServiceError):
            build_route("Loop", **bad)
    with pytest.raises(ServiceError):
        check_route_name("x" * 33)
    assert "confirm" in route_refusal("New", False, False) and route_refusal("New", True, False) is None
    sent = []
    monkeypatch.setenv("BRYTON_NO_AUTOSTART", "1")
    monkeypatch.setattr(mod, "_agent", lambda path, payload=None, **k: sent.append(payload) or {"ok": True, "message": "ok"})
    assert mod.upload_route("Loop", gpx_text=GPX).startswith("Not uploaded") and not sent
    out = mod.upload_route("Loop", gpx_text=GPX, name_confirmed_new=True)
    assert sent and sent[0]["kind"] == "route" and "Loop" in mod.list_known_routes() and "km" in out
    assert "already" in mod.upload_route("loop", gpx_text=GPX, name_confirmed_new=True)
    assert "Error" in mod.preview_route("Loop", gpx_text="<gpx/>")


def test_agent_route_upload(state):
    got = {}

    async def fake_route(payload, name, log=print):
        got["payload"], got["name"] = payload, name

    from http.server import ThreadingHTTPServer
    from bryton import agent
    srv = ThreadingHTTPServer(("127.0.0.1", 0), agent.make_handler("secret", route_uploader=fake_route))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        data = b"\x1f\x8b" + b"\0" * 30
        code, body = _post(url, {"kind": "route", "name": "R", "payload": base64.b64encode(data).decode()}, "secret")
        assert code == 200 and body["ok"] and got == {"payload": data, "name": "R"}
        assert _post(url, {"kind": "route", "name": "R", "payload": base64.b64encode(b"plain").decode()}, "secret")[0] == 400
    finally:
        srv.shutdown()


def test_outdated_agent_is_refused(monkeypatch):
    from bryton import mcp_server as mod
    monkeypatch.setattr(mod, "_raw_agent", lambda path, payload=None, timeout=150: {"ok": True, "busy": False})  # no "api"
    with pytest.raises(mod.AgentOutdated):
        mod._agent("/upload", {"x": 1})
    assert "old Bluetooth agent" in mod.agent_status()
    monkeypatch.setattr(mod, "_raw_agent", lambda path, payload=None, timeout=150: {"ok": True, "busy": False, "api": 2})
    assert mod.agent_status() == "Agent running"


def test_route_warns_without_elevation():
    from bryton.service import build_route, route_text
    flat = GPX.replace("<ele>", "<x>").replace("</ele>", "</x>")
    payload, info = build_route("Flat", gpx_text=flat)
    assert info["flat"] and "no <ele>" in route_text("Flat", info)
    assert "no <ele>" not in route_text("Hilly", build_route("Hilly", gpx_text=GPX)[1])
