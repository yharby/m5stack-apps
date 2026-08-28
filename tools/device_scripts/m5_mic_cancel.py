"""Verify that M5.Mic can cancel an in-flight buffer and restart cleanly."""

import time

import M5

SAMPLE_RATE = 16000

M5.begin()
M5.Mic.end()
M5.Mic.config(sample_rate=SAMPLE_RATE)
print("begin:", M5.Mic.begin())

long_buf = bytearray(SAMPLE_RATE * 5 * 2)
print("queue long:", M5.Mic.record(long_buf, SAMPLE_RATE, False))
time.sleep_ms(300)
started = time.ticks_ms()
M5.Mic.end()
print(
    "cancelled in:",
    time.ticks_diff(time.ticks_ms(), started),
    "ms; running:",
    M5.Mic.isRunning(),
    "depth:",
    M5.Mic.isRecording(),
)

short_buf = bytearray(SAMPLE_RATE)
print("restart begin:", M5.Mic.begin())
print("queue short:", M5.Mic.record(short_buf, SAMPLE_RATE, False))
deadline = time.ticks_add(time.ticks_ms(), 3000)
while M5.Mic.isRecording() and time.ticks_diff(deadline, time.ticks_ms()) > 0:
    time.sleep_ms(20)
print("restart depth:", M5.Mic.isRecording())
M5.Mic.end()
print("M5 MIC CANCEL TEST COMPLETE")
