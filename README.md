# bryton-s810-ble

Write workouts to a **Bryton Rider S810** bike computer over Bluetooth LE, the way the Bryton Active app does,
and plan them with Claude through an MCP server.

> **Unofficial.** Not affiliated with or endorsed by Bryton. The protocol was reverse-engineered from one device
> (firmware `IV049.002.0008.04`). Uploads replace workouts with the same name without asking. Back up your
> device over USB first and use at your own risk. Other Bryton models are untested.

## What works

- Upload a workout (FIT + JSON) to the device's workout list, or plan it for a day in the **calendar**.
- Targets per step: % FTP, % max HR, % LTHR, speed, cadence; fixed value or range; time or distance steps.
- Workout descriptions, which the device displays.
- **Routes from GPX files**, shown on the device under `Courses > Route`.
- **Rider profile from the device:** FTP, max HR, LTHR and weight are read from the device and used for plans.
- **Download recorded rides** as FIT files (those the Bryton app has not fetched yet; read-only, the app can still sync them afterwards).
- **Pace Pilot (experimental):** a pacing plan (target power per segment, nutrition) calculated for a GPX route and sent as the app does, from the MCP server (`preview_pace_plan`, `upload_pace_plan`) or with `tools/bryton_pace_test.py`. Wind and temperature can be given or taken from the Open-Meteo forecast; only the "moderate" effort level is calibrated.
- Correct cadence ranges next to power/HR/speed targets (the device needs `target_high` before `target_low`, [details](docs/file-formats.md)).
- An **MCP server** so Claude Desktop / Claude Code can turn "4x4 minutes at 110-120 % FTP" into a workout and upload it
  after you confirm.

Not supported: reading the workout or route list over Bluetooth, deleting workouts or routes, absolute watt/bpm targets, turn-by-turn instructions in routes.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[mcp]"          # from a checkout; bleak is always installed, mcp only for the MCP server
# or directly from GitHub:
pip install "bryton-s810-ble[mcp] @ git+https://github.com/lassehein/bryton-s810-ble.git"
```

Needs Python 3.10+. Tested on macOS (Bluetooth via CoreBluetooth); other platforms are untested.

## Use with Claude

See **[docs/mcp-server.md](docs/mcp-server.md)** for Claude Desktop and Claude Code. In short:

1. Register `bryton-mcp` with Claude.
2. Switch Bluetooth off on your phone, then ask Claude for a workout. The server starts the Bluetooth agent
   itself; if macOS blocks that, run `bryton-agent` in a Terminal.

## Use from Python

```python
import asyncio
from bryton.service import build
from bryton.ble import upload

steps = [{"kind": "warmup", "minutes": 5, "power": [50, 60]},
         {"repeat": 4, "steps": [{"kind": "active", "minutes": 4, "power": [110, 120], "cadence": [90, 100]},
                                 {"kind": "rest", "minutes": 4, "power": [40, 50]}]},
         {"kind": "cooldown", "minutes": 5, "power": [40, 50]}]
fit, js, final_steps, warnings = build("VO2_4x4", steps, "4x4 minutes, cadence 90-100")
asyncio.run(upload(fit, js))                 # workout list
# calendar: build(..., date="2026-11-03"); asyncio.run(upload_many([(fit, js)], target="calendar"))
```

## Documentation

- [Bluetooth protocol](docs/bluetooth.md): characteristics, handshake, upload sequence, packet format.
- [File formats](docs/file-formats.md): FIT and JSON layout, targets, cadence, calendar, routes.
- [MCP server](docs/mcp-server.md): setup, tools, safety rules, troubleshooting.
- [Reverse-engineering notes](docs/reverse-engineering.md): how the protocol was found.

## Development

```bash
pip install -e ".[mcp,dev]" && pytest
```

The tests need no device (a simulated device is used). Anything that sends data to a real device lives in
`tools/` and does a dry run unless `--send` is given. Use names starting with `Test_` for experiments.

## License

MIT, see [LICENSE](LICENSE).
