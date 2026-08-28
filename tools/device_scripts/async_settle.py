"""Verify that asynchronous Recorder captures can restart after full cleanup.

Every capture is allowed to finish before the next ``record_into`` call.  The
firmware keeps the async task alive for another second after it clears the
pipeline pointer, so this probe also waits through that final task delay.

Progress is written to /flash/settle.log so the last safe point survives if
the firmware wedges.
"""

import time

import M5
from audio import Recorder

LOG_PATH = "/flash/settle.log"
SAMPLE_RATE = 16000
CAPTURE_SECONDS = 3
SETTLE_TIMEOUT_MS = 15000
TASK_EXIT_GRACE_MS = 1200
CYCLES = 4


def log(*parts):
    line = " ".join(str(part) for part in parts)
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def wait_until_settled(started_ms):
    """Return elapsed milliseconds when pipeline cleanup finishes, else -1."""
    while time.ticks_diff(time.ticks_ms(), started_ms) < SETTLE_TIMEOUT_MS:
        if not recorder.is_recording():
            return time.ticks_diff(time.ticks_ms(), started_ms)
        time.sleep_ms(50)
    return -1


with open(LOG_PATH, "w") as f:
    f.write("")

M5.begin()
recorder = Recorder(SAMPLE_RATE, 16, False)
buffers = [
    recorder.create_pcm_buf(CAPTURE_SECONDS),
    recorder.create_pcm_buf(CAPTURE_SECONDS),
]
log("recorder built; buffer bytes", len(buffers[0]))

completed = 0
for cycle in range(CYCLES):
    current = buffers[cycle % len(buffers)]
    log("=== cycle", cycle, "start ===")
    started = time.ticks_ms()
    recorder.record_into(current, sync=False)
    returned_ms = time.ticks_diff(time.ticks_ms(), started)
    log("  record_into returned in", returned_ms, "ms")

    settled_ms = wait_until_settled(started)
    log(
        "  expected fill at",
        CAPTURE_SECONDS * 1000,
        "ms; is_recording false at",
        settled_ms,
        "ms",
    )
    if settled_ms < 0:
        log("  cleanup did not finish; no restart attempted")
        break

    # async_record_task delays for 1000 ms after stop_helper clears pipeline,
    # then deletes itself. Do not let the next task overlap that final delay.
    time.sleep_ms(TASK_EXIT_GRACE_MS)
    nonzero = 0
    for offset in range(0, len(current), 512):
        if current[offset] or current[offset + 1]:
            nonzero += 1
    log("  task-exit grace complete; nonzero samples", nonzero)
    completed += 1

log("SETTLE TEST COMPLETE", completed, "of", CYCLES)
