# M5Stack Apps

My registry of custom apps, utilities, and hardware experiments for M5Stack
devices. The current apps target the CoreS3 running UIFlow2 MicroPython and can
be installed directly into its `APP.LIST` menu.

## App registry

| App | What it does | Hardware | Configuration |
|---|---|---|---|
| [`wifi_qr`](device/apps/wifi_qr.py) | Scans a standard Wi-Fi QR code and safely switches UIFlow2's saved network | Camera, touch | None |
| [`translator`](device/apps/translator.py) | Realtime multilingual translation with a scrollable bidi conversation feed | Microphones, display, touch, Wi-Fi; optional SD | OpenAI API key; RTL firmware for Arabic/Hebrew |

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

## Multilingual Translator

The translator provides pocket multilingual interpretation optimized for
FOSS4G and open-geospatial terminology:

```text
CoreS3 mics → pause detection → transcription → translation → LCD
                    capture continues during both API calls
```

It uses natural-pause endpointing, a reusable HTTP/1.1 TLS connection,
continuous microphone capture, script-aware automatic direction, and an LVGL
turn feed. The feed follows new turns until the user scrolls away; **LIVE**
returns to the newest turn. Heard text is compact and the translation is the
visual focus. Dedicated pair, start/stop, live, and settings controls prevent
a feed gesture from stopping capture.

Pair choices are English, Japanese, Korean, Simplified Chinese, Arabic,
Hebrew, and horizontal Cyrillic Mongolian. Traditional vertical Mongolian is
not silently approximated. Arabic/Hebrew text stays in logical Unicode order
for APIs and transcripts; LVGL handles bidi ordering and contextual shaping.

Copy the example configuration, add the OpenAI credential, and place the
private file at `/flash/res/config.json`:

```bash
cp device/config.example.json device/config.json
```

Wi-Fi fields in that file are optional fallbacks. The translator normally uses
the network currently selected by UIFlow2—including one selected by
`wifi_qr`—and re-reads those settings whenever it needs to reconnect.

Use **START/STOP** to control capture, tap the pair control to choose languages,
and use **SET** for audio/storage controls. `Chunk` is the maximum utterance
length when no natural pause occurs.

### Arabic and Hebrew firmware

Stock UIFlow2 2.5.1 disables LVGL bidi, Arabic contextual shaping, and its
Arabic/Hebrew font. Translator refuses to start an Arabic/Hebrew pair unless a
firmware ABI marker proves all three are enabled. Prepare a current UIFlow2
checkout after its normal `submodules` and `patch` steps:

```bash
make rtl-firmware-patch UIFLOW_DIR=/path/to/uiflow-micropython
make -C /path/to/uiflow-micropython/m5stack littlefs
make -C /path/to/uiflow-micropython/m5stack BOARD=M5STACK_CoreS3 pack_all
```

The patch is exact and idempotent. It enables bidi and Arabic/Persian shaping,
then embeds compact 16 px heard-text and readable 24 px translation faces:
Cairo for Arabic and Noto Sans Hebrew for Hebrew. It freezes a
`translator_rtl.ABI_VERSION == 2` capability marker, and the app refuses an
RTL session unless every required face is present. Both font subsets remain
under SIL OFL 1.1; source pins, generator details, and the license are in
[`firmware/fonts`](firmware/fonts/README.md). Flash and visually validate
Arabic/Hebrew on the real display before release.

### Optional SD transcripts

Insert a FAT32-formatted SDHC card up to 32 GB; a genuine 16 or 32 GB card is
the safest choice. On CoreS3, insert it with the contacts facing the same
direction as the screen. Stop Translator and any other SD user before running:

```bash
make sd-probe
```

In Translator settings, toggle **SD Save ON**. Saving is off by default, and
ordinary missing/full/write errors are handled as nonfatal so translation can
continue. Hot removal is unsupported: stop listening and exit/reset the app
before removing the card. Each listening session gets one file set under
`/sd/m5stack-apps/translator/`; completed turns are appended, flushed, closed,
and synced individually. Files rotate into bounded parts at 1 MiB by default.

Markdown is the default because it opens cleanly on any computer. Set
`"transcript_format": "jsonl"` in the device config for one structured record
per line. Both contain original text, translation, language direction,
capture-relative timing, UTC, and Japan local time when UIFlow2 has synchronized
the clock. Otherwise filenames are `undated-*`, JSON timestamps are null, and
Markdown uses relative time. The firmware clock is UTC, so the example config
uses a fixed Japan offset of `540` minutes; this is an offset, not a timezone
database or daylight-saving calculation:

```json
{
  "save_transcripts": false,
  "transcript_format": "md",
  "transcript_max_file_bytes": 1048576,
  "transcript_timezone_offset_minutes": 540
}
```

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
make sd-probe                # SD mount, capacity, write/read/sync test
make selftest                # translator network/mic/OpenAI end-to-end test
make logs                    # read /flash/translator.log
make reset                   # reboot into UIFlow2
make check                   # formatting, lint, and host language/UI tests
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
