"""Verify which OpenAI models this account can actually use from the device.

Research says gpt-transcribe + gpt-5.6-luna are current, but availability is
per-account, so probe before hardcoding. Prints status codes only, no secrets.
"""

import json
import struct

import network
import requests2
from audio import Recorder

CFG = {}
for p in ("/flash/config.json", "/flash/res/config.json"):
    try:
        with open(p) as f:
            CFG = json.loads(f.read())
        break
    except Exception:
        continue
key = CFG["openai_api_key"]
H = {"Authorization": "Bearer " + key}

w = network.WLAN(network.STA_IF)
w.active(True)
print("wifi:", w.isconnected())

print("\n=== chat models ===")
for model in ("gpt-5.6-luna", "gpt-5-nano", "gpt-4o-mini"):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Translate to Japanese. Output only the translation."},
            {"role": "user", "content": "Where is the train station?"},
        ],
        "max_completion_tokens": 200,
    }
    if model.startswith("gpt-5"):
        body["reasoning_effort"] = "none"
    try:
        r = requests2.post("https://api.openai.com/v1/chat/completions", json=body, headers=H)
        if r.status_code == 200:
            print("  %-14s 200  %s" % (model, r.json()["choices"][0]["message"]["content"]))
        else:
            print("  %-14s %d  %s" % (model, r.status_code, r.text[:120]))
    except Exception as e:
        print("  %-14s EXC %s" % (model, e))

print("\n=== transcription models ===")
rec = Recorder(16000, 16, False)
pcm = rec.create_pcm_buf(2)
rec.record_into(pcm, sync=True)
hdr = struct.pack(
    "<4sI4s4sIHHIIHH4sI",
    b"RIFF",
    36 + len(pcm),
    b"WAVE",
    b"fmt ",
    16,
    1,
    1,
    16000,
    32000,
    2,
    16,
    b"data",
    len(pcm),
)
wav = bytearray(hdr)
wav += pcm

B = "----M5Probe"


def upload(model, extra_fields):
    parts = bytearray()
    for name, val in [("model", model), *extra_fields]:
        parts += (
            "--" + B + "\r\n"
            'Content-Disposition: form-data; name="' + name + '"\r\n\r\n' + val + "\r\n"
        ).encode()
    parts += (
        "--" + B + "\r\n"
        'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()
    body = bytearray(parts)
    body += wav
    body += ("\r\n--" + B + "--\r\n").encode()
    hh = dict(H)
    hh["Content-Type"] = "multipart/form-data; boundary=" + B
    return requests2.post("https://api.openai.com/v1/audio/transcriptions", data=body, headers=hh)


for model, extra in (
    ("gpt-transcribe", [("languages[]", "en"), ("languages[]", "ja"), ("response_format", "json")]),
    ("gpt-4o-mini-transcribe", [("response_format", "json")]),
    ("whisper-1", [("response_format", "json")]),
):
    try:
        r = upload(model, extra)
        print("  %-22s %d  %s" % (model, r.status_code, r.text[:150]))
    except Exception as e:
        print("  %-22s EXC %s" % (model, e))

print("\nPROBE DONE")
