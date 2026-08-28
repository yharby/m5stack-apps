# CLAUDE.md

Realtime EN <-> JA speech translator running on an M5Stack CoreS3.
The device records from its built-in mics, sends audio to OpenAI for
transcription, translates the text, and shows both languages on screen.
It keeps the mic open during the network round trip, so it listens and
talks to OpenAI at the same time.

## Hardware and firmware

| Item | Value |
|---|---|
| Board | M5Stack CoreS3 (ESP32-S3, 320x240 LCD, ES7210 mic ADC, AW88298 amp) |
| Mics | two real MEMS mics, U12 on ES7210 ch1 and U13 on ch2 |
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

---

# FACTS

Everything below was established either by probing this exact board or by
reading the `uiflow_micropython` tag `2.5.1` source (commit `96c8a6e2`), plus
M5Unified, M5GFX and the official CoreS3 schematic. **Do not "fix" any of it
back to what the docs suggest without re-probing.** Several of these were
found the hard way, by hanging the board.

## Recovering a wedged board

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

## Audio, `audio.Recorder`

Constructor is `Recorder(sample, bits, stereo)`. `create_pcm_buf`, `record`,
`record_into`, `rms`, `volume`, `config`, `pause`, `resume`, `stop`,
`is_running`, `is_recording` and the format constants `PCM=1 AMR=2 WAV=4
MP3=5` are the whole API.

- **`record_into(buf, sync)` takes ONLY those two arguments.** Passing
  `sample=`, `bits=` or `stereo=` raises
  `TypeError: extra keyword arguments given`. Format comes from the
  constructor or from `config()`.
- **Always pass a buffer from `recorder.create_pcm_buf(seconds)`.** Passing a
  plain `bytearray` of the same size crashed the board hard, twice. Every
  capture that has ever worked on this unit used `create_pcm_buf`.
- `create_pcm_buf(seconds)` takes whole seconds and multiplies by the current
  channel count, so 5 s at 16 kHz mono is 160000 bytes and stereo is 320000.
  The docs omit the channel factor and are wrong.
- **`record_into(buf, sync=False)` returns in about 60 ms and fills the buffer
  from a background task.** Verified: 163451 Python loop iterations ran during
  a 2 s capture, and a 10 s buffer filled completely with real audio while the
  interpreter was blocked in a 33 s TLS upload. The consumer task is pinned to
  core 0, the I2S and resample elements to core 1. This is what makes the
  pipeline in `translator.py` possible, and it needs no `_thread`.
- **There is no way to ask how many bytes have been captured.** The progress
  counter is a stack local in the C task and is never written back.
  `is_recording()` is literally `pipeline != NULL`.
- **`is_recording()` cannot be used as a completion signal.** It was still
  `True` 23 s after a 10 s buffer had finished filling. Time the capture
  instead, the ADF task runs at exactly the sample rate.
- **Never call `recorder.stop()`.** Its spin wait (`while state !=
  AEL_STATE_FINISHED`) has no timeout. Calling it after an async capture hung
  the board permanently. Let the capture run itself out instead.
- **`rms()` and `volume()` are destructive and misleading.** Both tear down
  the capture pipeline, rebuild it hard coded at 8000 Hz / 16 bit / stereo,
  read 1024 fresh bytes (64 ms of the room *now*), and tear it down again.
  Measured 13 dB apart from the truth on this unit: `rms()` said -45.7 dBFS
  for a clip whose real level, computed from the PCM, was -32.1 dBFS. Compute
  levels from the buffer, which is what `channel_dbfs()` does.
- **Every `audio.Recorder(...)` leaks a 4 KB FreeRTOS task that is never
  freed.** Build exactly one per boot. Repeatedly `mpremote run`-ing a script
  that constructs a Recorder, without rebooting in between, is a slow path to
  a wedge.
- `config(sample, bits, stereo)` only writes struct fields, which take effect
  on the next pipeline build. It fills defaults for anything omitted, so
  **always pass all three**, otherwise `config(sample=16000)` silently resets
  bits and stereo.
- `record(uri, time, sync)` writing to a file hung the board for 240 s and
  never returned. Avoid it, use `record_into`.
- `socket.setdefaulttimeout` does not exist. `requests2.post` accepts an
  undocumented `timeout=`.

## Microphones and gain

The schematic (`Sch_M5_CoreS3_v1.0.pdf` p4) shows ES7210 (U9, 7-bit I2C
address `0x40`, internal bus port 1, SCL 11, SDA 12) with:

- ch1 = U12, ch2 = U13, two separate MSM381A3729H9BPC analog MEMS mics
- ch3 = an echo reference tapped off the AW88298 speaker through 150K
- ch4 = grounded

Firmware enables ch1 and ch2 only. **`stereo=False` mixes both mics rather
than picking one**, so mono is the right choice for uploads and halves the
bytes on the wire. Stereo is only useful for metering the two mics separately.

Sensitivity levers, in order of preference:

1. **Analog.** `board_init.c` sets the ES7210 PGA to 30 dB (code 10). The
   ladder tops out at 37.5 dB (code 14), so there is **7.5 dB free**. Poke
   registers `0x43` and `0x44`, preserving bit 4 which is the PGA enable.
   `board_codec_init` is guarded and runs once per boot, so the write should
   persist. Off by default in the app (`analog_gain_code: 0`) until verified.
2. **Digital.** A saturating in-place gain with `@micropython.viper` over
   `ptr16`. Viper `ptr16` loads are unsigned so samples must be sign extended
   by hand, and the result must be clamped, never allowed to wrap. Speech
   peaks around -32 dBFS leave roughly 30 dB of headroom, so 4x to 8x is safe.
3. **The gate.** The real cause of "the mic is not sensitive" was the broken
   `rms()` reading 13 dB low. Levels computed from the buffer are correct, and
   the gate is user tunable on the settings page.

Measured on this unit, computed from the PCM buffer: quiet room about
-55 dBFS, speech peaks about -32 dBFS.

## Display and touch

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
- Fonts present on CoreS3: Montserrat 12/14/16/18/24/40/44/48 and the three
  Alibaba CJK 24 faces. **Montserrat 20, 22, 30 and 36 do not exist** and
  raise `AttributeError`. `ASCII7` and the `DejaVu*` names are aliases onto
  Montserrat, not the small bitmap fonts, so do not size a layout assuming
  `ASCII7` is tiny. `fontHeight()` is not the point size, Montserrat18 is
  21 px. Measure at runtime.
- Passing `font=` to `drawString()` or `textWidth()` **permanently changes the
  current font** as a side effect. Set it back.
- `M5.Lcd.newCanvas(w, h, bpp, psram)` plus `canvas.push(x, y)` is the
  vendor-documented flicker fix. `startWrite()`/`endWrite()` batch the SPI
  transaction but do not buffer. A canvas has neither of those, nor `.FONTS`
  or `.COLOR`, so pass `M5.Lcd.FONTS.X` into it.
- Never hold `startWrite()` open across a network call, the SD card shares the
  LCD SPI host.
- `widgets.Button` is an empty stub that draws nothing. `M5.Widgets` has no
  button, slider or meter. `m5ui` is LVGL and would seize the framebuffer.
  Raw `M5.Lcd` calls are the right choice here.
- Taps that start and end during a blocking `requests2.post` are lost, and a
  finger held across one is misreported as a hold.

## HTTP and OpenAI

- `requests2` has no `files=`, so the multipart body is hand built and passed
  as `data=<bytearray>`. It is HTTP/1.0 with `Connection: close`, so every
  request pays a fresh TLS handshake.
- Measured: first POST after boot about 33 s (one-off TLS and cert setup),
  steady state about 11 s for a 160 KB upload, translate 3.5 to 7 s.
- `gpt-transcribe` returns `""` on silence and puts the detected language in
  `languages[0].code`. It takes `languages[]` (plural), which replaces
  `language`.
- `whisper-1` hallucinates confidently on silence. A silent 5 s clip from this
  device returned `ご視聴ありがとうございました。` and `"Thank you for
  watching!"`. That is why it is not used, and why the app still keeps a
  phrase blocklist as a second line of defence.
- `gpt-5.6-luna` needs `reasoning_effort: "none"`, otherwise it burns hidden
  reasoning tokens on a one-line translation. `gpt-5-nano` **rejects** `none`
  with a 400.
- Use `max_completion_tokens`, not the deprecated `max_tokens`.

## Config and secrets

Real config lives on the device at `/flash/res/config.json` and is
**never committed**. `device/config.example.json` is the template.
`.gitignore` blocks `config.json` everywhere. The settings page writes
`gate_dbfs`, `chunk_seconds` and `mic_gain` back to that file.

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
4. If the board stops answering, it needs a physical hard reset, see the
   FACTS section above.
