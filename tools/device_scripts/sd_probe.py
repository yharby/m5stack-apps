"""Mount and verify a CoreS3 microSD card without touching existing files."""

import os

from hardware import sdcard

MOUNT = "/sd"
PROBE_BASENAME = ".m5stack_apps_sd_probe"
probe_path = None


def human_bytes(value):
    units = ("B", "KiB", "MiB", "GiB")
    size = float(value)
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    return "%.1f %s" % (size, unit)


try:
    sdcard.SDCard(
        slot=3,
        width=1,
        sck=36,
        miso=35,
        mosi=37,
        cs=4,
        freq=20_000_000,
    )
    entries = os.listdir(MOUNT)
    stats = os.statvfs(MOUNT)
    unit = stats[1] or stats[0]
    total = unit * stats[2]
    free = unit * stats[3]
    print("sd: mounted at", MOUNT)
    print("sd: capacity", human_bytes(total), "free", human_bytes(free))
    print("sd: top-level entries", entries[:40])

    for number in range(1, 1000):
        candidate = MOUNT + "/" + PROBE_BASENAME + ".%03d.tmp" % number
        try:
            os.stat(candidate)
        except OSError:
            probe_path = candidate
            break
    if probe_path is None:
        raise RuntimeError("no unused SD probe filename available")

    payload = "m5stack-apps SD probe: 日本語 UTF-8\n".encode()
    with open(probe_path, "wb") as target:
        written = target.write(payload)
        target.flush()
    os.sync()
    with open(probe_path, "rb") as source:
        verified = source.read() == payload
    if written != len(payload) or not verified:
        raise RuntimeError("SD write/read verification failed")
    print("sd: write/read/sync verified")
finally:
    try:
        if probe_path is not None:
            os.remove(probe_path)
            os.sync()
            print("sd: probe file removed")
    except OSError:
        pass
    try:
        os.umount(MOUNT)
        print("sd: unmounted cleanly")
    except OSError:
        pass
