# Workout file formats (Bryton Rider S810)

A workout is a FIT file plus a JSON file with the same name. Generator: [`bryton/fitgen.py`](../bryton/fitgen.py).
Reference files written by the Bryton app: [`reference/app_targets/`](../reference/app_targets/).

## FIT

Standard FIT workout file: `file_id`, one `workout` message and one `workout_step` message per step. Layout
copied from the app's files (profile version 2167, workout name up to 15 characters, step name up to 9).

| `workout_step` field | Use |
|---|---|
| `duration_type` 0 | time, `duration_value` in ms |
| `duration_type` 1 | distance, `duration_value` in **centimetres** (5 km = 500000) |
| `target_type`, `custom_target_value_low/high` | the target, see below |
| `intensity` | 0 active, 1 rest, 2 warmup, 3 cooldown |

### Targets

A fixed target is a range with `low == high`.

| Target | `target_type` | Value |
|---|---|---|
| % FTP | 245 | `100000 + percent` |
| % LTHR | 248 | `100000 + percent` |
| % max HR | 249 | `100000 + percent` |
| Speed | 0 | mm/s, truncated (27 km/h = 7500, 33 km/h = 9166) |
| Cadence | 3 | rpm |

The app offers only these for the S810, one target per step. The FIT profile also has absolute watts and
bpm (`target_type` 4 and 1); early experiments suggested the device shows them, but the generator does not
produce them *(unverified)*.
The device converts percentages with the zones stored on it (watts = percent of FTP; bpm rounded down).

## JSON

```json
{"name":"4x4","description":"shown on the device","plan":[],"provider":"bryton",
 "interval":[{"target_type":""}, {"target_low":80,"target_high":90,"target_type":"cadence"}],
 "create_time":1792184880000,"ver":5}
```

- `name` becomes the file name on the device. Keep it to letters, digits, `_` and `-`.
- `description` is shown on the device. ASCII only is safest.
- `plan` is a list of `YYYY-MM-DD` dates. For a calendar upload it holds exactly that day.
- `interval` holds one entry per FIT step. `{"target_type":""}` means no secondary target.

### Cadence as a secondary target

A step can carry a cadence range next to its power/HR/speed target. This lives only in the JSON entry of that step.

**The device reads `target_high` before `target_low`.** Its JSON reader keeps a slot counter that moves to the
next step after `target_low`; a `target_high` that follows is written to the *next* step. Entries must
therefore be written as `{"target_high":95,"target_low":85,"target_type":"cadence"}`. With the opposite key order
(which the Bryton app writes for workout-list uploads) every step shows its own lower value with the upper value of
the previous step (`80/0` in step 1). Confirmed on the device with `Test_CadOrder`.

Steps without a cadence target need `{"target_type":""}`: exactly one entry per FIT step.

A step with **only** a cadence target can also use the FIT primary target (`target_type` 3).

## Calendar

A calendar entry is the same pair of files with `plan: ["YYYY-MM-DD"]`, uploaded with destination `01`
(see [bluetooth.md](bluetooth.md)). The `plan` field alone does not make a workout appear in the calendar.
