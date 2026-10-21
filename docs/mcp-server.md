# MCP server for Claude Desktop and Claude Code

Describe a training in plain language; Claude turns it into steps, shows a preview, and (after you confirm)
uploads it to the S810 over Bluetooth.

## Architecture

```
Claude Desktop / Claude Code  --stdio-->  bryton-mcp   (builds files, keeps a local calendar copy)
                                              |  HTTP, 127.0.0.1 only, token
                                              v
                                          bryton-agent (Bluetooth upload)  --BLE-->  S810
```

The Bluetooth part is a separate process so that a crash or a missing permission cannot take the MCP server
down. The MCP server starts the agent itself on the first upload; this works from Claude Desktop.
If macOS refuses Bluetooth to the process (CoreBluetooth aborts with exit 134 / SIGABRT, or the server tells you
the agent could not be started), start `bryton-agent` by hand in a Terminal that may use Bluetooth
(System Settings > Privacy & Security > Bluetooth).

The agent listens on `127.0.0.1` only and requires the token in `~/.bryton_s810/agent_token` (created at start,
readable only by you).

## Install

```bash
python3 -m venv ~/.venvs/bryton && source ~/.venvs/bryton/bin/activate
pip install -e ".[mcp]"               # from a checkout of this repository
# or straight from GitHub:
pip install "bryton-s810-ble[mcp] @ git+https://github.com/lassehein/bryton-s810-ble.git"
```

Optional settings in `~/.bryton_s810/config.json` (used for the preview and for name safety):

```json
{"ftp": 220, "mhr": 188, "lthr": 154, "known_workouts": ["4x4", "MyOldWorkout"], "known_routes": ["My Old Route"]}
```

- `ftp`, `mhr`, `lthr` only convert percentages to watts/bpm in the preview. The device uses its own zones.
- `known_workouts`, `known_routes`: names that already exist on the device. The server will not overwrite them without `overwrite`.

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (use the absolute path of the
venv script, find it with `which bryton-mcp`), then quit and restart Claude Desktop:

```json
{"mcpServers": {"bryton-s810": {"command": "/Users/you/.venvs/bryton/bin/bryton-mcp"}}}
```

### Claude Code

```bash
claude mcp add bryton-s810 -- /Users/you/.venvs/bryton/bin/bryton-mcp
```

Claude Code started from a Terminal inherits that Terminal's Bluetooth permission, so the MCP server's
automatic agent start should work there (untested). Otherwise start `bryton-agent` yourself.

## Use

1. Phone and tablet: Bluetooth off. S810 on, not connected via USB.
2. In Claude: *"Plan 4x4 minutes at 110-120 % FTP with 4 minutes rest, cadence 90-100, upload it as VO2_4x4."*

## Tools

| Tool | What it does |
|---|---|
| `preview_workout` | Build and show the workout as text. No device access. |
| `upload_workout` | Upload to the workout list, or to the calendar with `date`. |
| `list_known_workouts` | Names this server knows to be on the device. |
| `list_calendar` | The calendar this server last sent. |
| `remove_calendar_entry` | Remove a planned day and resend the calendar. |
| `preview_route` | Check a GPX file and show distance, points and ascent. No device access. |
| `upload_route` | Upload a GPX route; it appears under `Courses > Route` on the device. |
| `read_device_profile` | Read FTP, max HR, LTHR, height and weight from the device (read-only) and use them for later previews and plans instead of `config.json`. |
| `preview_pace_plan` | Calculate a Pace Pilot plan for a GPX route and show it (no device access). |
| `upload_pace_plan` | Upload the route with its plan; it appears under `Courses > Route` and `Pace Pilot`. |
| `list_rides` | List the rides on the device that the Bryton app has not fetched yet (read-only). |
| `download_rides` | Save those rides as FIT files in `~/.bryton_s810/rides` (read-only, the app can still sync them). |
| `list_known_routes` | Route names this server knows to be on the device. |
| `agent_status` | Is the Bluetooth agent reachable? |

### Steps

```json
[{"kind": "warmup", "minutes": 10, "power": [50, 60]},
 {"repeat": 4, "steps": [
   {"kind": "active", "minutes": 4, "power": [110, 120], "cadence": [90, 100], "name": "VO2"},
   {"kind": "rest", "minutes": 4, "power": [40, 50]}]},
 {"kind": "cooldown", "minutes": 10, "power": [40, 50]}]
```

- Duration: one of `minutes`, `seconds`, `km`, `meters`.
- Target: exactly one of `power` (% FTP), `mhr` (% max HR), `lthr` (% LTHR), `speed` (km/h). A number is a fixed
  target, `[low, high]` a range. Optional `cadence` (rpm) on top. A step with only `cadence` is a cadence step.
- `name`: up to 9 characters. Workout name: 1-20 characters of letters, digits, `_`, `-`.
- Umlauts in descriptions are transliterated (`ä` becomes `ae`).

### Routes

Give `gpx_path` (a `.gpx` file on your computer) or `gpx_text`. Tracks and routes are accepted; dense tracks are
thinned to roughly one point every 30 m. The route list of the device cannot be read over Bluetooth, so Claude
must ask whether the name is new (`name_confirmed_new`). Names are at most 32 bytes. Routes have no calendar and
no Pace Pilot data; turn instructions and points of interest are not generated. Delete routes on the device.

### Safety rules built into `upload_workout`

- The device list cannot be read over Bluetooth, and a name that exists is replaced without warning.
  Claude must ask you whether the name is new (`name_confirmed_new`) or may be replaced (`overwrite`).
- Calendar uploads replace the **whole** device calendar. The server keeps a copy of everything it planned
  (`~/.bryton_s810/calendar/`) and resends all days from today on. Entries planned in the Bryton app are not
  known to the server and disappear. Claude must get your agreement (`calendar_replace_ok`).
- A failed upload leaves the local copy unchanged and never leaves the device in transfer mode.

## Troubleshooting

- *"S810 not found"*: it is connected to a phone/tablet, in USB mode, or switched off. Switch Bluetooth off
  on the phone and reconnect nothing.
- *Exit 134 / SIGABRT*, or "agent could not be started": the process has no Bluetooth permission. Run
  `bryton-agent` from a Terminal that has it and leave it open.
- *MCP server missing in Claude Desktop*: use the absolute path to the venv's `bryton-mcp`, then restart
  Claude Desktop completely (quit, not just close the window). Logs: `~/Library/Logs/Claude/mcp*.log`.
- Agent log: `~/.bryton_s810/agent.log`, upload logs: `~/.bryton_s810/logs/`.

### Rides

`list_rides` and `download_rides` read finished rides over Bluetooth. They only see rides the Bryton app has not fetched
yet; the transfer does not mark them as synced, so the app still gets them on its next sync. Files are saved to
`~/.bryton_s810/rides/<UTC start>_<id>.fit` (heart rate, power and position data: keep them private). The phone's
Bluetooth must be off and the device must not be connected via USB.

### Pace Pilot

`preview_pace_plan` / `upload_pace_plan` calculate a pacing plan for a GPX route: up to 12 segments with target power
range, estimated speed and time, a hint per segment, and nutrition and hydration. The device shows it as the
app's Pace Pilot does. Inputs: effort (`very_light` to `maximal`), rider and bike weight, bike type, surface, language,
and optionally the six data fields on the screen. FTP, max HR and LTHR come from `config.json`; the weights can be
set there too (`rider_kg`, `bike_kg`), otherwise Claude asks.

FTP, max HR, LTHR and the rider weight are taken from the device (`read_device_profile`, remembered in
`~/.bryton_s810/device_profile.json`) and override `config.json`; run it again after you change the profile on the device.
The bike weight is not stored on the device: pass `bike_kg` or set it in `config.json`.

**Wind and weather.** Either give `wind_kmh`, `wind_from_deg` (where the wind blows *from*: 0 = north, 90 = east) and
`temperature_c`, or give `ride_date` (today up to about 15 days ahead) and `start_time` to use the free
[Open-Meteo](https://open-meteo.com) forecast at the middle of the route (needs internet; the 10 m wind is scaled by 0.7
to rider height, a rough estimate). The wind is applied per segment from the direction of travel and changes speeds and
times, so also the nutrition schedule; the target watts stay the same. Values you give win over the forecast.

The plan is a physics model (power, grade, drag, rolling resistance). Only the `moderate`
effort level is checked against the Bryton app's own plan; the other levels are estimates. Compare the first segments
with the app before relying on it.
