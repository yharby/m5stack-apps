"""Realtime EN <-> JA speech translator for M5Stack CoreS3 (UIFlow2 MicroPython).

Tap the screen or press the power button to start and stop. Tap the gear in
the top right for the settings page, which shows live mic meters and lets you
change the sensitivity gate and the chunk length. Every stage is logged to
/flash/translator.log and to the serial console.

Device API facts, all verified by probing this board or by reading the
uiflow-micropython 2.5.1 source. Do not "fix" these back to what the docs
suggest, see the FACTS section of CLAUDE.md for the full list.

  * Recorder.record_into(buf, sync) takes ONLY those two arguments. The audio
    format comes from the Recorder(sample, bits, stereo) constructor.
  * record_into(buf, sync=False) returns in about 60 ms and the ADF task
    fills the buffer in the background, so capture overlaps Python work.
    That is what the pipeline below exploits.
  * Recorder.rms() and volume() are DESTRUCTIVE. They tear down the capture
    pipeline and rebuild it at 8000/16/stereo to read 1024 fresh bytes, so
    they measure the room now, not the clip you just recorded. Measured 13 dB
    off on this unit. Levels here are computed from the PCM buffer instead.
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

CFG = {
    "wifi_ssid": "",
    "wifi_pass": "",
    "openai_api_key": "",
    "transcribe_url": "https://api.openai.com/v1/audio/transcriptions",
    "chat_url": "https://api.openai.com/v1/chat/completions",
    "transcribe_model": "gpt-transcribe",
    "chat_model": "gpt-5.6-luna",
    # Sensitivity. Measured from the PCM buffer, which reads about 13 dB
    # higher than the broken recorder.rms(). Quiet room is about -55 dBFS on
    # this unit and speech peaks about -32 dBFS, so -52 is deliberately hot.
    "gate_dbfs": -52,
    # Seconds of audio per API call. The mic stays open for exactly this
    # long while the previous chunk is being uploaded, so setting it near the
    # round trip time (about 15 s) means almost no dead air.
    "chunk_seconds": 10,
    # Capture two ES7210 channels so the settings page can meter both.
    "stereo": True,
    # Digital gain applied to the PCM before upload, 1 to 16. Speech peaks at
    # about -32 dBFS on this unit so there is roughly 30 dB of headroom.
    "mic_gain": 4,
    # ES7210 PGA code, 10 is the 30 dB the firmware sets, 14 is the 37.5 dB
    # maximum. Off by default until the I2C poke is verified on this unit.
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

recorder = None
channels = 1
running = False
fatal = ""
last_orig = ""
last_trans = ""
last_level = -99.0
capture_started = 0
font_ja = None
font_ui = None


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
        "config: api_key=%s stt=%s chat=%s gate=%s chunk=%ss stereo=%s"
        % (
            "yes" if CFG["openai_api_key"] else "NO",
            CFG["transcribe_model"],
            CFG["chat_model"],
            CFG["gate_dbfs"],
            CFG["chunk_seconds"],
            CFG["stereo"],
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
        M5.Lcd.setTextColor(color, BG)
        M5.Lcd.drawString(text, 6, 3)
    except Exception:
        pass


def draw_frame():
    M5.Lcd.fillScreen(BG)
    M5.Lcd.drawLine(0, 31, W - 1, 31, FG_LINE)
    M5.Lcd.drawLine(0, 132, W - 1, 132, FG_LINE)
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


def draw_region(y_top, height, text, color):
    M5.Lcd.fillRect(0, y_top, W, height, BG)
    if not text:
        M5.Lcd.setTextColor(FG_DIM, BG)
        M5.Lcd.drawString("...", 6, y_top + 4)
        return
    M5.Lcd.setTextColor(color, BG)
    y = y_top + 4
    for ln in wrap_text(text)[: height // 26]:
        M5.Lcd.drawString(ln, 6, y)
        y += 26


def update_display():
    draw_region(36, 94, last_orig, FG_ORIG)
    draw_region(137, 100, last_trans, FG_TRANS)


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

    set_channels(2 if CFG["stereo"] else 1)
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

    # create_pcm_buf takes whole seconds and record_into fills the whole
    # buffer, so the meters refresh about once a second. Each call also builds
    # and tears down a pipeline, which costs another 100 ms or so.
    frame = recorder.create_pcm_buf(1)
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
            recorder.record_into(frame, sync=True)
            d1 = channel_dbfs(frame, 0, channels)
            d2 = channel_dbfs(frame, channels - 1, channels)
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
    byte_rate = SAMPLE_RATE * 2 * channels
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

    board_codec_init runs once per boot and is guarded, so this write survives
    later record_into calls. Codes are 0..14 for 0..37.5 dB.
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


def set_channels(n):
    """Switch mono/stereo at runtime.

    config() only writes struct fields, they take effect on the next pipeline
    build. It also fills defaults for anything omitted, so all three arguments
    are always passed. Uploads run mono because that halves the bytes on the
    wire and the firmware mixes both mics into it. Stereo is only used to meter
    the two mics separately on the settings page.
    """
    global channels
    if channels == n:
        return
    try:
        recorder.config(sample=SAMPLE_RATE, bits=16, stereo=(n == 2))
        channels = n
        log("recorder: switched to %d ch" % n)
    except Exception as e:
        log("recorder: config(%d ch) failed %r" % (n, e))


def start_capture(buf):
    """Kick off a background capture. Returns in about 60 ms.

    The ADF consumer task runs on core 0 and the I2S and resample elements on
    core 1, so the board keeps listening while the interpreter is blocked in a
    TLS upload. That is the whole point of the pipeline below.
    """
    global capture_started
    recorder.record_into(buf, sync=False)
    capture_started = time.ticks_ms()


def capture_done():
    """True once the capture window has elapsed.

    This is deliberately time based. is_recording() is only pipeline != NULL
    and was observed still True 23 s after a 10 s buffer had filled, so it
    cannot be used as a completion signal. The ADF task captures at exactly the
    sample rate, so elapsed time is the reliable one. Never call stop(): its
    spin wait has no timeout and hangs the board for good.
    """
    window = CFG["chunk_seconds"] * 1000 + 400
    return time.ticks_diff(time.ticks_ms(), capture_started) >= window


# ------------------------------------------------------------------ HTTP


class ApiError(Exception):
    def __init__(self, status, body, retryable):
        Exception.__init__(self, "HTTP %d" % status)
        self.status = status
        self.body = body
        self.retryable = retryable


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
    fields = (
        ("model", CFG["transcribe_model"]),
        ("response_format", "json"),
        # gpt-transcribe takes languages[] (plural) and replaces `language`.
        # Constraining it to the two languages we care about improves accuracy.
        ("languages[]", "en"),
        ("languages[]", "ja"),
    )
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


def looks_hallucinated(text):
    t = text.strip().lower().rstrip(".!?。！？")
    if not t:
        return True
    return any(h.lower() in t for h in HALLUCINATIONS)


def translate(text, src):
    src_name, tgt_name = ("Japanese", "English") if src == "ja" else ("English", "Japanese")
    payload = {
        "model": CFG["chat_model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise translator. Translate the user's %s "
                    "text into %s. Reply with only the translation, no "
                    "explanation and no quotes." % (src_name, tgt_name)
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

    r = post_with_retry(
        "tt",
        lambda: do_post(CFG["chat_url"], json=payload, headers=auth_headers()),
    )
    return r.json()["choices"][0]["message"]["content"]


# ------------------------------------------------------------------ pipeline


def wait_for_capture():
    """Hold until the background capture has filled its buffer.

    This is the only window where a tap can land while the app is running, so
    the UI is serviced here.
    """
    global running
    while True:
        M5.update()
        if BtnPWR.wasClicked():
            running = False
        pos = tap()
        if pos:
            if hit(pos[0], pos[1], GEAR_BOX):
                settings_page()
                set_status("Listening and sending...")
            else:
                running = False
                set_status("Stopping after this chunk...", FG_DIM)
        if capture_done() or not running:
            return
        time.sleep_ms(20)


def handle(pcm):
    """Transcribe and translate one captured chunk. The mic is already open."""
    global last_orig, last_trans, last_level

    level = channel_dbfs(pcm, 0, channels)
    peak = channel_peak_dbfs(pcm, 0, channels)
    last_level = level
    secs = len(pcm) / (SAMPLE_RATE * 2.0 * channels)
    log("rec: %d bytes %.1fs rms=%.1f peak=%.1f dBFS" % (len(pcm), secs, level, peak))

    # Gate on RMS or on a clear transient, so a single short word still passes.
    if level < CFG["gate_dbfs"] and peak < CFG["gate_dbfs"] + 18:
        set_status("Too quiet, speak closer", FG_DIM)
        log("rec: below gate %d dBFS, not uploading" % CFG["gate_dbfs"])
        return

    set_status("Transcribing...")
    apply_gain(pcm)
    text, lang = transcribe(pcm)
    gc.collect()

    text = (text or "").strip()
    if looks_hallucinated(text):
        set_status("No speech detected", FG_DIM)
        log("stt: discarded %r (lang=%r)" % (text, lang))
        return

    src = lang if lang in ("en", "ja") else detect_source(text)
    log("stt: [%s] %s" % (src, text))
    last_orig = text
    draw_region(36, 94, last_orig, FG_ORIG)
    set_status("JA to EN" if src == "ja" else "EN to JA")

    last_trans = translate(text, src)
    log("tt: %s" % last_trans)
    draw_region(137, 100, last_trans, FG_TRANS)
    gc.collect()


def run_session():
    """Capture and upload in a loop, with the mic open during every network call.

    The ADF capture task is independent of the interpreter, so reopening the
    mic into the other buffer BEFORE the upload means the board listens for the
    whole time it is talking to OpenAI. No _thread is needed, the second core
    is already doing the work.
    """
    set_channels(1)
    # Always create_pcm_buf. record_into on a plain bytearray crashes this
    # firmware, verified twice, see the FACTS section of CLAUDE.md.
    cur = recorder.create_pcm_buf(CFG["chunk_seconds"])
    nxt = recorder.create_pcm_buf(CFG["chunk_seconds"])
    log("pipeline: 2 x %d byte buffers, %d s per chunk" % (len(cur), CFG["chunk_seconds"]))

    set_status("Listening...")
    start_capture(cur)

    while running:
        wait_for_capture()
        if not running:
            break
        # Reopen the mic into the other buffer BEFORE the slow network work.
        start_capture(nxt)
        set_status("Listening and sending...")
        try:
            handle(cur)
        finally:
            gc.collect()
        cur, nxt = nxt, cur

    # Let the in flight capture run itself out rather than calling stop().
    while not capture_done():
        M5.update()
        time.sleep_ms(50)


# ------------------------------------------------------------------ lifecycle


def probe_channels():
    """Build the one and only Recorder, and confirm both mics are distinct.

    Every audio.Recorder(...) leaks a 4 KB FreeRTOS task that is never freed,
    so this must run exactly once per boot. The board has two real MEMS mics,
    U12 on ES7210 channel 1 and U13 on channel 2. stereo=False mixes them
    rather than picking one, so stereo is used and the app reads channel 0.
    """
    global recorder, channels
    import audio

    want_stereo = bool(CFG["stereo"])
    recorder = audio.Recorder(SAMPLE_RATE, 16, want_stereo)
    channels = 2 if want_stereo else 1
    log("recorder: created %d Hz 16 bit %d ch" % (SAMPLE_RATE, channels))

    boost_analog_gain(CFG["analog_gain_code"])

    if channels == 2:
        probe = recorder.create_pcm_buf(1)
        try:
            recorder.record_into(probe, sync=True)
            d1 = channel_dbfs(probe, 0, 2)
            d2 = channel_dbfs(probe, 1, 2)
            same = 0
            checked = 0
            for i in range(0, len(probe) - 3, 400):
                checked += 1
                if probe[i] == probe[i + 2] and probe[i + 1] == probe[i + 3]:
                    same += 1
            log("recorder: mic1=%.1f mic2=%.1f dBFS identical=%d/%d" % (d1, d2, same, checked))
        except Exception as e:
            log("recorder: stereo probe failed %r" % e)


def setup():
    global font_ja, font_ui
    log("boot: translator starting")
    M5.begin()
    try:
        font_ja = M5.Lcd.FONTS.AlibabaSansJA24
        font_ui = M5.Lcd.FONTS.Montserrat16
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
    global running, fatal
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
