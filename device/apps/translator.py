"""Realtime EN <-> JA speech translator for M5Stack CoreS3 (UIFlow2 MicroPython).

Tap the screen or press the power button to start and stop. Tap the gear in
the top right for the settings page, which shows live mic meters and lets you
change the sensitivity gate and the maximum utterance length. Every stage is logged to
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

import _thread
import gc
import io
import json
import math
import os
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
    # Upper bound in seconds on one utterance. Speech is normally cut on a
    # natural pause, so this only applies when the talker never stops. It is
    # still what the settings stepper moves.
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
# /flash is small and the app appends a line per pipeline stage forever.
LOG_MAX_BYTES = 64 * 1024
LOG_ROTATE_EVERY = 200
HTTP_TIMEOUT = 45
WIFI_BOOT_SETTLE_MS = 12000
WIFI_IDLE_GRACE_MS = 1500
WIFI_ATTEMPT_TIMEOUT_MS = 15000
WIFI_CREDENTIAL_GRACE_MS = 750
WIFI_TRANSIENT_GRACE_MS = 2000
WIFI_ATTEMPTS_PER_NETWORK = 3
# Measured on this unit with tools/device_scripts/thread_probe.py: a 32 KB
# task stack CANNOT be created here and raises OSError("can't create
# thread") every time, which silently sent every POST down do_post's
# blocking fallback and disabled touch during network calls. 16 KB creates
# reliably, and tls_thread_probe.py completed a real TLS handshake and HTTP
# round trip on stacks down to 8 KB, so this leaves real margin.
HTTP_THREAD_STACK = 16384

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
# Keep the icon compact, but give a fingertip a full 44 x 44 px target.
GEAR_HIT_BOX = (276, 0, 44, 44)

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

# requests2 is synchronous. Run it on a Python worker so the main thread can
# keep M5.update() moving and latch touch presses during TLS/network waits.
# This stack size was verified with requests2 TLS on the target UIFlow2 build.
try:
    _thread.stack_size(HTTP_THREAD_STACK)
except Exception:
    pass


# ------------------------------------------------------------------ logging


_log_file = None
_log_writes = 0


def _open_log():
    """Hold the log file open across writes.

    The previous version opened and closed the file on every line. That is
    several milliseconds of flash work each time and the app logs about eight
    lines per translation turn, all of it on the UI thread.
    """
    global _log_file
    try:
        # Deliberately not a context manager. The handle is held open for the
        # life of the app and closed by _close_log/_rotate_log.
        _log_file = open(LOG_PATH, "a")  # noqa: SIM115
    except Exception:
        _log_file = None
    return _log_file


def _close_log():
    global _log_file
    try:
        if _log_file is not None:
            _log_file.close()
    except Exception:
        pass
    _log_file = None


def _rotate_log():
    """Keep one previous log so /flash cannot fill up during a long session."""
    global _log_writes

    _log_writes = 0
    try:
        size = os.stat(LOG_PATH)[6]
    except Exception:
        return
    if size < LOG_MAX_BYTES:
        return
    _close_log()
    try:
        os.remove(LOG_PATH + ".1")
    except Exception:
        pass
    try:
        os.rename(LOG_PATH, LOG_PATH + ".1")
    except Exception:
        pass
    _open_log()


def log(msg):
    global _log_writes

    line = "[t=%d] %s" % (time.ticks_ms(), msg)
    print(line)
    f = _log_file
    if f is None:
        f = _open_log()
        if f is None:
            return
    try:
        f.write(line + "\n")
        # Flush every line instead of buffering in RAM. CLAUDE.md's debugging
        # loop depends on this file surviving a board wedge, and a wedge is
        # precisely when an unflushed RAM buffer would be lost.
        f.flush()
    except Exception:
        _close_log()
        return
    _log_writes += 1
    if _log_writes >= LOG_ROTATE_EVERY:
        _rotate_log()


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


_wifi_tuned = False
_wifi_boot_checked = False


def tune_wifi(w, force=False):
    """Turn off modem power save after activation and radio resets.

    Power save parks the radio between DTIM beacons, which adds latency and
    jitter to every API round trip. This app does short interactive sessions,
    so responsiveness is worth more than the milliamps. The keyword differs
    between MicroPython ESP32 builds, so try both spellings and accept that
    neither may exist.
    """
    global _wifi_tuned

    if _wifi_tuned and not force:
        return
    _wifi_tuned = True
    for key, value in (("pm", getattr(network.WLAN, "PM_NONE", 0)), ("ps_mode", 0)):
        try:
            w.config(**{key: value})
            log("wifi: power save disabled via %s" % key)
            return
        except Exception:
            continue
    log("wifi: power save unchanged, no supported config key")


def wifi_status_name(status):
    names = (
        (getattr(network, "STAT_IDLE", 1000), "idle"),
        (getattr(network, "STAT_CONNECTING", 1001), "connecting"),
        (getattr(network, "STAT_GOT_IP", 1010), "got-ip"),
        (getattr(network, "STAT_NO_AP_FOUND", 201), "no-ap"),
        (
            getattr(network, "STAT_NO_AP_FOUND_IN_RSSI_THRESHOLD", -101),
            "no-ap-rssi",
        ),
        (
            getattr(network, "STAT_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD", -102),
            "no-ap-auth",
        ),
        (
            getattr(network, "STAT_NO_AP_FOUND_W_COMPATIBLE_SECURITY", -103),
            "no-compatible-security",
        ),
        (getattr(network, "STAT_WRONG_PASSWORD", 202), "wrong-password"),
        (getattr(network, "STAT_ASSOC_FAIL", 203), "association-failed"),
        (getattr(network, "STAT_HANDSHAKE_TIMEOUT", 204), "handshake-timeout"),
        (getattr(network, "STAT_BEACON_TIMEOUT", -104), "beacon-timeout"),
    )
    for value, name in names:
        if status == value:
            return name
    return str(status)


def wifi_attempt_terminal_statuses():
    """Statuses that require a new connect() call to make progress."""
    return (
        getattr(network, "STAT_NO_AP_FOUND", 201),
        getattr(network, "STAT_NO_AP_FOUND_IN_RSSI_THRESHOLD", -101),
        getattr(network, "STAT_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD", -102),
        getattr(network, "STAT_NO_AP_FOUND_W_COMPATIBLE_SECURITY", -103),
        getattr(network, "STAT_WRONG_PASSWORD", 202),
        getattr(network, "STAT_ASSOC_FAIL", 203),
        getattr(network, "STAT_HANDSHAKE_TIMEOUT", 204),
        getattr(network, "STAT_BEACON_TIMEOUT", -104),
    )


def wifi_nonretryable_statuses():
    """Statuses that cannot improve without changing the credentials."""
    return (
        getattr(network, "STAT_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD", -102),
        getattr(network, "STAT_NO_AP_FOUND_W_COMPATIBLE_SECURITY", -103),
        getattr(network, "STAT_WRONG_PASSWORD", 202),
    )


def wait_for_wifi(w, timeout_ms, accept_terminal=True):
    """Pump UI events until Wi-Fi connects, fails terminally, or times out."""
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    terminal_at = None
    last_status = None
    terminals = wifi_attempt_terminal_statuses()
    nonretryable = wifi_nonretryable_statuses()

    while not w.isconnected():
        M5.update()
        status = w.status()
        if status != last_status:
            log("wifi: status=%s" % wifi_status_name(status))
            last_status = status
            terminal_at = time.ticks_ms() if status in terminals else None
        elif accept_terminal and terminal_at is not None:
            grace_ms = (
                WIFI_CREDENTIAL_GRACE_MS if status in nonretryable else WIFI_TRANSIENT_GRACE_MS
            )
            if time.ticks_diff(time.ticks_ms(), terminal_at) >= grace_ms:
                return False
        if time.ticks_diff(deadline, time.ticks_ms()) < 0:
            return False
        time.sleep_ms(100)
    return True


def uiflow_wifi_credentials():
    """Read the network selected by UIFlow2 Settings or the Wi-Fi QR app."""
    try:
        import esp32

        settings = esp32.NVS("uiflow")
        try:
            net_mode = settings.get_str("net_mode")
        except OSError:
            net_mode = "WIFI"
        if net_mode and net_mode != "WIFI":
            log("wifi: UIFlow2 network mode is %r, skipping its Wi-Fi credentials" % net_mode)
            return None
        try:
            ssid = settings.get_str("ssid0")
        except OSError:
            ssid = ""
        try:
            password = settings.get_str("pswd0")
        except OSError:
            password = ""
        if ssid:
            return ssid, password
    except Exception as e:
        log("wifi: could not read UIFlow2 settings: %r" % e)
    return None


def wifi_candidates():
    """Return unique (source, ssid, password) candidates in priority order."""
    candidates = []
    uiflow = uiflow_wifi_credentials()
    if uiflow is not None:
        candidates.append(("UIFlow2", uiflow[0], uiflow[1]))

    config_ssid = CFG["wifi_ssid"]
    config_password = CFG["wifi_pass"]
    if config_ssid:
        duplicate = False
        for _source, ssid, password in candidates:
            if ssid == config_ssid and password == config_password:
                duplicate = True
                break
        if not duplicate:
            candidates.append(("config", config_ssid, config_password))
    return candidates


def reset_wifi_station(w):
    try:
        w.disconnect()
    except Exception:
        pass
    w.active(False)
    time.sleep_ms(300)
    w.active(True)
    time.sleep_ms(500)
    tune_wifi(w, force=True)


def connect_wifi_candidate(w, source, ssid, password):
    """Try one saved network with real asynchronous retries."""
    for attempt in range(1, WIFI_ATTEMPTS_PER_NETWORK + 1):
        reset_wifi_station(w)
        log(
            "wifi: connecting source=%s ssid=%r attempt=%d/%d"
            % (source, ssid, attempt, WIFI_ATTEMPTS_PER_NETWORK)
        )
        try:
            if password:
                w.connect(ssid, password)
            else:
                w.connect(ssid)
        except Exception as e:
            log("wifi: connect call failed source=%s error=%r" % (source, e))
            continue

        if wait_for_wifi(w, WIFI_ATTEMPT_TIMEOUT_MS):
            log("wifi: connected source=%s ssid=%r ip=%s" % (source, ssid, w.ifconfig()[0]))
            return True

        status = w.status()
        if status in wifi_nonretryable_statuses():
            log("wifi: not retrying source=%s status=%s" % (source, wifi_status_name(status)))
            break
        if attempt < WIFI_ATTEMPTS_PER_NETWORK:
            backoff_ms = attempt * 1000
            log("wifi: transient failure, retrying in %d ms" % backoff_ms)
            time.sleep_ms(backoff_ms)
    return False


def ensure_wifi():
    """Prefer UIFlow2's live/dynamic network, with JSON as a fallback.

    UIFlow2 starts association in boot.py and immediately launches main.py.
    The first call therefore waits for that in-flight connection instead of
    interrupting it with the translator's older JSON credentials. Later calls
    retain the cheap isconnected() fast path and re-read NVS after a QR switch.
    """
    global _wifi_boot_checked

    w = network.WLAN(network.STA_IF)
    w.active(True)
    tune_wifi(w)
    if w.isconnected():
        return True

    if not _wifi_boot_checked:
        _wifi_boot_checked = True
        status = w.status()
        connecting = getattr(network, "STAT_CONNECTING", 1001)
        idle = getattr(network, "STAT_IDLE", 1000)
        if status in (connecting, idle):
            settle_ms = WIFI_BOOT_SETTLE_MS if status == connecting else WIFI_IDLE_GRACE_MS
            log("wifi: allowing UIFlow2's boot connection to settle")
            if wait_for_wifi(w, settle_ms):
                log("wifi: UIFlow2 boot connection ready ip=%s" % w.ifconfig()[0])
                return True

    candidates = wifi_candidates()
    if not candidates:
        log("wifi: down and no UIFlow2 or config credentials available")
        return False

    for source, ssid, password in candidates:
        if connect_wifi_candidate(w, source, ssid, password):
            return True
    log("wifi: all configured networks failed")
    return False


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


def press():
    """Touch position on initial contact, without waiting for release."""
    if M5.Touch.getCount() == 0:
        return None
    d = M5.Touch.getDetail(0)
    if d[5]:  # wasPressed
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
    # The screen is blank again, so whatever the region cache believes is on
    # it is gone. Every full clear has to forget it or the next update_display
    # would skip both regions and leave them empty.
    invalidate_display()


# textWidth() is the only real metric this firmware offers and it costs a full
# glyph walk, so each glyph's advance is measured once and kept. There is no
# "which font is currently set" query on M5.Lcd, and widths differ between the
# LCD and an off screen canvas, so the caller names both in the key.
char_widths = {}
CHAR_CACHE_MAX = 1500

# Both text regions are double buffered through the vendor documented
# newCanvas/push path. The canvases are allocated once and reused: allocating
# per frame would churn the heap while the microphone FIFO is running.
CANVAS_BPP = 16
region_canvases = [None, None]
canvas_ok = True

# What was last actually painted into each region, so an unchanged region is
# left alone. recognize() calls update_display() while a translation is still
# on screen, and wiping that region to redraw identical text made it flash.
region_state = [None, None]


def invalidate_display():
    """Forget what is on screen. Call after anything clears the LCD."""
    region_state[0] = None
    region_state[1] = None


def release_canvases():
    """Give up on double buffering permanently and fall back to direct draw."""
    global canvas_ok
    canvas_ok = False
    for i in range(len(region_canvases)):
        c = region_canvases[i]
        region_canvases[i] = None
        if c is not None:
            # delete() is not documented for this binding, so dropping the
            # reference and collecting is the reliable half of the teardown.
            try:
                c.delete()
            except Exception:
                pass
    char_widths.clear()
    invalidate_display()
    gc.collect()


def region_canvas(slot, w, h):
    """Lazily allocate one reusable off screen buffer for a text region."""
    if not canvas_ok:
        return None
    c = region_canvases[slot]
    if c is not None:
        return c
    try:
        c = M5.Lcd.newCanvas(w, h, CANVAS_BPP, True)
        # Publish it before the smoke test so a half working canvas is torn
        # down by release_canvases() rather than leaking.
        region_canvases[slot] = c
        # A canvas has no .FONTS and no .COLOR of its own, so exercise exactly
        # the calls the renderer makes, with an M5.Lcd font passed in, before
        # trusting it with a frame.
        c.fillScreen(BG)
        c.setTextColor(FG_DIM, BG)
        if font_label is not None:
            c.setFont(font_label)
        c.textWidth("M")
    except Exception as e:
        log("lcd: canvas unavailable (%r), drawing direct" % e)
        release_canvases()
        return None
    return c


def char_width(surface, metrics, ch):
    """Advance of one glyph on one surface, cached. -1 means no metrics."""
    key = (metrics, ch)
    w = char_widths.get(key, -2)
    if w != -2:
        return w
    try:
        w = surface.textWidth(ch)
    except Exception:
        w = -1
    # Japanese transcripts keep introducing new glyphs, so cap the cache
    # rather than letting it grow for the life of the session.
    if len(char_widths) >= CHAR_CACHE_MAX:
        char_widths.clear()
    char_widths[key] = w
    return w


def wrap_text(text, max_px=W - 12, surface=None, metrics="lcd"):
    """Wrap with real glyph metrics, which matters for mixed Latin and CJK.

    Widths are summed per character instead of remeasuring the whole growing
    line. That measurement was quadratic and ran up to sixteen times a turn,
    stalling the UI thread for hundreds of milliseconds. These are non kerned
    bitmap faces, so the per character sum is exact.
    """
    if surface is None:
        surface = M5.Lcd
    lines = []
    line = ""
    width = 0
    for ch in text:
        if ch == "\n":
            lines.append(line)
            line = ""
            width = 0
            continue
        w = char_width(surface, metrics, ch)
        # w < 0 means textWidth raised, so keep the old character count rule.
        too_wide = len(line) + 1 > 20 if w < 0 else width + w > max_px
        if too_wide and line:
            lines.append(line)
            line = ch
            width = w if w > 0 else 0
        else:
            line += ch
            if w > 0:
                width += w
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


def fit_lines(text, candidates, max_px, available, surface=None, metrics="lcd"):
    """Choose the largest available font whose wrapped text fits.

    Each candidate carries its own metrics key because nothing can be asked
    which font is selected, so the width cache has to be told.
    """
    if surface is None:
        surface = M5.Lcd
    chosen_font, chosen_height, chosen_lines = candidates[-1][0], candidates[-1][1], []
    for font, line_height, key in candidates:
        if font is not None:
            surface.setFont(font)
        lines = wrap_text(text, max_px, surface, metrics + key)
        chosen_font, chosen_height, chosen_lines = font, line_height, lines
        if len(lines) * line_height <= available:
            break
    return chosen_font, chosen_height, chosen_lines


def render_region(canvas, label_y, text_y, bottom, label, text, color, language, primary):
    """Paint one region, into an off screen canvas when there is one.

    Canvas coordinates are region relative, so every y is shifted by oy and
    the finished buffer is pushed back at the region's real origin.
    """
    if canvas is None:
        surface = M5.Lcd
        # A canvas measures on its own glyph cache, so the LCD and each canvas
        # get their own key space in the width cache.
        metrics = "lcd"
        oy = 0
        M5.Lcd.fillRect(0, label_y, W, bottom - label_y, BG)
    else:
        surface = canvas
        metrics = "c%d" % label_y
        oy = label_y
        canvas.fillScreen(BG)

    if font_label is not None:
        surface.setFont(font_label)
    surface.setTextColor(FG_DIM, BG)
    surface.drawString(label, 7, label_y - oy)

    available = bottom - text_y
    # Select the glyph set from the text itself. The transcription API has
    # returned language="en" for clearly Japanese text on this device; using
    # that metadata for rendering produces tofu boxes.
    text_language = detect_source(text) if text else language
    shown_text = lcd_safe_text(text)
    if text_language == "ja":
        candidates = ((font_ja, 26, "ja24"),)
    elif primary:
        candidates = (
            (font_trans_en, 28, "m24"),
            (font_source_en, 22, "m18"),
            (font_ui, 19, "m16"),
            (font_label, 15, "m12"),
        )
    else:
        candidates = ((font_source_en, 22, "m18"), (font_ui, 19, "m16"), (font_label, 15, "m12"))
    font, line_height, lines = fit_lines(
        shown_text or "...", candidates, W - 14, available, surface, metrics
    )
    if font is not None:
        surface.setFont(font)
    surface.setTextColor(color if text else FG_DIM, BG)
    y = text_y - oy
    visible = available // line_height
    for line in lines[:visible]:
        surface.drawString(line, 7, y)
        y += line_height
    if canvas is not None:
        canvas.push(0, label_y)


def draw_labeled_region(slot, label_y, text_y, bottom, label, text, color, language, primary):
    state = (label, text, color, label_y, text_y, bottom)
    if region_state[slot] == state:
        return
    # Clear first: a failure part way through leaves the region dirty, and the
    # cache must not claim it is clean.
    region_state[slot] = None
    canvas = region_canvas(slot, W, bottom - label_y)
    if canvas is not None:
        try:
            render_region(canvas, label_y, text_y, bottom, label, text, color, language, primary)
            region_state[slot] = state
            return
        except Exception as e:
            # Never let the buffered path take the screen down with it.
            log("lcd: canvas draw failed %r, drawing direct" % e)
            release_canvases()
    render_region(None, label_y, text_y, bottom, label, text, color, language, primary)
    region_state[slot] = state


def update_display():
    target = last_trans_lang if last_trans else ("ja" if last_src == "en" else "en")
    draw_labeled_region(
        0,
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
        1,
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

# M5.Mic's FIFO is two slots deep and cannot be made deeper. Capture is
# therefore a stream of short frames that the pump folds into a ring, and an
# utterance ends when the talker stops rather than when a wall clock expires.
# One second per frame keeps the endpointer responsive while still leaving two
# seconds of runway in the FIFO if the UI thread stalls on a repaint.
FRAME_MS = 1000
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000

# Levels are measured once per window as each frame lands. The same numbers
# drive the endpointer, the gate and the silence trim, so the audio is walked
# exactly once per frame instead of twice per chunk.
WINDOW_MS = 100
WINDOW_BYTES = SAMPLE_RATE * 2 * WINDOW_MS // 1000
WINS_PER_FRAME = FRAME_BYTES // WINDOW_BYTES
LEVEL_STEP = 4

# A window counts as speech when its RMS clears the gate, or when a short
# transient peaks well above it. That is the same two part test the fixed
# chunk used, now applied per 100 ms so one quiet word in a long utterance is
# no longer averaged into silence.
GATE_PEAK_MARGIN = 18

# Trailing quiet that closes an utterance, and the minimum speech that makes
# one worth an API call. 600 ms is a natural sentence gap; 400 ms of speech
# rejects a cough, a chair or a door.
ENDPOINT_SILENCE_MS = 600
MIN_SPEECH_MS = 400
ENDPOINT_SILENCE_WINS = ENDPOINT_SILENCE_MS // WINDOW_MS
MIN_SPEECH_WINS = MIN_SPEECH_MS // WINDOW_MS

# Guard kept either side of the trimmed speech so a soft onset or a trailing
# consonant is never clipped by the window grid.
TRIM_GUARD_MS = 200
TRIM_GUARD_BYTES = SAMPLE_RATE * 2 * TRIM_GUARD_MS // 1000

# Upload buffers are preallocated and rotated. The old code returned pcm[:]
# every turn, a fresh 192 KB object that the multipart build then copied
# again. M5.Mic never sees a pointer to a pooled buffer.
UPLOAD_POOL = 2

# Session state for the ring and the endpointer. These live here rather than
# in the module wide globals block because they are only meaningful while a
# capture session is armed. Sequence numbers are absolute and only ever
# increase; the slot for a sequence is seq % ring_slots, which avoids every
# wraparound comparison bug.
capture_views = None
ring_rms = None
ring_peak = None
ring_slots = 0
max_frames = 0
upload_pool = None
upload_slot = 0
head_seq = 0
tail_seq = 0
open_seq = 0
pending_ends = None
speech_windows = 0
silence_run = 0
pump_armed = False
pump_busy = False
pump_last_ms = 0
starved_logged = False
stalled_logged = False
overflow_logged = False
pump_error_logged = False


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


def apply_gain(buf, nbytes=None):
    """Gain the first nbytes of buf in place, defaulting to the whole buffer.

    The upload buffers are pooled at the maximum utterance length, so a
    trimmed utterance only fills a prefix and only that prefix is worth the
    viper pass. The buffer itself stays a plain bytearray starting at offset
    zero, which is what ptr16 wants.
    """
    g = int(CFG["mic_gain"])
    if g <= 1:
        return
    n = len(buf) if nbytes is None else nbytes
    t0 = time.ticks_ms()
    try:
        _gain_inplace(buf, n // 2, g)
    except Exception as e:
        log("gain: failed %r" % e)
        return
    log("gain: x%d over %d samples in %d ms" % (g, n // 2, time.ticks_diff(time.ticks_ms(), t0)))


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


def dbfs(amplitude):
    """Amplitude in 16 bit counts to dBFS, with a floor for digital silence."""
    if amplitude <= 0:
        return -99.0
    return 20.0 * math.log(amplitude / 32768.0, 10)


def analyze_frame(pcm, rms_out, peak_out):
    """RMS and peak dBFS for every window of one completed mono frame.

    channel_dbfs and channel_peak_dbfs each walk the buffer separately. The
    pump needs both numbers for every frame, so this makes one strided pass
    and fills two preallocated lists, which also keeps per frame allocation at
    zero. Roughly 4000 iterations for a one second frame, once per second.
    """
    stride = 2 * LEVEL_STEP
    base = 0
    w = 0
    while w < WINS_PER_FRAME:
        end = base + WINDOW_BYTES
        total = 0
        peak = 0
        n = 0
        i = base
        while i < end:
            v = pcm[i] | (pcm[i + 1] << 8)
            if v >= 32768:
                v -= 65536
            if v < 0:
                v = -v
            if v > peak:
                peak = v
            total += v * v
            n += 1
            i += stride
        rms_out[w] = dbfs(math.sqrt(total / n)) if n else -99.0
        peak_out[w] = dbfs(peak)
        base = end
        w += 1


def utterance_max_seconds():
    """Ceiling on one utterance, derived from the chunk_seconds tunable.

    chunk_seconds is no longer a fixed slice length, it is the upper bound the
    endpointer falls back to when the talker never pauses. It maps 1:1 rather
    than adding headroom: on continuous speech the endpointer never fires, so
    any headroom here would make a monologue slower than the old fixed slice
    instead of merely no faster. Measured on device against a podcast with no
    pauses, chunk_seconds + 2 closed every utterance at the ceiling.
    """
    s = int(CFG["chunk_seconds"])
    if s < 4:
        s = 4
    elif s > 12:
        s = 12
    return s


def begin_capture():
    """Allocate the frame ring and hand M5.Mic its first two slots."""
    global capture_buffers, capture_views, ring_rms, ring_peak
    global ring_slots, max_frames, upload_pool, upload_slot
    global head_seq, tail_seq, open_seq, pending_ends
    global speech_windows, silence_run
    global pump_armed, pump_busy, pump_last_ms
    global starved_logged, stalled_logged, overflow_logged, pump_error_logged

    max_frames = utterance_max_seconds() * 1000 // FRAME_MS
    # One finished utterance can be waiting on the network while the next one
    # is still being spoken, so the ring holds both plus a little slack.
    ring_slots = max_frames + 6
    capture_buffers = [bytearray(FRAME_BYTES) for _ in range(ring_slots)]
    # Built once so copying a frame into the upload buffer never allocates a
    # temporary slice of the audio.
    capture_views = [memoryview(b) for b in capture_buffers]
    ring_rms = [[-99.0] * WINS_PER_FRAME for _ in range(ring_slots)]
    ring_peak = [[-99.0] * WINS_PER_FRAME for _ in range(ring_slots)]
    upload_pool = [bytearray(max_frames * FRAME_BYTES) for _ in range(UPLOAD_POOL)]
    upload_slot = 0
    head_seq = 0
    tail_seq = 0
    open_seq = 0
    pending_ends = []
    speech_windows = 0
    silence_run = 0
    pump_busy = False
    pump_last_ms = time.ticks_ms()
    starved_logged = False
    stalled_logged = False
    overflow_logged = False
    pump_error_logged = False

    log(
        "pipeline: ring %d x %d bytes, upload %d x %d bytes, utterance max %d s"
        % (
            ring_slots,
            FRAME_BYTES,
            UPLOAD_POOL,
            max_frames * FRAME_BYTES,
            max_frames * FRAME_MS // 1000,
        )
    )
    queue_capture(capture_buffers[0])
    queue_capture(capture_buffers[1])
    pump_armed = True


def end_capture():
    """Stop requeueing, let the two raw pointers finish, then drop the ring.

    M5.Mic.end() waits for an in flight buffer instead of cancelling it, and
    the FIFO stores raw pointers, so every ring slot has to stay rooted until
    isRecording() reaches zero.
    """
    global capture_buffers, capture_views, ring_rms, ring_peak
    global upload_pool, pending_ends, pump_armed

    pump_armed = False
    if M5.Mic.isRecording():
        set_status("Finishing queued audio...", FG_DIM)
        drain_captures()
    capture_buffers = None
    capture_views = None
    ring_rms = None
    ring_peak = None
    upload_pool = None
    pending_ends = None


def report_starvation():
    """Say once why the FIFO ran dry. The two causes need different fixes.

    Without this the loss is silent: the status still reads Listening while
    nothing is being recorded.
    """
    global starved_logged, stalled_logged

    gap = time.ticks_diff(time.ticks_ms(), pump_last_ms)
    if gap > FRAME_MS:
        # Nothing serviced the mic for longer than a whole frame. The usual
        # cause is a POST that took do_post's blocking fallback, which never
        # polls, so no amount of pump code can cover it.
        if not starved_logged:
            starved_logged = True
            log("mic: FIFO empty, pump not called for %d ms, audio dropped" % gap)
    elif not stalled_logged:
        # The pump is being called promptly and the FIFO is still empty, so
        # capture itself has stopped. That is a device fault, not scheduling.
        stalled_logged = True
        log("mic: FIFO empty although the pump ran %d ms ago" % gap)


def report_pump_error(e):
    global pump_error_logged

    if not pump_error_logged:
        pump_error_logged = True
        log("pump: %r" % e)


def reserve_ring_room():
    """Guarantee the slot about to be queued is not still holding audio.

    Once this frame is absorbed the mic owns seq tail+1 and tail+2, so any
    frame the pipeline has not taken by then has to go. That only happens when
    the network is far behind, and it is logged once.
    """
    global head_seq, open_seq, speech_windows, silence_run, overflow_logged

    limit = tail_seq + 3 - ring_slots
    if head_seq >= limit:
        return
    dropped = limit - head_seq
    head_seq = limit
    while pending_ends and pending_ends[0] <= head_seq:
        pending_ends.pop(0)
    if open_seq < head_seq:
        open_seq = head_seq
        speech_windows = 0
        silence_run = 0
    if not overflow_logged:
        overflow_logged = True
        log("mic: ring full, dropped %d frames of unsent audio" % dropped)


def close_utterance(has_speech):
    """Hand a finished utterance to the pipeline, or retire a silent one."""
    global open_seq, head_seq, speech_windows, silence_run

    if tail_seq > open_seq:
        if has_speech:
            pending_ends.append(tail_seq)
            log(
                "utt: closed %d frames, %d speech windows, %d trailing quiet"
                % (tail_seq - open_seq, speech_windows, silence_run)
            )
        elif not pending_ends and head_seq == open_seq:
            # Never spoken into, so forget it here rather than spend a copy
            # and a gate check on it downstream.
            head_seq = tail_seq
    open_seq = tail_seq
    speech_windows = 0
    silence_run = 0


def score_frame(slot):
    """Fold one frame's window levels into the open utterance."""
    global speech_windows, silence_run, head_seq, open_seq, last_level

    gate = CFG["gate_dbfs"]
    transient = gate + GATE_PEAK_MARGIN
    rms = ring_rms[slot]
    peak = ring_peak[slot]
    loudest = -99.0
    for i in range(WINS_PER_FRAME):
        r = rms[i]
        if r > loudest:
            loudest = r
        if r >= gate or peak[i] >= transient:
            speech_windows += 1
            silence_run = 0
        else:
            silence_run += 1
    last_level = loudest

    frames = tail_seq - open_seq
    if speech_windows >= MIN_SPEECH_WINS and silence_run >= ENDPOINT_SILENCE_WINS:
        close_utterance(True)
    elif frames >= max_frames:
        close_utterance(speech_windows >= MIN_SPEECH_WINS)
    elif speech_windows == 0 and open_seq == head_seq and frames > 1:
        # Nothing but room tone so far. Retire the oldest frame so the ring
        # never fills with silence, keeping one frame of pre-roll in case a
        # word starts right on the boundary.
        head_seq += 1
        open_seq = head_seq
        silence_run = 0


def absorb_frame():
    """Take one completed frame, then hand the mic a fresh slot.

    The completed slot is measured before anything is requeued, and the slot
    given back to the mic is two sequences ahead of it, so no buffer M5.Mic
    holds a raw pointer to is ever read or written from here. tail_seq only
    advances after the requeue is accepted, so a rejected requeue retries this
    same frame instead of walking onto a slot that was never filled.
    """
    global tail_seq, head_seq

    slot = tail_seq % ring_slots
    analyze_frame(capture_buffers[slot], ring_rms[slot], ring_peak[slot])
    if not pending_ends and head_seq < open_seq:
        # A closed but silent utterance only becomes reclaimable once the
        # pipeline has taken everything queued ahead of it.
        head_seq = open_seq
    reserve_ring_room()
    if not M5.Mic.record(capture_buffers[(tail_seq + 2) % ring_slots], SAMPLE_RATE, False):
        raise RuntimeError("M5.Mic.record failed")
    tail_seq += 1
    score_frame(slot)


def service_capture():
    """Keep both M5.Mic slots busy and fold finished frames into the ring.

    poll_session_controls calls this every 20 ms, including from inside
    do_post's wait loop, so capture survives a multi second network turn. It
    must never raise: its callers are blocking wait loops where an exception
    would be almost impossible to trace back to here. It is also allowed to be
    arbitrarily late, for instance when do_post falls back to a blocking POST;
    then the FIFO simply drains, audio is lost, and report_starvation says so.
    """
    global pump_busy, pump_last_ms

    if not pump_armed or not running or capture_buffers is None:
        return
    if pump_busy:
        return
    pump_busy = True
    try:
        depth = M5.Mic.isRecording()
        if depth == 0:
            report_starvation()
        # 2 - depth is exactly how many queued slots have completed since the
        # last call, because the pump is the only thing that ever requeues.
        n = 2 - depth
        while n > 0:
            absorb_frame()
            n -= 1
    except Exception as e:
        report_pump_error(e)
    finally:
        pump_busy = False
        pump_last_ms = time.ticks_ms()


# ------------------------------------------------------------------ HTTP

# requests2 is HTTP/1.0 and always sends Connection: close, so every call pays
# a fresh TLS handshake to api.openai.com and every translation turn makes two
# calls. The first handshake after boot measured about 33 s on this board. The
# client below holds one TLS socket open and speaks HTTP/1.1 with keep-alive so
# the second and later requests skip the handshake entirely. It is only ever an
# optimisation: anything it raises falls through to requests2, see
# _blocking_post.

KEEPALIVE_DEFAULT_PORT = 443
# mbedtls copies each write into its own record buffer, so hand it slices
# rather than one 200 KB fragment.
KEEPALIVE_WRITE_CHUNK = 1024
# Drop a socket that has sat unused this long instead of discovering it is dead
# after uploading 200 KB of audio. Server idle timeouts are typically 60 s.
KEEPALIVE_MAX_IDLE_MS = 55000
# A stale keep-alive socket fails almost immediately. A failure later than this
# is a real timeout or a real server problem, so the request is not replayed.
KEEPALIVE_REPLAY_WINDOW_MS = 8000
# Stop paying for the attempt after this many consecutive failures.
KEEPALIVE_MAX_FAILURES = 3
# How long a new request waits for a worker abandoned by a cancelled session
# to finish draining the socket, before giving up and letting requests2 do it.
KEEPALIVE_BUSY_WAIT_MS = 3000

# Worker stack sizes to try, largest first. Measured on this unit: 32768 is
# always refused, while 16384, 12288 and 8192 each created a thread that
# completed a real TLS handshake and round trip to api.openai.com. Blocking on
# the UI thread is worse than a small stack, so step down before giving up.
HTTP_THREAD_STACK_STEPS = (HTTP_THREAD_STACK, 12288, 8192)

keepalive_disabled = False
http_thread_stack = 0

_socket_mod = None
_ssl_mod = None
_select_mod = None


class ConnectionLost(Exception):
    """The socket died, which for a reused keep-alive socket is expected."""


def _net_modules():
    """Import the raw network stack lazily so a missing module just falls back."""
    global _socket_mod, _ssl_mod, _select_mod

    if _socket_mod is None:
        import socket as socket_mod
        import ssl as ssl_mod

        try:
            import select as select_mod
        except Exception:
            select_mod = None
        _socket_mod = socket_mod
        _ssl_mod = ssl_mod
        _select_mod = select_mod
    return _socket_mod, _ssl_mod


def _wrap_tls(ssl_mod, raw, host):
    """Wrap a connected socket, MicroPython builds differ on how.

    server_hostname carries SNI, which api.openai.com needs to serve the right
    certificate. Neither path verifies the chain, matching what requests2 does.
    """
    try:
        return ssl_mod.wrap_socket(raw, server_hostname=host)
    except (AttributeError, TypeError):
        pass
    ctx = ssl_mod.SSLContext(ssl_mod.PROTOCOL_TLS_CLIENT)
    try:
        ctx.verify_mode = ssl_mod.CERT_NONE
    except Exception:
        pass
    return ctx.wrap_socket(raw, server_hostname=host)


def split_url(url):
    """Minimal https URL split, this build has no urllib."""
    rest = url
    if rest.startswith("https://"):
        rest = rest[8:]
    elif rest.startswith("http://"):
        raise ValueError("keep-alive client is https only")
    slash = rest.find("/")
    if slash < 0:
        hostport, path = rest, "/"
    else:
        hostport, path = rest[:slash], rest[slash:]
    port = KEEPALIVE_DEFAULT_PORT
    colon = hostport.find(":")
    if colon >= 0:
        port = int(hostport[colon + 1 :])
        hostport = hostport[:colon]
    return hostport, port, path


def join_exact(parts):
    """Concatenate into one exactly sized buffer, one copy per part.

    `body += pcm` reallocates and copies the whole upload buffer, which for a
    six second chunk is about 192 KB and the largest allocation in the app.
    Slice assignment needs MICROPY_PY_BUILTINS_SLICE_ASSIGN, which this build
    has, but fall back to concatenation rather than ever failing an upload.
    """
    total = 0
    for part in parts:
        total += len(part)
    try:
        out = bytearray(total)
        view = memoryview(out)
        off = 0
        for part in parts:
            n = len(part)
            view[off : off + n] = part
            off += n
        return out
    except Exception as e:
        log("http: preallocated body build failed %r" % e)
        out = bytearray()
        for part in parts:
            out += part
        return out


class KeepAliveHeaders:
    """Case-insensitive header map. post_with_retry asks for two spellings."""

    def __init__(self):
        self.map = {}

    def add(self, name, value):
        self.map[ascii_lower(name)] = value

    def get(self, name, default=None):
        return self.map.get(ascii_lower(name), default)

    def __contains__(self, name):
        return ascii_lower(name) in self.map


class KeepAliveResponse:
    """Drop-in for a requests2 response, as consumed by classify/transcribe."""

    def __init__(self, status_code, headers, body):
        self.status_code = status_code
        self.headers = headers
        self.content = body
        try:
            self.text = body.decode()
        except Exception:
            # Never let one odd byte take down the caller. classify() only
            # wants a printable slice and json() can still parse the bytes.
            self.text = ""

    def json(self):
        return json.loads(self.text or self.content)

    def close(self):
        # The body was read to its exact end before this object existed, so
        # the socket is already clean and stays in the cache.
        pass


class KeepAliveClient:
    """One persistent HTTP/1.1 TLS connection to the API host.

    Requests in this app are strictly serial, but do_post runs each one on a
    worker thread and the main thread can abandon it by raising
    SessionCancelled. The lock keeps a restarted session from touching a socket
    an abandoned worker is still reading, and invalidate() makes sure a
    half-read socket is closed rather than reused.
    """

    def __init__(self):
        self.lock = _thread.allocate_lock()
        self.sock = None  # the TLS wrapper that is read and written
        self.raw = None  # the plain socket underneath, kept for the poll check
        self.host = None
        self.port = 0
        self.addr = None  # cached getaddrinfo entry
        self.addr_key = None
        self.idle_since = 0
        self.discard = False
        self.failures = 0

    # -- connection ------------------------------------------------------

    def _close(self):
        sock, raw = self.sock, self.raw
        self.sock = None
        self.raw = None
        for s in (sock, raw):
            if s is None:
                continue
            try:
                s.close()
            except Exception:
                pass

    def invalidate(self):
        """Mark the cached socket unusable without touching it.

        The main thread calls this when a session is cancelled while a worker
        may still be mid-response. Closing here would pull the buffer out from
        under that worker, so the flag is honoured by whichever thread next
        holds the lock.
        """
        self.discard = True

    def _resolve(self, socket_mod, host, port):
        """Cache getaddrinfo, DNS on this board is a real network round trip."""
        key = (host, port)
        if self.addr is not None and self.addr_key == key:
            return self.addr
        t0 = time.ticks_ms()
        try:
            ai = socket_mod.getaddrinfo(host, port, 0, socket_mod.SOCK_STREAM)[0]
        except TypeError:
            ai = socket_mod.getaddrinfo(host, port)[0]
        self.addr = ai
        self.addr_key = key
        log("http: resolved %s in %d ms" % (host, time.ticks_diff(time.ticks_ms(), t0)))
        return ai

    def _connect(self, host, port):
        socket_mod, ssl_mod = _net_modules()
        t0 = time.ticks_ms()
        ai = self._resolve(socket_mod, host, port)
        raw = None
        try:
            raw = socket_mod.socket(ai[0], ai[1], ai[2])
            # socket.setdefaulttimeout does not exist on this firmware. The
            # timeout has to be set on the socket itself, before wrapping,
            # because the TLS wrapper does not always expose settimeout.
            raw.settimeout(HTTP_TIMEOUT)
            raw.connect(ai[-1])
            sock = _wrap_tls(ssl_mod, raw, host)
        except Exception:
            # A cached address that no longer answers is one likely cause.
            self.addr = None
            self.addr_key = None
            if raw is not None:
                try:
                    raw.close()
                except Exception:
                    pass
            raise
        self.raw = raw
        self.sock = sock
        self.host = host
        self.port = port
        self.idle_since = time.ticks_ms()
        log("http: keep-alive TLS to %s in %d ms" % (host, time.ticks_diff(time.ticks_ms(), t0)))

    def _stale(self):
        """True if the cached socket should not be trusted with a big upload."""
        if time.ticks_diff(time.ticks_ms(), self.idle_since) > KEEPALIVE_MAX_IDLE_MS:
            return True
        # A server that closed the connection leaves the raw socket readable,
        # which is a free way to notice before uploading 200 KB of audio. The
        # body was read to its exact end, so nothing else can be pending.
        if _select_mod is None or self.raw is None:
            return False
        try:
            poller = _select_mod.poll()
            poller.register(self.raw, _select_mod.POLLIN)
            return bool(poller.poll(0))
        except Exception:
            return False

    # -- wire ------------------------------------------------------------

    def _send_all(self, data):
        """Write in slices. memoryview keeps a 200 KB body from being copied."""
        view = memoryview(data)
        total = len(view)
        sent = 0
        while sent < total:
            end = sent + KEEPALIVE_WRITE_CHUNK
            if end > total:
                end = total
            n = self.sock.write(view[sent:end])
            if not n:
                # A blocking socket should never report "would block" (None).
                raise ConnectionLost("write returned %r" % n)
            sent += n

    def _readline(self):
        try:
            line = self.sock.readline()
        except AttributeError:
            line = self._readline_bytewise()
        if not line:
            raise ConnectionLost("server closed the connection")
        return line

    def _readline_bytewise(self):
        """Fallback for a TLS socket without the stream readline binding."""
        out = bytearray()
        while True:
            ch = self.sock.read(1)
            if not ch:
                break
            out += ch
            if ch == b"\n":
                break
        return bytes(out)

    def _read_exactly(self, n):
        """Read exactly n bytes. read() is free to return fewer at any time."""
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            chunk = self.sock.read(n - got)
            if not chunk:
                raise ConnectionLost("body truncated at %d of %d" % (got, n))
            view[got : got + len(chunk)] = chunk
            got += len(chunk)
        return buf

    def _read_to_eof(self):
        out = bytearray()
        while True:
            chunk = self.sock.read(512)
            if not chunk:
                break
            out += chunk
        return out

    def _read_head(self):
        """Parse the status line and headers, and decide if the socket lives."""
        line = self._readline()
        parts = line.split()
        if not line.startswith(b"HTTP/") or len(parts) < 2:
            raise ConnectionLost("bad status line %r" % line[:40])
        status = int(parts[1].decode())
        headers = KeepAliveHeaders()
        while True:
            line = self._readline().strip()
            if not line:
                break
            colon = line.find(b":")
            if colon < 0:
                continue
            headers.add(line[:colon].decode().strip(), line[colon + 1 :].decode().strip())
        conn = ascii_lower(headers.get("connection") or "")
        close_after = "close" in conn
        if parts[0] == b"HTTP/1.0" and "keep-alive" not in conn:
            close_after = True
        return status, headers, close_after

    def _read_body(self, status, headers):
        """Return (payload, must_close). Exact framing is what makes reuse safe."""
        if status in (204, 304):
            return b"", False
        if "chunked" in ascii_lower(headers.get("transfer-encoding") or ""):
            return self._read_chunked(), False
        length = headers.get("content-length")
        if length is not None:
            try:
                n = int(length.strip())
            except Exception:
                raise ConnectionLost("bad content-length %r" % length)
            return self._read_exactly(n), False
        # No framing at all means the body ends at EOF, so this socket cannot
        # be reused whatever the headers claimed.
        return self._read_to_eof(), True

    def _read_chunked(self):
        out = bytearray()
        while True:
            line = self._readline().strip()
            if not line:
                # Tolerate a stray blank line between chunks.
                continue
            semi = line.find(b";")
            if semi >= 0:
                line = line[:semi]
            size = int(line.decode(), 16)
            if size == 0:
                break
            out += self._read_exactly(size)
            self._read_exactly(2)  # the CRLF that terminates every chunk
        # Trailers, then the blank line that ends the message.
        while self._readline().strip():
            pass
        return out

    # -- requests --------------------------------------------------------

    def _build_head(self, method, host, port, path, body, headers):
        length = len(body) if body else 0
        host_header = host if port == KEEPALIVE_DEFAULT_PORT else "%s:%d" % (host, port)
        lines = [
            "%s %s HTTP/1.1" % (method, path),
            "Host: " + host_header,
            "Connection: keep-alive",
            "Accept: application/json",
            # This build has no decompressor and the framing has to stay exact.
            "Accept-Encoding: identity",
            "Content-Length: %d" % length,
        ]
        if headers:
            for name in headers:
                lines.append("%s: %s" % (name, headers[name]))
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    def _acquire(self):
        """Bounded wait, a MicroPython lock has no timeout argument.

        A worker abandoned by a cancelled session can still be draining the
        socket. Waiting behind it is correct, waiting forever is not, so give
        up after a few seconds and let the caller fall back to requests2.
        """
        deadline = time.ticks_add(time.ticks_ms(), KEEPALIVE_BUSY_WAIT_MS)
        while not self.lock.acquire(False):
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                raise ConnectionLost("keep-alive client still busy")
            time.sleep_ms(20)

    def post(self, url, body, headers):
        host, port, path = split_url(url)
        self._acquire()
        try:
            return self._request_locked("POST", host, port, path, body, headers)
        finally:
            self.lock.release()

    def prewarm_url(self, url):
        """Open the connection now. Returns True if a handshake actually ran."""
        host, port, _ = split_url(url)
        self._acquire()
        try:
            self._drop_if_discarded()
            if (
                self.sock is not None
                and host == self.host
                and port == self.port
                and not self._stale()
            ):
                return False
            self._close()
            self._connect(host, port)
            return True
        finally:
            self.lock.release()

    def _drop_if_discarded(self):
        if self.discard:
            # A cancelled session may have abandoned a half-read socket.
            if self.sock is not None:
                log("http: dropping keep-alive socket left by a cancelled session")
            self._close()
            self.discard = False

    def _request_locked(self, method, host, port, path, body, headers):
        self._drop_if_discarded()
        if self.sock is not None and (host != self.host or port != self.port):
            self._close()
        head = self._build_head(method, host, port, path, body, headers)
        for attempt in (0, 1):
            if self.sock is not None and self._stale():
                self._close()
            reused = self.sock is not None
            t0 = time.ticks_ms()
            try:
                if self.sock is None:
                    self._connect(host, port)
                    t0 = time.ticks_ms()
                self._send_all(head)
                if body:
                    self._send_all(body)
                status, resp_headers, close_after = self._read_head()
            except Exception as e:
                self._close()
                quick = time.ticks_diff(time.ticks_ms(), t0) < KEEPALIVE_REPLAY_WINDOW_MS
                if attempt == 0 and reused and quick:
                    # Servers close idle keep-alive sockets routinely and this
                    # is the normal way to find out. Reconnect and replay: the
                    # request never reached the server, so nothing is repeated.
                    log("http: keep-alive socket was stale (%r), replaying" % e)
                    continue
                raise
            try:
                payload, must_close = self._read_body(status, resp_headers)
            except Exception:
                # A half-read socket is never reusable.
                self._close()
                raise
            if close_after or must_close:
                self._close()
            else:
                self.idle_since = time.ticks_ms()
            return KeepAliveResponse(status, resp_headers, payload)
        raise ConnectionLost("keep-alive replay exhausted")


HTTP_CLIENT = KeepAliveClient()


def prewarm():
    """Open the TLS session before the first utterance has to wait for it.

    The first handshake after boot measured about 33 s on this board. This is
    purely an optimisation, so every failure here is logged and swallowed.
    """
    if keepalive_disabled:
        return False
    t0 = time.ticks_ms()
    try:
        opened = HTTP_CLIENT.prewarm_url(CFG["transcribe_url"])
    except Exception as e:
        log("http: prewarm failed %r" % e)
        return False
    if opened:
        log("http: prewarmed in %d ms" % time.ticks_diff(time.ticks_ms(), t0))
    return True


class ApiError(Exception):
    def __init__(self, status, body, retryable):
        self.status = status
        self.body = body
        self.retryable = retryable

    def __str__(self):
        return "HTTP %d" % self.status


class SessionCancelled(Exception):
    """A touch or power-button press stopped an in-flight session."""


def poll_session_controls():
    """Update input, service the mic, and latch a stop/settings request.

    Every blocking wait in the pipeline already calls this every 20 ms, so it
    is also the only place that can keep M5.Mic's two slot FIFO occupied while
    a POST is in flight. Without service_capture here both slots complete
    during a slow turn, the mic goes idle, and audio is dropped while the
    status still reads Listening.
    """
    global running, settings_requested

    M5.update()
    service_capture()
    power = BtnPWR.wasClicked()
    pos = press()
    if not power and pos is None:
        return False

    running = False
    if pos and hit(pos[0], pos[1], GEAR_HIT_BOX):
        settings_requested = True
        set_status("Opening settings...", FG_DIM)
        log("touch: settings requested")
    else:
        set_status("Stopping...", FG_DIM)
        log("touch: stop requested")
    return True


def _requests2_post(url, kw):
    """requests2 accepts an undocumented timeout=, fall back if it ever stops."""
    try:
        return requests2.post(url, timeout=HTTP_TIMEOUT, **kw)
    except TypeError:
        return requests2.post(url, **kw)


def _keepalive_usable(kw):
    """The client only speaks the two kwargs this app actually posts with."""
    if kw.get("data") is None:
        return False
    return all(name in ("data", "headers") for name in kw)


def _blocking_post(url, kw):
    """One POST, keep-alive first, requests2 as the guaranteed fallback.

    The keep-alive client is an optimisation and never a dependency. A missing
    ssl module, a handshake failure, an unparseable response, a second
    consecutive dead socket: all of it lands here and is re-sent over
    requests2, which pays a fresh TLS handshake but always works.
    """
    global keepalive_disabled

    if not keepalive_disabled and _keepalive_usable(kw):
        try:
            r = HTTP_CLIENT.post(url, kw.get("data"), kw.get("headers"))
            HTTP_CLIENT.failures = 0
            return r
        except Exception as e:
            HTTP_CLIENT.failures += 1
            log("http: keep-alive POST failed (%d) %r" % (HTTP_CLIENT.failures, e))
            if HTTP_CLIENT.failures >= KEEPALIVE_MAX_FAILURES:
                keepalive_disabled = True
                log("http: keep-alive disabled for this run, using requests2 only")
    return _requests2_post(url, kw)


def _post_worker(result, url, kw):
    """Publish a response or exception into a small cross-thread mailbox."""
    try:
        value = _blocking_post(url, kw)
        result[1] = value
        result[0] = 1
    except BaseException as e:
        result[1] = e
        result[0] = -1


def _start_post_worker(result, url, kw):
    """Start the POST worker, stepping the stack down until one is accepted.

    A 32 KB stack is rejected outright on this board, which quietly sent every
    single POST down the blocking path. Retry once at each size before dropping
    to the next, because the ESP32 port can also transiently reject a task
    while old resources are still settling.
    """
    global http_thread_stack

    for size in HTTP_THREAD_STACK_STEPS:
        if http_thread_stack and size > http_thread_stack:
            continue  # already known to be more than this board will allocate
        for attempt in (0, 1):
            try:
                _thread.stack_size(size)
            except Exception:
                pass
            try:
                _thread.start_new_thread(_post_worker, (result, url, kw))
            except Exception as e:
                log("http: worker start failed at %d bytes %r" % (size, e))
                gc.collect()
                if attempt == 0:
                    time.sleep_ms(50)
                continue
            if size != http_thread_stack:
                log("http: POST worker stack is now %d bytes" % size)
                http_thread_stack = size
            return True
    return False


def do_post(url, **kw):
    """POST without starving display/touch updates on the main thread."""
    result = [0, None]
    if not _start_post_worker(result, url, kw):
        # Genuine last resort. This blocks the main thread for the whole round
        # trip, so touch is dead and poll_session_controls stops running, which
        # also stalls the capture pump. Make that visible rather than silent.
        log("http: NO worker thread available, blocking POST, touch and capture stall")
        return _blocking_post(url, kw)

    while result[0] == 0:
        poll_session_controls()
        time.sleep_ms(20)

    if not running:
        if result[0] > 0:
            try:
                result[1].close()
            except Exception:
                pass
        # The abandoned worker may still be reading the shared keep-alive
        # socket. Flag it so a half-read socket is dropped, never reused.
        HTTP_CLIENT.invalidate()
        raise SessionCancelled()
    if result[0] < 0:
        raise result[1]
    response = result[1]
    return response


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
        deadline = time.ticks_add(time.ticks_ms(), wait * 1000)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            poll_session_controls()
            if not running:
                raise SessionCancelled()
            time.sleep_ms(20)
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
    head = bytearray()
    for name, value in fields:
        head += (
            "--" + BOUNDARY + "\r\n"
            'Content-Disposition: form-data; name="' + name + '"\r\n\r\n' + value + "\r\n"
        ).encode()
    head += (
        "--" + BOUNDARY + "\r\n"
        'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()
    head += wav_header(len(pcm))
    # The preamble is small, so it can grow. The PCM is not: appending it to a
    # bytearray reallocates and copies the whole ~192 KB body, so the parts are
    # measured first and copied once into an exactly sized buffer.
    body = join_exact((head, pcm, ("\r\n--" + BOUNDARY + "--\r\n").encode()))

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


def wait_for_utterance():
    """Poll input and the mic pump until the endpointer closes an utterance.

    This replaces the old fixed wall clock wait. Nothing here counts seconds
    any more; the pump decides when the talker has stopped.
    """
    shown = -1
    while running and not pending_ends:
        poll_session_controls()
        if not running:
            return
        held = tail_seq - open_seq if speech_windows else 0
        if held != shown:
            shown = held
            if held:
                set_status("Listening  %d s" % held)
            else:
                set_status("Listening")
        time.sleep_ms(20)


def prepare_chunk(start_seq, end_seq):
    """Gate, trim, gain and freeze one endpointed utterance for upload.

    The returned memoryview is over a pooled buffer that M5.Mic has never been
    handed a pointer to, so capture keeps running into the ring for the whole
    upload. Returning a view rather than pcm[:] also removes the fresh 192 KB
    allocation the old code made on every single turn.
    """
    global last_level, upload_slot

    frames = end_seq - start_seq
    if frames > max_frames:
        # Defensive. The endpointer closes at max_frames, so this should not
        # fire; keep the newest audio rather than overrun the pooled buffer.
        log("rec: clamped %d frames to %d" % (frames, max_frames))
        start_seq = end_seq - max_frames
        frames = max_frames
    total = frames * FRAME_BYTES

    # The pump measured every window as its frame landed, so the gate and the
    # trim points cost no second pass over the audio. The gate keeps its old
    # two part shape, RMS over the gate or a transient well above it, but a
    # single loud window is now enough instead of the whole chunk's average.
    gate = CFG["gate_dbfs"]
    transient = gate + GATE_PEAK_MARGIN
    level = -99.0
    peak = -99.0
    first = -1
    last = -1
    for w in range(frames * WINS_PER_FRAME):
        slot = (start_seq + w // WINS_PER_FRAME) % ring_slots
        j = w % WINS_PER_FRAME
        r = ring_rms[slot][j]
        p = ring_peak[slot][j]
        if r > level:
            level = r
        if p > peak:
            peak = p
        if r >= gate or p >= transient:
            if first < 0:
                first = w
            last = w
    last_level = level
    log(
        "rec: %d bytes %.1fs rms=%.1f peak=%.1f dBFS"
        % (total, total / (SAMPLE_RATE * 2.0), level, peak)
    )
    if first < 0:
        set_status("Too quiet, speak closer", FG_DIM)
        log("rec: below gate %d dBFS, not uploading" % gate)
        return None

    # Trim the room tone either side of the speech. Typically a third to a
    # half of the bytes, which comes straight off the upload time. The guard
    # margin keeps a soft onset or a trailing consonant that the window grid
    # would otherwise cut. Both bounds are multiples of two, so sample
    # alignment and byte order are preserved.
    lo = first * WINDOW_BYTES - TRIM_GUARD_BYTES
    hi = (last + 1) * WINDOW_BYTES + TRIM_GUARD_BYTES
    if lo < 0:
        lo = 0
    if hi > total:
        hi = total
    n = hi - lo

    buf = upload_pool[upload_slot]
    upload_slot = (upload_slot + 1) % UPLOAD_POOL
    pos = 0
    seq = start_seq + lo // FRAME_BYTES
    off = lo % FRAME_BYTES
    while pos < n:
        take = FRAME_BYTES - off
        if take > n - pos:
            take = n - pos
        buf[pos : pos + take] = capture_views[seq % ring_slots][off : off + take]
        pos += take
        off = 0
        seq += 1

    apply_gain(buf, n)
    log("rec: trimmed %d to %d bytes, %.1fs on the wire" % (total, n, n / (SAMPLE_RATE * 2.0)))
    return memoryview(buf)[:n]


def recognize(pcm):
    """Transcribe a stable PCM copy, returning (text, source) or None."""
    global last_orig, last_src

    try:
        text, lang = transcribe(pcm)
    except TypeError:
        # transcribe builds the multipart body with `body += pcm`, and pcm is
        # a memoryview over a pooled buffer. Every MicroPython bytearray we
        # know of extends from any buffer object, but pay for one copy rather
        # than lose the utterance if this build disagrees.
        log("stt: multipart rejected a memoryview, copying")
        text, lang = transcribe(bytearray(pcm))
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
    """Upload endpointed utterances while the pump keeps the mic ring full."""
    global head_seq

    if M5.Mic.isRecording():
        drain_captures()

    try:
        # Inside the try so a rejected first queue still reaches end_capture,
        # which is the only thing that drains M5.Mic's raw pointers.
        begin_capture()
        set_status("Listening")
        while running:
            wait_for_utterance()
            if not running:
                break
            if not pending_ends:
                # reserve_ring_room can retire a queued utterance if the
                # network fell far enough behind. Go back to listening.
                continue
            end_seq = pending_ends.pop(0)
            start_seq = head_seq
            upload_pcm = prepare_chunk(start_seq, end_seq)
            # Retire the frames before anything can poll again. prepare_chunk
            # is synchronous and never polls, so the pump cannot touch these
            # ring slots until head_seq has moved past them.
            head_seq = end_seq
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
        end_capture()


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

    # Join at boot rather than lazily on the first POST. A dead link should
    # say so on screen, not surface later as an opaque 45 second timeout.
    set_status("Checking Wi-Fi...")
    online = ensure_wifi()
    if online:
        set_status("Wi-Fi connected")
    else:
        set_status("Wi-Fi unavailable", FG_ALERT)

    probe_channels()

    # Open the TLS connection now, while the user is still reading the idle
    # screen. The first POST after boot otherwise pays DNS plus a full cold
    # handshake, which was measured at about 33 seconds on this board.
    if online:
        set_status("Connecting to API...")
        prewarm()

    update_display()
    if online:
        set_status("Tap screen to start")
    else:
        set_status("No Wi-Fi, tap to retry", FG_ALERT)


def loop():
    global running, fatal, settings_requested
    M5.update()

    pos = tap()
    if pos and hit(pos[0], pos[1], GEAR_HIT_BOX):
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

    if not ensure_wifi():
        running = False
        set_status("Wi-Fi down, tap to retry", FG_ALERT)
        return

    try:
        run_session()
    except SessionCancelled:
        running = False
        log("pipeline: cancelled by user")
        if not settings_requested:
            set_status("Paused")
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
