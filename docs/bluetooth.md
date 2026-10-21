# Bryton Rider S810: Bluetooth LE protocol

How the Bryton Active app writes a workout to the device, as observed from iPhone traffic (PacketLogger
captures) and verified by writing files from a Mac. Everything here is reverse-engineered from one device
(firmware `IV049.002.0008.04`) and may differ on other models or firmware. Items marked *(unverified)* were
not confirmed on the device.

Reference implementation: [`bryton/protocol.py`](../bryton/protocol.py) (framing),
[`bryton/ble.py`](../bryton/ble.py) (session). Real traffic: [`reference/capture_transfer_window.txt`](../reference/capture_transfer_window.txt).

## Connecting

- The device advertises as `RiderS810` (service `0x2014`). A device that is connected to a phone stops
  advertising, so switch Bluetooth off on the phone/tablet first. A device in USB mode is not reachable.
- The ATT MTU must be at least 153 (150-byte packets plus 3 bytes ATT header). CoreBluetooth/bleak negotiates this.
- macOS shows no MAC addresses, only per-host UUIDs. Select the device by name.

## Characteristics

| Handle | UUID | Role | Properties |
|---|---|---|---|
| `0x0d` | `454d1188-a122-058d-9b42-9d0f3772af82` | file channel (device info, file transfer) | write-without-response, write, notify |
| `0x10` | `454d2288-a122-058d-9b42-9d0f3772af82` | control / handshake | write, notify |
| `0x18` | `50b81188-c2de-a994-094d-340090726877` | command channel (request / reply) | write, notify |

Subscribe to notifications on all three before writing.

## Handshake

Write `08 08` + ASCII `12345678ios` to `0x10`. The device answers `08 28 02` on `0x10`. The string is a fixed
value in the app.

## `0x18` command frames

`<cmd> <sub> [payload] <checksum>`, where the checksum is the sum of all previous bytes modulo 256.

- `<cmd> 04` is a request, the device answers `<cmd> 05 ...`.
- `<cmd> 0a` is an acknowledgement (2 bytes, no checksum). The app acknowledges every notification longer than
  two bytes with `<cmd> 0a`; the device's own 2-byte `0a` frames get no reply.
- Settings queries seen in the capture: `21` power zones, `22` heart-rate zones, `23` LTHR zones, `1e` profile,
  `41` language, `53` time. They are not needed for uploading. `bryton_query_probe.py` only sends a
  whitelist of read-only queries.

## Uploading a workout

A workout is two files, FIT first, then JSON (see [file-formats.md](file-formats.md)). The file name on the device
comes from the `name` field of the JSON, not from the FIT.

```
0x10  08 08 "12345678ios"            -> 08 28 02            handshake
0x18  46 04 4a                       -> 46 05 ...           query; acknowledge with 46 0a
0x18  46 01 01 <T> <sum>             -> 46 0a               begin session
for each file (FIT, then JSON):
  0x10  10 00 09  (FIT) | 10 00 0f (JSON)  -> 10 20 02       prepare
  0x10  10 40 05 01 f4
  0x0d  start packet
  0x0d  data packets                 (write without response, ~1-2 ms apart)
  0x0d  end packet                   (write with response)
        device notifies on 0x10: 10 40 03 00 00 = accepted   (10 40 01 00 00 = rejected)
0x18  46 01 00 <T> <sum>             -> 46 0a               end session
```

`<T>` selects the destination: `00` = workout list (`System/Plan/Cycling/`), `01` = calendar
(`Workout/calendar/<YYYYMMDD>/`, the day comes from the `plan` field of the JSON). Begin frames are
`46 01 01 00 48` / `46 01 01 01 49`, end frames `46 01 00 00 47` / `46 01 00 01 48`.

The meaning of the `09` / `0f` argument is unknown; it looks like a file type. Always send the end-session frame,
even after an error, otherwise the device stays in transfer mode.

### Packets on `0x0d`

The transferred *body* is `00 00 00 00` + file length (u32, big-endian) + the file.

- **Start packet** (7 bytes): `00 00 00 <n> <last> 10 08`, where `n` is the number of data packets and `last` is
  the payload length of the **last** data packet. A wrong `last` makes the device reject the file.
- **Data packets**: sequence number (u16, big-endian, starting at 1) followed by up to **148** bytes of body.
- **End packet** (7 bytes): `<n+1 as u16> 00 <sum of all body bytes as u32, big-endian>`.

### Timing pitfall

A reply can arrive while the write call that triggered it is still running. Record the position in the
notification list *before* writing, then wait for a reply after that position.

### Calendar uploads replace the whole calendar

Sending one calendar workout deletes every other calendar entry on the device. To plan a week, send all days
in a single session (repeat the per-file steps for every FIT/JSON pair between begin and end).
Workouts in the workout list are not affected.

## Uploading a route

Routes use no `46 01 ...` begin/end frames. The route file (gzip FIT, kind `0b`) is split into chunks of at most
6922 data bytes (47 packets). **Every chunk is its own transfer**: `10 00 0b` -> `10 20 02`, `10 40 05 01 f4`, start,
data, end packets, then the device answers `10 40 03 00 00`. The first 8 bytes of each chunk body are the chunk's
offset in the file and the total file length (both u32 big-endian), instead of `00 00 00 00` + length of a workout
file. The start packet is `00 00 00 <packets> <last packet payload> 10 08`, the end packet as for workouts.

Then the name is sent on the command channel:

```
0x18  31 04 35                       -> 31 05 01 00 01 38      (acknowledge with 31 0a)
0x18  31 01 00 <frames> <length> <sum>  -> 31 0a, 31 02 01 <sum>   header: frames = data frames + 1, length = name bytes (max 32)
0x18  31 01 01 <16 bytes> <sum>         -> 31 0a, 31 02 02 <sum>   data frame 1 (name, zero padded)
0x18  31 01 02 <16 bytes> <sum>         -> 31 0a                   data frame 2 (only for names over 16 bytes)
```

The device requests the next frame with `31 02 <n> <sum>`; acknowledge each notification with `31 0a`.

## Where files land

The device first writes an upload to `Workout/saved_temp/temp0.*` (calendar: `Workout/calendar_temp/temp0.*`)
and then files it under the JSON name. This happens without any action on the device. The drive `BRYTON`
shows up over USB mass storage; the device cannot be uploaded to over Bluetooth while in USB mode.

## Safety

- Uploading a name that exists replaces that workout without a prompt.
- The device list cannot be read over Bluetooth. Keep your own list, or look at the USB drive.
- Back up `System/Plan/Cycling/` and `Workout/` over USB before experimenting.


## Ride download (app sync)

Seen in a capture of the Bryton app syncing a finished ride. Everything is read-only until the app's final acknowledgement.

```
0x10  0b 08                               -> 0b 28 02      list the rides the app has not fetched yet
0x10  0b 48 03                            -> the device sends the list on 0x0d (36 bytes for one ride)
                                             (with no ride waiting, `0b 08` is answered with `0b 28 06` instead, unsure)
0x10  11 08 <id u32> 01 <offset u32> <length u32>  -> 11 28 02   read a byte range of ride <id>
0x10  11 48 05 01 f4                      -> the device sends the range on 0x0d
```

- The list entry holds the ride id (Unix start time), moving time in seconds and distance in metres: `13 <id> <time> <dist> 00 00 00 00 00`, followed by `02` and padding. Only one ride was captured, so the stride and the meaning of `13` / `02` are unsure.
- A device -> phone transfer on 0x0d is framed like an upload, but the start packet carries the packet count as u16: `00 00 <n u16> <last length> <tag>`, then packets `<seq u16> <data>` and an end packet `<n+1 u16> 00 <sum32>`. There is no offset/length header in the body.
- The first read (`offset 0, length 4`) answers with the total size of the file (u32) plus 14 bytes of file data. The app then reads in ranges of at most `0x9ab0` (39600) bytes; ranges of 39600 + 39600 + 3773 made up an 82973-byte file.
- A long ride arrives gzip-compressed, a short one (1858 bytes, 0.1 km) as a plain FIT file (starts with `0e 10`); decide by the first bytes (`1f 8b`). The FIT file is a normal FIT activity (`.FIT` header, Bryton manufacturer, session with power, heart rate, cadence, zones). In the capture it had 8634 records for an indoor ride of 8606 s.
- After the download the app sends `33 01 <id u32 little-endian> <sum>` on 0x18 and the device answers `33 0a`, probably marking the ride as synced. `bryton.ble.download_rides` never sends it.
- Connecting also runs a profile sync before the download: `2f`, `34`, `47`, `41` (language), `5b`, `29`, `4a`, `53` (clock), `1d`/`1e` (user data) and `21`-`23` (FTP/MHR/LTHR with their zone limits). Only `2f 04` is needed for the download.
