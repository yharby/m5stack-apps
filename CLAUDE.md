# M5Stack Apps engineering reference

This repository is the personal registry for custom M5Stack applications and
device experiments. Apps are independent UIFlow2 MicroPython files that share
one verified CoreS3 development workflow.

| App | Purpose |
|---|---|
| `wifi_qr.py` | Camera-based Wi-Fi provisioning for UIFlow2 |
| `translator.py` | Realtime EN ↔ JA speech translation for geospatial conversations |

Keep repository-wide guidance generic. Put app-specific behavior in a clearly
named section and never assume that a future app needs the Translator's API
key, microphones, networking, or boot behavior.

## Hardware and firmware

| Item | Value |
|---|---|
| Board | M5Stack CoreS3 (ESP32-S3, 320x240 LCD, ES7210 mic ADC, AW88298 amp) |
| Mics | two real MEMS mics, U12 on ES7210 ch1 and U13 on ch2 |
| Firmware | UIFlow2 V2.5.1, MicroPython v1.27.0 |
| Serial port | `/dev/cu.usbmodem*` (autodetected) |
| Wi-Fi | UIFlow2 NVS is authoritative; JSON credentials are fallback only |

## Layout

```
device/apps/                  installable registry apps, one Python file each
device/config.example.json    Translator config template; no real credentials
tools/m5.py                   shared USB device CLI
tools/device_scripts/         hardware and Translator diagnostics
Makefile                      common development commands
HANDOVER.md                   Translator performance handover
```

Apps are one file per app in `device/apps/`, installed to
`/flash/apps/<name>.py` so the device APP.LIST menu lists them by name.

## Commands

```bash
make setup       # install tooling into .venv (uv)
make info        # port, firmware, free memory
make catalog     # apps available in this repository
make apps        # what is installed on the device
make push APP=wifi_qr
make run APP=translator
make autorun APP=translator  # also writes /flash/main.py
make selftest    # Translator config/Wi-Fi/mic/OpenAI test
make probe       # hardware smoke test
make logs n=100  # read /flash/translator.log from the device
make repl        # interactive MicroPython REPL
make reset       # reboot back into UIFlow2
make check       # ruff format check + lint (device/ and tools/)
```

Always name the intended registry app with `APP=name`. The Makefile retains
`translator` as a compatibility default, but documentation and automation
should be explicit.

## Registry app contract

- One app is one self-contained `device/apps/<name>.py` file.
- Installation must target `/flash/apps/<name>.py` so UIFlow2 `APP.LIST` can
  discover it.
- Never commit credentials or write passwords to diagnostic logs.
- State hardware and firmware requirements; M5Stack model APIs are not
  interchangeable.
- Keep recoverable errors interactive and ensure cameras, microphones, files,
  and network state are cleaned up before exit or reset.
- Test source with `make run APP=<name>`, then install and verify the exact
  device copy before calling an app production-ready.

## The one thing that trips up every tool

UIFlow2 boots an asyncio launcher that owns the serial REPL, so a plain
`mpremote connect` fails with `could not enter raw repl`. `tools/m5.py`
solves this by hammering Ctrl-C over raw serial first, then using
`mpremote ... resume` so the board is not soft-reset back into the launcher.
Never drop the `resume`, and never talk to the board while the UIFlow2 web
IDE or a `screen` session holds the port.

---

## Verified platform facts

Everything below was established either by probing this exact board or by
reading the `uiflow_micropython` tag `2.5.1` source (commit `96c8a6e2`), plus
M5Unified, M5GFX and the official CoreS3 schematic. **Do not "fix" any of it
back to what the docs suggest without re-probing.** Several of these were
found the hard way, by hanging the board.

### Wi-Fi ownership and startup

UIFlow2 2.5.1 stores its active station credentials in the `uiflow` NVS
namespace under `ssid0` and `pswd0`, with `net_mode` set to `WIFI`. Its
`boot.py` begins association asynchronously and can launch `main.py` while
that connection is still in progress. The translator must let that attempt
settle before calling `WLAN.connect()` itself; otherwise an old
`config.json` SSID can interrupt the newly selected UIFlow2 network.

`ensure_wifi()` therefore uses an already-connected station first, waits for
UIFlow2's initial attempt, then re-reads NVS and tries that network before the
optional JSON fallback. Do not add a preflight scan: an iPhone hotspot on this
device has connected successfully even when it was absent from scan results.

### Recovering a wedged board

The CoreS3 uses native USB CDC, not a USB-to-serial bridge, so **there is no
EN line to toggle**. DTR/RTS reset does nothing and `esptool` cannot reach it
either. The only recovery is a physical hard reset, hold the power button for
about six seconds, then press it once. Symptoms of a wedge are either the port
disappearing and re-enumerating (the board panicked and rebooted) or the port
staying present while every `mpremote` call hangs (the board is stuck inside a
C call where Ctrl-C cannot reach).

Because a wedge costs a manual reset, on-device test scripts should write
their progress to a file under `/flash` as they go. That file survives the
wedge and tells you exactly which line hung.

## Translator-specific implementation notes

The rest of this file records the Translator's audio, rendering, HTTP, and
OpenAI behavior. Apply it to another registry app only when that app uses the
same subsystem.

### Audio capture: use `M5.Mic`, never `audio.Recorder`

The production app and all ordinary diagnostics use the M5Unified
`M5.Mic` binding. It owns one persistent I2S task and a two-slot FIFO.

The app no longer slices audio on a wall clock. It captures 1 s frames into a
ring, scores each 100 ms window against the gate, and closes an utterance on
600 ms of trailing sub-gate audio after at least 400 ms of speech, falling back
to a `chunk_seconds` ceiling when the talker never pauses.
`poll_session_controls()` doubles as the audio pump, so the FIFO keeps both
slots filled during network waits. Facts about the FIFO itself:

- Configure once with
  `M5.Mic.config(sample_rate=16000, magnification=2, task_pinned_core=0)`,
  then call `M5.Mic.begin()`. MicroPython runs on the other core.
- `M5.Mic.record(buf, 16000, stereo)` queues a writable `bytearray` and
  returns immediately. A plain `bytearray` is correct; `create_pcm_buf` also
  returns one and provides no special safety.
- `M5.Mic.isRecording()` is the FIFO occupancy: 0, 1, or 2. The app keeps two
  mono buffers queued so recording continues while Python is blocked in TLS.
- The C++ queue stores raw pointers, not Python references. Every queued
  buffer must remain strongly rooted until its slot completes. The app keeps
  both in the global `capture_buffers` list and uploads a copy before
  requeueing the original.
- At 16 kHz, signed 16-bit mono needs 32000 bytes per second. Stereo is twice
  that. Arbitrary buffer lengths work, including 150 ms stereo meter frames.
- `M5.Mic.end()` waits for an in-flight buffer instead of cancelling it. A
  real five-second test called `end()` after 350 ms and it returned after the
  remaining 4.65 s. Stop a session by ceasing requeues and draining the FIFO.
- Six repeated three-second FIFO captures, followed by teardown and restart,
  completed on this unit. The full translator also ran repeated six-second
  captures during both OpenAI calls with 0-1 ms requeue time.
- `M5.Mic` and `audio.Recorder` both claim CoreS3 I2S port 1. They must never
  coexist in the same process.

`audio.Recorder` is retained only in `async_settle.py` as a regression probe.
Its first `record_into(..., sync=False)` works and genuinely overlaps TLS, but
the second capture enters an ADF cleanup spin wait with no timeout. In the
decisive test, cycle 0 settled after 3219 ms; cycle 1 was still recording after
15 seconds. `stop()` and file recording have also wedged the board. Do not use
this API in the app or general tests. Its `rms()`/`volume()` methods are also
destructive and measured about 13 dB below the PCM's true level.

`socket.setdefaulttimeout` does not exist. `requests2.post` accepts an
undocumented `timeout=`.

### Microphones and gain

The schematic (`Sch_M5_CoreS3_v1.0.pdf` p4) shows ES7210 (U9, 7-bit I2C
address `0x40`, internal bus port 1, SCL 11, SDA 12) with:

- ch1 = U12, ch2 = U13, two separate MSM381A3729H9BPC analog MEMS mics
- ch3 = an echo reference tapped off the AW88298 speaker through 150K
- ch4 = grounded

Firmware enables ch1 and ch2 only. **`stereo=False` mixes both mics rather
than picking one**, so mono is the right choice for uploads and halves the
bytes on the wire. Stereo is only useful for metering the two mics separately.

Sensitivity levers, in order of preference:

1. **Analog.** `M5.Mic.begin()` initializes the ES7210 PGA to code 11. The
   ladder tops out at 37.5 dB (code 14). Poke
   registers `0x43` and `0x44`, preserving bit 4 which is the PGA enable.
   Apply the poke after `M5.Mic.begin()`, which rewrites the codec registers.
   It remains off by default (`analog_gain_code: 0`) until verified.
2. **Digital.** A saturating in-place gain with `@micropython.viper` over
   `ptr16`. Viper `ptr16` loads are unsigned so samples must be sign extended
   by hand, and the result must be clamped, never allowed to wrap. Speech
   peaks around -32 dBFS leave roughly 30 dB of headroom, so 4x to 8x is safe.
3. **The gate.** The real cause of "the mic is not sensitive" was the broken
   `rms()` reading 13 dB low. Levels computed from the buffer are correct, and
   the gate is user tunable on the settings page.

Measured on this unit, computed from the PCM buffer: quiet room about
-55 dBFS, speech peaks about -32 dBFS.

### Display and touch

- **`M5.update()` must run every loop** or touch state never changes.
- **`M5.Touch` has no `wasClicked()` method.** Only `getX`, `getY`,
  `getCount`, `getDetail`, `getTouchPointRaw` are bound. The predicates are
  indices into the 11-tuple from `getDetail(i)`:
  `0 deltaX, 1 deltaY, 2 distanceX, 3 distanceY, 4 isPressed, 5 wasPressed,
  6 wasClicked, 7 isReleased, 8 wasReleased, 9 isHolding, 10 wasHold`.
  Tap is `[6]`, hold is `[9]`. Thresholds are 8 px and 500 ms, not settable.
- A release still reports for one more update, so guarding on
  `getCount() > 0` does not swallow the click.
- Touch coordinates equal LCD pixel coordinates 1:1, origin top left,
  0..319 by 0..239. `getTouchPointRaw()` is NOT rotation corrected.
- The virtual BtnA/B/C strip has zero height on CoreS3, so the whole screen is
  usable and those buttons never fire. Only `BtnPWR` works.
- **Colours are 24-bit `0xRRGGBB` ints. There is no `color565()`.** Presets
  live in `M5.Lcd.COLOR.*`.
- Omitting the `color` argument means "reuse the last colour" (the sentinel is
  -1), which makes drawing order dependent. Always pass it explicitly.
- Japanese needs `M5.Lcd.setFont(M5.Lcd.FONTS.AlibabaSansJA24)`, otherwise
  glyphs do not render. `M5.Lcd.textWidth()` exists, so wrap by real
  measurement rather than guessing character widths.
- Do not choose the font from the transcription API's language metadata. It
  returned `en` for several clearly Japanese kana/kanji transcripts during a
  live run. `detect_source()` inspects the actual characters, and the drawing
  code independently does the same before choosing the glyph set.
- Normalize smart quotes, dashes, ellipses, and non-breaking spaces to ASCII
  for LCD display. Some Latin font builds omit those glyphs. This changes only
  the rendered copy, not the logged or translated text.
- Avoid `str.lower()` over arbitrary transcripts on this MicroPython build. A
  real Japanese response containing an uncommon Unicode code point raised
  `UnicodeError`. `ascii_lower()` folds only A-Z for the English hallucination
  blocklist and leaves all other characters untouched.
- Fonts present on CoreS3: Montserrat 12/14/16/18/24/40/44/48 and the three
  Alibaba CJK 24 faces. **Montserrat 20, 22, 30 and 36 do not exist** and
  raise `AttributeError`. `ASCII7` and the `DejaVu*` names are aliases onto
  Montserrat, not the small bitmap fonts, so do not size a layout assuming
  `ASCII7` is tiny. `fontHeight()` is not the point size, Montserrat18 is
  21 px. Measure at runtime.
- Passing `font=` to `drawString()` or `textWidth()` **permanently changes the
  current font** as a side effect. Set it back.
- `M5.Lcd.newCanvas(w, h, bpp, psram)` plus `canvas.push(x, y)` is the
  vendor-documented flicker fix, and it is verified working on this unit at
  `bpp=16, psram=True`. A canvas does bind `fillScreen`, `setFont`,
  `setTextColor`, `drawString` and `textWidth`.
  `startWrite()`/`endWrite()` batch the SPI transaction but do not buffer. A
  canvas has neither of those, nor `.FONTS` or `.COLOR`, so pass
  `M5.Lcd.FONTS.X` into it.
- Never hold `startWrite()` open across a network call, the SD card shares the
  LCD SPI host.
- `widgets.Button` is an empty stub that draws nothing. `M5.Widgets` has no
  button, slider or meter. `m5ui` is LVGL and would seize the framebuffer.
  Raw `M5.Lcd` calls are the right choice here.
- `requests2.post` is synchronous, so the app runs each POST on a short-lived
  `_thread` worker. **The stack must be 16 KB, not 32 KB.** A 32 KB task stack
  cannot be allocated on this board and raises `OSError("can't create thread")`
  every single time, which silently routed every POST down the blocking
  fallback and disabled touch during network calls. `thread_probe.py` shows
  16384/8192/4096/2048 create fine and 32768 never does; `tls_thread_probe.py`
  completed a real TLS handshake and HTTP round trip on stacks down to 8 KB.
  Re-probe before raising this value again. The main thread continues
  calling `M5.update()` every 20 ms and latches the initial `wasPressed` event,
  allowing one touch to stop or request settings during either network call.
  Do not move POSTs back onto the UI thread: taps that start and end during a
  blocking request are otherwise lost.

### HTTP and OpenAI

- `requests2` has no `files=`, so the multipart body is hand built and passed
  as `data=<bytearray>`. It is HTTP/1.0 with `Connection: close`, so every
  request pays a fresh TLS handshake. **The app no longer uses it as the
  primary path.** `KeepAliveClient` holds one HTTP/1.1 TLS socket to
  api.openai.com open and reuses it for every request in a session, keeping
  `requests2` as an automatic fallback that latches after three consecutive
  failures. Verified on this board: raw `socket` + `ssl` works, the handshake
  costs about 540 ms, and a whole multi-turn session logs exactly one
  `keep-alive TLS` line.
- `prewarm()` opens that socket at boot while the idle screen is up. The
  server may still close it before the first utterance, in which case one
  extra handshake appears. Reuse within a session is the reliable win.
- Never use `requests2.post(json=payload)` for non-ASCII JSON. It declares
  `Content-Length` using the Unicode character count but sends UTF-8 bytes,
  causing HTTP 400 or a truncated body for Japanese, curly punctuation, or an
  em dash. Use `data=json.dumps(payload).encode()` and explicitly set
  `Content-Type: application/json`.
- Measured on this board with continuous speech and `chunk_seconds = 7`:

  | Stage | Wall-clock chunks + requests2 | Endpointed + keep-alive |
  |---|---|---|
  | STT round trip | 2.0-2.9 s | 1.75-1.94 s |
  | Translation round trip | 2.7-3.4 s | 1.7-2.5 s |
  | Pipeline total | ~5.2 s | ~3.7 s |

  The wall-clock version also ran an 8.1-8.4 s cycle for a 7 s chunk, so the
  mic sat idle about 1.2 s of every cycle and roughly 15% of speech was
  dropped. `mic: queued ... depth=1` right after a requeue is that starvation.
- `gpt-transcribe` returns `""` on silence and puts the detected language in
  `languages[0].code`. It takes `languages[]` (plural), which replaces
  `language`.
- `gpt-transcribe` supports a `keywords` array for domain words and phrases.
  The app sends repeated `keywords[]` multipart fields containing canonical
  open-geospatial terms. Keep recognition keywords short and factual; do not
  put translation policy or self-correction rules into transcription.
- Translation uses a compact open-geospatial system instruction. It preserves
  canonical Latin spellings, applies consistent Japanese STAC terms, and
  resolves explicit spoken self-corrections by omitting only the superseded
  words. It deliberately does not contain command-execution rules: this app
  translates speech, it does not execute it.
- `whisper-1` hallucinates confidently on silence. A silent 5 s clip from this
  device returned `ご視聴ありがとうございました。` and `"Thank you for
  watching!"`. That is why it is not used, and why the app still keeps a
  phrase blocklist as a second line of defence.
- `gpt-5.6-luna` needs `reasoning_effort: "none"`, otherwise it burns hidden
  reasoning tokens on a one-line translation. `gpt-5-nano` **rejects** `none`
  with a 400.
- Use `max_completion_tokens`, not the deprecated `max_tokens`.

### Translator config and secrets

Real config lives on the device at `/flash/res/config.json` and is
**never committed**. `device/config.example.json` is the template.
`.gitignore` blocks `config.json` everywhere. The settings page writes
`gate_dbfs`, `chunk_seconds` and `mic_gain` back to that file.

To update it on the device:

```bash
uv run python tools/m5.py repl     # then edit, or push a file with mpremote cp
```

### Translator debugging loop

1. `make logs` reads the on-device log, which records every stage and full
   tracebacks. This is what found the original crash.
2. `make run` streams output live while the app runs.
3. `make selftest` isolates config, Wi-Fi, mic and both OpenAI calls
   separately, so a failure points at one stage.
4. If the board stops answering, it needs a physical hard reset; see the
   verified platform facts above.
