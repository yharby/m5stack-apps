#!/usr/bin/env python3
"""Device control CLI for the M5Stack UIFlow2 app registry.

The one non-obvious thing this handles: UIFlow2 boots into an asyncio launcher
that owns the serial REPL, so a plain `mpremote` connect fails with
"could not enter raw repl". Every command here first breaks into the REPL by
hammering Ctrl-C over raw serial, then talks to the board with `mpremote resume`
so the board is not soft-reset back into that launcher.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import subprocess
import sys
import time

import serial

BAUD = 115200
APPS_DIR = "device/apps"  # host-side source of truth, one file per app
REMOTE_APPS = "/flash/apps"  # what the device APP.LIST menu shows
REMOTE_AUTORUN = "/flash/main.py"  # what the device runs on boot / APP.RUN
DEFAULT_APP = "translator"
LOCAL_CONFIG = "device/config.json"
REMOTE_CONFIG = "/flash/res/config.json"
REMOTE_LOG = "/flash/translator.log"
SELFTEST = "tools/device_scripts/selftest.py"
SD_PROBE = "tools/device_scripts/sd_probe.py"


def app_paths(name: str) -> tuple[str, str]:
    """(host source path, device destination path) for a named app."""
    return f"{APPS_DIR}/{name}.py", f"{REMOTE_APPS}/{name}.py"


def find_port() -> str:
    """Return the CoreS3 serial port, preferring the USB CDC device."""
    candidates = glob.glob("/dev/cu.usbmodem*") or glob.glob("/dev/cu.usbserial*")
    if not candidates:
        sys.exit("No M5Stack serial port found. Plug the CoreS3 in over USB-C.")
    return min(candidates)


def breakin(port: str, attempts: int = 40, quiet: bool = True) -> None:
    """Interrupt the UIFlow2 launcher so the REPL becomes available."""
    try:
        s = serial.Serial(port, BAUD, timeout=0.2)
    except serial.SerialException as e:
        sys.exit(f"Cannot open {port}: {e}\nIs another program (UIFlow2 web, screen) holding it?")
    try:
        time.sleep(0.2)
        s.reset_input_buffer()
        for _ in range(attempts):
            s.write(b"\x03")
            s.flush()
            time.sleep(0.05)
        time.sleep(0.3)
        s.write(b"\x02")  # Ctrl-B: leave raw REPL, land on a friendly prompt
        s.flush()
        time.sleep(0.3)
        s.read(4000)
    except (OSError, serial.SerialException) as e:
        # The port is enumerated but the board is not answering. This is what a
        # wedge looks like, and there is no EN line on the CoreS3's native USB
        # CDC, so no software reset can reach it.
        sys.exit(
            f"{port} stopped responding ({e}).\n"
            "The board is wedged. Hold the power button for about six seconds, "
            "release it, then press it once, and try again."
        )
    finally:
        with contextlib.suppress(Exception):
            s.close()
    if not quiet:
        print(f"[m5] interrupted UIFlow2 launcher on {port}")


def mp(port: str, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    """Run mpremote against an already-interrupted board."""
    cmd = ["mpremote", "connect", port, "resume", *args]
    return subprocess.run(cmd, capture_output=capture, text=True, check=False)


def remote_exec(port: str, code: str, capture: bool = False) -> subprocess.CompletedProcess:
    return mp(port, "exec", code, capture=capture)


# --------------------------------------------------------------------------- commands


def cmd_info(port: str, _args) -> int:
    breakin(port)
    print(f"port: {port}")
    return remote_exec(
        port,
        "import sys, os, gc, machine, M5\n"
        "print('micropython:', sys.version)\n"
        "print('platform:', sys.platform)\n"
        "print('reset cause:', machine.reset_cause())\n"
        "print('reset constants:', [(n, getattr(machine, n, None)) for n in "
        "('PWRON_RESET','HARD_RESET','WDT_RESET','DEEPSLEEP_RESET','SOFT_RESET')])\n"
        "try:\n"
        "    import esp32; print('uiflow:', esp32.firmware_info()[3])\n"
        "except Exception as e: print('uiflow: ?', e)\n"
        "gc.collect()\n"
        "print('free mem: %d bytes' % gc.mem_free())\n"
        "print('flash:', os.listdir('/flash'))\n",
    ).returncode


def cmd_ls(port: str, _args) -> int:
    breakin(port)
    return remote_exec(
        port,
        "import os\n"
        "def walk(p, d=0):\n"
        "    try: items = sorted(os.listdir(p))\n"
        "    except Exception as e:\n"
        "        print(' '*d, p, 'ERR', e); return\n"
        "    for it in items:\n"
        "        fp = p.rstrip('/') + '/' + it\n"
        "        st = os.stat(fp)\n"
        "        if st[0] & 0x4000:\n"
        "            print(' '*d + '[D] ' + fp)\n"
        "            if d < 6: walk(fp, d+2)\n"
        "        else:\n"
        "            print(' '*d + '    %s  %d B' % (fp, st[6]))\n"
        "walk('/flash')\n",
    ).returncode


def cmd_cat(port: str, args) -> int:
    breakin(port)
    return mp(port, "cat", args.path).returncode


def cmd_push(port: str, args) -> int:
    """Install an app into /flash/apps/<name>.py so APP.LIST shows it by name."""
    name = args.app or DEFAULT_APP
    src, dest = app_paths(name)
    breakin(port)
    print(f"[m5] {src} -> {dest}")
    return mp(port, "cp", src, ":" + dest).returncode


def cmd_push_config(port: str, _args) -> int:
    """Validate and atomically replace Translator's private device config."""
    import json
    from pathlib import Path

    source = Path(LOCAL_CONFIG)
    try:
        config = json.loads(source.read_text())
    except Exception as error:
        sys.exit(f"Invalid {LOCAL_CONFIG}: {error}")
    if not isinstance(config, dict) or not config.get("openai_api_key"):
        sys.exit(f"{LOCAL_CONFIG} must be a JSON object with openai_api_key")

    temporary = REMOTE_CONFIG + ".new"
    backup = REMOTE_CONFIG + ".bak"
    breakin(port)
    print(f"[m5] {LOCAL_CONFIG} -> {REMOTE_CONFIG} (validated; secrets hidden)")
    copied = mp(port, "cp", LOCAL_CONFIG, ":" + temporary)
    if copied.returncode:
        return copied.returncode
    code = (
        "import json, os\n"
        f"new={temporary!r}; dst={REMOTE_CONFIG!r}; bak={backup!r}\n"
        "f=open(new); cfg=json.loads(f.read()); f.close()\n"
        "assert isinstance(cfg, dict) and cfg.get('openai_api_key')\n"
        "try: os.remove(bak)\n"
        "except OSError: pass\n"
        "try: os.rename(dst, bak)\n"
        "except OSError: pass\n"
        "try:\n"
        "    os.rename(new, dst)\n"
        "except Exception:\n"
        "    try: os.rename(bak, dst)\n"
        "    except OSError: pass\n"
        "    raise\n"
        "print('config installed:', dst)\n"
        "print('pair:', cfg.get('language_pair'), 'mode:', cfg.get('source_mode'), "
        "'history:', cfg.get('history_turns'))\n"
    )
    return remote_exec(port, code).returncode


def cmd_autorun(port: str, args) -> int:
    """Also install the app as /flash/main.py so it runs on boot."""
    name = args.app or DEFAULT_APP
    src, _ = app_paths(name)
    breakin(port)
    print(f"[m5] {src} -> {REMOTE_AUTORUN} (runs on boot)")
    return mp(port, "cp", src, ":" + REMOTE_AUTORUN).returncode


def cmd_apps(port: str, _args) -> int:
    """List the apps installed on the device."""
    breakin(port)
    return remote_exec(
        port,
        "import os\n"
        f"for f in sorted(os.listdir('{REMOTE_APPS}')):\n"
        f"    st = os.stat('{REMOTE_APPS}/' + f)\n"
        "    print('  %-24s %6d B' % (f, st[6]))\n",
    ).returncode


def cmd_rm_app(port: str, args) -> int:
    name = args.app
    _, dest = app_paths(name)
    breakin(port)
    return remote_exec(
        port,
        f"import os\ntry:\n    os.remove('{dest}')\n    print('removed {dest}')\n"
        "except OSError as e: print('not removed:', e)\n",
    ).returncode


def cmd_run(port: str, args) -> int:
    """Run the app live; its output streams here. Ctrl-C stops it."""
    name = args.app or DEFAULT_APP
    src, _ = app_paths(name)
    breakin(port)
    print(f"[m5] running {src} on device (Ctrl-C to stop)\n")
    return mp(port, "run", src).returncode


def cmd_run_file(port: str, args) -> int:
    """Run an arbitrary host-side script on the device, for one-off probes."""
    breakin(port)
    return mp(port, "run", args.path).returncode


def cmd_selftest(port: str, _args) -> int:
    """Run the on-device end-to-end check (config, wifi, mic, OpenAI)."""
    breakin(port)
    print(f"[m5] running {SELFTEST} on device\n")
    return mp(port, "run", SELFTEST).returncode


def cmd_sd_probe(port: str, _args) -> int:
    """Mount the CoreS3 SD card and verify temporary read/write/sync."""
    breakin(port)
    print(f"[m5] running {SD_PROBE} on device\n")
    return mp(port, "run", SD_PROBE).returncode


def cmd_repl(port: str, _args) -> int:
    breakin(port)
    return mp(port, "repl").returncode


def cmd_reset(port: str, _args) -> int:
    breakin(port)
    print("[m5] resetting into UIFlow2")
    return remote_exec(port, "import machine; machine.reset()").returncode


def cmd_logs(port: str, args) -> int:
    breakin(port)
    return remote_exec(
        port,
        "try:\n"
        f"    f = open('{REMOTE_LOG}')\n"
        "    lines = f.read().splitlines()\n"
        "    f.close()\n"
        f"    print('\\n'.join(lines[-{args.lines}:]))\n"
        "except OSError:\n"
        "    print('no log file yet')\n",
    ).returncode


def cmd_clear_logs(port: str, _args) -> int:
    breakin(port)
    return remote_exec(
        port,
        f"import os\ntry:\n    os.remove('{REMOTE_LOG}')\n    print('log cleared')\n"
        "except OSError: print('no log file')\n",
    ).returncode


def cmd_probe(port: str, _args) -> int:
    """Smoke-test the hardware the app depends on."""
    breakin(port)
    return remote_exec(
        port,
        "import struct, math, json, network, time\n"
        "print('--- config ---')\n"
        "cfg = None\n"
        "for p in ('/flash/config.json','/flash/res/config.json'):\n"
        "    try:\n"
        "        f=open(p); cfg=json.loads(f.read()); f.close()\n"
        "        print('found', p, '| key:', 'yes' if cfg.get('openai_api_key') else 'NO')\n"
        "        break\n"
        "    except Exception: pass\n"
        "if cfg is None: print('NO CONFIG FOUND')\n"
        "print('--- wifi ---')\n"
        "w = network.WLAN(network.STA_IF); w.active(True)\n"
        "print('connected:', w.isconnected(), w.ifconfig()[0] if w.isconnected() else '')\n"
        "print('--- display ---')\n"
        "import M5\n"
        "M5.begin()\n"
        "M5.Lcd.setFont(M5.Lcd.FONTS.AlibabaSansJA24)\n"
        "print('JA font width test:', M5.Lcd.textWidth('\\u3053\\u3093\\u306b\\u3061\\u306f'))\n"
        "print('--- mic ---')\n"
        "M5.Mic.end()\n"
        "M5.Mic.config(sample_rate=16000, magnification=2, task_pinned_core=0)\n"
        "if not M5.Mic.begin(): raise RuntimeError('M5.Mic.begin failed')\n"
        "b = bytearray(32000)\n"
        "if not M5.Mic.record(b, 16000, False): raise RuntimeError('M5.Mic.record failed')\n"
        "while M5.Mic.isRecording(): time.sleep_ms(20)\n"
        "M5.Mic.end()\n"
        "n = len(b)//2; acc = 0\n"
        "for i in range(0, n, 7):\n"
        "    v = struct.unpack_from('<h', b, i*2)[0]; acc += v*v\n"
        "print('captured %d bytes, rms=%.1f' % (len(b), math.sqrt(acc/(n//7))))\n",
    ).returncode


COMMANDS = {
    "info": cmd_info,
    "ls": cmd_ls,
    "cat": cmd_cat,
    "apps": cmd_apps,
    "push": cmd_push,
    "push-config": cmd_push_config,
    "autorun": cmd_autorun,
    "rm-app": cmd_rm_app,
    "run": cmd_run,
    "run-file": cmd_run_file,
    "selftest": cmd_selftest,
    "sd-probe": cmd_sd_probe,
    "repl": cmd_repl,
    "reset": cmd_reset,
    "logs": cmd_logs,
    "clear-logs": cmd_clear_logs,
    "probe": cmd_probe,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", help="serial port (default: autodetect)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in COMMANDS:
        p = sub.add_parser(name)
        if name in ("cat", "run-file"):
            p.add_argument("path")
        if name in ("push", "run", "autorun"):
            p.add_argument("app", nargs="?", help=f"app name (default: {DEFAULT_APP})")
        if name == "rm-app":
            p.add_argument("app")
        if name == "logs":
            p.add_argument("-n", "--lines", type=int, default=40)

    args = ap.parse_args()
    port = args.port or find_port()
    return COMMANDS[args.cmd](port, args)


if __name__ == "__main__":
    sys.exit(main())
