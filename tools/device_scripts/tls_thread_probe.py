"""Find the smallest _thread stack that survives a real TLS handshake.

thread_probe.py showed 32768 cannot be created on this board but 16384 can,
which means the translator's worker-thread POST path has never actually run.
Creating the thread is only half the answer: requests2's TLS handshake is the
stack-hungry part, and undersizing it panics the task instead of raising.

No API key is used or needed. An unauthenticated GET to the API host still
performs the full handshake, which is what we are measuring, and returns 401.

Progress is flushed to /flash after every step, per the CLAUDE.md wedge rule,
so if a size panics the board the log names the exact size that did it.
"""

import _thread
import gc
import time

import requests2

LOG = "/flash/tlsprobe.log"
URL = "https://api.openai.com/v1/models"
_f = open(LOG, "w")  # noqa: SIM115  held open, flushed per line to survive a wedge


def p(s):
    print(s)
    _f.write(s + "\n")
    _f.flush()


result = {}


def worker(size):
    try:
        r = requests2.post(URL, data=b"{}", headers={"Content-Type": "application/json"})
        result[size] = "status=%d" % r.status_code
        try:
            r.close()
        except Exception:
            pass
    except Exception as e:
        result[size] = "exc %r" % e


for size in (16384, 12288, 10240, 8192):
    gc.collect()
    p("--- trying stack %d (free=%d)" % (size, gc.mem_free()))
    try:
        _thread.stack_size(size)
        _thread.start_new_thread(worker, (size,))
    except Exception as e:
        p("stack %5d: create FAILED %r" % (size, e))
        continue
    deadline = time.ticks_add(time.ticks_ms(), 40000)
    while size not in result and time.ticks_diff(deadline, time.ticks_ms()) > 0:
        time.sleep_ms(100)
    p("stack %5d: %s" % (size, result.get(size, "TIMED OUT after 40s")))

p("PROBE COMPLETE %r" % (result,))
_f.close()
