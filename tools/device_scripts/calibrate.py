"""Measure mic RMS in 1 s windows so the silence threshold is data, not a guess.

Stay quiet for the first few windows, then talk normally at arm's length.
"""

import struct

from audio import Recorder

rec = Recorder(16000, 16, False)
buf = rec.create_pcm_buf(1)

print("window  rms   peak  bar")
for i in range(12):
    rec.record_into(buf, sync=True)
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
print("DONE")
