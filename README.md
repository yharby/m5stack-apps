# M5Stack Apps

My registry of custom apps, utilities, and hardware experiments for M5Stack
devices. The current apps target the CoreS3 running UIFlow2 MicroPython and can
be installed directly into its `APP.LIST` menu.

## App registry

| App | What it does | Hardware | Configuration |
|---|---|---|---|
| [`wifi_qr`](device/apps/wifi_qr.py) | Scans a standard Wi-Fi QR code and safely switches UIFlow2's saved network | Camera, touch | None |
| [`translator`](device/apps/translator.py) | Realtime English ↔ Japanese speech translation for FOSS4G and geospatial conversations | Microphones, display, touch, Wi-Fi | OpenAI API key |

Both apps are production-tested on an M5Stack CoreS3 with UIFlow2 `2.5.1` and
MicroPython `1.27.0`. Compatibility with other M5Stack models is not assumed;
each app's hardware requirements are listed above.

## Quick start

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
make setup
make info
make push APP=wifi_qr
make push APP=translator
```

Installed files go to `/flash/apps/<name>.py`, where UIFlow2 exposes them in
`APP.LIST`. To run without installing, or to make one app the boot app:

```bash
make run APP=wifi_qr
make run APP=translator
make autorun APP=translator
```

`make run` streams serial output until interrupted. `make autorun` also writes
the selected app to `/flash/main.py`; use it only for the app you want launched
by UIFlow2's boot mode.

## Wi-Fi QR

The CoreS3 camera starts immediately and recognizes the standard format:

```text
WIFI:T:WPA;S:network;P:password;;
```

The app validates the payload, shows the SSID and security type, and requires
confirmation before changing anything. It tests the new connection, restores
the previous network after a failure, and only then saves the credentials to
UIFlow2's own NVS settings. Passwords are never displayed, logged, or copied to
an extra plaintext file.

After connecting, choose **SCAN AGAIN** or **EXIT**. EXIT and the power button
clean up the camera and reboot into the UIFlow2 launcher. CoreS3 Wi-Fi is
2.4 GHz; a 5 GHz-only network cannot be used.

## Geo Translator

The translator provides pocket English ↔ Japanese interpretation optimized for
FOSS4G and open-geospatial terminology:

```text
CoreS3 mics → pause detection → transcription → translation → LCD
                    capture continues during both API calls
```

It uses natural-pause endpointing, a reusable HTTP/1.1 TLS connection,
continuous microphone capture, automatic language direction, and flicker-free
EN/JA rendering. On the tested device, the optimized pipeline completed in
about 3.7 seconds compared with roughly 5.2 seconds for the original fixed-slice
implementation.

Copy the example configuration, add the OpenAI credential, and place the
private file at `/flash/res/config.json`:

```bash
cp device/config.example.json device/config.json
```

Wi-Fi fields in that file are optional fallbacks. The translator normally uses
the network currently selected by UIFlow2—including one selected by
`wifi_qr`—and re-reads those settings whenever it needs to reconnect.

Tap the screen to start or pause. Tap the gear for microphone meters and
sensitivity controls. `Chunk` is the maximum utterance length when no natural
pause occurs.

## Development workflow

```bash
make catalog                 # list apps in this repository
make apps                    # list apps installed on the device
make info                    # firmware, memory, and connection details
make push APP=<name>         # install one registry app
make run APP=<name>          # run source live over USB
make autorun APP=<name>      # install the selected app as /flash/main.py
make rm-app APP=<name>       # remove one installed app
make probe                   # display, microphone, config, and Wi-Fi smoke test
make selftest                # translator network/mic/OpenAI end-to-end test
make logs                    # read /flash/translator.log
make reset                   # reboot into UIFlow2
make check                   # formatting and lint
```

To add an app, place one self-contained MicroPython file in `device/apps/`.
Keep credentials out of source control, document any model-specific hardware,
and verify both live execution and `APP.LIST` installation on the real device.

## Configuration and safety

The committed [example configuration](device/config.example.json) contains no
real credentials. `.gitignore` excludes `config.json`, keys, logs, virtual
environments, and Python caches.

[CLAUDE.md](CLAUDE.md) is the durable engineering reference for the shared
CoreS3/UIFlow2 behavior and app-specific findings. [HANDOVER.md](HANDOVER.md)
contains the Translator performance handover.
