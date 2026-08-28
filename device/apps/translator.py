"""Realtime EN <-> JA speech translator for M5Stack CoreS3 (UIFlow2 MicroPython).

Tap the screen or press the power button to start and stop. Tap the gear in
the top right for the settings page, which shows live mic meters and lets you
change the sensitivity gate and the chunk length. Every stage is logged to
/flash/translator.log and to the serial console.

Device API facts, all verified by probing this board or by reading the
uiflow-micropython 2.5.1 source. Do not "fix" these back to what the docs
suggest, see the FACTS section of CLAUDE.md for the full list.

  * audio.Recorder's second asynchronous capture wedges in its untimed ADF
    cleanup loop. This app does not construct one. M5.Mic owns one persistent
    I2S task and a two-buffer FIFO that has completed repeated real-device
    capture, teardown, and restart tests.
  * M5.Mic.record(buf, 16000, stereo) is asynchronous. isRecording() returns
    queue occupancy: 0 idle, 1 one slot active, 2 both slots occupied.
  * M5.Mic keeps only a raw pointer to each buffer, so queued buffers remain
    globally rooted until their captures finish.
  * M5.Touch exposes only getX/getY/getCount/getDetail/getTouchPointRaw.
    There is no wasClicked() method, it is getDetail(0)[6].
  * M5.update() must run every loop or touch state never changes.
  * Colours are 24 bit 0xRRGGBB ints. There is no color565().
  * Japanese needs M5.Lcd.FONTS.AlibabaSansJA24 or glyphs do not render.
  * CoreS3 has no BtnA/B/C, only BtnPWR.
  * requests2 has no files= parameter, so the multipart body is hand built
    and passed as data=<bytearray>.
  * socket.setdefaulttimeout does not exist on this firmware.

API choices, verified against the live API from this device:

  * gpt-transcribe returns empty text on silence. whisper-1 hallucinates
    ("Thank you for watching!"), which is why it is not used here.
  * gpt-transcribe returns the detected language in languages[0].code.
  * gpt-5.6-luna needs reasoning_effort "none". gpt-5-nano rejects "none".
  * max_completion_tokens, not the deprecated max_tokens.
"""

import gc
import io
import json
import math
import struct
import sys
import time

import M5
import micropython
import network
import requests2
from M5 import BtnPWR

CONFIG_PATHS = (
    "/flash/res/config.json",
    "/flash/config.json",
    "/flash/apps/config.json",
)

DEFAULT_DOMAIN_KEYWORDS = (
    "FOSS4G",
    "OSGeo",
    "STAC",
    "SpatioTemporal Asset Catalog",
    "OGC",
    "OGC API",
    "Cloud Native Geospatial",
    "COG",
    "Cloud Optimized GeoTIFF",
    "GeoParquet",
    "Apache Parquet",
    "GeoServer",
    "MapServer",
    "GeoJSON",
    "GeoTIFF",
    "Zarr",
    "GeoZarr",
    "MapLibre",
    "MapLibre GL JS",
    "GDAL",
    "PROJ",
    "PostGIS",
    "QGIS",
    "GeoPackage",
    "FlatGeobuf",
    "PMTiles",
    "GeoArrow",
    "DuckDB",
    "Apache Sedona",
    "COPC",
    "vector tiles",
    "raster tiles",
    "coordinate reference system",
    "CRS",
    "EPSG",
    "WMS",
    "WFS",
    "WCS",
    "WMTS",
)

CFG = {
    "wifi_ssid": "",
    "wifi_pass": "",
    "openai_api_key": "",
    "transcribe_url": "https://api.openai.com/v1/audio/transcriptions",
    "chat_url": "https://api.openai.com/v1/chat/completions",
    "transcribe_model": "gpt-transcribe",
    "chat_model": "gpt-5.6-luna",
    # gpt-transcribe supports a keywords array specifically for domain terms.
    # This can be replaced by a JSON array in the device config.
    "domain_keywords": DEFAULT_DOMAIN_KEYWORDS,
    "domain_context": (
        "a live FOSS4G/OSGeo conversation about open geospatial standards, "
        "software, data formats, and cloud-native geospatial architecture"
    ),
    # Sensitivity. Measured from the PCM buffer, which reads about 13 dB
    # higher than the broken recorder.rms(). Quiet room is about -55 dBFS on
    # this unit and speech peaks about -32 dBFS, so -52 is deliberately hot.
    "gate_dbfs": -52,
    # Seconds of audio per API call. Two buffers remain queued while the
    # previous chunk is uploaded. Six seconds feels responsive and generally
    # gives the network enough audio runway without producing long sentences.
    "chunk_seconds": 6,
    # Digital gain applied to the PCM before upload, 1 to 16. Speech peaks at
    # about -32 dBFS on this unit so there is roughly 30 dB of headroom.
    "mic_gain": 4,
    # ES7210 PGA code. M5.Mic initializes code 11 and the maximum is 14.
    # Off by default until the I2C poke is verified on this unit.
    "analog_gain_code": 0,
}

SAMPLE_RATE = 16000
LOG_PATH = "/flash/translator.log"
HTTP_TIMEOUT = 45

GATE_MIN, GATE_MAX = -70, -25
CHUNK_MIN, CHUNK_MAX = 3, 15
GAIN_MIN, GAIN_MAX = 1, 16

# ES7210 is at 7 bit address 0x40 on internal I2C port 1, SCL 11, SDA 12.
# Registers 0x43 and 0x44 are MIC1_GAIN and MIC2_GAIN, low nibble is the PGA
# code, bit 4 is the PGA enable that board_codec_init already sets.
ES7210_ADDR = 0x40
ES7210_MIC_GAIN_REGS = (0x43, 0x44)

# Second line of defence. gpt-transcribe returns "" for silence, but if the
# model is ever switched back to whisper-1 these are what it invents.
HALLUCINATIONS = (
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございます",
    "チャンネル登録",
    "thank you for watching",
    "thanks for watching",
    "please subscribe",
    "subtitles by",
    "amara.org",
)

W, H = 320, 240
BG = 0x000000
FG_STATUS = 0x00CFFF
FG_ORIG = 0xE0E0E0
FG_TRANS = 0x40FF70
FG_DIM = 0x707070
FG_LINE = 0x404040
FG_ALERT = 0xFF5050
FG_TEXT = 0xFFFFFF
FG_PANEL = 0x303030

GEAR_BOX = (288, 1, 32, 30)

BOUNDARY = "----M5CoreS3TranslatorBoundary"

running = False
fatal = ""
last_orig = ""
last_trans = ""
last_src = "en"
last_trans_lang = "ja"
last_level = -99.0
font_ja = None
font_ui = None
font_label = None
font_source_en = None
font_trans_en = None
capture_buffers = None
settings_requested = False


# ------------------------------------------------------------------ logging


def log(msg):
    line = "[t=%d] %s" % (time.ticks_ms(), msg)
    print(line)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_exc(e):
    try:
        buf = io.StringIO()
        sys.print_exception(e, buf)
        log("EXC: " + buf.getvalue())
    except Exception:
        log("EXC: %r" % e)


# ------------------------------------------------------------------ config


def load_config():
    for path in CONFIG_PATHS:
        try:
            with open(path) as f:
                incoming = json.loads(f.read())
        except Exception:
            continue
        for k in CFG:
            if k in incoming:
                CFG[k] = incoming[k]
        log("config: loaded %s" % path)
        break
    else:
        log("config: NONE of %s found" % (CONFIG_PATHS,))
    log(
        "config: api_key=%s stt=%s chat=%s gate=%s chunk=%ss gain=%sx"
        % (
            "yes" if CFG["openai_api_key"] else "NO",
            CFG["transcribe_model"],
            CFG["chat_model"],
            CFG["gate_dbfs"],
            CFG["chunk_seconds"],
            CFG["mic_gain"],
        )
    )


def save_config():
    """Persist only the tunables the settings page can change."""
    path = CONFIG_PATHS[0]
    try:
        try:
            with open(path) as f:
                data = json.loads(f.read())
        except Exception:
            data = {}
        data["gate_dbfs"] = CFG["gate_dbfs"]
        data["chunk_seconds"] = CFG["chunk_seconds"]
        data["mic_gain"] = CFG["mic_gain"]
        with open(path, "w") as f:
            f.write(json.dumps(data))
        log(
            "config: saved gate=%s chunk=%s gain=%s"
            % (CFG["gate_dbfs"], CFG["chunk_seconds"], CFG["mic_gain"])
        )
    except Exception as e:
        log("config: save failed %r" % e)


def ensure_wifi():
    w = network.WLAN(network.STA_IF)
    w.active(True)
    if w.isconnected():
        return True
    if not CFG["wifi_ssid"]:
        log("wifi: down and no ssid configured")
        return False
    log("wifi: connecting to %s" % CFG["wifi_ssid"])
    w.connect(CFG["wifi_ssid"], CFG["wifi_pass"])
    deadline = time.ticks_add(time.ticks_ms(), 20000)
    while not w.isconnected():
        M5.update()
        if time.ticks_diff(deadline, time.ticks_ms()) < 0:
            log("wifi: timeout, status=%s" % w.status())
            return False
        time.sleep_ms(300)
    log("wifi: connected ip=%s" % w.ifconfig()[0])
    return True


# ------------------------------------------------------------------ levels


def channel_dbfs(pcm, ch=0, nch=1, step=8):
    """RMS level of one interleaved channel, read straight from the PCM buffer.

    This exists because recorder.rms() rebuilds the capture pipeline and
    measures fresh audio rather than the clip in hand.
    """
    stride = 2 * nch * step
    start = 2 * ch
    total = 0
    n = 0
    i = start
    end = len(pcm) - 1
    while i < end:
        v = pcm[i] | (pcm[i + 1] << 8)
        if v >= 32768:
            v -= 65536
        total += v * v
        n += 1
        i += stride
    if not n:
        return -99.0
    mean = total / n
    if mean <= 0:
        return -99.0
    return 20.0 * math.log(math.sqrt(mean) / 32768.0, 10)


def channel_peak_dbfs(pcm, ch=0, nch=1, step=4):
    stride = 2 * nch * step
    start = 2 * ch
    peak = 0
    i = start
    end = len(pcm) - 1
    while i < end:
        v = pcm[i] | (pcm[i + 1] << 8)
        if v >= 32768:
            v -= 65536
        if v < 0:
            v = -v
        if v > peak:
            peak = v
        i += stride
    if peak <= 0:
        return -99.0
    return 20.0 * math.log(peak / 32768.0, 10)


def level_fraction(db, lo=-70.0, hi=-10.0):
    if db <= lo:
        return 0.0
    if db >= hi:
        return 1.0
    return (db - lo) / (hi - lo)


# ------------------------------------------------------------------ display


def hit(x, y, box):
    bx, by, bw, bh = box
    return bx <= x < bx + bw and by <= y < by + bh


def tap():
    """One shot tap position, or None. M5.update() must have run this loop."""
    if M5.Touch.getCount() == 0:
        return None
    d = M5.Touch.getDetail(0)
    if d[6]:  # wasClicked
        return (M5.Touch.getX(), M5.Touch.getY())
    return None


def holding():
    if M5.Touch.getCount() == 0:
        return None
    d = M5.Touch.getDetail(0)
    if d[9]:  # isHolding
        return (M5.Touch.getX(), M5.Touch.getY())
    return None


def draw_gear(color=FG_DIM):
    x, y, w, h = GEAR_BOX
    cx, cy = x + w // 2, y + h // 2
    r = 9
    M5.Lcd.fillRect(x, y, w, h, BG)
    for a in range(0, 360, 45):
        try:
            M5.Lcd.fillArc(cx, cy, r, r + 4, a - 9, a + 9, color)
        except Exception:
            break
    M5.Lcd.fillCircle(cx, cy, r, color)
    M5.Lcd.fillCircle(cx, cy, r // 2, BG)


def set_status(text, color=FG_STATUS):
    try:
        M5.Lcd.fillRect(0, 0, GEAR_BOX[0] - 2, 30, BG)
        if font_ui is not None:
            M5.Lcd.setFont(font_ui)
        M5.Lcd.setTextColor(color, BG)
        M5.Lcd.drawString(text, 6, 3)
    except Exception:
        pass


def draw_frame():
    M5.Lcd.fillScreen(BG)
    M5.Lcd.drawLine(0, 31, W - 1, 31, FG_LINE)
    M5.Lcd.drawLine(0, 104, W - 1, 104, FG_LINE)
    draw_gear()


def wrap_text(text, max_px=W - 12):
    """Wrap with real glyph metrics, which matters for mixed Latin and CJK."""
    lines = []
    line = ""
    for ch in text:
        if ch == "\n":
            lines.append(line)
            line = ""
            continue
        trial = line + ch
        try:
            too_wide = M5.Lcd.textWidth(trial) > max_px
        except Exception:
            too_wide = len(trial) > 20
        if too_wide and line:
            lines.append(line)
            line = ch
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def lcd_safe_text(text):
    """Replace common smart punctuation missing from some Latin font builds."""
    for old, new in (
        ("\u2018", "'"),
        ("\u2019", "'"),
        ("\u201c", '"'),
        ("\u201d", '"'),
        ("\u2013", "-"),
        ("\u2014", "-"),
        ("\u2026", "..."),
        ("\u00a0", " "),
    ):
        text = text.replace(old, new)
    return text


def fit_lines(text, candidates, max_px, available):
    """Choose the largest available font whose wrapped text fits."""
    chosen_font, chosen_height, chosen_lines = candidates[-1][0], candidates[-1][1], []
    for font, line_height in candidates:
        if font is not None:
            M5.Lcd.setFont(font)
        lines = wrap_text(text, max_px)
        chosen_font, chosen_height, chosen_lines = font, line_height, lines
        if len(lines) * line_height <= available:
            break
    return chosen_font, chosen_height, chosen_lines


def draw_labeled_region(label_y, text_y, bottom, label, text, color, language, primary):
    M5.Lcd.fillRect(0, label_y, W, bottom - label_y, BG)
    if font_label is not None:
        M5.Lcd.setFont(font_label)
    M5.Lcd.setTextColor(FG_DIM, BG)
    M5.Lcd.drawString(label, 7, label_y)

    available = bottom - text_y
    # Select the glyph set from the text itself. The transcription API has
    # returned language="en" for clearly Japanese text on this device; using
    # that metadata for rendering produces tofu boxes.
    text_language = detect_source(text) if text else language
    shown_text = lcd_safe_text(text)
    if text_language == "ja":
        candidates = ((font_ja, 26),)
    elif primary:
        candidates = (
            (font_trans_en, 28),
            (font_source_en, 22),
            (font_ui, 19),
            (font_label, 15),
        )
    else:
        candidates = ((font_source_en, 22), (font_ui, 19), (font_label, 15))
    font, line_height, lines = fit_lines(shown_text or "...", candidates, W - 14, available)
    if font is not None:
        M5.Lcd.setFont(font)
    M5.Lcd.setTextColor(color if text else FG_DIM, BG)
    y = text_y
    visible = available // line_height
    for line in lines[:visible]:
        M5.Lcd.drawString(line, 7, y)
        y += line_height


def update_display():
    target = last_trans_lang if last_trans else ("ja" if last_src == "en" else "en")
    draw_labeled_region(
        35,
        50,
        103,
        "HEARD  %s" % last_src.upper(),
        last_orig,
        FG_ORIG,
        last_src,
        False,
    )
    draw_labeled_region(
        108,
        124,
        239,
        "TRANSLATION  %s" % target.upper(),
        last_trans,
        FG_TRANS,
        target,
        True,
    )


# ------------------------------------------------------------------ settings


class Meter:
    """Horizontal bar that only repaints the pixels that changed, so no flicker."""

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.prev = 0
        self.mark = -1

    def frame(self):
        M5.Lcd.drawRect(self.x - 1, self.y - 1, self.w + 2, self.h + 2, FG_DIM)
        self.prev = 0
        M5.Lcd.fillRect(self.x, self.y, self.w, self.h, BG)

    def set_mark(self, frac):
        """Draw the gate threshold as a tick inside the bar."""
        if self.mark >= 0:
            M5.Lcd.fillRect(self.x + self.mark, self.y, 1, self.h, BG)
        self.mark = int(self.w * frac)
        if self.mark >= self.w:
            self.mark = self.w - 1
        M5.Lcd.fillRect(self.x + self.mark, self.y, 1, self.h, FG_ALERT)

    def set(self, frac, color):
        n = int(self.w * frac)
        if n > self.w:
            n = self.w
        if n > self.prev:
            M5.Lcd.fillRect(self.x + self.prev, self.y, n - self.prev, self.h, color)
        elif n < self.prev:
            M5.Lcd.fillRect(self.x + n, self.y, self.prev - n, self.h, BG)
        self.prev = n
        if self.mark >= 0 and self.mark < self.w:
            M5.Lcd.fillRect(self.x + self.mark, self.y, 1, self.h, FG_ALERT)


def draw_stepper(y, label, value, unit):
    M5.Lcd.fillRect(0, y, 206, 30, BG)
    M5.Lcd.setTextColor(FG_TEXT, BG)
    M5.Lcd.drawString("%s %d%s" % (label, value, unit), 10, y + 5)
    for bx, up in ((214, False), (266, True)):
        M5.Lcd.fillRoundRect(bx, y, 44, 30, 6, FG_PANEL)
        cx, cy = bx + 22, y + 15
        if up:
            M5.Lcd.fillTriangle(cx, cy - 7, cx - 8, cy + 5, cx + 8, cy + 5, FG_TEXT)
        else:
            M5.Lcd.fillTriangle(cx, cy + 7, cx - 8, cy - 5, cx + 8, cy - 5, FG_TEXT)


ROW_Y = (80, 116, 152)
ROWS = (
    ("Gate", "gate_dbfs", " dB", GATE_MIN, GATE_MAX),
    ("Chunk", "chunk_seconds", " s", CHUNK_MIN, CHUNK_MAX),
    ("Gain", "mic_gain", "x", GAIN_MIN, GAIN_MAX),
)
BACK_BOX = (10, 194, 120, 34)


def settings_page():
    """Live meters for both ES7210 mics, plus the three tunables."""
    global last_level

    if font_ui is not None:
        M5.Lcd.setFont(font_ui)
    M5.Lcd.fillScreen(BG)
    M5.Lcd.setTextColor(FG_TEXT, BG)
    M5.Lcd.drawString("Settings", 10, 2)
    M5.Lcd.drawLine(0, 24, W - 1, 24, FG_LINE)

    M5.Lcd.setTextColor(FG_DIM, BG)
    M5.Lcd.drawString("MIC 1", 10, 32)
    M5.Lcd.drawString("MIC 2", 10, 56)
    left = Meter(70, 32, 240, 16)
    right = Meter(70, 56, 240, 16)
    left.frame()
    right.frame()

    gate_frac = level_fraction(CFG["gate_dbfs"])
    left.set_mark(gate_frac)
    right.set_mark(gate_frac)

    for y, row in zip(ROW_Y, ROWS):
        draw_stepper(y, row[0], CFG[row[1]], row[2])

    M5.Lcd.fillRoundRect(BACK_BOX[0], BACK_BOX[1], BACK_BOX[2], BACK_BOX[3], 8, FG_PANEL)
    M5.Lcd.setTextColor(FG_TEXT, FG_PANEL)
    M5.Lcd.drawString("Back", BACK_BOX[0] + 34, BACK_BOX[1] + 7)

    # M5.Mic's persistent task accepts arbitrary buffer lengths. A 150 ms
    # stereo frame gives responsive independent meters for the two real mics.
    meter_frames = int(SAMPLE_RATE * 0.15)
    frame = bytearray(meter_frames * 2 * 2)
    dirty = False
    repeat_at = 0

    while True:
        M5.update()
        pos = tap()
        if pos is None:
            held = holding()
            now = time.ticks_ms()
            if held and time.ticks_diff(now, repeat_at) > 0:
                pos = held
                repeat_at = time.ticks_add(now, 150)
        else:
            repeat_at = time.ticks_add(time.ticks_ms(), 400)

        if pos:
            x, y = pos
            if hit(x, y, BACK_BOX):
                break
            for ry, row in zip(ROW_Y, ROWS):
                label, key, unit, lo, hi = row
                down = (214, ry, 44, 30)
                up = (266, ry, 44, 30)
                if not (hit(x, y, up) or hit(x, y, down)):
                    continue
                v = CFG[key] + (1 if hit(x, y, up) else -1)
                if lo <= v <= hi:
                    CFG[key] = v
                    dirty = True
                    draw_stepper(ry, label, v, unit)
                    if key == "gate_dbfs":
                        f = level_fraction(v)
                        left.set_mark(f)
                        right.set_mark(f)
                break

        try:
            if not M5.Mic.record(frame, SAMPLE_RATE, True):
                raise RuntimeError("mic queue failed")
            while M5.Mic.isRecording():
                time.sleep_ms(10)
            d1 = channel_dbfs(frame, 0, 2)
            d2 = channel_dbfs(frame, 1, 2)
        except Exception:
            d1 = d2 = -99.0
        last_level = d1
        gate = CFG["gate_dbfs"]
        left.set(level_fraction(d1), FG_TRANS if d1 >= gate else FG_STATUS)
        right.set(level_fraction(d2), FG_TRANS if d2 >= gate else FG_STATUS)

    if dirty:
        save_config()
    if font_ja is not None:
        M5.Lcd.setFont(font_ja)
    draw_frame()
    update_display()


# ------------------------------------------------------------------ audio


def wav_header(n):
    channels = 1
    byte_rate = SAMPLE_RATE * 2
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + n,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        SAMPLE_RATE,
        byte_rate,
        2 * channels,
        16,
        b"data",
        n,
    )


@micropython.viper
def _gain_inplace(buf: ptr16, n: int, g: int):  # noqa: F821
    """Saturating integer gain, in place, no allocation.

    Viper ptr16 loads are unsigned so each sample is sign extended by hand,
    and the result is clamped rather than allowed to wrap. A wrap would turn a
    loud peak into a full scale spike, which is far worse for the model than
    clipping.
    """
    i = 0
    while i < n:
        v = int(buf[i])
        if v > 32767:
            v -= 65536
        v = v * g
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        buf[i] = v
        i += 1


def apply_gain(buf):
    g = int(CFG["mic_gain"])
    if g <= 1:
        return
    t0 = time.ticks_ms()
    try:
        _gain_inplace(buf, len(buf) // 2, g)
    except Exception as e:
        log("gain: failed %r" % e)
        return
    log(
        "gain: x%d over %d samples in %d ms"
        % (g, len(buf) // 2, time.ticks_diff(time.ticks_ms(), t0))
    )


def boost_analog_gain(code):
    """Raise the ES7210 PGA above the 30 dB the firmware sets.

    M5.Mic keeps the codec active between queued buffers. Codes are 0..14 for
    0..37.5 dB; configure_mic reapplies this after every Mic.begin().
    """
    if not code:
        return
    try:
        from machine import I2C, Pin

        i2c = I2C(1, scl=Pin(11), sda=Pin(12), freq=100000)
        for reg in ES7210_MIC_GAIN_REGS:
            v = i2c.readfrom_mem(ES7210_ADDR, reg, 1)[0]
            i2c.writeto_mem(ES7210_ADDR, reg, bytes([(v & 0xF0) | (code & 0x0F)]))
        log("es7210: PGA code set to %d on 0x43/0x44" % code)
    except Exception as e:
        log("es7210: PGA poke failed %r" % e)


def configure_mic():
    """Start the persistent M5.Mic task once, pinned away from MicroPython."""
    M5.Mic.end()
    M5.Mic.config(
        sample_rate=SAMPLE_RATE,
        magnification=2,
        task_pinned_core=0,
    )
    if not M5.Mic.begin():
        raise RuntimeError("M5.Mic.begin failed")
    # M5.Mic.begin's codec callback rewrites the ES7210 registers, so an
    # optional analog PGA override must come afterwards.
    boost_analog_gain(CFG["analog_gain_code"])
    log(
        "mic: running=%s rate=%s stereo_in=%s core=%s"
        % (
            M5.Mic.isRunning(),
            M5.Mic.config("sample_rate"),
            M5.Mic.config("stereo"),
            M5.Mic.config("task_pinned_core"),
        )
    )


def queue_capture(buf):
    """Append one mono buffer to M5.Mic's two-slot FIFO."""
    t0 = time.ticks_ms()
    if not M5.Mic.record(buf, SAMPLE_RATE, False):
        raise RuntimeError("M5.Mic.record failed")
    log(
        "mic: queued %d bytes in %d ms depth=%d"
        % (len(buf), time.ticks_diff(time.ticks_ms(), t0), M5.Mic.isRecording())
    )


def drain_captures():
    """Let queued raw pointers finish before their Python buffers are freed."""
    while M5.Mic.isRecording():
        M5.update()
        time.sleep_ms(20)


# ------------------------------------------------------------------ HTTP


class ApiError(Exception):
    def __init__(self, status, body, retryable):
        self.status = status
        self.body = body
        self.retryable = retryable

    def __str__(self):
        return "HTTP %d" % self.status


def do_post(url, **kw):
    """requests2 accepts an undocumented timeout=, fall back if it ever stops."""
    try:
        return requests2.post(url, timeout=HTTP_TIMEOUT, **kw)
    except TypeError:
        return requests2.post(url, **kw)


def classify(r):
    """Turn a response into None (ok) or an ApiError with a retry verdict."""
    if r.status_code == 200:
        return None
    body = ""
    try:
        body = r.text[:200]
    except Exception:
        pass
    # 429 is either real rate limiting (retry) or exhausted quota (never retry).
    retryable = r.status_code in (500, 502, 503, 504)
    if r.status_code == 429:
        retryable = not any(
            s in body
            for s in (
                "credit_balance_exhausted",
                "spend_limit_exceeded",
                "usage_limit_exceeded",
            )
        )
    return ApiError(r.status_code, body, retryable)


def post_with_retry(label, make_post, attempts=3):
    delay = 2
    for attempt in range(attempts):
        r = make_post()
        err = classify(r)
        if err is None:
            return r
        log("%s: status=%d %s" % (label, err.status, err.body))
        if not err.retryable or attempt == attempts - 1:
            raise err
        wait = delay
        try:
            ra = r.headers.get("Retry-After") or r.headers.get("retry-after")
            if ra:
                wait = int(ra)
        except Exception:
            pass
        log("%s: retrying in %ds" % (label, wait))
        time.sleep(wait)
        delay *= 2
    raise ApiError(0, "unreachable", False)


def auth_headers(extra=None):
    h = {"Authorization": "Bearer " + CFG["openai_api_key"]}
    if extra:
        h.update(extra)
    return h


# ------------------------------------------------------------------ OpenAI


def transcribe(pcm):
    """Upload PCM as a multipart WAV. Returns (text, language_code_or_empty).

    The WAV header goes straight into the body rather than building a separate
    WAV first, which avoids a second full sized copy of the audio.
    """
    fields = [
        ("model", CFG["transcribe_model"]),
        ("response_format", "json"),
        # gpt-transcribe takes languages[] (plural) and replaces `language`.
        # Constraining it to the two languages we care about improves accuracy.
        ("languages[]", "en"),
        ("languages[]", "ja"),
    ]
    # The official gpt-transcribe `keywords` field guides recognition of
    # uncommon words and phrases. Repeated keywords[] form fields encode the
    # array in the same way as languages[].
    keywords = CFG.get("domain_keywords") or DEFAULT_DOMAIN_KEYWORDS
    if not isinstance(keywords, (list, tuple)):
        keywords = DEFAULT_DOMAIN_KEYWORDS
    for keyword in keywords:
        if keyword:
            fields.append(("keywords[]", str(keyword)))
    body = bytearray()
    for name, value in fields:
        body += (
            "--" + BOUNDARY + "\r\n"
            'Content-Disposition: form-data; name="' + name + '"\r\n\r\n' + value + "\r\n"
        ).encode()
    body += (
        "--" + BOUNDARY + "\r\n"
        'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()
    body += wav_header(len(pcm))
    body += pcm
    body += ("\r\n--" + BOUNDARY + "--\r\n").encode()

    log("stt: POST %d bytes to %s" % (len(body), CFG["transcribe_model"]))
    r = post_with_retry(
        "stt",
        lambda: do_post(
            CFG["transcribe_url"],
            data=body,
            headers=auth_headers({"Content-Type": "multipart/form-data; boundary=" + BOUNDARY}),
        ),
    )
    data = r.json()
    langs = data.get("languages") or []
    code = langs[0].get("code", "") if langs else ""
    return data.get("text", ""), code


def detect_source(text):
    """Fallback when the API returns no language, e.g. an empty languages[]."""
    kana = kanji = latin = 0
    for ch in text:
        o = ord(ch)
        if 0x3041 <= o <= 0x30FF:
            kana += 1
        elif 0x4E00 <= o <= 0x9FFF:
            kanji += 1
        elif 0x41 <= o <= 0x5A or 0x61 <= o <= 0x7A:
            latin += 1
    return "ja" if (kana or (kanji and kanji > latin)) else "en"


def ascii_lower(text):
    """Lowercase ASCII without MicroPython's Unicode case-mapping table.

    `str.lower()` raised UnicodeError on a real Japanese transcript containing
    an uncommon code point. The hallucination blocklist only needs English
    A-Z folding, so leave every other character unchanged.
    """
    chars = []
    for ch in text:
        code = ord(ch)
        chars.append(chr(code + 32) if 0x41 <= code <= 0x5A else ch)
    return "".join(chars)


def looks_hallucinated(text):
    t = ascii_lower(text.strip())
    while t and t[-1] in ".!?。！？":
        t = t[:-1]
    if not t:
        return True
    return any(ascii_lower(h) in t for h in HALLUCINATIONS)


def translate(text, src):
    src_name, tgt_name = ("Japanese", "English") if src == "ja" else ("English", "Japanese")
    keywords = CFG.get("domain_keywords") or DEFAULT_DOMAIN_KEYWORDS
    if not isinstance(keywords, (list, tuple)):
        keywords = DEFAULT_DOMAIN_KEYWORDS
    glossary = ", ".join(str(term) for term in keywords)
    payload = {
        "model": CFG["chat_model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "Live EN-JA interpreter for %s. Translate %s to %s. Use "
                    "canonical spellings: %s. Keep project names, standards, "
                    "formats, APIs, and acronyms in Latin script. In Japanese "
                    "STAC context use アセット/カタログ/コレクション/アイテム "
                    "for asset/catalog/collection/item. Resolve explicit "
                    "self-corrections (no, wait, actually, scratch that, "
                    "いや, じゃなくて, やっぱり, 訂正) by keeping the corrected "
                    "intent and omitting only superseded words; preserve "
                    "independent clauses. Output only the natural translation."
                    % (CFG["domain_context"], src_name, tgt_name, glossary)
                ),
            },
            {"role": "user", "content": text},
        ],
        "max_completion_tokens": 200,
    }
    if CFG["chat_model"].startswith("gpt-5"):
        # These are reasoning models and default to medium effort, which wastes
        # tokens and latency on a one-line translation.
        payload["reasoning_effort"] = "none"

    # requests2 serializes json= to a Unicode str and uses its character count
    # as Content-Length even though the socket writes UTF-8 bytes. An em dash
    # or Japanese text therefore truncates the declared request body. Encode
    # explicitly so Content-Length is the actual byte count.
    body = json.dumps(payload).encode()
    r = post_with_retry(
        "tt",
        lambda: do_post(
            CFG["chat_url"],
            data=body,
            headers=auth_headers({"Content-Type": "application/json"}),
        ),
    )
    return r.json()["choices"][0]["message"]["content"]


# ------------------------------------------------------------------ pipeline


def wait_for_front_buffer(estimated_done):
    """Wait until the oldest of two queued M5.Mic buffers is complete."""
    global running, settings_requested
    shown = -1
    while True:
        M5.update()
        if BtnPWR.wasClicked():
            running = False
        pos = tap()
        if pos:
            if hit(pos[0], pos[1], GEAR_BOX):
                settings_requested = True
                running = False
                set_status("Opening settings after capture...", FG_DIM)
            else:
                running = False
                set_status("Stopping after queued audio...", FG_DIM)
        if M5.Mic.isRecording() < 2 or not running:
            return
        remaining_ms = time.ticks_diff(estimated_done, time.ticks_ms())
        remaining = max(0, (remaining_ms + 999) // 1000)
        if remaining != shown:
            shown = remaining
            set_status("Listening  ~%d s" % remaining)
        time.sleep_ms(20)


def prepare_chunk(pcm):
    """Gate, gain, and freeze a completed buffer so it can be requeued."""
    global last_level

    level = channel_dbfs(pcm)
    peak = channel_peak_dbfs(pcm)
    last_level = level
    secs = len(pcm) / (SAMPLE_RATE * 2.0)
    log("rec: %d bytes %.1fs rms=%.1f peak=%.1f dBFS" % (len(pcm), secs, level, peak))

    # Gate on RMS or on a clear transient, so a single short word still passes.
    if level < CFG["gate_dbfs"] and peak < CFG["gate_dbfs"] + 18:
        set_status("Too quiet, speak closer", FG_DIM)
        log("rec: below gate %d dBFS, not uploading" % CFG["gate_dbfs"])
        return None

    apply_gain(pcm)
    # M5.Mic retains only raw pointers and will overwrite this buffer as soon
    # as it is requeued. Upload a stable copy while capture continues into the
    # original. At 15 s this is 480 KB, well inside the measured 8 MB free.
    return pcm[:]


def recognize(pcm):
    """Transcribe a stable PCM copy, returning (text, source) or None."""
    global last_orig, last_src

    text, lang = transcribe(pcm)
    gc.collect()

    text = (text or "").strip()
    if looks_hallucinated(text):
        set_status("No speech detected", FG_DIM)
        log("stt: discarded %r (lang=%r)" % (text, lang))
        return None

    # Character inspection is decisive for this two-language app. In a live
    # Japanese test gpt-transcribe repeatedly returned lang="en" for kana and
    # kanji, which selected the wrong translation direction and LCD font.
    src = detect_source(text)
    log("stt: [%s, api=%r] %s" % (src, lang, text))
    last_src = src
    last_orig = text
    # Keep the previous large translation readable until its replacement is
    # complete; only the compact HEARD preview changes during the next POST.
    update_display()
    set_status("JA to EN" if src == "ja" else "EN to JA")
    return text, src


def finish_translation(recognized):
    """Translate recognized text while both microphone queue slots run."""
    global last_trans, last_trans_lang

    text, src = recognized
    last_trans = translate(text, src)
    last_trans_lang = "en" if src == "ja" else "ja"
    log("tt: %s" % last_trans)
    update_display()
    gc.collect()


def run_session():
    """Continuously rotate M5.Mic's two FIFO buffers around the API calls."""
    global capture_buffers

    if M5.Mic.isRecording():
        drain_captures()
    size = SAMPLE_RATE * int(CFG["chunk_seconds"]) * 2
    capture_buffers = [bytearray(size), bytearray(size)]
    cur, nxt = capture_buffers
    log("pipeline: M5.Mic FIFO 2 x %d bytes, %d s per chunk" % (size, CFG["chunk_seconds"]))

    set_status("Listening  ~%d s" % CFG["chunk_seconds"])
    queue_capture(cur)
    queue_capture(nxt)
    estimated_done = time.ticks_add(time.ticks_ms(), int(CFG["chunk_seconds"]) * 1000)

    try:
        while running:
            wait_for_front_buffer(estimated_done)
            if not running:
                break

            upload_pcm = prepare_chunk(cur)
            # The upload owns a stable copy now. Requeue the original before
            # either API call so both FIFO slots cover the network round trip.
            queue_capture(cur)
            cur, nxt = nxt, cur
            estimated_done = time.ticks_add(estimated_done, int(CFG["chunk_seconds"]) * 1000)
            if upload_pcm is not None:
                set_status("Listening and transcribing...")
                recognized = recognize(upload_pcm)
            else:
                recognized = None
            if recognized is not None:
                set_status("Listening and translating...")
                finish_translation(recognized)
            gc.collect()
    finally:
        if M5.Mic.isRecording():
            set_status("Finishing queued audio...", FG_DIM)
            drain_captures()
        capture_buffers = None


# ------------------------------------------------------------------ lifecycle


def probe_channels():
    """Start the persistent microphone task and confirm both physical mics."""
    configure_mic()
    probe = bytearray(SAMPLE_RATE * 2 * 2)
    try:
        if not M5.Mic.record(probe, SAMPLE_RATE, True):
            raise RuntimeError("stereo queue failed")
        while M5.Mic.isRecording():
            time.sleep_ms(20)
        d1 = channel_dbfs(probe, 0, 2)
        d2 = channel_dbfs(probe, 1, 2)
        same = 0
        checked = 0
        for i in range(0, len(probe) - 3, 400):
            checked += 1
            if probe[i] == probe[i + 2] and probe[i + 1] == probe[i + 3]:
                same += 1
        log("mic: mic1=%.1f mic2=%.1f dBFS identical=%d/%d" % (d1, d2, same, checked))
    except Exception as e:
        log("mic: stereo probe failed %r" % e)


def setup():
    global font_ja, font_label, font_source_en, font_trans_en, font_ui
    log("boot: translator starting")
    M5.begin()
    try:
        font_ja = M5.Lcd.FONTS.AlibabaSansJA24
        font_ui = M5.Lcd.FONTS.Montserrat16
        font_label = M5.Lcd.FONTS.Montserrat12
        font_source_en = M5.Lcd.FONTS.Montserrat18
        font_trans_en = M5.Lcd.FONTS.Montserrat24
    except Exception as e:
        log("fonts: %r" % e)
    if font_ja is not None:
        M5.Lcd.setFont(font_ja)
    draw_frame()
    set_status("Booting...")

    try:
        import esp32

        log("fw: %s" % esp32.firmware_info()[3])
    except Exception:
        pass

    load_config()
    probe_channels()

    update_display()
    set_status("Tap screen to start")


def loop():
    global running, fatal, settings_requested
    M5.update()

    pos = tap()
    if pos and hit(pos[0], pos[1], GEAR_BOX):
        settings_page()
        set_status("Tap screen to start" if not running else "Listening...")
        return

    if BtnPWR.wasClicked() or pos:
        running = not running
        fatal = ""
        log("btn: running=%s" % running)
        set_status("Starting..." if running else "Paused")

    if not running:
        return

    try:
        run_session()
    except ApiError as e:
        log_exc(e)
        running = False
        if e.status == 401:
            fatal = "Bad API key (401)"
        elif e.status == 429:
            fatal = "Quota or rate limit (429)"
        elif e.status == 403:
            fatal = "Region not supported (403)"
        else:
            fatal = "API error %d" % e.status
        set_status(fatal + ", tap to retry", FG_ALERT)
    except Exception as e:
        log_exc(e)
        running = False
        set_status("ERR %s" % e, FG_ALERT)

    if settings_requested:
        settings_requested = False
        settings_page()
        set_status("Tap screen to start")


def run():
    try:
        setup()
    except Exception as e:
        log_exc(e)
        set_status("SETUP FAILED: %s" % e, FG_ALERT)
        return
    while True:
        loop()
        time.sleep_ms(20)


run()
