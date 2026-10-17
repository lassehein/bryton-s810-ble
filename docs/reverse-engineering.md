# How the protocol was found

1. **Capture the app.** Connect an iPhone to a Mac, record with *PacketLogger* (Xcode "Additional Tools") while
   the Bryton Active app sends a workout, save as `.pklg`.
2. **Extract the ATT traffic:** `python3 tools/pklg_parse.py capture.pklg`; analyse with
   `tools/pklg_analyze.py` (`adv`, `gatt`, `att --handle 0x0d`, `cmds --baseline other.pklg` marks commands
   that only occur in one capture).
3. **Reassemble the files** from the data packets (`bryton/protocol.py: reassemble`) and check the FIT CRC.
4. **Replay** the capture byte for byte to a test workout (`tools/bryton_write_test.py --replay`) before
   changing anything.
5. **Vary one thing at a time** with workouts named `Test_*` and read the result over USB
   (`System/Plan/Cycling/`, `Workout/`), or on the device screen.

Captures of the upload are in `reference/` (`capture_transfer_window.txt`, `capture_0x0d_transfer.txt`);
the unit tests replay them against the packet builder. Raw `.pklg` files are not included (they contain
identifiers of the device and other Bluetooth devices nearby).

The Bryton web app's workout parser (`woParser.js`) and the open-source
[intervalssync](https://github.com/jorge-huxley/intervalssync) were useful references for the file formats.
