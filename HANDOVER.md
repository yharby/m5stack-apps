# Translator app performance handover

This document is specific to `device/apps/translator.py` in the broader
M5Stack Apps registry. Repository-wide workflow and platform facts live in
`README.md` and `CLAUDE.md`.

Updated 2026-08-30 after latency, Wi-Fi, and optional SD transcript passes.
Read `CLAUDE.md` first; its verified platform facts are the durable reference
and include corrected entries that contradict earlier notes.

## Current status: working, measurably faster

Four workstreams landed together on `perf/integration` and were verified on
the physical board.

| Stage | Before | After |
|---|---|---|
| Transcription | 2.0 to 2.9 s | 1.75 to 1.94 s |
| Translation | 2.7 to 3.4 s | 1.7 to 2.5 s |
| Pipeline total | ~5.2 s | ~3.7 s |

## The bug that mattered most

`HTTP_THREAD_STACK` was 32768. **A 32 KB task stack cannot be allocated on
this board.** Every POST raised `OSError("can't create thread")`, fell through
`do_post`'s two retries, and took the blocking fallback:

```
http: worker start retry after OSError("can't create thread",)
http: worker unavailable OSError("can't create thread",); using blocking POST
```

So the documented "main thread keeps calling `M5.update()` during TLS" design
had never actually run, and touch was dead during every network call. The
previous notes claiming a verified 32 KB stack were wrong.

`tools/device_scripts/thread_probe.py` shows 16384, 8192, 4096 and 2048 create
fine and 32768 never does. `tools/device_scripts/tls_thread_probe.py` then
completed a real TLS handshake and full HTTP round trip on stacks down to
8 KB. The constant is now 16384, and `do_post` steps down through
16384/12288/8192 before it will ever block again.

Re-probe before raising that value.

## What changed

**Transport.** `KeepAliveClient` holds one HTTP/1.1 TLS socket to
api.openai.com and reuses it for every call in a session. Raw `socket` + `ssl`
works on this firmware, the handshake costs about 540 ms, and a full
multi-turn session now logs exactly one `keep-alive TLS` line instead of two
per turn. `requests2` remains an automatic fallback that latches off after
three consecutive failures. `prewarm()` opens the socket at boot.

**Capture.** Audio is no longer sliced on a wall clock. 1 s frames go into a
ring, each 100 ms window is scored against the gate, and an utterance closes
on 600 ms of trailing quiet after at least 400 ms of speech. `chunk_seconds`
is now the ceiling for when the talker never pauses, mapped 1:1 so a monologue
is never slower than the old fixed slice. `poll_session_controls()` doubles as
the audio pump, keeping both FIFO slots filled during network waits.

**Rendering.** `wrap_text` was O(n squared), measuring the whole growing line
per character across up to four candidate fonts, roughly sixteen full passes
per turn. It is linear now with a glyph-width cache. Regions skip repaint when
unchanged, and both text regions draw through `M5.Lcd.newCanvas` double
buffers, verified working at `bpp=16, psram=True`.

**Plumbing.** The log file is held open instead of reopened per line, still
flushed per line so it survives a wedge, and rotated at 64 KB. `ensure_wifi()`
existed but was never called, so a dropped link surfaced as an opaque 45 s
timeout; it now runs at boot and before each session. It waits for UIFlow2's
in-flight boot association, dynamically reads `ssid0`/`pswd0` from the
`uiflow` NVS namespace, and uses JSON credentials only as a fallback. Wi-Fi
modem power save is off, `pm` is the key this build accepts. The LCD now
distinguishes Wi-Fi association from the API TLS warm-up, which can otherwise
make a successful Wi-Fi connection look stuck for tens of seconds.

**Storage.** SD transcript saving is opt-in from Settings. It creates one
bounded Markdown (default) or JSONL file set per listening session under
`/sd/m5stack-apps/translator/`, recording both languages, relative audio time,
UTC, and a fixed configured local offset. Every completed turn is appended,
closed, and synced; ordinary SD errors disable saving for that session without
stopping translation. The real 32 GB card passed mount/read/write/UTF-8 tests,
and live turn writes measured 19-108 ms.

## Known remaining gaps

1. **Endpointing is unverified on conversational speech.** Every device test
   used continuous podcast audio with no pauses, so the endpointer always hit
   the ceiling and the silence trimmer never trimmed. Both paths need a real
   two-person conversation to exercise. Expect `utt: closed N frames, ...`
   with a non-zero trailing-quiet count when it is working.
2. **A capture gap remains during upload preparation.** One
   `mic: FIFO empty, pump not called for 1596 ms, audio dropped` still appears
   per session. Nothing pumps during `prepare_chunk`'s gain pass (~260 ms) and
   the multipart body build, so a little audio is still lost there.
3. **The prewarmed socket does not always survive to the first utterance.**
   A second handshake sometimes appears on the first real POST. Reuse within a
   session is the reliable win, the boot prewarm is a bonus.
4. **Deferred by design.** Collapsing transcription and translation into one
   audio-input call, and streaming the translation over SSE, are the two
   largest remaining wins. Both depend on the socket layer, which is why they
   were held back until keep-alive proved out on hardware.

## Watch the log for

```
http: keep-alive TLS to api.openai.com in 541 ms   once per session, not per call
http: POST worker stack is now 16384 bytes         the thread fix working
utt: closed 7 frames, 63 speech windows, 0 trailing quiet
rec: trimmed 224000 to 138000 bytes                trimmer actually firing
mic: FIFO empty, pump not called for N ms          capture gap, see gap 2
http: keep-alive disabled for this run             fell back to requests2
```

## Security

The OpenAI API key must be rotated if it was ever pasted into a conversation.
Never echo it. Real config stays only at `/flash/res/config.json`, and
`.gitignore` blocks `config.json` everywhere.
