"""Exercise the CoreS3's persistent M5.Mic two-buffer recording queue.

Unlike audio.Recorder, M5.Mic keeps one I2S task alive and marks each queued
buffer complete without rebuilding the pipeline.  This probe continuously
keeps two three-second buffers queued for six captures, then ends and restarts
the microphone once.  Progress is flash logged for post-mortem diagnosis.
"""

import time

import M5

LOG_PATH = "/flash/m5mic.log"
SAMPLE_RATE = 16000
CAPTURE_SECONDS = 3
TOTAL_CAPTURES = 6
BYTES_PER_CAPTURE = SAMPLE_RATE * CAPTURE_SECONDS * 2
WAIT_TIMEOUT_MS = 10000


def log(*parts):
    line = " ".join(str(part) for part in parts)
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def sampled_nonzero(buf):
    count = 0
    for offset in range(0, len(buf), 512):
        if buf[offset] or buf[offset + 1]:
            count += 1
    return count


def queue(buf, label):
    started = time.ticks_ms()
    ok = M5.Mic.record(buf, SAMPLE_RATE, False)
    elapsed = time.ticks_diff(time.ticks_ms(), started)
    log("  queued", label, "ok", ok, "in", elapsed, "ms; depth", M5.Mic.isRecording())
    return ok


with open(LOG_PATH, "w") as f:
    f.write("")

M5.begin()
cfg = M5.Mic.config()
log(
    "config rate",
    cfg.sample_rate,
    "stereo",
    cfg.stereo,
    "magnification",
    cfg.magnification,
    "core",
    cfg.task_pinned_core,
    "i2s",
    cfg.i2s_port,
)

M5.Mic.end()
M5.Mic.config(sample_rate=SAMPLE_RATE)
log("begin", M5.Mic.begin(), "running", M5.Mic.isRunning())

buffers = [bytearray(BYTES_PER_CAPTURE), bytearray(BYTES_PER_CAPTURE)]
scheduled = 0
completed = 0
for index, buf in enumerate(buffers):
    if queue(buf, "AB"[index]):
        scheduled += 1

last_completion = time.ticks_ms()
while completed < TOTAL_CAPTURES:
    depth = M5.Mic.isRecording()
    expected_depth = scheduled - completed
    if depth < expected_depth:
        # Buffers complete FIFO, alternating A then B while both queue slots
        # remain occupied.
        finished_index = completed % len(buffers)
        elapsed = time.ticks_diff(time.ticks_ms(), last_completion)
        log(
            "completed",
            completed,
            "buffer",
            "AB"[finished_index],
            "after",
            elapsed,
            "ms; depth",
            depth,
            "nonzero",
            sampled_nonzero(buffers[finished_index]),
        )
        completed += 1
        last_completion = time.ticks_ms()
        if scheduled < TOTAL_CAPTURES and queue(buffers[finished_index], "AB"[finished_index]):
            scheduled += 1
        continue

    if time.ticks_diff(time.ticks_ms(), last_completion) > WAIT_TIMEOUT_MS:
        log("timeout; scheduled", scheduled, "completed", completed, "depth", depth)
        break
    time.sleep_ms(20)

log("ending mic; depth", M5.Mic.isRecording())
M5.Mic.end()
log("ended; running", M5.Mic.isRunning(), "depth", M5.Mic.isRecording())

# A clean end/begin cycle is required when settings change or a session stops.
short = bytearray(SAMPLE_RATE)
log("restart begin", M5.Mic.begin())
queue(short, "short")
deadline = time.ticks_add(time.ticks_ms(), 3000)
while M5.Mic.isRecording() and time.ticks_diff(deadline, time.ticks_ms()) > 0:
    time.sleep_ms(20)
log("restart capture depth", M5.Mic.isRecording(), "nonzero", sampled_nonzero(short))
M5.Mic.end()
log("M5 MIC TEST COMPLETE", completed, "of", TOTAL_CAPTURES)
