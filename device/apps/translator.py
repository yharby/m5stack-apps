"""Realtime multilingual speech translator for M5Stack CoreS3.

Use the dedicated bottom controls to choose a language pair and start or stop
listening. The transcript is a bounded conversation feed: it follows the newest
turn until the user swipes upward, then exposes a Live button to resume. Tap the
gear for microphone and storage settings, or EXIT to return to UIFlow. Every stage is logged to
/flash/translator.log and the serial console.

Device API facts, all verified by probing this board or by reading the
uiflow-micropython 2.5.1 source. Do not "fix" these back to what generic docs
suggest; see the verified I/O notes in CLAUDE.md.

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
  * Japanese, Korean and Chinese need their matching UIFlow2 font objects.
  * Arabic/Hebrew require the repository's RTL UIFlow2 firmware profile;
    stock UIFlow2 disables bidi ordering and Arabic contextual shaping.
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

import lvgl as lv
import M5
import m5ui
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

# Direction and renderer are explicit so text never silently falls through to
# a Latin font. The rtl renderer is supplied by this repository's UIFlow2
# firmware patch; logical Unicode order is retained everywhere outside LVGL.
LANGUAGE_PROFILES = {
    "en": {
        "name": "English",
        "native": "English",
        "short": "EN",
        "script": "latin",
        "direction": "ltr",
        "font": "latin",
    },
    "ja": {
        "name": "Japanese",
        "native": "日本語",
        "short": "JA",
        "script": "japanese",
        "direction": "ltr",
        "font": "ja",
    },
    "ko": {
        "name": "Korean",
        "native": "한국어",
        "short": "KO",
        "script": "hangul",
        "direction": "ltr",
        "font": "ko",
    },
    "zh": {
        "name": "Simplified Chinese",
        "native": "中文",
        "short": "ZH",
        "script": "han",
        "direction": "ltr",
        "font": "zh",
    },
    "ar": {
        "name": "Arabic",
        "native": "العربية",
        "short": "AR",
        "script": "arabic",
        "direction": "rtl",
        "font": "rtl",
    },
    "he": {
        "name": "Hebrew",
        "native": "עברית",
        "short": "HE",
        "script": "hebrew",
        "direction": "rtl",
        "font": "rtl",
    },
    # Modern Mongolian is normally written in Cyrillic and is horizontal LTR.
    # Traditional vertical Mongolian is a separate renderer capability and is
    # intentionally rejected until the device has a vertical-layout font/UI.
    "mn": {
        "name": "Mongolian",
        "native": "Монгол",
        "short": "MN",
        "script": "cyrillic",
        "direction": "ltr",
        "font": "mn",
    },
}
LANGUAGE_CODES = ("en", "ja", "ko", "zh", "ar", "he", "mn")

CFG = {
    "wifi_ssid": "",
    "wifi_pass": "",
    "openai_api_key": "",
    "transcribe_url": "https://api.openai.com/v1/audio/transcriptions",
    "chat_url": "https://api.openai.com/v1/chat/completions",
    # Speed-first defaults. The mini models trade a little accuracy for much
    # lower latency; both were verified with this device/account.
    "transcribe_model": "gpt-4o-mini-transcribe",
    "chat_model": "gpt-4o-mini",
    # gpt-transcribe supports a keywords array specifically for domain terms.
    # This can be replaced by a JSON array in the device config.
    "domain_keywords": DEFAULT_DOMAIN_KEYWORDS,
    "domain_context": (
        "a live FOSS4G/OSGeo conversation about open geospatial standards, "
        "software, data formats, and cloud-native geospatial architecture"
    ),
    # A session is bidirectional between exactly two capability-checked
    # languages. Auto chooses the speaker side from API metadata and script;
    # fixed_a/fixed_b can be used where automatic detection is undesirable.
    "language_pair": ["en", "ja"],
    "source_mode": "auto",
    # History is bounded independently of the optional full SD transcript.
    "history_turns": 8,
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
    # Production logs contain timings and lengths, not conversation content.
    "debug_content_logs": False,
    # Optional SD transcript archive. Markdown is easy to read directly from
    # the card; JSONL is available for structured downstream processing.
    "save_transcripts": False,
    "transcript_format": "md",
    "transcript_max_file_bytes": 1024 * 1024,
    # The firmware clock is UTC. Japan Standard Time is UTC+09:00.
    "transcript_timezone_offset_minutes": 9 * 60,
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
SD_MOUNT = "/sd"
SD_TRANSCRIPT_DIR = "/sd/m5stack-apps/translator"
SD_SLOT = 3
SD_SCK = 36
SD_MISO = 35
SD_MOSI = 37
SD_CS = 4
SD_FREQ = 20_000_000
# Measured on this unit with tools/device_scripts/thread_probe.py: a 32 KB
# task stack CANNOT be created here and raises OSError("can't create
# thread") every time, which silently sent every POST down do_post's
# blocking fallback and disabled touch during network calls. 16 KB creates
# reliably, and tls_thread_probe.py completed a real TLS handshake and HTTP
# round trip on stacks down to 8 KB, so this leaves real margin.
HTTP_THREAD_STACK = 16384

GATE_MIN, GATE_MAX = -70, -25
CHUNK_MIN, CHUNK_MAX = 4, 12
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
FG_DIM = 0x808080
FG_LINE = 0x404040
FG_ALERT = 0xFF5050
FG_TEXT = 0xFFFFFF
FG_PANEL = 0x303030
FG_ACTIVE = 0x184A55

HEADER_H = 44
ACTION_Y = 196
ACTION_H = H - ACTION_Y
VIEW_Y = HEADER_H
VIEW_H = ACTION_Y - VIEW_Y
EXIT_BOX = (260, 0, 60, HEADER_H)
LANGUAGE_BOX = (0, ACTION_Y, 112, ACTION_H)
RUN_BOX = (112, ACTION_Y, 120, ACTION_H)
LIVE_BOX = (232, ACTION_Y, 44, ACTION_H)
GEAR_HIT_BOX = (276, ACTION_Y, 44, ACTION_H)

BOUNDARY = "----M5CoreS3TranslatorBoundary"

running = False
fatal = ""
config_error = ""
last_orig = ""
last_trans = ""
last_src = "en"
last_trans_lang = "ja"
last_level = -99.0
font_ja = None
font_ko = None
font_zh = None
font_ar_source = None
font_ar_translation = None
font_he_source = None
font_he_translation = None
rtl_firmware_abi = 0
font_ui = None
font_label = None
font_source_en = None
font_trans_en = None
ui_screen = None
ui_feed = None
ui_status = None
ui_pair_label = None
ui_run_label = None
ui_live_button = None
ui_live_label = None
ui_callbacks = []
ui_cards = []
ui_turn_views = []
ui_programmatic_scroll = False
language_requested = False
stop_requested = False
exit_requested = False
ui_action = None
ui_port_loop = None
ui_last_tick = 0
# Each turn is a small dict so it remains cheap on MicroPython and serializes
# cleanly when needed. The active recognized turn lives in the same ring and
# changes state in place from translating to complete/error.
turns = []
active_turn = None
follow_latest = True
scroll_lines = 0
new_turns_while_scrolled = 0
feed_revision = 0
feed_painted_revision = -1
touch_origin = None
touch_last = None
touch_dragged = False
capture_buffers = None
settings_requested = False
sd_ready = False
sd_warning = ""
transcript_session_base = None
transcript_session_path = None
transcript_session_epoch = None
transcript_session_ticks = 0
transcript_capture_epoch = None
transcript_capture_ticks = 0
transcript_part = 0
transcript_sequence = 0

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
_log_rotation_deferred = False


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
    global _log_writes, _log_rotation_deferred

    _log_writes = 0
    _log_rotation_deferred = False
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
    global _log_writes, _log_rotation_deferred

    line = "[t=%d] %s" % (time.ticks_ms(), msg)
    print(line)
    # Flash stat/rename can block for over a second on this firmware. Once a
    # capture session reaches the rotation checkpoint, keep serial logging but
    # defer further flash writes and the rotation itself until capture stops.
    if _log_rotation_deferred and pump_armed:
        return
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
        if pump_armed:
            _log_rotation_deferred = True
        else:
            _rotate_log()


def log_exc(e):
    try:
        buf = io.StringIO()
        sys.print_exception(e, buf)
        log("EXC: " + buf.getvalue())
    except Exception:
        log("EXC: %r" % e)


# ------------------------------------------------------------------ config


def configured_pair():
    pair = CFG.get("language_pair")
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return ("en", "ja")
    a, b = str(pair[0]), str(pair[1])
    if a not in LANGUAGE_PROFILES or b not in LANGUAGE_PROFILES or a == b:
        return ("en", "ja")
    return (a, b)


def normalize_language_code(value):
    text = str(value or "").strip().replace("_", "-")
    folded = "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in text)
    base = folded.split("-", 1)[0]
    aliases = {
        "en": "en",
        "eng": "en",
        "english": "en",
        "ja": "ja",
        "jpn": "ja",
        "japanese": "ja",
        "ko": "ko",
        "kor": "ko",
        "korean": "ko",
        "zh": "zh",
        "zho": "zh",
        "chi": "zh",
        "cmn": "zh",
        "chinese": "zh",
        "mandarin": "zh",
        "ar": "ar",
        "ara": "ar",
        "arabic": "ar",
        "he": "he",
        "heb": "he",
        "hebrew": "he",
        "mn": "mn",
        "mon": "mn",
        "mongolian": "mn",
    }
    return aliases.get(folded, aliases.get(base, ""))


def language_profile(code):
    return LANGUAGE_PROFILES.get(code, LANGUAGE_PROFILES["en"])


def language_short(code):
    return language_profile(code)["short"]


def other_language(code):
    a, b = configured_pair()
    return b if code == a else a


def script_language(text):
    """Return a decisive script language, or an empty string if ambiguous.

    Text remains in logical Unicode order. This function only selects a route;
    it never reverses or presentation-shapes text for display.
    """
    counts = {"en": 0, "ja": 0, "ko": 0, "zh": 0, "ar": 0, "he": 0, "mn": 0}
    for ch in text:
        code = ord(ch)
        if 0x0590 <= code <= 0x05FF or 0xFB1D <= code <= 0xFB4F:
            counts["he"] += 1
        elif (
            0x0600 <= code <= 0x06FF
            or 0x0750 <= code <= 0x077F
            or 0x08A0 <= code <= 0x08FF
            or 0xFB50 <= code <= 0xFDFF
            or 0xFE70 <= code <= 0xFEFF
        ):
            counts["ar"] += 1
        elif 0x3040 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF:
            counts["ja"] += 1
        elif 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F or 0xAC00 <= code <= 0xD7AF:
            counts["ko"] += 1
        elif 0x0400 <= code <= 0x052F:
            counts["mn"] += 1
        elif 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF:
            counts["zh"] += 1
        elif 0x0041 <= code <= 0x005A or 0x0061 <= code <= 0x007A:
            counts["en"] += 1
    # Kana is decisive for Japanese even when the same sentence contains more
    # Han characters. Han is considered only when no language-specific script
    # is present.
    best = ""
    best_count = 0
    tied = False
    for code in ("ar", "he", "ko", "ja", "mn"):
        count = counts[code]
        if count > best_count:
            best, best_count, tied = code, count, False
        elif count and count == best_count:
            tied = True
    if best_count:
        return "" if tied else best
    if counts["zh"]:
        return "zh"
    return "en" if counts["en"] else ""


def resolve_route(text, api_code=""):
    """Resolve the source and target within the configured two-language pair."""
    pair = configured_pair()
    mode = CFG.get("source_mode", "auto")
    if mode in pair:
        return mode, other_language(mode)

    scripted = script_language(text)
    api_lang = normalize_language_code(api_code)

    # Han alone cannot distinguish Japanese from Chinese. Kana is decisive
    # for Japanese; otherwise prefer matching API metadata or the only Han
    # language selected in the pair.
    if scripted == "zh":
        if api_lang in pair and api_lang in ("ja", "zh"):
            scripted = api_lang
        elif "zh" in pair and "ja" not in pair:
            scripted = "zh"
        elif "ja" in pair and "zh" not in pair:
            scripted = "ja"

    if scripted:
        if scripted in pair:
            return scripted, other_language(scripted)
        return "", ""
    if api_lang in pair:
        return api_lang, other_language(api_lang)
    return pair[0], pair[1]


def pair_render_error(pair=None):
    """Gate sessions whose selected scripts cannot be rendered faithfully."""
    if pair is None:
        pair = configured_pair()
    rtl_fonts = {
        "ar": (font_ar_source, font_ar_translation),
        "he": (font_he_source, font_he_translation),
    }
    if rtl_firmware_abi != 2 and ("ar" in pair or "he" in pair):
        return "RTL firmware required"
    for code in ("ar", "he"):
        if code in pair and any(font is None for font in rtl_fonts[code]):
            return "%s font unavailable" % language_short(code)
    missing = []
    for code, font in (("ja", font_ja), ("ko", font_ko), ("zh", font_zh), ("mn", font_ja)):
        if code in pair and font is None:
            missing.append(language_short(code))
    if missing:
        return "%s font unavailable" % "/".join(missing)
    return ""


def load_config():
    global config_error

    config_error = ""

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
    CFG["save_transcripts"] = CFG["save_transcripts"] is True
    CFG["debug_content_logs"] = CFG["debug_content_logs"] is True
    pair = CFG.get("language_pair")
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        config_error = "language_pair needs two codes"
        pair = ("en", "ja")
    else:
        pair = (normalize_language_code(pair[0]), normalize_language_code(pair[1]))
        if not pair[0] or not pair[1] or pair[0] == pair[1]:
            config_error = "language_pair is invalid"
            pair = ("en", "ja")
    CFG["language_pair"] = list(pair)
    source_mode = normalize_language_code(CFG["source_mode"])
    if CFG["source_mode"] == "auto":
        source_mode = "auto"
    if source_mode != "auto" and source_mode not in pair:
        config_error = "source_mode must be auto or in pair"
        source_mode = "auto"
    CFG["source_mode"] = source_mode
    try:
        history_turns = int(CFG["history_turns"])
    except Exception:
        history_turns = 8
    if history_turns < 2:
        history_turns = 2
    elif history_turns > 12:
        history_turns = 12
    CFG["history_turns"] = history_turns
    if CFG["transcript_format"] not in ("md", "jsonl"):
        CFG["transcript_format"] = "md"
    try:
        transcript_limit = int(CFG["transcript_max_file_bytes"])
    except Exception:
        transcript_limit = 1024 * 1024
    if transcript_limit < 64 * 1024:
        transcript_limit = 64 * 1024
    elif transcript_limit > 8 * 1024 * 1024:
        transcript_limit = 8 * 1024 * 1024
    CFG["transcript_max_file_bytes"] = transcript_limit
    try:
        timezone_offset = int(CFG["transcript_timezone_offset_minutes"])
    except Exception:
        timezone_offset = 9 * 60
    if timezone_offset < -12 * 60:
        timezone_offset = -12 * 60
    elif timezone_offset > 14 * 60:
        timezone_offset = 14 * 60
    CFG["transcript_timezone_offset_minutes"] = timezone_offset
    log(
        "config: api_key=%s stt=%s chat=%s pair=%s/%s mode=%s history=%d "
        "gate=%s chunk=%ss gain=%sx transcripts=%s/%s"
        % (
            "yes" if CFG["openai_api_key"] else "NO",
            CFG["transcribe_model"],
            CFG["chat_model"],
            CFG["language_pair"][0],
            CFG["language_pair"][1],
            CFG["source_mode"],
            CFG["history_turns"],
            CFG["gate_dbfs"],
            CFG["chunk_seconds"],
            CFG["mic_gain"],
            "on" if CFG["save_transcripts"] else "off",
            CFG["transcript_format"],
        )
    )


def save_config():
    """Persist only the tunables the settings page can change."""
    path = CONFIG_PATHS[0]
    temp_path = path + ".tmp"
    try:
        try:
            with open(path) as f:
                data = json.loads(f.read())
        except Exception:
            data = {}
        data["gate_dbfs"] = CFG["gate_dbfs"]
        data["chunk_seconds"] = CFG["chunk_seconds"]
        data["mic_gain"] = CFG["mic_gain"]
        data["save_transcripts"] = CFG["save_transcripts"]
        data["transcript_format"] = CFG["transcript_format"]
        data["language_pair"] = list(configured_pair())
        data["source_mode"] = CFG["source_mode"]
        data["history_turns"] = CFG["history_turns"]
        try:
            os.remove(temp_path)
        except OSError:
            pass
        with open(temp_path, "w") as f:
            f.write(json.dumps(data))
            f.flush()
        os.sync()
        os.rename(temp_path, path)
        os.sync()
        log(
            "config: saved pair=%s/%s mode=%s gate=%s chunk=%s gain=%s transcripts=%s/%s"
            % (
                CFG["language_pair"][0],
                CFG["language_pair"][1],
                CFG["source_mode"],
                CFG["gate_dbfs"],
                CFG["chunk_seconds"],
                CFG["mic_gain"],
                "on" if CFG["save_transcripts"] else "off",
                CFG["transcript_format"],
            )
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
        service_ui()
        if exit_requested:
            return False
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


# ------------------------------------------------------------------ SD transcripts


def ensure_sd():
    """Mount and verify the optional CoreS3 SD card without formatting it."""
    global sd_ready

    try:
        stats = os.statvfs(SD_MOUNT)
    except Exception:
        try:
            from hardware import sdcard

            sdcard.SDCard(
                slot=SD_SLOT,
                width=1,
                sck=SD_SCK,
                miso=SD_MISO,
                mosi=SD_MOSI,
                cs=SD_CS,
                freq=SD_FREQ,
            )
            stats = os.statvfs(SD_MOUNT)
        except Exception as e:
            sd_ready = False
            log("sd: mount failed %r" % e)
            return False

    unit = stats[1] or stats[0]
    total = unit * stats[2]
    free = unit * stats[3]
    if not sd_ready:
        log("sd: ready total=%d MiB free=%d MiB" % (total // 1048576, free // 1048576))
    sd_ready = True
    return True


def ensure_directory(path):
    """Create each component of an absolute directory if it is missing."""
    current = ""
    for part in path.split("/"):
        if not part:
            continue
        current += "/" + part
        try:
            os.mkdir(current)
        except OSError:
            try:
                os.listdir(current)
            except Exception:
                raise


def valid_clock(epoch=None):
    """The firmware clock is useful only after UIFlow2 has synchronized it."""
    try:
        parts = time.localtime(time.time() if epoch is None else epoch)
        return 2024 <= parts[0] <= 2100
    except Exception:
        return False


def iso_timestamp(epoch, offset_minutes=0):
    """Format an epoch using an explicit offset; the device clock itself is UTC."""
    if epoch is None or not valid_clock(epoch):
        return None
    parts = time.localtime(epoch + offset_minutes * 60)
    if offset_minutes == 0:
        zone = "Z"
    else:
        sign = "+" if offset_minutes >= 0 else "-"
        minutes = abs(offset_minutes)
        zone = "%s%02d:%02d" % (sign, minutes // 60, minutes % 60)
    return "%04d-%02d-%02dT%02d:%02d:%02d%s" % (
        parts[0],
        parts[1],
        parts[2],
        parts[3],
        parts[4],
        parts[5],
        zone,
    )


def elapsed_timestamp(milliseconds):
    milliseconds = max(0, int(milliseconds))
    hours = milliseconds // 3600000
    milliseconds %= 3600000
    minutes = milliseconds // 60000
    milliseconds %= 60000
    seconds = milliseconds // 1000
    return "%02d:%02d:%02d.%03d" % (hours, minutes, seconds, milliseconds % 1000)


def transcript_extension():
    return ".jsonl" if CFG["transcript_format"] == "jsonl" else ".md"


def transcript_path_for_part(part):
    suffix = "" if part == 1 else "-part%02d" % part
    return transcript_session_base + suffix + transcript_extension()


def unique_transcript_base(stem):
    entries = os.listdir(SD_TRANSCRIPT_DIR)
    for number in range(1, 1000):
        suffix = "" if number == 1 else "-%03d" % number
        name = stem + suffix
        occupied = False
        for entry in entries:
            if entry in (name + ".md", name + ".jsonl") or (
                entry.startswith(name + "-part")
                and (entry.endswith(".md") or entry.endswith(".jsonl"))
            ):
                occupied = True
                break
        if not occupied:
            return SD_TRANSCRIPT_DIR + "/" + name
    raise OSError("too many transcript files with the same session name")


def write_transcript_bytes(payload):
    """Append one complete UTF-8 record, close it, and sync FAT immediately."""
    global transcript_session_path, transcript_part, sd_ready

    global sd_warning

    if transcript_session_path is None:
        return False
    started = time.ticks_ms()
    try:
        try:
            current_size = os.stat(transcript_session_path)[6]
        except OSError:
            current_size = 0
        if current_size and current_size + len(payload) > CFG["transcript_max_file_bytes"]:
            transcript_part += 1
            transcript_session_path = transcript_path_for_part(transcript_part)
            write_transcript_header()
        with open(transcript_session_path, "ab") as target:
            written = target.write(payload)
            target.flush()
        os.sync()
        if written != len(payload):
            raise OSError("short SD write")
        return True
    except Exception as e:
        elapsed = time.ticks_diff(time.ticks_ms(), started)
        log("sd: transcript write failed after %d ms: %r" % (elapsed, e))
        transcript_session_path = None
        sd_ready = False
        sd_warning = "SD ERR"
        set_status("SD save failed; listening", FG_ALERT)
        return False


def write_transcript_header():
    """Create a new session/rotation file without ever overwriting a prior one."""
    if CFG["transcript_format"] == "jsonl":
        metadata = {
            "type": "session",
            "v": 1,
            "session_id": transcript_session_base.rsplit("/", 1)[-1],
            "started_at_utc": iso_timestamp(transcript_session_epoch),
            "started_at_local": iso_timestamp(
                transcript_session_epoch, CFG["transcript_timezone_offset_minutes"]
            ),
            "timezone_offset_minutes": CFG["transcript_timezone_offset_minutes"],
            "part": transcript_part,
            "transcribe_model": CFG["transcribe_model"],
            "chat_model": CFG["chat_model"],
        }
        payload = (json.dumps(metadata) + "\n").encode()
    else:
        local = iso_timestamp(transcript_session_epoch, CFG["transcript_timezone_offset_minutes"])
        utc = iso_timestamp(transcript_session_epoch)
        payload = (
            "# M5Stack Translator Session\n\n"
            "- Session: `%s`\n"
            "- Started locally: `%s`\n"
            "- Started UTC: `%s`\n"
            "- Models: `%s` / `%s`\n"
            "- Part: %d\n\n"
            % (
                transcript_session_base.rsplit("/", 1)[-1],
                local or "clock unavailable",
                utc or "clock unavailable",
                CFG["transcribe_model"],
                CFG["chat_model"],
                transcript_part,
            )
        ).encode()
    last_error = None
    for attempt in range(1, 3):
        try:
            with open(transcript_session_path, "wb") as target:
                written = target.write(payload)
                target.flush()
            os.sync()
            if written != len(payload):
                raise OSError("short SD header write")
            return
        except OSError as e:
            last_error = e
            log("sd: header attempt %d/2 failed %r" % (attempt, e))
            if attempt < 2:
                time.sleep_ms(200)
    raise last_error


def start_transcript_session():
    """Prepare one optional per-listening-session transcript file."""
    global sd_ready, sd_warning
    global transcript_session_base, transcript_session_path
    global transcript_session_epoch, transcript_session_ticks
    global transcript_capture_epoch, transcript_capture_ticks
    global transcript_part, transcript_sequence

    transcript_session_base = None
    transcript_session_path = None
    transcript_session_epoch = None
    transcript_session_ticks = time.ticks_ms()
    transcript_capture_epoch = None
    transcript_capture_ticks = 0
    transcript_part = 1
    transcript_sequence = 0
    sd_warning = ""
    if not CFG["save_transcripts"]:
        return False
    if not ensure_sd():
        sd_warning = "SD ERR"
        set_status("SD unavailable; not saving", FG_ALERT)
        return False
    try:
        ensure_directory(SD_TRANSCRIPT_DIR)
        log("sd: transcript directory ready")
        epoch = time.time()
        if valid_clock(epoch):
            transcript_session_epoch = epoch
            local = time.localtime(epoch + CFG["transcript_timezone_offset_minutes"] * 60)
            stem = "%04d%02d%02d-%02d%02d%02d" % local[:6]
        else:
            stem = "undated-%010d" % transcript_session_ticks
        transcript_session_base = unique_transcript_base(stem)
        transcript_session_path = transcript_path_for_part(transcript_part)
        log("sd: creating transcript %s" % transcript_session_path)
        write_transcript_header()
        log("sd: transcript session %s" % transcript_session_path)
        return True
    except Exception as e:
        log("sd: transcript session failed %r" % e)
        sd_ready = False
        transcript_session_base = None
        transcript_session_path = None
        sd_warning = "SD ERR"
        set_status("SD unavailable; not saving", FG_ALERT)
        return False


def mark_transcript_capture_start():
    """Anchor sequence timing immediately before the first mic buffer queues."""
    global transcript_capture_epoch, transcript_capture_ticks

    if transcript_session_path is None:
        return
    transcript_capture_ticks = time.ticks_ms()
    epoch = time.time()
    transcript_capture_epoch = epoch if valid_clock(epoch) else None


def markdown_quote(text):
    return "\n".join("> " + line if line else ">" for line in text.split("\n"))


def save_transcript_chunk(recognized, start_seq, end_seq):
    """Persist one successful bilingual turn; failures remain nonfatal."""
    global transcript_sequence, transcript_session_path, sd_ready, sd_warning

    if transcript_session_path is None:
        return
    started = time.ticks_ms()
    try:
        text, src, target, _turn = recognized
        start_ms = start_seq * FRAME_MS
        end_ms = end_seq * FRAME_MS
        capture_epoch = transcript_capture_epoch
        if capture_epoch is not None:
            capture_epoch += start_ms // 1000
        transcript_sequence += 1
        record = {
            "type": "turn",
            "v": 1,
            "seq": transcript_sequence,
            "session_id": transcript_session_base.rsplit("/", 1)[-1],
            "captured_at_utc": iso_timestamp(capture_epoch),
            "captured_at_local": iso_timestamp(
                capture_epoch, CFG["transcript_timezone_offset_minutes"]
            ),
            "audio_start_ms": start_ms,
            "audio_end_ms": end_ms,
            "saved_elapsed_ms": time.ticks_diff(time.ticks_ms(), transcript_capture_ticks),
            "source_lang": src,
            "target_lang": target,
            "original": text,
            "translation": last_trans,
        }
        if CFG["transcript_format"] == "jsonl":
            payload = (json.dumps(record) + "\n").encode()
        else:
            stamp = record["captured_at_local"] or ("+" + elapsed_timestamp(start_ms))
            utc = record["captured_at_utc"] or "clock unavailable"
            payload = (
                "## %s\n\n"
                "- UTC: `%s`\n"
                "- Audio: `+%s` to `+%s`\n\n"
                "**Original (%s)**\n\n%s\n\n"
                "**Translation (%s)**\n\n%s\n\n"
                % (
                    stamp,
                    utc,
                    elapsed_timestamp(start_ms),
                    elapsed_timestamp(end_ms),
                    src,
                    markdown_quote(text),
                    last_trans_lang,
                    markdown_quote(last_trans),
                )
            ).encode()

        service_capture()
        if write_transcript_bytes(payload):
            elapsed = time.ticks_diff(time.ticks_ms(), started)
            log(
                "sd: saved turn=%d bytes=%d in %d ms" % (transcript_sequence, len(payload), elapsed)
            )
    except Exception as e:
        elapsed = time.ticks_diff(time.ticks_ms(), started)
        log("sd: optional save failed after %d ms: %r" % (elapsed, e))
        transcript_session_path = None
        sd_ready = False
        sd_warning = "SD ERR"
        set_status("SD save failed; listening", FG_ALERT)
    finally:
        service_capture()


def end_transcript_session():
    global transcript_session_base, transcript_session_path

    if transcript_session_path is not None:
        log("sd: transcript closed %s" % transcript_session_path)
    transcript_session_base = None
    transcript_session_path = None


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

# LVGL owns the display and touch driver. Direct M5.Lcd painting is avoided so
# touch scrolling, clipping, bidi ordering, and Arabic contextual shaping all
# use one renderer. Keep callback closures rooted: the binding stores a C-side
# callback pointer but MicroPython still needs the Python callable alive.
screen_mode = "main"
picker_slot = 0


def _color(value):
    return lv.color_hex(value)


def _style_panel(obj, color=BG, radius=0):
    obj.set_style_bg_color(_color(color), 0)
    obj.set_style_border_width(0, 0)
    obj.set_style_pad_all(0, 0)
    obj.set_style_radius(radius, 0)


def _label(parent, text, x, y, w, color=FG_TEXT, font=None):
    label = lv.label(parent)
    label.set_pos(x, y)
    label.set_width(w)
    label.set_long_mode(lv.label.LONG_MODE.WRAP)
    label.set_text(text)
    label.set_style_text_color(_color(color), 0)
    if font is not None:
        label.set_style_text_font(font, 0)
    return label


def _button(parent, text, x, y, w, h, callback, color=FG_PANEL):
    button = lv.button(parent)
    button.set_pos(x, y)
    button.set_size(w, h)
    button.set_style_bg_color(_color(color), 0)
    button.set_style_radius(7, 0)
    button.set_style_border_width(0, 0)
    button.set_style_pad_all(0, 0)
    label = lv.label(button)
    label.set_text(text)
    label.set_style_text_color(_color(FG_TEXT), 0)
    if font_label is not None:
        label.set_style_text_font(font_label, 0)
    label.center()
    button.add_event_cb(callback, lv.EVENT.CLICKED, None)
    return button, label


def _load_screen(screen, callbacks):
    global ui_screen, ui_callbacks
    old = ui_screen
    ui_screen = screen
    ui_callbacks = callbacks
    lv.screen_load(screen)
    if old is not None and old is not screen:
        try:
            old.delete_async()
        except Exception:
            pass


# LVGL rewrites Arabic to presentation forms before drawing, so a face that
# carries the base block can still be unable to draw a word. Cairo 3.130 is
# exactly that case: its cmap holds every final, initial, and medial form but
# none of the 36 isolated ones, and isolated position is constant in Arabic
# (word-initial alef, standalone waw, any letter after a non-joining one).
# Each one LVGL cannot resolve becomes an empty placeholder box.
#
# These are the isolated forms of alef, lam, meem, waw, yeh, and teh. A face
# that can draw all six can draw the rest; a face missing them is unusable for
# Arabic no matter what its symbol table says.
ARABIC_ISOLATED_PROBE = (0xFE8D, 0xFEDD, 0xFEE1, 0xFEED, 0xFEF1, 0xFE95)

# Complete Arabic shaping coverage, verified on device. Only U+06EF, a Persian
# letter, is absent. Used when the configured face fails the probe above.
ARABIC_FALLBACK_FONT = "font_dejavu_16_persian_hebrew"


def font_covers(font, codes):
    """True when `font` itself can supply every code point in `codes`.

    The struct callback takes the font as its first argument and reports the
    answer in its return value. `is_placeholder` is set later by
    `lv_font_get_glyph_dsc_internal()` once the whole fallback chain has been
    tried, so it is not the test to use here.

    The fallback chain cannot be repaired from Python. Generated fonts are
    `const lv_font_t` in flash, and assigning `.fallback` faults the board.
    """
    if font is None:
        return False
    try:
        dsc = lv.font_glyph_dsc_t()
        for code in codes:
            if not font.get_glyph_dsc(font, dsc, code, 0):
                return False
    except Exception:
        return False
    return True


def font_for_language(code, primary=False):
    script_font = {
        "ja": font_ja,
        "ko": font_ko,
        "zh": font_zh,
        "ar": font_ar_translation if primary else font_ar_source,
        "he": font_he_translation if primary else font_he_source,
        # The bundled Japanese face includes Cyrillic and covers horizontal
        # modern Mongolian without pretending to support vertical script.
        "mn": font_ja,
    }.get(code)
    if script_font is not None:
        return script_font
    return font_trans_en if primary else font_label


def style_language_label(label, code, primary=False):
    font = font_for_language(code, primary)
    if font is not None:
        label.set_style_text_font(font, 0)
    if language_profile(code)["direction"] == "rtl":
        label.set_style_base_dir(lv.BASE_DIR.RTL, 0)
        label.set_style_text_align(lv.TEXT_ALIGN.RIGHT, 0)
    else:
        label.set_style_base_dir(lv.BASE_DIR.LTR, 0)
        label.set_style_text_align(lv.TEXT_ALIGN.LEFT, 0)


def set_status(text, color=FG_STATUS):
    if ui_status is None:
        return
    try:
        ui_status.set_text(text)
        ui_status.set_style_text_color(_color(color), 0)
    except Exception:
        pass


def service_ui():
    """Advance LVGL on the main thread instead of MicroPython's tiny queue."""
    global ui_last_tick
    if not ui_last_tick:
        return
    now = time.ticks_ms()
    elapsed = time.ticks_diff(now, ui_last_tick)
    if elapsed <= 0:
        return
    ui_last_tick = now
    lv.tick_inc(elapsed)
    lv.task_handler()


def _set_running_widgets():
    if ui_run_label is not None:
        ui_run_label.set_text("STOP" if running else "START")
    if ui_pair_label is not None:
        a, b = configured_pair()
        ui_pair_label.set_text("%s  <->  %s" % (language_short(a), language_short(b)))


def _on_feed_scroll(event):
    global follow_latest, new_turns_while_scrolled
    if event.code != lv.EVENT.SCROLL or ui_programmatic_scroll:
        return
    follow_latest = ui_feed.get_scroll_bottom() <= 4
    if follow_latest:
        new_turns_while_scrolled = 0
    _update_live_button()


def _update_live_button():
    if ui_live_button is None:
        return
    if follow_latest:
        ui_live_button.add_flag(lv.obj.FLAG.HIDDEN)
    else:
        try:
            # UIFlow2's LVGL binding exposes lv_obj_remove_flag(), matching
            # LVGL 9.  Some generated examples use clear_flag(), but that
            # method is not present on the firmware shipped for CoreS3.
            ui_live_button.remove_flag(lv.obj.FLAG.HIDDEN)
        except AttributeError:
            # UIFlow2 2.5.1's shipped binding still exposes the LVGL 8 name.
            ui_live_button.remove_flag(lv.obj.FLAG.HIDDEN)
        ui_live_label.set_text(
            "LIVE" if not new_turns_while_scrolled else str(new_turns_while_scrolled)
        )


def go_live(_event=None):
    global follow_latest, new_turns_while_scrolled, ui_programmatic_scroll
    follow_latest = True
    new_turns_while_scrolled = 0
    if ui_cards:
        ui_programmatic_scroll = True
        ui_cards[-1].scroll_to_view(False)
        ui_programmatic_scroll = False
    _update_live_button()


def _on_run(_event):
    global ui_action
    ui_action = ("toggle_run",)


def _on_language(_event):
    global ui_action
    ui_action = ("languages",)


def _on_settings(_event):
    global ui_action
    ui_action = ("settings",)


def _on_exit(_event):
    """Latch an exit without doing teardown inside an LVGL callback."""
    global ui_action, exit_requested, stop_requested
    ui_action = ("exit",)
    exit_requested = True
    stop_requested = True


def begin_turn(text, source, target):
    global active_turn, feed_revision, new_turns_while_scrolled
    turn = {
        "source_text": text,
        "translation_text": "",
        "source": source,
        "target": target,
        "state": "translating",
    }
    turns.append(turn)
    limit = CFG.get("history_turns", 8)
    while len(turns) > limit:
        evicted = turns.pop(0)
        if ui_turn_views and ui_turn_views[0]["turn"] is evicted:
            view = ui_turn_views.pop(0)
            if ui_cards:
                ui_cards.pop(0)
            try:
                view["card"].delete()
            except Exception:
                pass
    active_turn = turn
    feed_revision += 1
    if not follow_latest:
        new_turns_while_scrolled += 1
    update_display()
    return turn


def complete_turn(turn, translation):
    global active_turn, feed_revision
    turn["translation_text"] = translation
    turn["state"] = "complete"
    if active_turn is turn:
        active_turn = None
    feed_revision += 1
    update_display()


def fail_turn(turn, message):
    global active_turn, feed_revision
    turn["translation_text"] = message
    turn["state"] = "error"
    if active_turn is turn:
        active_turn = None
    feed_revision += 1
    update_display()


def _turn_card(turn):
    card = lv.obj(ui_feed)
    card.set_width(W - 18)
    card.set_height(lv.SIZE_CONTENT)
    _style_panel(card, 0x171A1D, 8)
    card.set_flex_flow(lv.FLEX_FLOW.COLUMN)
    card.set_style_pad_top(5, 0)
    card.set_style_pad_bottom(7, 0)
    card.set_style_pad_left(8, 0)
    card.set_style_pad_right(8, 0)
    card.set_style_pad_row(5, 0)

    source = turn["source"]
    target = turn["target"]
    _label(
        card,
        "%s -> %s" % (language_short(source), language_short(target)),
        8,
        0,
        W - 42,
        FG_DIM,
        font_label,
    )
    source_label = _label(card, turn["source_text"], 8, 0, W - 42, FG_ORIG)
    style_language_label(source_label, source)

    translated = turn["translation_text"]
    if not translated:
        translated = "Translating..."
    trans_color = FG_ALERT if turn["state"] == "error" else FG_TRANS
    trans_label = _label(card, translated, 8, 0, W - 42, trans_color)
    if turn["translation_text"]:
        style_language_label(trans_label, target, True)
    elif font_label is not None:
        # The placeholder is Latin UI chrome. Avoid walking a multi-megabyte
        # CJK face before there is any translated CJK text to draw.
        trans_label.set_style_text_font(font_label, 0)
    return {
        "turn": turn,
        "card": card,
        "translation_label": trans_label,
        "painted_translation": translated,
        "painted_state": turn["state"],
    }


def update_display(force=False):
    global feed_painted_revision, ui_cards, ui_turn_views, ui_programmatic_scroll
    if screen_mode != "main" or ui_feed is None:
        return
    if not force and feed_painted_revision == feed_revision:
        return

    rebuild = force or len(ui_turn_views) > len(turns)
    if not rebuild:
        for index, view in enumerate(ui_turn_views):
            if view["turn"] is not turns[index]:
                rebuild = True
                break
    if rebuild:
        ui_feed.clean()
        ui_cards = []
        ui_turn_views = []

    if not turns and not ui_turn_views:
        hint = _label(ui_feed, "Choose languages, then tap START", 12, 46, W - 36, FG_DIM, font_ui)
        hint.set_style_text_align(lv.TEXT_ALIGN.CENTER, 0)
    else:
        if not ui_turn_views:
            ui_feed.clean()
        for turn in turns[len(ui_turn_views) :]:
            view = _turn_card(turn)
            ui_turn_views.append(view)
            ui_cards.append(view["card"])
        for view in ui_turn_views:
            turn = view["turn"]
            translated = turn["translation_text"] or "Translating..."
            if translated != view["painted_translation"] or turn["state"] != view["painted_state"]:
                view["translation_label"].set_text(translated)
                style_language_label(view["translation_label"], turn["target"], True)
                color = FG_ALERT if turn["state"] == "error" else FG_TRANS
                view["translation_label"].set_style_text_color(_color(color), 0)
                view["painted_translation"] = translated
                view["painted_state"] = turn["state"]
    feed_painted_revision = feed_revision
    if follow_latest and ui_cards:
        ui_programmatic_scroll = True
        ui_cards[-1].scroll_to_view(False)
        ui_programmatic_scroll = False
    _update_live_button()


def show_main_screen():
    global screen_mode, ui_feed, ui_status, ui_pair_label
    global ui_run_label, ui_live_button, ui_live_label, feed_painted_revision
    screen_mode = "main"
    callbacks = [_on_feed_scroll, _on_run, _on_language, _on_settings, _on_exit, go_live]
    screen = lv.obj()
    screen.set_size(W, H)
    _style_panel(screen)

    ui_status = _label(screen, "Ready", 6, 13, EXIT_BOX[0] - 12, FG_STATUS, font_label)
    exit_button, _ = _button(
        screen,
        "EXIT",
        EXIT_BOX[0],
        EXIT_BOX[1],
        EXIT_BOX[2],
        EXIT_BOX[3],
        _on_exit,
        0x5A2323,
    )
    exit_button.set_style_radius(0, 0)
    ui_feed = lv.obj(screen)
    ui_feed.set_pos(0, VIEW_Y)
    ui_feed.set_size(W, VIEW_H)
    _style_panel(ui_feed)
    ui_feed.set_scroll_dir(lv.DIR.VER)
    ui_feed.set_flex_flow(lv.FLEX_FLOW.COLUMN)
    ui_feed.set_style_pad_all(4, 0)
    ui_feed.set_style_pad_row(6, 0)
    ui_feed.add_event_cb(_on_feed_scroll, lv.EVENT.SCROLL, None)

    pair_button, ui_pair_label = _button(
        screen, "", LANGUAGE_BOX[0], LANGUAGE_BOX[1], LANGUAGE_BOX[2], LANGUAGE_BOX[3], _on_language
    )
    pair_button.set_style_radius(0, 0)
    run_button, ui_run_label = _button(
        screen, "START", RUN_BOX[0], RUN_BOX[1], RUN_BOX[2], RUN_BOX[3], _on_run, FG_ACTIVE
    )
    run_button.set_style_radius(0, 0)
    ui_live_button, ui_live_label = _button(
        screen, "LIVE", LIVE_BOX[0], LIVE_BOX[1], LIVE_BOX[2], LIVE_BOX[3], go_live, 0x225E45
    )
    ui_live_button.set_style_radius(0, 0)
    settings_button, _ = _button(
        screen,
        "SET",
        GEAR_HIT_BOX[0],
        GEAR_HIT_BOX[1],
        GEAR_HIT_BOX[2],
        GEAR_HIT_BOX[3],
        _on_settings,
    )
    settings_button.set_style_radius(0, 0)
    _load_screen(screen, callbacks)
    _set_running_widgets()
    feed_painted_revision = -1
    update_display(True)


def _select_picker_slot(slot):
    global ui_action
    ui_action = ("picker_slot", slot)


def _select_language(code):
    global ui_action
    ui_action = ("pick_language", code)


def _done_language(_event=None):
    global ui_action
    ui_action = ("main",)


def language_page():
    global screen_mode, ui_status, ui_feed, ui_pair_label, ui_run_label
    global ui_live_button, ui_live_label
    screen_mode = "language"
    ui_feed = ui_pair_label = ui_run_label = ui_live_button = ui_live_label = None
    callbacks = []
    screen = lv.obj()
    screen.set_size(W, H)
    _style_panel(screen)
    ui_status = _label(
        screen, "Choose language %s" % (picker_slot + 1), 8, 8, 160, FG_STATUS, font_label
    )

    for slot, x in ((0, 174), (1, 248)):

        def choose_slot(_event, selected=slot):
            _select_picker_slot(selected)

        callbacks.append(choose_slot)
        color = FG_ACTIVE if slot == picker_slot else FG_PANEL
        _button(screen, language_short(configured_pair()[slot]), x, 0, 72, 44, choose_slot, color)

    positions = ((4, 48), (108, 48), (212, 48), (4, 94), (108, 94), (212, 94), (4, 140))
    for code, pos in zip(LANGUAGE_CODES, positions):

        def choose_language(_event, selected=code):
            _select_language(selected)

        callbacks.append(choose_language)
        color = FG_ACTIVE if code in configured_pair() else FG_PANEL
        _button(
            screen, LANGUAGE_PROFILES[code]["name"], pos[0], pos[1], 100, 44, choose_language, color
        )

    callbacks.append(_done_language)
    _button(screen, "DONE", 108, 140, 204, 44, _done_language, 0x225E45)
    rtl_ready = (
        rtl_firmware_abi == 2
        and font_ar_source is not None
        and font_ar_translation is not None
        and font_he_source is not None
        and font_he_translation is not None
    )
    note = "RTL firmware: OK" if rtl_ready else "Arabic/Hebrew need RTL firmware"
    _label(screen, note, 8, 198, W - 16, FG_DIM, font_label)
    _load_screen(screen, callbacks)


SETTINGS_ROWS = (
    ("Gate", "gate_dbfs", " dB", GATE_MIN, GATE_MAX),
    ("Chunk", "chunk_seconds", " s", CHUNK_MIN, CHUNK_MAX),
    ("Gain", "mic_gain", "x", GAIN_MIN, GAIN_MAX),
)


def _change_setting(key, delta, lo, hi):
    global ui_action
    ui_action = ("setting", key, delta, lo, hi)


def _toggle_sd(_event=None):
    global ui_action
    ui_action = ("toggle_sd",)


def settings_page():
    global screen_mode, ui_status, ui_feed, ui_pair_label, ui_run_label
    global ui_live_button, ui_live_label
    screen_mode = "settings"
    ui_feed = ui_pair_label = ui_run_label = ui_live_button = ui_live_label = None
    callbacks = [_done_language, _toggle_sd]
    screen = lv.obj()
    screen.set_size(W, H)
    _style_panel(screen)
    ui_status = _label(screen, "Audio and storage", 8, 12, 210, FG_STATUS, font_label)
    _button(screen, "BACK", 228, 0, 92, 44, _done_language, FG_PANEL)

    for index, row in enumerate(SETTINGS_ROWS):
        label, key, unit, lo, hi = row
        y = 44 + index * 44
        _label(screen, "%s  %d%s" % (label, CFG[key], unit), 10, y + 13, 210, FG_TEXT, font_ui)

        def down(_event, k=key, low=lo, high=hi):
            _change_setting(k, -1, low, high)

        def up(_event, k=key, low=lo, high=hi):
            _change_setting(k, 1, low, high)

        callbacks.extend((down, up))
        _button(screen, "-", 228, y, 44, 44, down)
        _button(screen, "+", 276, y, 44, 44, up)

    sd_color = 0x225E45 if CFG["save_transcripts"] else FG_PANEL
    _button(
        screen,
        "SD TRANSCRIPT  " + ("ON" if CFG["save_transcripts"] else "OFF"),
        8,
        181,
        304,
        52,
        _toggle_sd,
        sd_color,
    )
    _load_screen(screen, callbacks)


def process_ui_action():
    """Apply one LVGL request on the main thread, outside event callbacks."""
    global ui_action, running, stop_requested, fatal
    global settings_requested, language_requested, picker_slot

    action = ui_action
    if action is None:
        return False
    ui_action = None
    kind = action[0]
    if kind == "exit":
        running = False
        stop_requested = True
        set_status("Exiting...", FG_DIM)
    elif kind == "toggle_run":
        if running:
            running = False
            stop_requested = True
            set_status("Stopping...", FG_DIM)
        else:
            error = pair_render_error()
            if error:
                set_status(error, FG_ALERT)
            elif screen_mode == "main":
                fatal = ""
                running = True
                set_status("Starting...")
        _set_running_widgets()
        log("ui: running=%s" % running)
    elif kind in ("settings", "languages"):
        if kind == "settings":
            settings_requested = True
        else:
            language_requested = True
        if running:
            running = False
            stop_requested = True
            set_status("Opening %s..." % kind, FG_DIM)
        elif kind == "settings":
            settings_requested = False
            settings_page()
        else:
            language_requested = False
            language_page()
    elif kind == "picker_slot":
        picker_slot = action[1]
        language_page()
    elif kind == "pick_language":
        pair = list(configured_pair())
        other = 1 - picker_slot
        if pair[other] == action[1]:
            pair[other] = pair[picker_slot]
        pair[picker_slot] = action[1]
        CFG["language_pair"] = pair
        picker_slot = other
        save_config()
        language_page()
    elif kind == "setting":
        key, delta, lo, hi = action[1:]
        value = CFG[key] + delta
        if lo <= value <= hi:
            CFG[key] = value
            if key == "mic_gain":
                configure_mic()
            save_config()
        settings_page()
    elif kind == "toggle_sd":
        CFG["save_transcripts"] = not CFG["save_transcripts"]
        save_config()
        settings_page()
    elif kind == "main":
        show_main_screen()
        error = pair_render_error()
        set_status(error if error else "Languages ready", FG_ALERT if error else FG_STATUS)
    return True


# ------------------------------------------------------------------ audio

# M5.Mic's FIFO is two slots deep and cannot be made deeper. Capture is
# therefore a stream of short frames that the pump folds into a ring, and an
# utterance ends when the talker stops rather than when a wall clock expires.
# One second per frame keeps the endpointer responsive while still leaving two
# seconds of runway in the FIFO if the UI thread stalls on a repaint.
# A 500 ms buffer keeps one second of native FIFO runway while cutting the
# endpoint's observation delay in half versus the old one-second buffers.
FRAME_MS = 500
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
# Absorb cloud jitter without dropping the oldest untranslated speech. The S3
# has ample PSRAM; ten seconds costs 320 KB of mono PCM at 16 kHz.
RING_BACKLOG_MS = 10000

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


def normalized_mic_db(value):
    """Keep the existing gate/meter scale after moving gain into native I2S."""
    gain = int(CFG["mic_gain"])
    if value <= -99.0 or gain <= 1:
        return value
    return value - 20.0 * math.log(gain, 10)


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
        # M5Unified applies this inside its native I2S task. Base 2 preserves
        # the old raw capture; multiplying here replaces the 86-175 ms Python
        # gain pass without changing the uploaded signal level.
        magnification=2 * int(CFG["mic_gain"]),
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
        service_ui()
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
    zero. Sampling one quarter of the PCM costs roughly 4000 iterations per
    second regardless of the capture frame size.
    """
    stride = 2 * LEVEL_STEP
    gain = int(CFG["mic_gain"])
    gain_db = 20.0 * math.log(gain, 10) if gain > 1 else 0.0
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
        rms_value = (dbfs(math.sqrt(total / n)) if n else -99.0) - gain_db
        peak_value = dbfs(peak) - gain_db
        rms_out[w] = rms_value if rms_value > -99.0 else -99.0
        peak_out[w] = peak_value if peak_value > -99.0 else -99.0
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


def prepare_capture_storage():
    """Allocate reusable PSRAM storage before the interactive UI starts."""
    global capture_buffers, capture_views, ring_rms, ring_peak
    global ring_slots, max_frames, upload_pool

    wanted_max = utterance_max_seconds() * 1000 // FRAME_MS
    wanted_slots = wanted_max + RING_BACKLOG_MS // FRAME_MS
    if (
        capture_buffers is not None
        and ring_slots == wanted_slots
        and max_frames == wanted_max
        and upload_pool is not None
    ):
        return
    capture_buffers = [bytearray(FRAME_BYTES) for _ in range(wanted_slots)]
    capture_views = [memoryview(buf) for buf in capture_buffers]
    ring_rms = [[-99.0] * WINS_PER_FRAME for _ in range(wanted_slots)]
    ring_peak = [[-99.0] * WINS_PER_FRAME for _ in range(wanted_slots)]
    upload_pool = [bytearray(wanted_max * FRAME_BYTES) for _ in range(UPLOAD_POOL)]
    ring_slots = wanted_slots
    max_frames = wanted_max
    log(
        "pipeline: prepared ring %d x %d bytes, upload %d x %d bytes"
        % (ring_slots, FRAME_BYTES, UPLOAD_POOL, max_frames * FRAME_BYTES)
    )


def begin_capture():
    """Reset the reusable frame ring and hand M5.Mic its first two slots."""
    global upload_slot
    global head_seq, tail_seq, open_seq, pending_ends
    global speech_windows, silence_run
    global pump_armed, pump_busy, pump_last_ms
    global starved_logged, stalled_logged, overflow_logged, pump_error_logged

    prepare_capture_storage()
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
        "pipeline: armed ring %d x %d bytes, upload %d x %d bytes, utterance max %d s"
        % (
            ring_slots,
            FRAME_BYTES,
            UPLOAD_POOL,
            max_frames * FRAME_BYTES,
            max_frames * FRAME_MS // 1000,
        )
    )
    mark_transcript_capture_start()
    queue_capture(capture_buffers[0])
    queue_capture(capture_buffers[1])
    pump_armed = True


def end_capture():
    """Stop requeueing and let the two raw pointers finish.

    M5.Mic.end() waits for an in flight buffer instead of cancelling it, and
    the FIFO stores raw pointers, so every ring slot has to stay rooted until
    isRecording() reaches zero. The storage stays allocated for the next run.
    """
    global pending_ends, pump_armed

    pump_armed = False
    if M5.Mic.isRecording():
        set_status("Finishing queued audio...", FG_DIM)
        drain_captures()
    pending_ends = None
    if _log_rotation_deferred:
        _rotate_log()


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


class BodyParts:
    """Deferred HTTP body used to keep large PCM copies off the capture loop."""

    def __init__(self, parts):
        self.parts = tuple(parts)
        self.length = sum(len(part) for part in self.parts)


def body_length(body):
    return body.length if isinstance(body, BodyParts) else len(body) if body else 0


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

    def _send_body(self, body):
        if isinstance(body, BodyParts):
            for part in body.parts:
                self._send_all(part)
        elif body:
            self._send_all(body)

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
        length = body_length(body)
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
                self._send_body(body)
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
    global running, stop_requested

    M5.update()
    service_ui()
    service_capture()
    process_ui_action()
    power = BtnPWR.wasClicked()
    if not power and not stop_requested:
        return False

    running = False
    stop_requested = False
    if exit_requested:
        set_status("Exiting...", FG_DIM)
    elif not settings_requested and not language_requested:
        set_status("Stopping...", FG_DIM)
    log("ui: stop requested")
    _set_running_widgets()
    return True


def _requests2_post(url, kw):
    """requests2 fallback; materialize deferred body parts on the worker."""
    data = kw.get("data")
    if isinstance(data, BodyParts):
        kw = dict(kw)
        kw["data"] = join_exact(data.parts)
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
        try:
            r.close()
        except Exception:
            pass
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


# The embedded Cairo and Noto Sans Hebrew subsets stop at U+007E on the Latin
# side, so every typographic character the models emit has no glyph. LVGL then
# takes the placeholder path in lv_font_get_glyph_dsc_internal(), which sets
# box_w to half the line height and draws an empty rectangle. A curly
# apostrophe inside an English contraction therefore shows up as a box between
# two letters. Widening the generated ranges would be the other fix, but the
# application partition has under 50 KB of headroom, so fold these onto the
# ASCII the subsets already carry instead.
GLYPH_SUBSTITUTIONS = {
    0x2018: "'",
    0x2019: "'",
    0x201A: "'",
    0x201B: "'",
    0x201C: '"',
    0x201D: '"',
    0x201E: '"',
    0x201F: '"',
    0x2039: "'",
    0x203A: "'",
    0x00AB: '"',
    0x00BB: '"',
    0x2010: "-",
    0x2011: "-",
    0x2012: "-",
    0x2013: "-",
    0x2014: "-",
    0x2015: "-",
    0x2212: "-",
    0x2026: "...",
    0x2022: "*",
    0x00B7: ".",
    0x2027: ".",
    0x00A0: " ",
    0x202F: " ",
    0x2007: " ",
    0x2008: " ",
    0x2009: " ",
    0x200A: " ",
    0x2005: " ",
    0x2003: " ",
    0x2002: " ",
    0x00B0: " ",
    0x2032: "'",
    0x2033: '"',
}

# Invisible formatting codes. LVGL has no glyph for any of them, so each one
# would also draw a box. They are dropped rather than substituted because the
# app already keeps canonical logical-order text in Python and lets LVGL do the
# BiDi run resolution, so an explicit direction override is not wanted here.
GLYPH_DROPPED = frozenset(
    (
        0x061C,
        0x200B,
        0x200C,
        0x200D,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
        0xFEFF,
    )
)


def renderable_text(text):
    """Fold model output onto code points the embedded subsets can draw.

    Anything left unmapped is passed through unchanged, so Arabic, Hebrew, and
    CJK are untouched. Only characters that would otherwise render as an empty
    placeholder box are rewritten.
    """
    if not text:
        return text
    out = []
    changed = False
    for ch in text:
        code = ord(ch)
        if code < 0x80:
            out.append(ch)
            continue
        if code in GLYPH_DROPPED:
            changed = True
            continue
        sub = GLYPH_SUBSTITUTIONS.get(code)
        if sub is None:
            out.append(ch)
        else:
            out.append(sub)
            changed = True
    return "".join(out) if changed else text


def transcribe(pcm):
    """Upload PCM as a multipart WAV. Returns (text, language_code_or_empty).

    The WAV header goes straight into the body rather than building a separate
    WAV first, which avoids a second full sized copy of the audio.
    """
    model = CFG["transcribe_model"]
    fields = [("model", model), ("response_format", "json")]
    pair = configured_pair()
    if model.startswith("gpt-4o-"):
        fields.append(
            (
                "prompt",
                "Speech is in %s or %s. Transcribe verbatim in the original "
                "language and writing system; do not translate. Context: %s."
                % (
                    language_profile(pair[0])["name"],
                    language_profile(pair[1])["name"],
                    CFG["domain_context"],
                ),
            )
        )
    if model == "gpt-transcribe":
        # These current fields are specific to gpt-transcribe; the faster mini
        # model rejects them and relies on automatic language detection.
        for code in pair:
            fields.append(("languages[]", code))
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
    body = BodyParts((head, pcm, ("\r\n--" + BOUNDARY + "--\r\n").encode()))

    log("stt: POST %d bytes to %s" % (body.length, model))
    r = post_with_retry(
        "stt",
        lambda: do_post(
            CFG["transcribe_url"],
            data=body,
            headers=auth_headers({"Content-Type": "multipart/form-data; boundary=" + BOUNDARY}),
        ),
    )
    try:
        data = r.json()
    finally:
        r.close()
    langs = data.get("languages") or []
    code = langs[0].get("code", "") if langs else ""
    return renderable_text(data.get("text", "")), code


def detect_source(text):
    """Compatibility helper returning only the configured source route."""
    return resolve_route(text)[0] or configured_pair()[0]


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


def translate(text, src, tgt):
    src_name = language_profile(src)["name"]
    tgt_name = language_profile(tgt)["name"]
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
                    "Live multilingual interpreter for %s. Translate %s to %s. Use "
                    "canonical spellings: %s. Keep project names, standards, "
                    "formats, APIs, URLs, numbers, and acronyms accurate. "
                    "Preserve the natural writing direction and punctuation "
                    "of the target language. Resolve explicit "
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
    try:
        return renderable_text(r.json()["choices"][0]["message"]["content"])
    finally:
        r.close()


# ------------------------------------------------------------------ pipeline


def listening_status(held=0):
    text = "Listening"
    if held:
        text += "  %d s" % held
    if transcript_session_path is not None:
        text += " + SD"
    elif sd_warning:
        text += " / " + sd_warning
    return text


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
        held = (tail_seq - open_seq) * FRAME_MS // 1000 if speech_windows else 0
        if held != shown:
            shown = held
            set_status(listening_status(held))
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

    log("rec: trimmed %d to %d bytes, %.1fs on the wire" % (total, n, n / (SAMPLE_RATE * 2.0)))
    return memoryview(buf)[:n]


def recognize(pcm):
    """Transcribe a stable PCM copy, returning (text, source, target) or None."""
    global last_orig, last_src, last_trans, last_trans_lang

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

    # Script is decisive when possible; API metadata resolves ambiguous Han
    # and Latin text. Never coerce a clearly out-of-pair language.
    src, tgt = resolve_route(text, lang)
    if not src:
        detected = script_language(text) or normalize_language_code(lang) or "other"
        set_status("Detected %s; change pair" % detected.upper(), FG_ALERT)
        log("stt: rejected out-of-pair source=%s api=%r" % (detected, lang))
        return None
    if CFG["debug_content_logs"]:
        log("stt: [%s, api=%r] %s" % (src, lang, text))
    else:
        log("stt: source=%s api=%r chars=%d" % (src, lang, len(text)))
    last_src = src
    last_orig = text
    last_trans = ""
    last_trans_lang = tgt
    turn = begin_turn(text, src, tgt)
    set_status("%s to %s" % (language_short(src), language_short(tgt)))
    return text, src, tgt, turn


def finish_translation(recognized):
    """Translate recognized text while both microphone queue slots run."""
    global last_trans, last_trans_lang

    text, src, tgt, turn = recognized
    try:
        last_trans = translate(text, src, tgt)
    except Exception:
        fail_turn(turn, "Translation failed")
        raise
    last_trans_lang = tgt
    if CFG["debug_content_logs"]:
        log("tt: %s" % last_trans)
    else:
        log("tt: target=%s chars=%d" % (last_trans_lang, len(last_trans)))
    complete_turn(turn, last_trans)
    gc.collect()


def run_session():
    """Upload endpointed utterances while the pump keeps the mic ring full."""
    global head_seq

    if M5.Mic.isRecording():
        drain_captures()

    try:
        start_transcript_session()
        # Inside the try so a rejected first queue still reaches end_capture,
        # which is the only thing that drains M5.Mic's raw pointers.
        begin_capture()
        set_status(listening_status())
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
                save_transcript_chunk(recognized, start_seq, end_seq)
            gc.collect()
    finally:
        try:
            end_capture()
        finally:
            end_transcript_session()


# ------------------------------------------------------------------ lifecycle


def probe_channels():
    """Start the persistent microphone task and confirm both physical mics."""
    configure_mic()
    probe = bytearray(SAMPLE_RATE * 2 * 2)
    try:
        if not M5.Mic.record(probe, SAMPLE_RATE, True):
            raise RuntimeError("stereo queue failed")
        while M5.Mic.isRecording():
            M5.update()
            service_ui()
            time.sleep_ms(20)
        d1 = normalized_mic_db(channel_dbfs(probe, 0, 2))
        d2 = normalized_mic_db(channel_dbfs(probe, 1, 2))
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
    global font_ja, font_ko, font_zh, rtl_firmware_abi
    global font_ar_source, font_ar_translation, font_he_source, font_he_translation
    global font_label, font_source_en, font_trans_en, font_ui
    global ui_port_loop, ui_last_tick
    log("boot: translator starting")
    load_config()
    # Allocate the large ring before LVGL's timer begins scheduling callbacks.
    # Session start then only resets indices and queues the first two frames.
    prepare_capture_storage()
    # m5ui.init() calls M5.begin() and installs the LVGL display/touch port.
    # Calling M5.begin() separately would initialize the board twice.
    m5ui.init()
    try:
        # UIFlow's timer raises "schedule queue full" whenever a long hardware
        # call keeps MicroPython busy. Stop only that timer; service_ui() owns
        # LVGL ticks and task handling in the same loops that pump touch/mic.
        ui_port_loop = m5ui.event_loop.get_instance()
        ui_port_loop.timer.deinit()
    except Exception:
        ui_port_loop = None
    ui_last_tick = time.ticks_ms()
    try:
        font_ja = getattr(lv, "AlibabaSans_JP24", None)
        font_ko = getattr(lv, "AlibabaSans_KR24", None)
        font_zh = getattr(lv, "AlibabaPuHuiTi_CN24", None)
        font_ui = lv.font_montserrat_16
        font_label = lv.font_montserrat_14
        font_source_en = lv.font_montserrat_16
        font_trans_en = lv.font_montserrat_24
        try:
            import translator_rtl

            rtl_firmware_abi = translator_rtl.ABI_VERSION
            if translator_rtl.BIDI and translator_rtl.ARABIC_SHAPING:
                font_ar_source = getattr(lv, translator_rtl.ARABIC_SOURCE_FONT, None)
                font_ar_translation = getattr(lv, translator_rtl.ARABIC_TRANSLATION_FONT, None)
                font_he_source = getattr(lv, translator_rtl.HEBREW_SOURCE_FONT, None)
                font_he_translation = getattr(lv, translator_rtl.HEBREW_TRANSLATION_FONT, None)
                # Prove the configured Arabic faces by asking for the shaped
                # glyphs LVGL will actually request, not by their presence.
                # Substituting a complete face keeps Arabic legible at the
                # cost of one size and a plainer look, which beats a line of
                # placeholder boxes. When the firmware ships a face that
                # passes, this check stops substituting on its own.
                if not font_covers(font_ar_source, ARABIC_ISOLATED_PROBE) or not font_covers(
                    font_ar_translation, ARABIC_ISOLATED_PROBE
                ):
                    spare = getattr(lv, ARABIC_FALLBACK_FONT, None)
                    if font_covers(spare, ARABIC_ISOLATED_PROBE):
                        log(
                            "fonts: %s lacks Arabic isolated forms, using %s"
                            % (translator_rtl.ARABIC_SOURCE_FONT, ARABIC_FALLBACK_FONT)
                        )
                        font_ar_source = spare
                        font_ar_translation = spare
                    else:
                        log("fonts: no Arabic face covers the isolated forms")
        except Exception:
            rtl_firmware_abi = 0
            font_ar_source = None
            font_ar_translation = None
            font_he_source = None
            font_he_translation = None
    except Exception as e:
        log("fonts: %r" % e)

    try:
        import esp32

        log("fw: %s" % esp32.firmware_info()[3])
    except Exception:
        pass

    show_main_screen()
    set_status(config_error or "Booting...", FG_ALERT if config_error else FG_STATUS)

    # Join at boot rather than lazily on the first POST. A dead link should
    # say so on screen, not surface later as an opaque 45 second timeout.
    set_status("Checking Wi-Fi...")
    online = ensure_wifi()
    if exit_requested:
        return
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

    if online:
        error = pair_render_error()
        set_status(error if error else "Ready", FG_ALERT if error else FG_STATUS)
    else:
        set_status("No Wi-Fi, START to retry", FG_ALERT)


def loop():
    global running, fatal, settings_requested, language_requested, stop_requested
    M5.update()
    service_ui()
    process_ui_action()

    if BtnPWR.wasClicked():
        if running:
            stop_requested = True
            running = False
            set_status("Stopping...", FG_DIM)
        elif screen_mode == "main":
            error = pair_render_error()
            if error:
                set_status(error, FG_ALERT)
            else:
                fatal = ""
                running = True
                set_status("Starting...")
        _set_running_widgets()

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
        if not settings_requested and not language_requested:
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
    elif language_requested:
        language_requested = False
        language_page()
    else:
        _set_running_widgets()


def run():
    global running
    setup_ok = True
    try:
        setup()
    except Exception as e:
        log_exc(e)
        set_status("SETUP FAILED: %s" % e, FG_ALERT)
        setup_ok = False
    try:
        while setup_ok and not exit_requested:
            loop()
            time.sleep_ms(20)
    except KeyboardInterrupt:
        log("app: interrupted, cleaning up")
    finally:
        running = False
        try:
            if pump_armed or M5.Mic.isRecording():
                end_capture()
        except Exception as e:
            log("app: mic cleanup failed %r" % e)
        try:
            end_transcript_session()
        except Exception:
            pass
        try:
            if ui_port_loop is not None:
                ui_port_loop.scheduled = 0
            M5.Lcd.lvgl_deinit()
            lv.mp_lv_deinit_gc()
        except Exception as e:
            log("app: LVGL cleanup failed %r" % e)
    if exit_requested:
        try:
            import esp32

            settings = esp32.NVS("uiflow")
            settings.set_u8("boot_option", 1)
            settings.commit()
        except Exception as e:
            log("app: could not select UIFlow launcher %r" % e)
        log("app: exit requested; restarting into UIFlow2")
        _close_log()
        time.sleep_ms(200)
        import machine

        machine.reset()


run()
