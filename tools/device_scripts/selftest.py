"""On-device end-to-end self test: config -> Wi-Fi -> chat -> mic -> STT.

Run with:  make selftest      (uses mpremote run, output streams to the host)
Verifies the real network path with the real key; prints no secrets.
"""

import json
import struct
import time

import M5
import network
import requests2

SAMPLE_RATE = 16000
SECONDS = 5
BOUNDARY = "----M5CoreS3TranslatorBoundary"

CFG = {}
for p in ("/flash/config.json", "/flash/res/config.json", "/flash/apps/config.json"):
    try:
        with open(p) as f:
            CFG = json.loads(f.read())
        print("config: loaded", p)
        break
    except Exception:
        continue

key = CFG.get("openai_api_key", "")
print("config: api_key present:", bool(key), "| length:", len(key))
if not key:
    raise SystemExit("no api key")

w = network.WLAN(network.STA_IF)
w.active(True)
wifi_candidates = []
try:
    import esp32

    settings = esp32.NVS("uiflow")
    try:
        net_mode = settings.get_str("net_mode")
    except OSError:
        net_mode = "WIFI"
    if not net_mode or net_mode == "WIFI":
        try:
            ui_ssid = settings.get_str("ssid0")
        except OSError:
            ui_ssid = ""
        try:
            ui_password = settings.get_str("pswd0")
        except OSError:
            ui_password = ""
        if ui_ssid:
            wifi_candidates.append(("UIFlow2", ui_ssid, ui_password))
except Exception as e:
    print("wifi: could not read UIFlow2 settings:", repr(e))

config_ssid = CFG.get("wifi_ssid", "")
config_password = CFG.get("wifi_pass", "")
if config_ssid and not any(
    ssid == config_ssid and password == config_password
    for _source, ssid, password in wifi_candidates
):
    wifi_candidates.append(("config", config_ssid, config_password))

if not w.isconnected():
    for source, ssid, password in wifi_candidates:
        print("wifi: trying", source, repr(ssid))
        try:
            w.disconnect()
        except Exception:
            pass
        try:
            if password:
                w.connect(ssid, password)
            else:
                w.connect(ssid)
        except Exception as e:
            print("wifi: connect call failed:", repr(e))
            continue
        for _ in range(60):
            if w.isconnected():
                break
            time.sleep_ms(300)
        if w.isconnected():
            break
print("wifi: connected:", w.isconnected(), w.ifconfig()[0] if w.isconnected() else "")

# ---- 1. chat completions (cheapest check of key + TLS + JSON) --------------
print("\n[1/3] chat completions ...")
chat_model = CFG.get("chat_model", "gpt-5.6-luna")
chat_payload = {
    "model": chat_model,
    "messages": [
        {
            "role": "system",
            "content": (
                "Translate Japanese to English for a FOSS4G/OSGeo audience. "
                "Preserve FOSS4G, STAC, and GeoParquet exactly. Output only translation."
            ),
        },
        # Non-ASCII is intentional: it verifies that requests2 receives an
        # explicit UTF-8 byte body and therefore sends a correct Content-Length.
        {"role": "user", "content": "FOSS4GでSTACとGeoParquetについて話します。"},
    ],
    "max_completion_tokens": 200,
}
if chat_model.startswith("gpt-5"):
    chat_payload["reasoning_effort"] = "none"
r = requests2.post(
    CFG.get("chat_url", "https://api.openai.com/v1/chat/completions"),
    data=json.dumps(chat_payload).encode(),
    headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    },
    timeout=45,
)
print("     status:", r.status_code)
if r.status_code == 200:
    print("     translation:", r.json()["choices"][0]["message"]["content"])
else:
    print("     body:", r.text[:300])

# ---- 2. microphone ---------------------------------------------------------
print("\n[2/3] recording", SECONDS, "s - SAY SOMETHING IN ENGLISH OR JAPANESE NOW ...")
M5.begin()
M5.Mic.end()
M5.Mic.config(sample_rate=SAMPLE_RATE, magnification=2, task_pinned_core=0)
if not M5.Mic.begin():
    raise SystemExit("M5.Mic.begin failed")
pcm = bytearray(SAMPLE_RATE * SECONDS * 2)
M5.Mic.record(pcm, SAMPLE_RATE, False)
while M5.Mic.isRecording():
    time.sleep_ms(20)
M5.Mic.end()
n = len(pcm) // 2
acc = 0
cnt = 0
for i in range(0, n, 11):
    v = struct.unpack_from("<h", pcm, i * 2)[0]
    acc += v * v
    cnt += 1
rms = int((acc / cnt) ** 0.5)
print("     captured", len(pcm), "bytes, rms =", rms)

# ---- 3. transcription multipart upload -----------------------------------
print("\n[3/3] transcription ...")
header = struct.pack(
    "<4sI4s4sIHHIIHH4sI",
    b"RIFF",
    36 + len(pcm),
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
    len(pcm),
)
wav = bytearray(header)
wav += pcm

model = CFG.get("transcribe_model", "gpt-transcribe")
fields = [("model", model), ("response_format", "json")]
if model == "gpt-transcribe":
    # Mirror translator.py. Only gpt-transcribe accepts these; every other
    # model rejects the whole request with HTTP 400 invalid_parameter.
    fields += [
        ("languages[]", "en"),
        ("languages[]", "ja"),
        ("keywords[]", "FOSS4G"),
        ("keywords[]", "OSGeo"),
        ("keywords[]", "STAC"),
        ("keywords[]", "GeoParquet"),
        ("keywords[]", "MapLibre"),
    ]

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

print("     uploading", len(body), "bytes ...")
r = requests2.post(
    CFG.get("transcribe_url", "https://api.openai.com/v1/audio/transcriptions"),
    data=body,
    headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "multipart/form-data; boundary=" + BOUNDARY,
    },
    timeout=45,
)
print("     status:", r.status_code)
print("     result:", r.text[:300])
print("\nSELFTEST DONE")
