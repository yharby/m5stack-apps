# CLAUDE.md

Realtime EN <-> JA speech translator running on an M5Stack CoreS3.
The device records from its built-in mic, sends audio to OpenAI for
transcription, translates the text, and shows both languages on screen.

## Hardware and firmware

| Item | Value |
|---|---|
| Board | M5Stack CoreS3 (ESP32-S3, 320x240 LCD, ES7210 mic, AW88298 amp) |
| Firmware | UIFlow2 V2.5.1, MicroPython v1.27.0 |
| Serial port | `/dev/cu.usbmodem*` (autodetected) |
| Wi-Fi | configured on device, joins automatically |

## Layout

```
device/apps/translator.py     the app that runs on the device
device/config.example.json    config template (real config is NOT committed)
tools/m5.py                   device control CLI (push, run, logs, repl, ...)
tools/device_scripts/         diagnostics that run on the device
Makefile                      every workflow command
```

Apps are one file per app in `device/apps/`, installed to
`/flash/apps/<name>.py` so the device APP.LIST menu lists them by name.

## Commands

```bash
make setup       # install tooling into .venv (uv)
make info        # port, firmware, free memory
make apps        # what is installed on the device
make push        # install device/apps/translator.py to /flash/apps/
make autorun     # also install as /flash/main.py so it runs at boot
make run         # run the app live, output streams to the terminal
make selftest    # on-device end-to-end check of config, wifi, mic, OpenAI
make probe       # hardware smoke test
make logs n=100  # read /flash/translator.log from the device
make repl        # interactive MicroPython REPL
make reset       # reboot back into UIFlow2
make check       # ruff format check + lint (device/ and tools/)
```

Override the app name with `APP=name`, for example `make run APP=helloworld`.

## The one thing that trips up every tool

UIFlow2 boots an asyncio launcher that owns the serial REPL, so a plain
`mpremote connect` fails with `could not enter raw repl`. `tools/m5.py`
solves this by hammering Ctrl-C over raw serial first, then using
`mpremote ... resume` so the board is not soft-reset back into the launcher.
Never drop the `resume`, and never talk to the board while the UIFlow2 web
IDE or a `screen` session holds the port.

## Device API facts, verified on this board

These were established by probing the live device. Do not trust the docs
over these without re-probing.

- `Recorder.record_into(buf, sync)` takes **only** those two arguments.
  Passing `sample=`, `bits=` or `stereo=` raises
  `TypeError: extra keyword arguments given`. Audio format comes from the
  `Recorder(sample, bits, stereo)` constructor.
- `Recorder.create_pcm_buf(seconds)` returns a `bytearray` already sized
  correctly, 32000 bytes per second at 16 kHz 16-bit mono.
- `Recorder.rms()` returns the level of the last capture in **dBFS**, and
  `volume()` is a read only meter. Neither sets input gain, there is no
  gain control exposed.
- Measured levels on this unit, quiet room about -55 dBFS, speech peaks
  about -37 dBFS. The app gates uploads at -45 dBFS.
- `socket.setdefaulttimeout` does **not** exist on this firmware.
- Japanese needs `M5.Lcd.setFont(M5.Lcd.FONTS.AlibabaSansJA24)`, otherwise
  glyphs do not render. `M5.Lcd.textWidth()` exists, so wrap text by real
  measurement rather than guessing character widths.
- CoreS3 has no BtnA/B/C, only `BtnPWR`. Touch is available.
- `requests2.post(url, data=<bytes>, headers=...)` sends a raw body, which
  is how the hand built multipart upload works. There is no `files=`.

## Whisper behaviour worth guarding

Whisper hallucinates confident text on silence. A silent 5 second clip from
this device returned `ご視聴ありがとうございました。` ("thank you for
watching"), a well known artifact. The app therefore gates on mic level
before uploading and filters known hallucination phrases.

## Config and secrets

Real config lives on the device at `/flash/res/config.json` and is
**never committed**. `device/config.example.json` is the template.
`.gitignore` blocks `config.json` everywhere.

To update it on the device:

```bash
uv run python tools/m5.py repl     # then edit, or push a file with mpremote cp
```

## Debugging loop

1. `make logs` reads the on-device log, which records every stage and full
   tracebacks. This is what found the original crash.
2. `make run` streams output live while the app runs.
3. `make selftest` isolates config, Wi-Fi, mic and both OpenAI calls
   separately, so a failure points at one stage.
