# Handover, M5Stack CoreS3 translator

Written mid-task. Read `CLAUDE.md` first, especially its **FACTS** section,
which already records everything verified about this board. This file covers
only what is *in flight*, what we are stuck on, and what was tried.

---

## 1. Where the project stands

Working and verified end to end on real hardware, in both directions:
record, transcribe with `gpt-transcribe`, translate with `gpt-5.6-luna`,
render EN and JA on the LCD.

Repo state at handover: 7 commits, `make check` passes (ruff format + lint on
`device/` and `tools/`). Latest commit is
`84e49bd Overlap capture with network I/O, add a touch settings panel`.

**The committed `device/apps/translator.py` has NOT been run on the device
yet.** It was written against conclusions that the last experiment partly
overturned. See section 4. Do not push it as-is without reading section 5.

## 2. What the user asked for, in their words

1. `"we need to optmise it more as it's so slow taking the advantage of 2
   cores of esp32-s3 .. so one can still lestining and caching the wav and the
   other core handling the sending to openai and reciving back?"`
   The old loop recorded 5 s, then went deaf for the entire ~15 s OpenAI round
   trip. They want capture and network overlapped.
2. `"make the mic so sensitive for now .. maybe we can have small settings icon
   that you can have the input metere of 2 mics visualisation and 2 arrows to
   modify the buffer/sensitivity?"`
3. Reread all scripts and bring them up to date with every finding.
4. Put all findings in `CLAUDE.md` under a FACTS section. **Done.**
5. Use subagents in parallel. **Done, two ran, results folded into CLAUDE.md.**

## 3. What is done

- `CLAUDE.md` rewritten with a FACTS section covering audio, mics and gain,
  display and touch, HTTP and OpenAI, and how to recover a wedged board.
- `device/apps/translator.py` rewritten (~1030 lines): pipelined capture,
  buffer-derived levels, settings page with two mic meters and three
  steppers, viper digital gain, `timeout=` on both posts, mono uploads,
  WAV header inlined into the multipart body.
- `tools/m5.py`: added `run-file <path>` for one-off device probes, and
  `breakin()` now exits with "the board is wedged, hard reset it" instead of a
  pyserial traceback.
- `pyproject.toml`: `B905` added to the device per-file-ignores, MicroPython's
  `zip()` has no `strict=`.

## 4. WHERE WE ARE STUCK

**You cannot start a second asynchronous capture. It hangs the board.**

`record_into(buf, sync=False)` works beautifully once. It returns in 3 to 60 ms
and an ADF task fills the buffer on the other core while the interpreter is
blocked in TLS. That part is proven (see 5.2). The problem is the *second*
call.

Mechanism, from `m5stack/cmodules/adf_module/audio_recorder.c` at tag `2.5.1`:

- `record_into` line 557: `if (self->pipeline != NULL) audio_recorder_stop_helper(self);`
- `stop_helper` lines 611 to 635 contains
  `while (state != AEL_STATE_FINISHED) vTaskDelay(100ms);` **with no timeout**.
- After an async capture, `is_recording()` (which is just `pipeline != NULL`)
  was still `True` 23 s after a 10 s buffer had completely filled.

So the pipeline does not reach `FINISHED` promptly, `stop_helper` spins
forever, and the board is gone. Ctrl-C cannot reach it because it is inside a
C call. Only a physical hard reset recovers it (no EN line on native USB CDC,
DTR/RTS and esptool both do nothing).

**This blocks the user's request 1 as currently designed.** The committed
`run_session()` does exactly the thing that hangs.

### The open question, and the exact next experiment

One earlier run *did* restart cleanly, so this may be a settling-time issue
rather than a permanent one:

| Run | Buffer | Gap before the next `record_into`/`stop()` | Result |
|---|---|---|---|
| `async_test.py` | 2 s | several seconds (a Python scan of the buffer ran in between) | `stop()` succeeded, script completed |
| `buf_test.py` D→E | 5 s | 400 ms | **restart hung** |
| `overlap2.py` | 10 s | 23 s, but WiFi/TLS had run concurrently | `stop()` hung |

Hypothesis: the pipeline needs a grace period after the buffer fills before it
reaches `AEL_STATE_FINISHED`, and 400 ms is not enough.

The script to settle this is already written at
`<scratchpad>/settle_test.py` (path in section 7). It polls `is_recording()`
every 100 ms for up to 20 s after the fill point, logs the exact millisecond it
flips to `False`, and only then attempts a restart, repeating four times.
Run it with:

```bash
uv run python tools/m5.py run-file <scratchpad>/settle_test.py
```

Read `/flash/settle.log` afterwards, it survives a wedge.

- If `is_recording()` reliably flips to `False`, **use that as the restart
  gate** and the pipeline works as designed. Change `capture_done()` in
  `translator.py` back to polling `is_recording()`, and keep a hard timeout so
  a stuck pipeline degrades instead of hanging.
- If it never flips, the async path is a **one-shot per boot** and the
  pipelined design must be abandoned. See section 6 for fallbacks.

## 5. Everything we tried, with evidence

### 5.1 Async capture returns immediately, CONFIRMED
```
recorder attrs: ['stop','AMR','MP3','PCM','WAV','config','create_pcm_buf',
                 'is_recording','is_running','pause','record','record_into',
                 'resume','rms','volume']
buf len 64000
record_into(sync=False) returned after 57 ms
python loop iterations during record: 163451
nonzero samples sampled: 123 of 125
DONE
```

### 5.2 Capture genuinely overlaps a blocking TLS upload, CONFIRMED
From `/flash/overlap.log`. A 10 s buffer was started, then a 33 s POST ran on
the main thread. Every offset in the buffer holds real audio:
```
async capture started, is_recording True
POST took 33176 ms -> {"text":"Outside your main domain is one of the ways
                       entrenchment.","languages":[{"code":"en"}],...}
elapsed since capture start 33236 ms  is_recording True
  off 0      (t=0s) level -49.5
  off 64000  (t=2s) level -38.2
  off 128000 (t=4s) level -45.6
  off 192000 (t=6s) level -47.5
  off 256000 (t=8s) level -40.3
  off 288000 (t=9s) level -50.1
```
The script then called `r.stop()` and **never printed its final line**. That is
the hang.

### 5.3 `rms()` is destructive and reads 13 dB low, CONFIRMED live
```
clip level from buffer: -32.108324 dBFS   recorder.rms(): -45.732976
```
Same clip, two methods. `rms()` tears the pipeline down, rebuilds it at
8 kHz/16/stereo, and reads 1024 fresh bytes, i.e. 64 ms of the room *now*.
**This was the real cause of "the mic is not sensitive".** The gate was
comparing against a number that was 13 dB too low.

### 5.4 Buffer type is NOT the problem, my earlier conclusion was wrong
From `/flash/buf.log`:
```
--- A: create_pcm_buf(1), sync=True ---
  type <class 'bytearray'> len 32000
  A ok
--- B: bytearray(32000), sync=True ---
  B ok
--- C: bytearray(4800) short, sync=True ---
  C ok
--- D: create_pcm_buf(5), sync=False ---
  D returned in 3 ms
  D done, is_recording True
--- E: restart into a second create_pcm_buf(5), sync=False ---
                                     <-- hung here, nothing further
```
`create_pcm_buf` returns a plain `bytearray`, and a hand-rolled `bytearray`
works fine, including a short 4800-byte one for fast meter updates. An earlier
crash on a plain bytearray (`cycle_test.py`) was almost certainly the restart
hazard, not the buffer.

**`translator.py` currently forces `create_pcm_buf` everywhere on the strength
of that wrong conclusion. It is harmless but the 1 fps settings meter it
causes is not. Once E is understood, switch the meter frame back to
`bytearray(int(SAMPLE_RATE * 0.15) * 2 * channels)` for ~5 fps.**

### 5.5 Things that hang or crash the board, all learned the hard way
- `r.record("file:///flash/t.mp3", 3)` hung for 240 s and never returned.
- `r.stop()` after an async capture, hung.
- `record_into(sync=False)` while a previous async capture has not settled, hung.
- Repeatedly `mpremote run`-ing a script that constructs an `audio.Recorder`
  without rebooting. Each constructor **leaks a 4 KB FreeRTOS task forever**.
  Build one per boot.

### 5.6 Two subagent research passes, both source-verified
Against `uiflow_micropython` tag `2.5.1` (commit `96c8a6e2`), M5Unified,
M5GFX, and the official CoreS3 schematic. Full findings are already merged
into `CLAUDE.md`. Headlines:
- Two **real** MEMS mics, U12 on ES7210 ch1 and U13 on ch2. Ch3 is an echo
  reference off the speaker, ch4 is grounded. `stereo=False` **mixes** both
  mics, it does not pick one.
- ES7210 PGA sits at 30 dB, ladder tops out at 37.5 dB. **7.5 dB is free** via
  I2C regs `0x43`/`0x44` at address `0x40` on port 1 (SCL 11, SDA 12),
  preserving bit 4. Untested on device, gated behind `analog_gain_code: 0`.
- `M5.Touch` has **no** `wasClicked()` method, it is `getDetail(0)[6]`.
  Hold is `[9]`. `M5.update()` must run every loop.
- Colours are 24-bit `0xRRGGBB`, there is **no** `color565()`.
- `Montserrat20/22/30/36` do not exist on CoreS3.
- `widgets.Button` is an empty stub that draws nothing.

## 6. Fallbacks if the restart really is impossible

In rough order of value:

1. **One long capture, consumed progressively.** Start a single
   `record_into(big, sync=False)` with, say, a 60 s buffer (1.92 MB, there is
   ~8 MB free). The ADF fills it sequentially and the buffer is ours, so read
   completed regions out of it *while it is still filling*, using elapsed time
   to know how far it has got (32000 bytes/s mono, minus a safety margin).
   Upload slices as they become available. This gives genuinely continuous
   capture with zero restarts. The session simply ends when the buffer is
   full, or the app reboots itself. **This is the most promising route.**
2. **`pause()` / `resume()`.** They exist and manipulate the same event group
   without going through `stop_helper`. Whether they let you retarget a buffer
   is unknown and worth 10 minutes of probing.
3. **Switch to `M5.Mic` instead of `audio.Recorder`.** It is bound and
   supported on CoreS3, exposes `config(magnification=N)` for real digital
   gain, and `isRecording()` returns a queue depth (0/1/2) which is closer to a
   progress signal. The catch is that `M5.Mic` and `audio.Recorder` both claim
   I2S port 1 and cannot coexist, so this is an all-or-nothing rewrite of the
   audio layer.
4. **Give up on overlap, shrink the round trip instead.** `requests2` is
   HTTP/1.0 with `Connection: close`, so every call pays a fresh TLS
   handshake. Measured: first POST after boot ~33 s, steady state ~11 s for
   160 KB, translate 3.5 to 7 s. Trimming silence before upload and using mono
   (already done, halves the bytes) are the cheap wins.

## 7. Operational notes for whoever picks this up

- **Scratchpad with all the probe scripts:**
  `/private/tmp/claude-501/-Users-yharby-Documents-gh-m5stack-translator/a746350a-5ff0-4a2c-b629-f3e0678766c7/scratchpad/`
  Contains `async_test.py`, `overlap_test.py`, `overlap2.py`, `cycle_test.py`,
  `buf_test.py`, `settle_test.py` (the next one to run), and `devrun.py`.
  `tools/m5.py run-file <path>` now does what `devrun.py` did, prefer it.
- **Always have on-device scripts append progress to a file under `/flash`.**
  Every finding above came from a log that survived a wedge. Serial output does
  not survive.
- **Budget for hard resets.** Ask the user to hold the power button ~6 s, then
  press once. There is no software path. The user has been doing this
  willingly, they said "ask me to hard reset it for you if you want that".
- Never run two things at the port at once, and kill stray `mpremote`
  processes before retrying.
- Between risky probes, get the user to reset, so a leaked Recorder task from
  the previous run cannot confound the next.

## 8. Outstanding, unrelated to the blocker

- **The user's OpenAI API key was pasted in plaintext in the first message of
  the original conversation and still needs rotating.** It has been reminded
  once and should be reminded again. Never echo it. `.gitignore` blocks
  `config.json` everywhere and the real config lives only on the device at
  `/flash/res/config.json`.
- The ES7210 +7.5 dB PGA poke is written but disabled (`analog_gain_code: 0`).
  Verify it on device, then default it to `14`.
- The viper `_gain_inplace` has never executed on the board. Confirm it
  compiles under this MicroPython build and time it over 160000 samples.
- `set_channels()` uses `recorder.config(...)` to switch mono/stereo at
  runtime. Never exercised. Remember `config()` fills defaults for omitted
  arguments, so all three must always be passed.
- Delete the probe logs left on the device: `/flash/overlap.log`,
  `/flash/buf.log`, `/flash/cycle.log`, `/flash/settle.log`.
