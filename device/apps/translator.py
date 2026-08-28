"""Realtime EN <-> JA speech translator for M5Stack CoreS3 (UIFlow2 MicroPython).

Press the power button to start or stop. Each cycle records 5 s from the
built-in mic, transcribes it with OpenAI, translates it into the other
language, and shows both on screen. Every stage is logged to
/flash/translator.log and to the serial console.

Device API facts, all verified by probing this board (UIFlow2 V2.5.1,
MicroPython v1.27.0). Do not "fix" these back to what the docs suggest:

  * Recorder.record_into(buf, sync) takes ONLY those two arguments. Passing
    sample/bits/stereo raises "TypeError: extra keyword arguments given".
    The audio format comes from the Recorder(sample, bits, stereo) constructor.
  * Recorder.rms() returns the last capture's level in dBFS. volume() is a
    read-only meter. There is no input gain control.
  * socket.setdefaulttimeout does not exist on this firmware.
  * Japanese needs M5.Lcd.FONTS.AlibabaSansJA24 or glyphs do not render.
  * CoreS3 has no BtnA/B/C, only BtnPWR.
  * requests2 has no files= parameter, so the multipart body is hand-built
    and passed as data=<bytearray>.

API choices, verified against the live API from this device:

  * gpt-transcribe returns empty text on silence. whisper-1 hallucinates
    ("Thank you for watching!"), which is why it is not used here.
  * gpt-transcribe returns the detected language in languages[0].code, so
    language detection is server side, with a Unicode fallback.
  * gpt-5.6-luna needs reasoning_effort "none", otherwise it burns hidden
    reasoning tokens on a trivial translation. gpt-5-nano rejects "none".
  * max_completion_tokens, not the deprecated max_tokens.
"""

import gc
import io
import json
import struct
import sys
import time

import M5
import network
import requests2
from M5 import BtnPWR

CONFIG_PATHS = (
    "/flash/config.json",
    "/flash/res/config.json",
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
}

SAMPLE_RATE = 16000
RECORD_SECONDS = 5
LOG_PATH = "/flash/translator.log"

# Measured on this unit: quiet room about -55 dBFS, speech peaks about -37 dBFS.
SILENCE_GATE_DBFS = -45.0

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
FG_ALERT = 0xFF5050

BOUNDARY = "----M5CoreS3TranslatorBoundary"

recorder = None
running = False
fatal = ""  # set on an unrecoverable API error, stops the loop
last_orig = ""
last_trans = ""


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
        "config: api_key=%s stt=%s chat=%s"
        % (
            "yes" if CFG["openai_api_key"] else "NO",
            CFG["transcribe_model"],
            CFG["chat_model"],
        )
    )


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


# ------------------------------------------------------------------ display


def set_status(text, color=FG_STATUS):
    try:
        M5.Lcd.fillRect(0, 0, W, 30, BG)
        M5.Lcd.setTextColor(color, BG)
        M5.Lcd.drawString(text, 6, 3)
    except Exception:
        pass


def draw_frame():
    M5.Lcd.fillScreen(BG)
    M5.Lcd.drawLine(0, 31, W - 1, 31, 0x404040)
    M5.Lcd.drawLine(0, 132, W - 1, 132, 0x404040)


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


# ------------------------------------------------------------------ audio


def record_pcm(seconds):
    if recorder is None:
        raise RuntimeError("recorder not initialised")
    buf = recorder.create_pcm_buf(seconds)
    recorder.record_into(buf, sync=True)
    return buf


def pcm_to_wav(pcm):
    n = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + n,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        SAMPLE_RATE,
        SAMPLE_RATE * 2,
        2,
        16,
        b"data",
        n,
    )
    wav = bytearray(header)
    wav += pcm
    return wav


# ------------------------------------------------------------------ HTTP


class ApiError(Exception):
    def __init__(self, status, body, retryable):
        Exception.__init__(self, "HTTP %d" % status)
        self.status = status
        self.body = body
        self.retryable = retryable


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


def post_with_retry(label, do_post, attempts=3):
    delay = 2
    for attempt in range(attempts):
        r = do_post()
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


def transcribe(wav):
    """Upload WAV as multipart. Returns (text, language_code_or_empty)."""
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
    body += wav
    body += ("\r\n--" + BOUNDARY + "--\r\n").encode()

    log("stt: POST %d bytes to %s" % (len(body), CFG["transcribe_model"]))
    r = post_with_retry(
        "stt",
        lambda: requests2.post(
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
        lambda: requests2.post(CFG["chat_url"], json=payload, headers=auth_headers()),
    )
    return r.json()["choices"][0]["message"]["content"]


# ------------------------------------------------------------------ pipeline


def process_chunk():
    global last_orig, last_trans

    if not CFG["openai_api_key"]:
        set_status("No API key in config.json", FG_ALERT)
        time.sleep_ms(1500)
        return
    if not ensure_wifi():
        set_status("No Wi-Fi", FG_ALERT)
        time.sleep_ms(1500)
        return

    set_status("Listening %ds..." % RECORD_SECONDS)
    pcm = record_pcm(RECORD_SECONDS)
    try:
        level = recorder.rms()
    except Exception:
        level = 0.0
    log("rec: %d bytes level=%.1f dBFS" % (len(pcm), level))
    if level < SILENCE_GATE_DBFS:
        set_status("Too quiet, speak closer", FG_DIM)
        log("rec: below gate %.1f dBFS, not uploading" % SILENCE_GATE_DBFS)
        return

    set_status("Transcribing...")
    wav = pcm_to_wav(pcm)
    del pcm
    gc.collect()
    text, lang = transcribe(wav)
    del wav
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
    update_display()
    set_status("Ready, keep speaking")
    gc.collect()


# ------------------------------------------------------------------ lifecycle


def setup():
    global recorder
    log("boot: translator starting")
    M5.begin()
    M5.Lcd.setFont(M5.Lcd.FONTS.AlibabaSansJA24)
    draw_frame()
    set_status("Booting...")

    try:
        import esp32

        log("fw: %s" % esp32.firmware_info()[3])
    except Exception:
        pass

    load_config()

    from audio import Recorder

    recorder = Recorder(SAMPLE_RATE, 16, False)
    log("recorder: ready %d Hz 16 bit mono" % SAMPLE_RATE)

    update_display()
    set_status("Press power button to start")


def loop():
    global running, fatal
    M5.update()

    if BtnPWR.wasClicked():
        running = not running
        fatal = ""
        log("btn: running=%s" % running)
        set_status("Starting..." if running else "Paused")

    if not running:
        return

    try:
        process_chunk()
    except ApiError as e:
        log_exc(e)
        if e.status == 401:
            fatal = "Bad API key (401)"
        elif e.status == 429:
            fatal = "Quota or rate limit (429)"
        elif e.status == 403:
            fatal = "Region not supported (403)"
        if fatal:
            running = False
            set_status(fatal + " - press btn", FG_ALERT)
        else:
            set_status("API error %d" % e.status, FG_ALERT)
            time.sleep_ms(1500)
    except Exception as e:
        log_exc(e)
        set_status("ERR %s" % e, FG_ALERT)
        time.sleep_ms(1500)


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
