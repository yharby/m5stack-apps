"""Measure mic RMS in 1 s windows so the silence threshold is data, not a guess.

Stay quiet for the first few windows, then talk normally at arm's length.
"""

import struct
import time

import M5

M5.begin()
M5.Mic.end()
M5.Mic.config(sample_rate=16000, magnification=2, task_pinned_core=0)
if not M5.Mic.begin():
    raise SystemExit("M5.Mic.begin failed")
buf = bytearray(32000)

print("window  rms   peak  bar")
for i in range(12):
    M5.Mic.record(buf, 16000, False)
    while M5.Mic.isRecording():
        time.sleep_ms(20)
    n = len(buf) // 2
    acc = 0
    peak = 0
    cnt = 0
    for j in range(0, n, 7):
        v = struct.unpack_from("<h", buf, j * 2)[0]
        acc += v * v
        cnt += 1
        av = v if v >= 0 else -v
        if av > peak:
            peak = av
    rms = int((acc / cnt) ** 0.5)
    bar = "#" * min(50, rms // 40)
    print("%4d  %6d %6d  %s" % (i, rms, peak, bar))
M5.Mic.end()
print("DONE")
