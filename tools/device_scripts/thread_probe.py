"""Find the largest _thread stack this board can actually allocate.

The translator logs OSError("can't create thread") on every POST, so the
worker-thread design is silently falling back to a blocking request. FreeRTOS
task stacks must live in internal DRAM, not PSRAM, so gc.mem_free() reporting
megabytes says nothing about whether a 32 KB task stack can be created.

Progress is written to /flash as it goes, per the CLAUDE.md wedge rule.
"""

import _thread
import gc
import time

LOG = "/flash/threadprobe.log"
_f = open(LOG, "w")  # noqa: SIM115  held open, flushed per line to survive a wedge


def p(s):
    print(s)
    _f.write(s + "\n")
    _f.flush()


p("free heap (gc, mostly PSRAM): %d" % gc.mem_free())

try:
    import esp32

    for name, cap in (("DATA", esp32.HEAP_DATA), ("EXEC", esp32.HEAP_EXEC)):
        info = esp32.idf_heap_info(cap)
        total = sum(r[0] for r in info)
        free = sum(r[1] for r in info)
        p("idf heap %s: total=%d free=%d regions=%d" % (name, total, free, len(info)))
        p("  largest free block: %d" % max(r[1] for r in info))
except Exception as e:
    p("idf_heap_info unavailable: %r" % e)

done = []


def worker(tag):
    time.sleep_ms(30)
    done.append(tag)


for size in (32768, 16384, 8192, 4096, 2048):
    try:
        _thread.stack_size(size)
    except Exception as e:
        p("stack_size(%d) rejected: %r" % (size, e))
        continue
    try:
        _thread.start_new_thread(worker, (size,))
        p("stack %5d: CREATED" % size)
    except Exception as e:
        p("stack %5d: FAILED %r" % (size, e))
    time.sleep_ms(400)

p("workers that ran: %r" % (done,))
p("PROBE COMPLETE")
_f.close()
