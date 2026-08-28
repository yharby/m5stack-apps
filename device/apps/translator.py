"""Realtime EN <-> JA speech translator for M5Stack CoreS3 (UIFlow2 MicroPython).

Cycle: record 5 s from the built-in mic -> OpenAI Whisper transcription ->
detect language -> translate to the other language -> show both on screen.

Press the power button (BtnPWR) to start/stop. Everything is logged to
/flash/translator.log and to the serial console.

Device API notes (verified on UIFlow2 V2.5.1, MicroPython v1.27.0):
  * Recorder.record_into(buf, sync) takes NO sample/bits/stereo kwargs -
    the format comes from the Recorder(sample, bits, stereo) constructor.
  * socket.setdefaulttimeout does not exist on this firmware.
  * Japanese glyphs need M5.Lcd.FONTS.AlibabaSansJA24; M5.Lcd.textWidth()
    is available for real text measurement.
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
    "whisper_model": "whisper-1",
    "chat_model": "gpt-4o-mini",
}

SAMPLE_RATE = 16000
RECORD_SECONDS = 5
LOG_PATH = "/flash/translator.log"

W, H = 320, 240
BG = 0x000000
FG_STATUS = 0x00CFFF
FG_ORIG = 0xE0E0E0
FG_TRANS = 0x40FF70
FG_DIM = 0x707070
FG_ALERT = 0xFF5050

recorder = None
running = False
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
        "config: api_key=%s wifi_ssid=%s"
        % (
            "yes" if CFG["openai_api_key"] else "NO",
            CFG["wifi_ssid"] or "(use existing connection)",
        )
    )


def ensure_wifi():
    w = network.WLAN(network.STA_IF)
    w.active(True)
    if w.isconnected():
        return True
    if not CFG["wifi_ssid"]:
        log("wifi: not connected and no ssid configured")
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
    """Wrap using real glyph metrics from the active font."""
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
    max_lines = height // 26
    lines = wrap_text(text)[:max_lines]
    M5.Lcd.setTextColor(color, BG)
    y = y_top + 4
    for ln in lines:
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
    # NOTE: record_into accepts only (buf, sync). Format comes from the
    # Recorder constructor - passing sample/bits/stereo raises TypeError.
    recorder.record_into(buf, sync=True)
    return buf


def pcm_to_wav(pcm):
    data_len = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_len,
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
        data_len,
    )
    wav = bytearray(header)
    wav += pcm
    return wav


def pcm_rms(pcm):
    n = len(pcm) // 2
    if n == 0:
        return 0
    acc = 0
    count = 0
    for i in range(0, n, 11):
        v = struct.unpack_from("<h", pcm, i * 2)[0]
        acc += v * v
        count += 1
    return int((acc / count) ** 0.5)


# ------------------------------------------------------------------ network

BOUNDARY = "----M5CoreS3TranslatorBoundary"


def transcribe(wav):
    pre = (
        "--" + BOUNDARY + "\r\n"
        'Content-Disposition: form-data; name="model"\r\n\r\n' + CFG["whisper_model"] + "\r\n"
        "--" + BOUNDARY + "\r\n"
        'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()
    body = bytearray(pre)
    body += wav
    body += ("\r\n--" + BOUNDARY + "--\r\n").encode()

    log("stt: POST %d bytes" % len(body))
    r = requests2.post(
        CFG["transcribe_url"],
        data=body,
        headers={
            "Authorization": "Bearer " + CFG["openai_api_key"],
            "Content-Type": "multipart/form-data; boundary=" + BOUNDARY,
        },
    )
    log("stt: status=%d" % r.status_code)
    if r.status_code != 200:
        log("stt: %s" % r.text[:200])
        raise RuntimeError("STT %d" % r.status_code)
    return r.json().get("text", "")


def detect_source(text):
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


def translate(text, src):
    src_name, tgt_name = ("Japanese", "English") if src == "ja" else ("English", "Japanese")
    r = requests2.post(
        CFG["chat_url"],
        json={
            "model": CFG["chat_model"],
            "temperature": 0,
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
        },
        headers={"Authorization": "Bearer " + CFG["openai_api_key"]},
    )
    log("tt: status=%d" % r.status_code)
    if r.status_code != 200:
        log("tt: %s" % r.text[:200])
        raise RuntimeError("TT %d" % r.status_code)
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
    level = pcm_rms(pcm)
    log("rec: %d bytes rms=%d" % (len(pcm), level))
    if level < 15:
        set_status("Too quiet - speak up", FG_DIM)
        log("rec: below noise floor, skipping upload")
        return

    set_status("Transcribing...")
    wav = pcm_to_wav(pcm)
    del pcm
    gc.collect()
    text = (transcribe(wav) or "").strip()
    del wav
    gc.collect()
    if not text:
        set_status("No speech detected", FG_DIM)
        return

    src = detect_source(text)
    log("stt: [%s] %s" % (src, text))
    last_orig = text
    draw_region(36, 94, last_orig, FG_ORIG)
    set_status("JA -> EN" if src == "ja" else "EN -> JA")

    last_trans = translate(text, src)
    log("tt: %s" % last_trans)
    update_display()
    set_status("Ready - keep speaking")
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
    log("recorder: ready %dHz 16bit mono" % SAMPLE_RATE)

    update_display()
    set_status("Press power btn to start")


def loop():
    global running
    M5.update()

    if BtnPWR.wasClicked():
        running = not running
        log("btn: running=%s" % running)
        set_status("Starting..." if running else "Paused")

    if running:
        try:
            process_chunk()
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
