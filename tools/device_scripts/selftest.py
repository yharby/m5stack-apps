"""On-device end-to-end self test: config -> wifi -> mic -> Whisper -> GPT.

Run with:  make selftest      (uses mpremote run, output streams to the host)
Verifies the real network path with the real key; prints no secrets.
"""

import json
import struct
import time

import M5
import network
import requests2
from audio import Recorder

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
if not w.isconnected() and CFG.get("wifi_ssid"):
    w.connect(CFG["wifi_ssid"], CFG.get("wifi_pass", ""))
    for _ in range(60):
        if w.isconnected():
            break
        time.sleep_ms(300)
print("wifi: connected:", w.isconnected(), w.ifconfig()[0] if w.isconnected() else "")

# ---- 1. chat completions (cheapest check of key + TLS + JSON) --------------
print("\n[1/3] chat completions ...")
r = requests2.post(
    CFG.get("chat_url", "https://api.openai.com/v1/chat/completions"),
    json={
        "model": CFG.get("chat_model", "gpt-4o-mini"),
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Translate the user's English text into Japanese. "
                    "Reply with only the translation."
                ),
            },
            {"role": "user", "content": "Where is the train station?"},
        ],
    },
    headers={"Authorization": "Bearer " + key},
)
print("     status:", r.status_code)
if r.status_code == 200:
    print("     translation:", r.json()["choices"][0]["message"]["content"])
else:
    print("     body:", r.text[:300])

# ---- 2. microphone ---------------------------------------------------------
print("\n[2/3] recording", SECONDS, "s - SAY SOMETHING IN ENGLISH NOW ...")
M5.begin()
rec = Recorder(SAMPLE_RATE, 16, False)
pcm = rec.create_pcm_buf(SECONDS)
rec.record_into(pcm, sync=True)
n = len(pcm) // 2
acc = 0
cnt = 0
for i in range(0, n, 11):
    v = struct.unpack_from("<h", pcm, i * 2)[0]
    acc += v * v
    cnt += 1
rms = int((acc / cnt) ** 0.5)
print("     captured", len(pcm), "bytes, rms =", rms)

# ---- 3. whisper multipart upload ------------------------------------------
print("\n[3/3] whisper transcription ...")
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

pre = (
    "--" + BOUNDARY + "\r\n"
    'Content-Disposition: form-data; name="model"\r\n\r\n'
    + CFG.get("whisper_model", "whisper-1")
    + "\r\n"
    "--" + BOUNDARY + "\r\n"
    'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
    "Content-Type: audio/wav\r\n\r\n"
).encode()
body = bytearray(pre)
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
)
print("     status:", r.status_code)
print("     result:", r.text[:300])
print("\nSELFTEST DONE")
