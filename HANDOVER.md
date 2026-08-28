# Handover, M5Stack CoreS3 translator

Updated 2026-08-28 after the blocker was resolved and tested on the real
device. Read `CLAUDE.md` first; its FACTS section is the durable reference.

## Current status: working

The translator now records continuously enough for practical use, transcribes
and translates in both directions, and keeps the LCD responsive between
network calls. The user tested the six-second UI and said it was working very
well.

The production design is `M5.Mic`, not `audio.Recorder`:

- One persistent M5Unified mic task is pinned to core 0.
- Two mono `bytearray` buffers are kept in the mic's FIFO.
- When the oldest buffer finishes, the app copies it, immediately requeues
  the original, then transcribes and translates the stable copy.
- Capture therefore continues while MicroPython is blocked in TLS.
- On stop, the app ceases requeues and drains both raw buffer pointers before
  allowing the Python objects to be freed.

Live end-to-end results with two 192000-byte buffers (six seconds each):

- Requeue took 0-1 ms.
- Viper 4x gain took about 35 ms for 96000 samples.
- Steady transcription took roughly 2-6 s.
- Translation generally took 2-3 s.
- Repeated English-to-Japanese chunks, silence gating, and Japanese speech
  were all exercised without an audio restart hang.

## Important fix made after the first successful run

`gpt-transcribe` repeatedly returned language `en` for transcripts containing
obvious Japanese kana and kanji. Trusting that metadata selected the wrong
translation direction and made the LCD render Japanese with a Latin font,
which appeared as boxes.

`recognize()` now determines EN versus JA from the returned text itself. The
display independently inspects the text before selecting its glyph font, so a
bad API language label cannot produce boxes again. A second live run verified
Japanese speech was logged as `[ja, api='en']` and translated into English.

## Why `audio.Recorder` was abandoned

The original ADF experiment proved that one asynchronous capture could fill a
buffer during a blocking 33-second TLS request. But restarting it is unsafe:

- cycle 0 returned in 57 ms and `is_recording()` became false after 3219 ms;
- cycle 1 returned in 3 ms but never settled within 15 seconds;
- the next restart or `stop()` enters an untimed firmware spin wait and wedges
  the board until a physical hard reset.

Plain `bytearray` was never the problem: `create_pcm_buf()` returns the same
type. The decisive scripts are kept in `tools/device_scripts/async_settle.py`,
`m5_mic_queue.py`, and `m5_mic_cancel.py`.

## M5.Mic evidence

The replacement was verified against UIFlow2 2.5.1 and M5Unified source, then
on the physical CoreS3:

- `M5.Mic.record(buf, 16000, False)` queues asynchronously.
- `isRecording()` is queue occupancy 0/1/2.
- Six consecutive three-second captures completed, followed by `end()`,
  `begin()`, and another successful capture.
- Calling `end()` 350 ms into a five-second capture waited the remaining
  4.65 s; it does not cancel an active buffer. The app drains instead.
- Mono mixes the two physical ES7210 mic channels. Stereo is used only for
  the channel probe and the responsive 150 ms settings meters.
- `M5.Mic` and `audio.Recorder` both own I2S1 and must not coexist.

## UI and HTTP changes

- Default chunk duration is six seconds for quicker page changes and shorter
  sentences.
- The HEARD preview is compact; TRANSLATION gets most of the screen.
- English text chooses among several font sizes using measured wrapping.
- Japanese uses `AlibabaSansJA24`; font choice is based on the actual text.
- Common smart punctuation is normalized only for LCD rendering so a missing
  Latin glyph cannot appear as an isolated box.
- The hallucination filter uses ASCII-only case folding. MicroPython's full
  `str.lower()` raised `UnicodeError` on one real Japanese transcript.
- The previous translation remains visible until its replacement is ready.
- The top status shows an approximate listening countdown.
- Settings use 150 ms stereo frames, giving responsive independent mic bars.
- Chat JSON is explicitly UTF-8 encoded before POST. `requests2`'s `json=`
  path uses character count for Content-Length and broke Japanese/em-dash
  payloads with HTTP 400.
- `gpt-transcribe` receives a concise `keywords[]` glossary for canonical
  FOSS4G/OSGeo terminology. The translation prompt preserves canonical names,
  uses consistent STAC nouns, and resolves explicit spoken self-corrections.
  Device verification returned HTTP 200, recognized `OSGeo JP` in Japanese
  speech, and translated a Japanese test sentence while preserving `FOSS4G`,
  `STAC`, and `GeoParquet`. The full glossary adds about 3.8 KB to a 192 KB
  six-second upload and did not measurably change the roughly two-second
  steady translation time.

## Final verification

- Ruff formatting, lint, and `git diff --check` pass.
- The finalized app was pushed to `/flash/apps/translator.py`.
- A live Japanese run produced multiple correctly directed English results.
- The same run produced curly U+2019 punctuation such as `today’s`; the
  LCD-only normalizer deterministically renders it as ASCII `today's`, which
  is present in the Latin font.
- `make probe` and `make selftest` are updated to use `M5.Mic`. The selftest
  deliberately sends Japanese JSON to cover the UTF-8 Content-Length bug.

If USB remains present but every command hangs inside a C call, ask the user
to hard reset: hold power about six seconds, then press once. Native USB CDC
has no EN line for the host to toggle.

## Security

The OpenAI API key pasted in the original conversation must be rotated. Never
echo it. The real config stays only at `/flash/res/config.json`, and
`.gitignore` blocks `config.json`.
