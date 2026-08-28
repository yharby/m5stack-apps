# CoreS3 Geo Translator

Pocket English ↔ Japanese interpretation for FOSS4G and open-geospatial
conversations, running on an M5Stack CoreS3.

```text
CoreS3 microphones → two-buffer capture → transcription → translation → LCD
                         listening continues during both API calls
```

## Highlights

- Continuous two-buffer capture using the CoreS3's two physical microphones
- Fast six-second turns with adaptive EN/JA text layout
- Open-geospatial recognition for FOSS4G, OSGeo, STAC, OGC API, GeoParquet,
  GeoServer, MapLibre, GeoZarr, and related terminology
- English ↔ Japanese direction inferred from the transcript itself
- Live two-channel meters with adjustable gate, chunk length, and mic gain
- UIFlow2 MicroPython app with a small host-side USB development CLI

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
sensitivity controls.

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
