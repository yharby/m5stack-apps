# CoreS3 Geo Translator

Pocket English ↔ Japanese interpretation for FOSS4G and open-geospatial
conversations, running on an M5Stack CoreS3.

```text
CoreS3 mics → 1 s frames → pause detection → transcription → translation → LCD
                    listening continues during both API calls
```

## Highlights

- Speech is cut on a natural pause, not on a wall clock, so a short sentence
  turns around as soon as the talker stops instead of waiting out a fixed slice
- One keep-alive TLS connection is reused for every API call in a session,
  removing two handshakes per turn
- Capture keeps running during both API calls, pumped from the same loop that
  services touch
- Open-geospatial recognition for FOSS4G, OSGeo, STAC, OGC API, GeoParquet,
  GeoServer, MapLibre, GeoZarr, and related terminology
- English ↔ Japanese direction inferred from the transcript itself
- Flicker-free canvas rendering with adaptive EN/JA text layout
- Live two-channel meters with adjustable gate, utterance ceiling, and mic gain
- UIFlow2 MicroPython app with a small host-side USB development CLI

## Measured on device

With continuous speech and a 7 s ceiling, against the previous fixed-slice and
`requests2` build:

| Stage | Before | After |
|---|---|---|
| Transcription | 2.0 to 2.9 s | 1.75 to 1.94 s |
| Translation | 2.7 to 3.4 s | 1.7 to 2.5 s |
| Pipeline total | ~5.2 s | ~3.7 s |

The old build also left the microphone idle about 1.2 s of every cycle, losing
roughly 15 percent of speech. Conversational audio with real pauses benefits
further, because the endpointer then fires well before the ceiling.

## Setup

Requires an M5Stack CoreS3 running UIFlow2 `2.5.1`, Python 3.11+, and
[`uv`](https://docs.astral.sh/uv/).

```bash
make setup
cp device/config.example.json device/config.json
# Add Wi-Fi and OpenAI credentials to device/config.json, then place the
# private config at /flash/res/config.json on the device.
make push
make run
```

Tap the screen to start or pause. Tap the gear for microphone meters and
sensitivity controls, where `Chunk` sets the longest an utterance can run
before it is sent regardless of pauses.

Useful commands:

```bash
make info       # inspect the connected board
make selftest   # mic + network + API end-to-end test
make logs       # read /flash/translator.log
make check      # formatting and lint
```

## Configuration

The committed [example configuration](device/config.example.json) contains no
credentials. Real secrets belong only in `/flash/res/config.json` and are
ignored by Git. The geospatial context and recognition keywords are also
configurable there.

See [CLAUDE.md](CLAUDE.md) for verified CoreS3/UIFlow2 implementation notes
and hardware-specific debugging details.
