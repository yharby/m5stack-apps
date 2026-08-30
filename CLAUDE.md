# M5Stack Apps maintainer guide

This repository is a personal registry of independent UIFlow2 MicroPython apps.
Keep shared tooling generic, keep hardware assumptions explicit, and keep
app-specific complexity inside its app or handover document.

## Current registry

| App | Purpose | Required I/O |
|---|---|---|
| `wifi_qr.py` | Confirmed QR-based UIFlow2 Wi-Fi provisioning | camera, LCD, touch, Wi-Fi/NVS |
| `translator.py` | Realtime EN ↔ JA geospatial interpretation | microphones, LCD, touch, Wi-Fi; optional SD |

## Verified target

| Item | Value |
|---|---|
| Board | M5Stack CoreS3, ESP32-S3, 320×240 LCD |
| Firmware | UIFlow2 2.5.1, MicroPython 1.27.0 |
| Serial | `/dev/cu.usbmodem*`, autodetected |
| App install | `/flash/apps/<name>.py` for UIFlow2 `APP.LIST` |
| Autorun | `/flash/main.py`, controlled by UIFlow2 boot mode |
| Private config | `/flash/res/config.json` |
| App log | `/flash/translator.log` for Translator |
| SD | `/sd`, FAT32, tested with a nominal 32 GB card |
| Clock | `time.time()`/`time.localtime()` are UTC on this device |

Other M5Stack models are not assumed compatible. Record the exact board and
firmware when adding support.

## Repository layout

```text
device/apps/                  one self-contained file per installable app
device/config.example.json    Translator config template, never real secrets
tools/m5.py                   shared USB/REPL/device CLI
tools/device_scripts/         bounded hardware and app diagnostics
Makefile                      uv-backed developer entry points
HANDOVER.md                   detailed Translator performance history
```

## Commands

Use `uv`; do not introduce another Python environment workflow.

```bash
make setup                    # uv sync
make catalog                  # source apps in this registry
make info                     # board, firmware, memory, filesystem
make apps                     # apps installed on the connected device
make push APP=<name>          # install in UIFlow2 APP.LIST
make run APP=<name>           # live run with serial output
make autorun APP=<name>       # also replace /flash/main.py
make rm-app APP=<name>        # remove an installed app
make probe                    # shared display/mic/Wi-Fi smoke test
make sd-probe                 # mount + capacity + write/read/sync SD test
make selftest                 # Translator mic/network/OpenAI test
make logs n=100               # Translator log tail
make reset                    # reboot into UIFlow2
make check                    # uv-managed Ruff format/lint gate
```

UIFlow2 owns the serial REPL. All device commands go through `tools/m5.py`,
which sends repeated Ctrl-C and then uses `mpremote ... resume`. Do not use a
plain `mpremote connect`, and do not compete with the UIFlow2 web IDE or a
`screen` process for the port.

## App contract

- One app is one `device/apps/<name>.py` file with no sibling import required.
- Call `M5.begin()` once, then call `M5.update()` on every interactive loop.
- Keep touch/network waits bounded and continue pumping UI events inside them.
- Use truthful states such as checking, connecting, connected, and failed.
- Require confirmation before persistent or disruptive changes.
- Treat optional hardware as nonfatal; the core app must continue without it.
- Put cameras, microphones, files, sockets, and temporary UI state behind a
  `try/finally` cleanup path.
- Never log passwords, API keys, tokens, or full private configuration.
- Verify live source, installed app, and launcher/autorun behavior separately.

## UI conventions

- The display is 320×240 with origin at the top left.
- Touch coordinates map 1:1 to display pixels. Use at least 44×44 touch targets.
- `M5.Touch` has no `wasClicked()`. A tap is `getDetail(0)[6]`; holding is
  `getDetail(0)[9]`. Guard with `getCount()`.
- CoreS3 has no usable BtnA/B/C strip; only `BtnPWR` is physical.
- Colors are 24-bit `0xRRGGBB`; there is no `color565()`.
- Pass colors explicitly because omitted color means reuse the previous value.
- Japanese requires `M5.Lcd.FONTS.AlibabaSansJA24`.
- Use `textWidth()` for wrapping. Do not estimate mixed Latin/CJK widths.
- `newCanvas(..., bpp=16, psram=True)` is verified for flicker-free regions.
- LCD and SD share SPI pins. Finish drawing before SD I/O and keep writes short.
- Surface optional-device problems in the UI, but do not trap the user in an
  error screen or disable unrelated functionality.

## Common I/O entry points

### Wi-Fi and UIFlow2 settings

UIFlow2 stores station settings in `esp32.NVS("uiflow")`:

- `net_mode = "WIFI"`
- `ssid0`
- `pswd0`
- `boot_option`

UIFlow2 starts Wi-Fi asynchronously before an app. Use an already-connected
station first, let that boot attempt settle, then re-read NVS. Only afterward
try app-specific fallback credentials. Do not require a preflight scan: the
tested iPhone hotspot connected even while absent from scan results.

CoreS3 Wi-Fi is 2.4 GHz. Use named `network.STAT_*` values when present, retry
transient association failures, and stop early for wrong credentials or
incompatible security.

### SD card

Use UIFlow2's `hardware.sdcard.SDCard` and mount at `/sd`:

```python
from hardware import sdcard

sdcard.SDCard(
    slot=3, width=1,
    sck=36, miso=35, mosi=37, cs=4,
    freq=20_000_000,
)
```

The CoreS3 pins are CS 4, SCK 36, MISO 35, MOSI 37. Insert the card with its
contacts facing the same direction as the screen. Prefer a genuine SDHC card
up to 32 GB formatted FAT32; 16 and 32 GB cards are the safest common choices.
Factory-formatted SDXC cards are usually exFAT and will not mount on this
firmware. Never autoformat media. The helper mounts `/sd` itself; ignore its
return value and do not call it repeatedly while `/sd` is already mounted.

Validate compatibility with `os.statvfs('/sd')` and a unique temporary
write/read/delete test; this does not identify the exact FAT variant. Run
`make sd-probe` only while Translator and other SD users are stopped because
the probe mounts and unmounts `/sd`. For durable records: append one complete
UTF-8 record, flush, close, and call `os.sync()`. This reduces loss but FAT
still has weak sudden-power-loss resilience and filesystem damage can exceed
the last record. No card-detect pin is exposed, so mount at startup/session
start and retry on a later user action rather than polling continuously. Hot
removal is unsupported; stop listening and exit/reset before removing the card.

Translator transcripts are opt-in and stored under
`/sd/m5stack-apps/translator/`. One file represents one listening session;
files rotate at a bounded size. Markdown is the human-readable default and
JSONL is the structured option. Each record stores relative capture timing,
UTC when the clock is valid, an explicit fixed local offset, both languages,
and no secrets. A fixed offset is not a timezone database; Japan's `+540`
minutes is stable because Japan does not observe daylight-saving time.

### Microphones

Use `M5.Mic`, never `audio.Recorder` in production. The latter wedges during
repeated asynchronous capture on this firmware.

`M5.Mic.record()` is asynchronous and its two-slot FIFO stores raw pointers,
not Python references. Every queued `bytearray` must remain strongly rooted
until `M5.Mic.isRecording()` returns zero. Stop requeueing, drain, then call
`M5.Mic.end()`.

Verified mono configuration:

```python
M5.Mic.config(sample_rate=16000, magnification=2, task_pinned_core=0)
M5.Mic.begin()
```

Mono mixes the two physical microphones and halves upload size. Use stereo
only for independent meters. Quiet room is about -55 dBFS; speech peaks near
-32 dBFS on this unit.

### Camera

Initialize only while the app owns the camera, handle recoverable frame/decode
errors interactively, and always attempt `camera.deinit()` in `finally` before
return or reset. Never persist a QR password until the new network has actually
associated and received an IP address.

### Network requests

`requests2` is synchronous, has no `files=`, and its `json=` path miscomputes
UTF-8 `Content-Length` for non-ASCII text. Encode JSON yourself and pass
`data=json.dumps(payload).encode()`.

Translator HTTP work runs on a 16 KB `_thread` stack while the main thread
pumps touch and microphone capture. A 32 KB stack cannot be allocated on this
board. The reusable socket is the primary path; `requests2` is fallback.

## Adding or changing an app

1. Add or edit one file in `device/apps/`.
2. Add it to the README registry table with exact hardware/config needs.
3. Keep optional features disabled by default and persist only explicit user
   choices.
4. Run `make check` through `uv`.
5. Run `make run APP=<name>` and exercise success, cancel, timeout, bad input,
   missing hardware, and repeated-use paths.
6. Install with `make push APP=<name>` and verify the device copy.
7. If autorun is intended, verify a real reboot and clean exit to UIFlow2.
8. Update this file only with durable, hardware-verified facts. Put detailed
   investigations and benchmarks in an app handover document.

## Recovery and safety

CoreS3 uses native USB CDC and exposes no reset line. If the port disappears or
the firmware is stuck in an uninterruptible C call, hold the power button for
about six seconds, release it, then press once. DTR/RTS and `esptool` cannot
recover that state.

Real config belongs only in `/flash/res/config.json`; `.gitignore` excludes
`config.json`, keys, logs, caches, and virtual environments. Diagnostic scripts
must use bounded operations, print progress before risky calls, avoid touching
existing SD contents, and clean up only their own temporary files.
