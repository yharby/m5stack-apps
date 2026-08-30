# UIFlow2 RTL on CoreS3: build, flash, and recovery runbook

This is the reproducible record of adding production-oriented Arabic and
Hebrew rendering to the Translator on an M5Stack CoreS3. It includes the
firmware changes, the real-device failures encountered on 2026-08-30, their
recovery, and the remaining verification work.

> **Current status:** two independent defects were found and fixed. The first
> is a real LVGL BiDi heap overflow on a UTF-8 truncated prefix, proven in both
> directions under ASan and carried as a backport. The second, and the actual
> cause of the reboot, is that our generated font subsets are compressed while
> UIFlow shipped `LV_USE_FONT_COMPRESSED 0`, so every glyph bitmap resolved to
> `NULL` and the software draw path dereferenced it. Both fixes are in the
> flashed firmware. On hardware, `rtl_probe.py` reports ABI 2 with all eight
> fonts, and `rtl_render_probe.py` now completes 872 Arabic and 1,528 Hebrew
> cases with `reset_cause()` still 1 (`PWRON_RESET`), where it previously
> hard-reset within about 91 Arabic cases. Live translation then ran, and
> exposed a third, cosmetic defect: the generated subsets stop at `U+007E`, so
> typographic punctuation drew LVGL placeholder boxes. `renderable_text()` in
> the app folds those onto ASCII. Do not call the Arabic path production-ready
> until the final checklist in this document passes.

## Goal and architecture

Stock UIFlow2 2.5.1 has LVGL BiDi, Arabic contextual shaping, and an
Arabic/Hebrew font disabled. Application code alone cannot provide correct
Arabic rendering. The solution therefore has two layers:

1. The custom firmware enables LVGL BiDi and Arabic/Persian shaping, embeds
   two sizes each of Cairo and Noto Sans Hebrew, freezes a capability module,
   and carries the LVGL BiDi safety backport.
2. [Translator](../device/apps/translator.py) keeps strings in logical Unicode
   order and selects `LV_BASE_DIR.RTL`, right alignment, and the correct font
   per label. Heard text uses 16 px; the visually dominant translation uses
   24 px. The app requires `translator_rtl.ABI_VERSION == 2` before starting
   an Arabic or Hebrew pair.

This supports Arabic and ordinary horizontal Hebrew. Hebrew text with niqqud
or other combining marks remains an upstream LVGL limitation. Traditional
vertical Mongolian is outside this implementation; the app's Mongolian option
is horizontal Cyrillic.

## Tested source pins and tools

Reproduce from commits, not a moving branch:

| Component | Tested pin/version |
|---|---|
| UIFlow2 | [`m5stack/uiflow-micropython@f44294d563207c04e09d1db2780db1c1c4525c38`](https://github.com/m5stack/uiflow-micropython/commit/f44294d563207c04e09d1db2780db1c1c4525c38) |
| UIFlow version reported by device | 2.5.1 |
| MicroPython reported by device | 1.27.0 |
| ESP-IDF | [`v5.5.4`, commit `735507283d5b2f9fb363a1901172dbd9e847945d`](https://github.com/espressif/esp-idf/commit/735507283d5b2f9fb363a1901172dbd9e847945d) |
| LVGL inside the binding | `9c043167685fc08fbcd30ddf2c285ea1089be82d` (`v9.2.0-624-g9c0431676`) |
| LVGL MicroPython binding | `44f70b1e1adb087e00ea5d39fe45a0b0f3551646` |
| Font generator | `lv_font_conv` 1.5.3 |
| Flash tool used | esptool.py 4.12.0 |
| Host used | Apple Silicon macOS; GNU Make 3.81, CMake 4.4.3, Ninja 1.13.2, Quilt 0.69 |

UIFlow2's current README requires ESP-IDF 5.5.4. Its checked-in `CLAUDE.md`
still mentioned an older IDF during this investigation, so the current README
and the exact pin above were treated as authoritative.

On macOS, install the ordinary host build tools first. `quilt` is required by
UIFlow2's `make patch`; its Homebrew install placed Emacs support under
`/opt/homebrew/share/emacs/site-lisp/quilt`, which is informational and not a
build error. The tested environment also needed the Python `future` package in
the active ESP-IDF Python environment when the build initially reported it
missing.

```bash
brew install quilt cmake ninja
```

## Prepare the pinned UIFlow2 checkout

The UIFlow repository root contains the `m5stack/`, `micropython/`, and
`esp-adf/` trees. Run UIFlow's build commands from its `m5stack/` child.

```bash
mkdir -p /tmp/uiflow-rtl-work
cd /tmp/uiflow-rtl-work

git clone https://github.com/m5stack/uiflow-micropython.git uiflow
git -C uiflow checkout f44294d563207c04e09d1db2780db1c1c4525c38
git -C uiflow submodule update --init --recursive

git clone --branch v5.5.4 https://github.com/espressif/esp-idf.git esp-idf
git -C esp-idf checkout 735507283d5b2f9fb363a1901172dbd9e847945d
git -C esp-idf submodule update --init --recursive
./esp-idf/install.sh
. ./esp-idf/export.sh

cd uiflow/m5stack
make submodules
make patch
```

If ESP-IDF is not located at UIFlow's expected sibling path, export its exact
path before building:

```bash
export IDF_PATH=/tmp/uiflow-rtl-work/esp-idf
```

Do not run this repository's RTL patch before `make submodules` and
`make patch`. It intentionally checks exact upstream contexts and fails rather
than guessing after an upstream change.

## Apply the RTL and font patch

From this Translator repository:

```bash
make rtl-firmware-patch UIFLOW_DIR=/tmp/uiflow-rtl-work/uiflow
```

[The patcher](../tools/patch_uiflow_rtl.py) is exact and idempotent. It:

- sets `LV_FONT_DEJAVU_16_PERSIAN_HEBREW=1`, `LV_USE_BIDI=1`, and
  `LV_USE_ARABIC_PERSIAN_CHARS=1` in LVGL configuration;
- declares the existing UIFlow CJK fonts plus all four Translator fonts;
- copies and byte-verifies the generated C font sources into M5Unified's font
  directory;
- freezes `translator_rtl.py` in the CoreS3 manifest with ABI 2, BiDi/shaping
  capability flags, exact font names, license, and copyright metadata; and
- applies the BiDi alignment memory-safety fix carried on our LVGL fork,
  [`yharby/lvgl`, branch `fix/bidi-truncated-utf8`](https://github.com/yharby/lvgl/tree/fix/bidi-truncated-utf8).

Expected output is either `patched` or `already enabled`, followed by:

```text
Enabled LVGL bidi, shaping, BiDi alignment safety backport, Cairo/Noto RTL fonts, and ABI
```

### Fonts and licensing

The generated sources and complete reproduction notes are in
[firmware/fonts](../firmware/fonts/README.md):

| Use | Face | Size/bpp | Source pin |
|---|---|---|---|
| Arabic heard text | Cairo 3.130 | 16 px, 2 bpp | Google Fonts `d2528f6d1f43e7d9d0d2e1794afe2ad6fd7d56ba` |
| Arabic translation | Cairo 3.130 | 24 px, 4 bpp | same pin |
| Hebrew heard text | Noto Sans Hebrew | 16 px, 2 bpp | Google Fonts `4c1913251a6dd1ba34a6ef4b7a630178d01b88ff` |
| Hebrew translation | Noto Sans Hebrew | 24 px, 4 bpp | same pin |

Both families and the generated subsets are SIL Open Font License 1.1. Keep
their source headers and [OFL text](../firmware/fonts/OFL-1.1.txt) with every
source or binary distribution. UIFlow2 itself is MIT licensed; the OFL fonts
are not relicensed as MIT. At the time of this work, this Translator repository
had no top-level project `LICENSE`, so its own non-font code does not yet have
a clear redistribution license. Add one before treating the whole repository
as a redistributable package; do not remove or replace the font notices when
doing so.

## Build and verify artifacts

Build the LittleFS packer once, then the CoreS3 package. The tested command used
`pack_all` because it produces every component needed for inspection and
recovery; it does **not** mean the user filesystem should normally be flashed.

```bash
make -C /tmp/uiflow-rtl-work/uiflow/m5stack littlefs
make -C /tmp/uiflow-rtl-work/uiflow/m5stack mpy-cross
make -C /tmp/uiflow-rtl-work/uiflow/m5stack BOARD=M5STACK_CoreS3 pack_all
```

The important outputs are under
`m5stack/build-M5STACK_CoreS3/`:

- `micropython.bin`: factory application at `0x10000`;
- `fs-system.bin`: matching 1 MiB `/system` filesystem at `0x9a0000`;
- `fs-user.bin`: matching user filesystem image, **not for routine updates**;
- `partition_table/partition-table.bin`;
- `uiflow-f44294d.bin`: combined package; and
- `flasher_args.json`: ESP-IDF bootloader/app offsets, not the separate
  UIFlow system/user filesystem policy.

The first RTL build, which carried the PR #9908 clamps, produced:

```text
micropython.bin: 9,975,584 bytes
SHA-256: 8cec2e9565fb3aab3f31bdc3386f4e362215bda35e3848a86d84a21e8118bad7
factory partition free: 51,424 bytes (about 1%)

fs-system.bin: 1,048,576 bytes
SHA-256: b91347cc59450e92f2ac48d7d2425c3e1bd54c40b50e92448912ab4b64a9e041
```

The app partition is tight. Always run the size check and never flash an image
that exceeds its partition. Also verify the four font symbols and the frozen
ABI module are present in the generated binding/frozen output before touching
hardware.

```bash
rg -n 'translator_(cairo|noto)' \
  /tmp/uiflow-rtl-work/uiflow/m5stack/build-M5STACK_CoreS3/lv_mp.c
rg -n 'ABI_VERSION|translator_rtl' \
  /tmp/uiflow-rtl-work/uiflow/m5stack/build-M5STACK_CoreS3/frozen_content.c
shasum -a 256 \
  /tmp/uiflow-rtl-work/uiflow/m5stack/build-M5STACK_CoreS3/micropython.bin \
  /tmp/uiflow-rtl-work/uiflow/m5stack/build-M5STACK_CoreS3/fs-system.bin
```

## CoreS3 partition safety

The physical board's partition table was read first and compared byte-for-byte
with the new build. It matched this 16 MiB layout:

| Partition | Offset | Size | End | Preserve? |
|---|---:|---:|---:|---|
| bootloader | `0x000000` | before table | — | normally yes |
| partition table | `0x008000` | one sector | — | verify, do not rewrite for this update |
| `nvs` | `0x009000` | 24 KiB | `0x00f000` | yes; holds UIFlow/Wi-Fi settings |
| `phy_init` | `0x00f000` | 4 KiB | `0x010000` | yes |
| `factory` app | `0x010000` | 9,792 KiB (`0x990000`) | `0x9a0000` | update with `micropython.bin` |
| `sys` | `0x9a0000` | 1 MiB | `0xaa0000` | update with matching `fs-system.bin` |
| `vfs` | `0xaa0000` | 4,864 KiB | `0xf60000` | **preserve; mounted as `/flash`** |
| `storage` | `0xf60000` | 640 KiB | `0x1000000` | preserve |

Read and compare before every write. Replace the serial port with the current
node shown by `ls /dev/cu.usbmodem*`; its number can change after reset.

```bash
export RTL_PORT=/dev/cu.usbmodem101
export RTL_BUILD=/tmp/uiflow-rtl-work/uiflow/m5stack/build-M5STACK_CoreS3

python -m esptool --chip esp32s3 --port "$RTL_PORT" \
  --before default_reset --after hard_reset \
  read_flash 0x8000 0x1000 /tmp/cores3-partition-table.bin

cmp /tmp/cores3-partition-table.bin \
  "$RTL_BUILD/partition_table/partition-table.bin"
```

Stop if `cmp` reports any difference. Do not infer offsets from this document
for a different firmware release or board.

### What preserves `/flash`

Writing only `micropython.bin` at `0x10000` and `fs-system.bin` at `0x9a0000`
does not overlap `vfs` at `0xaa0000`; the app, private config, transcripts, and
other user files remain present. Avoid all of the following unless intentionally
performing a destructive factory recovery with backups:

- `erase_flash`;
- writing `fs-user.bin`;
- `make flash_all`; and
- writing a combined `pack_all` image from address zero.

Preserving a user filesystem does not guarantee it is compatible with a new
firmware. The `/flash/boot.py` incident below is the concrete example.

## macOS USB, download mode, and reset procedure

Use a data-capable USB-C cable and close serial monitors before esptool or
`mpremote` opens the device.

- Normal running mode exposes `/dev/cu.usbmodem*` and shows the UIFlow screen.
- To enter ROM download mode, hold the bottom **RESET** button for about 2–3
  seconds and release when the internal green LED lights. The screen may remain
  black. Confirm esptool can identify the ESP32-S3 before writing.
- After a successful esptool write, single-click **RESET** once. Do not hold it,
  because holding enters download mode again.
- If the device is wedged and its enumerated port does not answer, hold the power
  button for about six seconds, release it, press once to power on, and reconnect
  USB. Use download mode only when the next action actually needs esptool.

The board may briefly disappear and re-enumerate under a different port after
any reset. A black display in download mode is normal; a black display after a
single-click normal reset means serial boot output should be inspected.

## Safe paired flash procedure

Obtain explicit operator approval immediately before the write. With a matching
partition table and validated artifact sizes/hashes, write the application and
system filesystem in one esptool transaction so they cannot be accidentally
mixed across builds:

```bash
python -m esptool --chip esp32s3 --port "$RTL_PORT" \
  --before default_reset --after hard_reset \
  write_flash \
  0x10000 "$RTL_BUILD/micropython.bin" \
  0x9a0000 "$RTL_BUILD/fs-system.bin"
```

Do not assume esptool's automatic reset leaves CoreS3 in normal mode. If it
remains in the bootloader, single-click RESET once. If the preserved user
filesystem came from another UIFlow build, inspect and update `/flash/boot.py`
as described next before expecting a clean launcher boot.

## Incident record: why the first flash showed a black screen

The first attempt deliberately wrote only the custom `micropython.bin` at
`0x10000` to preserve `/flash`. The partition layout was correct and the write
verified, but UIFlow boot failed with:

```text
ImportError: no module named 'm5sync'
```

### Failure 1: app-only update

The application firmware and `/system` libraries are a release unit. The new
application expected the matching system tree, while the device still had a
system filesystem from its previous UIFlow installation. Therefore an
application-only update was insufficient even though it safely preserved
`/flash`.

The matching, hash-verified 1 MiB `fs-system.bin` was then written at
`0x9a0000`. The import error still occurred.

### Failure 2: why the system-only correction was still insufficient

Serial inspection established that `/system` mounted correctly and
`sys.path` was:

```text
['', '.frozen', '/lib', '/system', '/flash/libs']
```

The pinned current UIFlow source contains no `m5sync` module. The remaining
import came from the old, deliberately preserved `/flash/boot.py`:

```python
from m5sync import sync
```

The matching build's `fs-user/boot.py` uses `startup` instead. This was not a
corrupt system image; it was a stale boot hook in the preserved VFS. Flashing
`fs-user.bin` would have fixed it by replacing all of `/flash`, but would also
have destroyed the app, private configuration, and other user files.

### Safe resolution

The matching generated `build-M5STACK_CoreS3/fs-user/boot.py` was uploaded as
`/flash/boot.py.new`, compiled/executed in a validation namespace, and only then
installed. The previous file was retained as `/flash/boot.py.bak`; if rename of
the new file failed, the backup could be restored. After a normal reset the
launcher booted, Translator emitted logs, the screen worked, and
`/flash/res/config.json` was still present. The Wi-Fi QR app subsequently
connected to the hotspot successfully and saved UIFlow's NVS credentials.

For future migrations, compare `/flash/boot.py` with the matching build before
flashing. Stage, validate, back up, and rename; never overwrite it blindly and
never copy the whole `fs-user.bin` merely to replace this one file.

After boot is healthy, reinstall the current app and atomically replace the
private config from this repository:

```bash
make push APP=translator
make push-config
```

[`make push-config`](../tools/m5.py) uploads to a `.new` file, validates JSON
on-device, retains `.bak`, and renames atomically. It prints only non-secret
settings.

## Arabic crash and the LVGL safety backport

The first Arabic translation session booted with pair `en/ar` in automatic
source mode. Microphone capture and both APIs were healthy. The final log tail
was:

```text
tt: target=ar chars=65
rec: 96000 bytes ... trimmed ... 60800
stt: POST 61554 bytes
```

The log then ended abruptly with no Python exception, and the user observed the
app crash and reboot while translating to Arabic. A later
`machine.reset_cause()` returned `5` (`SOFT_RESET`), but that sample may have
come from the Wi-Fi app's intentional reset and is not conclusive evidence of
the original failure.

Investigation found open [LVGL PR #9908, “fix(bidi): add bounds check in
rtl_reverse to prevent heap overflow”](https://github.com/lvgl/lvgl/pull/9908).
It describes the right defect. `lv_bidi_process_paragraph()` assumes `len` ends
on an encoded-character boundary, and label line breaking can pass a byte count
that lands inside a multibyte Arabic or Hebrew character. `rtl_reverse()` then
decodes that trailing character at its full encoded size, so the write cursor
moves past `len` and `lv_memcpy()` writes past the output buffer.

**That PR must not be used as-is.** Reading its full review history shows it is
open with **changes requested** by LVGL collaborator `AndreCostaaa` since
2026-04-13, with no author response. Reviewers raised seven findings, and five
are the same structural mistake. #9908 clamps the byte cursor in five separate
places but leaves the matching character counter untouched, so the output text
and the position map end up describing different things and part of the map is
left uninitialized. The cubic review filed this as a P1 against `rtl_reverse`.

An RTL render stress probe run against a build carrying the #9908 clamps still
produced an immediate `HARD_RESET` on the device, with the user touching
nothing. That is consistent with the desynchronization the reviewers describe,
though it is not proof of it.

The patcher therefore carries a different fix, developed on our fork at
[`yharby/lvgl`, branch `fix/bidi-truncated-utf8`](https://github.com/yharby/lvgl/tree/fix/bidi-truncated-utf8).
It rejects the unaligned length once, before any decoding, and copies the
bounded bytes through unchanged with an identity position map. Reordering a
partial character is not meaningful, and the caller still receives exactly
`len` bytes plus a terminator, so no counter can drift. This makes all five
#9908 desynchronization paths unreachable rather than individually patched.

The fix is verified on the host, not only reasoned about:

- LVGL's own Unity suite under AddressSanitizer and UndefinedBehaviorSanitizer,
  walking every byte prefix of Arabic, Hebrew, CJK, emoji and mixed-direction
  strings in all three base directions, with guard bytes placed immediately
  after `out[len]` and `map[map_len]`.
- Against an **unpatched** `lv_bidi.c` the same test aborts with an
  AddressSanitizer BUS-on-write inside `rtl_reverse` at `lv_bidi.c:567`, and
  writes `0xB9`, the trailing byte of Arabic ع, over the terminator. The test
  is therefore proven to detect the defect rather than merely pass.
- Regression suites `test_label` (37), `test_txt` (18), `test_text_ap` (13) and
  `test_textarea` (30) all pass. `test_span` reports 3 failures on this machine,
  all `freetype font create failed`, an environment issue unrelated to BiDi.

The backport applied to the pinned UIFlow LVGL is byte-identical to the fork
commit. Track #9908 upstream, but do not adopt it in its current form.

Do not redeploy the older application binary for Arabic. The rebuilt
`micropython.bin` carrying the #9908 clamps was written at `0x10000`, and an explicit
`verify_flash` comparison matched all 9,975,584 bytes:

```bash
python -m esptool --chip esp32s3 --port "$RTL_PORT" \
  --before no_reset --after no_reset \
  verify_flash 0x10000 "$RTL_BUILD/micropython.bin"
```

Only the application partition needed this incremental safety update because
the matching `fs-system.bin` was byte-identical to the system image already
installed. This is different from the initial app-only migration, which left
an old system image in place. esptool's own reset leaves the bootloader stub
loaded rather than booting the application, so a physical single-click RESET is
required before any probe. Do not hold the button, which re-enters download mode.

## Root cause of the Arabic reset: compressed fonts with the decompressor off

The BiDi work above is a real memory-safety fix, but it was **not** the cause of
the device reset. After flashing the verified BiDi fix and confirming both
partitions byte-for-byte, `rtl_render_probe.py` still produced
`machine.reset_cause() == 2` (`HARD_RESET`) with nobody touching the board.

### How the real cause was isolated

The probe printed nothing until an entire language finished, so a mid-run reset
carried no information. Adding a print before every case changed everything.

1. **Instrument before narrowing.** Printing `end`, byte length and `gc.mem_free()`
   before each case showed the reset at the **second** Arabic character, with
   8.13 MB still free. That immediately ruled out memory exhaustion and ruled
   out anything that needs a long string, including line wrapping.
2. **Check determinism.** Re-running moved the failure from `width=91` to
   `width=57`. A moving failure point means the trigger is state dependent, not
   one bad glyph.
3. **Swap one variable.** Rendering the same two-character string with LVGL's
   built-in `font_dejavu_16_persian_hebrew` succeeded at every width, while
   `translator_cairo_16` reset the board. The built-in font is compiled from the
   same LVGL tree with the same BiDi and shaping code, so the difference had to
   be in the generated font subsets, not in LVGL's RTL logic.

### The defect

`lv_font_conv` compresses bpp 2 and bpp 4 output **by default**. The generator
was run without `--no-compress`, so all four subsets declare:

```c
.bitmap_format = 1,   /* LV_FONT_FMT_TXT_COMPRESSED */
```

UIFlow ships `LV_USE_FONT_COMPRESSED 0`. In `lv_font_fmt_txt.c` the compressed
branch then becomes:

```c
#else /*!LV_USE_FONT_COMPRESSED*/
    LV_LOG_WARN("Compressed fonts is used but LV_USE_FONT_COMPRESSED is not enabled in lv_conf.h");
    return NULL;
#endif
```

So every glyph bitmap in every custom font resolves to `NULL`, behind a warning
that is itself compiled out of a release build. The software draw path does not
check for it, in `lv_draw_sw_letter.c`:

```c
const lv_draw_buf_t * draw_buf = glyph_draw_dsc->glyph_data;   /* NULL */
blend_dsc.mask_buf = draw_buf->data;                           /* NULL dereference */
```

On ESP32-S3 that is a `LoadProhibited` panic. The panic text never reaches the
host because the CoreS3 console is USB CDC and the peripheral resets before the
buffer flushes, which is exactly why every attempt to read a backtrace returned
nothing but a dropped port.

This also explains the non-determinism. Whether a given render actually reaches
the glyph blend depends on wrapping and clipping at that label width, so the
same string survives some widths and kills the board at others.

### The fix

The patcher now sets `LV_USE_FONT_COMPRESSED 1`. The alternative is to
regenerate the subsets with `--no-compress`, which trades flash for per-glyph
decode time. Enabling the decompressor was chosen because the factory partition
had only about 51 KB of headroom.

Verify both halves of the pairing before every release, because either one
alone is silently wrong:

```bash
grep -h "bitmap_format" firmware/fonts/*/*.c
grep -n "LV_USE_FONT_COMPRESSED" <checkout>/m5stack/cmodules/lv_binding_micropython/lv_conf.h
```

`bitmap_format = 1` requires `LV_USE_FONT_COMPRESSED 1`. `bitmap_format = 0`
works either way.

### Hardware verification

Verified on the real board after flashing the application partition and an
independent `verify_flash` digest match, then a single-click normal RESET.

| Check | Before the fix | After the fix |
|---|---|---|
| `rtl_probe.py` | ABI 2, 8 fonts, bidi, shaping | unchanged |
| `rtl_render_probe.py` Arabic | reset near case 91 | `completed Arabic cases=872` |
| `rtl_render_probe.py` Hebrew | never reached | `completed Hebrew cases=1528` |
| Final probe line | no output, port dropped | `PASS cases=1528 mem_free=8136976` |
| `reset_cause()` after the probe | 2, `HARD_RESET` | 1, `PWRON_RESET` |

`reset_cause()` stayed 1 across the whole run, so the board never restarted at
any point rather than restarting and recovering. Free memory after the probe was
8,158,512 bytes against 8,162,912 at boot, so roughly 4 KB of ordinary churn and
no per-render leak across 2,400 shaped strings.

Read the reset cause on the run that follows the probe, not inside it. A board
that resets mid-probe reports the cause only after it comes back up.

### Lessons worth keeping

- A capability probe that reports `font:translator_cairo_24=True` proves the
  symbol is linked, not that a single glyph can be drawn. Probe rendering, not
  presence.
- `LV_LOG_WARN` is not a diagnostic in a release build. Treat any LVGL path
  whose only error signal is a log warning as a silent failure.
- When a native reset gives no backtrace over USB CDC, bisect by swapping one
  component for a known-good equivalent from the same build. The built-in
  Persian/Hebrew font was the control that localized this in one step.
- Two independent defects were present at once. Fixing the first, provable one
  did not stop the symptom, and the correct response was to keep the verified
  fix and keep hunting rather than to doubt it.

## Empty boxes between letters: subset coverage, not shaping

With the reset fixed, live Arabic and English rendered correctly but showed
empty squares scattered between letters. That symptom is not a shaping or BiDi
problem. It is LVGL's missing-glyph placeholder.

### Where the box comes from

`lv_font_get_glyph_dsc_internal()` in `src/font/lv_font.c` walks the font and
its `fallback` chain. When nothing supplies the code point and
`LV_USE_FONT_PLACEHOLDER` is enabled, it does not skip the character:

```c
    dsc_out->box_w = font->line_height / 2;
    dsc_out->adv_w = dsc_out->box_w + 2;
    ...
    dsc_out->is_placeholder = true;
    return false;
```

The draw path then renders that as an empty rectangle half a line high. So
every box on screen is exactly one code point the embedded subset cannot draw,
and the set is knowable offline from the generated `.c` files.

### Reading the real coverage

The generator ranges are a request, not a result. `lv_font_conv` emits only the
glyphs the source `.ttf` actually contains, so the authority is the `cmaps`
array in each generated file, which is the same table LVGL consults at runtime.
`tests/test_translator.py` parses it with `font_codepoints()`, handling both
the `FORMAT0` dense ranges and the `SPARSE` `unicode_list` form.

Measured coverage of the shipped subsets:

| Subset | Glyphs | Highest Latin code point |
|---|---|---|
| `translator_cairo_16` / `_24` | 426 | `U+007E` |
| `translator_noto_hebrew_16` / `_24` | 233 | `U+007E` |

Both stop at `U+007E`, because the generator ranges start
`-r 0x20-0x7E,...`. Everything between ASCII and the script blocks is absent:

| Group | Code points | Effect |
|---|---|---|
| Curly quotes | `2018 2019 201A 201B 201C 201D 201E 201F` | box inside English contractions |
| Dashes, ellipsis | `2010 2011 2012 2013 2014 2015 2026` | box mid sentence |
| Spaces | `00A0 202F 2007 2009` | box instead of a space |
| Guillemets, Latin-1 | `00AB 00BB 00B7 00B0`, all of `00A0-00FF` | box |
| BiDi controls | `061C 200B-200F 202A-202E 2066-2069 FEFF` | box |

`U+2019` is the one that matches the reported symptom most exactly. The chat
models emit it for every English apostrophe, so `don't` becomes `don` box `t`,
a box literally between two letters.

### One Arabic gap that a wider range cannot fix

Cross-checking LVGL's `ap_chars_map` in `src/misc/lv_text_ap.c` against the
Cairo cmap shows the shaping engine can emit 157 code points, of which one is
Arabic-relevant and missing: **`U+FE81`**. LVGL maps every positional form of
`U+0622` (`آ`, alef with madda above) to `U+FE81`, because that row carries
zero beginning, middle, and isolated offsets. Cairo ships `U+FE82`, the final
form, but not `U+FE81`, even though `0xFE70-0xFEFF` was requested. Widening the
range therefore cannot help; the glyph is not in the source font. The remaining
options are a different Arabic face, or substituting `U+FE82` in Python before
LVGL sees the text. The substitution is behaviour-preserving, because that row
also declares `{0, 0}` conjunction, so LVGL already treats `آ` as joining on
neither side. It is left undone until it is judged against real text. The other
seven gaps are Persian-only forms and do not affect Arabic or Hebrew.

### The fix that shipped

Widening the subsets would cost flash, and the application partition has under
50 KB of headroom, so the app folds the offending code points onto ASCII the
subsets already carry. `renderable_text()` in `device/apps/translator.py` maps
typographic punctuation to its ASCII equivalent, drops invisible formatting
codes, and passes everything else through untouched, so Arabic, Hebrew, and CJK
are unaffected. It is applied at the two points where model text enters the
app, the `transcribe()` and `translate()` return paths, so the canonical
logical-order Python text is already clean before any label sees it.

Dropping the BiDi controls rather than substituting them is deliberate. The app
keeps logical-order text and lets LVGL resolve runs, so an embedded direction
override would fight the renderer rather than help it.

`RenderableTextTest.test_every_substitution_target_is_drawable_by_both_fonts`
parses all four generated subsets and asserts every replacement character is in
each cmap, so the substitution table cannot drift away from the fonts.

### Lesson

A generator range is an intent. Verify coverage from the generated cmap, not
from the command line that produced it, and treat any placeholder box as a
precise, decodable statement about one missing code point.

## Device probes and expected output

Start with repository and firmware metadata checks:

```bash
make check
make info
uv run python tools/m5.py run-file tools/device_scripts/rtl_probe.py
```

[The capability probe](../tools/device_scripts/rtl_probe.py) is read-only. A
correct build reports:

```text
lvgl: available
font:translator_cairo_16=True
font:translator_cairo_24=True
font:translator_noto_hebrew_16=True
font:translator_noto_hebrew_24=True
rtl_abi:2
rtl_bidi:True
rtl_arabic_shaping:True
rtl_font_license:OFL-1.1
```

It should also report the existing CJK faces. Missing `translator_rtl`, any
false required font, or an ABI other than 2 is a hard failure.

Next run [the render stress probe](../tools/device_scripts/rtl_render_probe.py):

```bash
uv run python tools/m5.py run-file tools/device_scripts/rtl_render_probe.py
```

It repeatedly renders progressively longer Arabic and Hebrew strings across
eight narrow-to-wide wrap widths, forcing the multibyte BiDi line-breaking path.
The final serial line must be similar to:

```text
rtl_render_probe: PASS cases=<nonzero> mem_free=<positive>
```

The display must end with connected, correctly ordered Arabic
`العربية: ناجح` and Hebrew `עברית: הצלחה`. A PASS line is not enough if the
glyphs are disconnected, visually reversed, clipped, or use fallback boxes.

Finally push both current files, start Translator, and watch logs during real
English→Arabic, Arabic→English, English→Hebrew, and Hebrew→English turns:

```bash
make push APP=translator
make push-config
make run APP=translator
```

Confirm automatic source detection changes direction based on heard script,
the original/heard line is smaller than the translation, scrolling disables
follow without stopping capture, LIVE returns to the newest turn, and mixed
Arabic/Latin/numbers remain legible.

## Related upstream work to follow

Checked on 2026-08-30:

- [M5Stack UIFlow2 issue #25, “UTF8 font?”](https://github.com/m5stack/uiflow-micropython/issues/25)
  is open. It confirms demand for Unicode fonts but does not cover BiDi,
  contextual Arabic shaping, per-language fonts, or the firmware capability
  contract.
- [M5Stack UIFlow2 issue #26, custom firmware cloud connectivity](https://github.com/m5stack/uiflow-micropython/issues/26)
  is open. It is relevant to custom firmware distribution and UIFlow cloud
  expectations, but not to the `m5sync` boot failure seen here.
- [LVGL issue #9488, Hebrew combining-symbol rendering](https://github.com/lvgl/lvgl/issues/9488)
  is closed as not planned. Basic Hebrew without combining marks should still
  be tested; niqqud cannot be claimed as supported by this bitmap-font path.
- [LVGL PR #9908, BiDi bounds check](https://github.com/lvgl/lvgl/pull/9908)
  is open with changes requested and unmaintained since 2026-04-13. It
  identifies the correct defect but its patch desynchronizes the output text
  from the position map. Follow it, do not ship it.

No exact upstream issue was found for the stale `m5sync` import or this paired
application/system/user-boot migration failure.

Recommended upstream strategy:

1. Complete the physical verification of the alignment fix and record
   binary-size and render-stress results.
2. Open one focused UIFlow2 issue titled approximately **“CoreS3/UIFlow2:
   expose LVGL Arabic/Hebrew RTL (BiDi, Arabic shaping, RTL fonts)”**. Link #25,
   describe the ABI probe and firmware-size cost, and keep the stale-boot
   migration incident as a separate compatibility note.
3. Fork `m5stack/uiflow-micropython` only after maintainers confirm the desired
   font/config approach. Submit a narrowly scoped PR for generic firmware
   capability—not the Translator application. Preserve MIT sign-off conventions
   and the OFL font notices.
4. The LVGL fork at `yharby/lvgl` carries the alignment fix and its sanitizer
   test on branch `fix/bidi-truncated-utf8`. Once device verification passes,
   it is worth offering upstream, either as a review comment on #9908 or as an
   alternative PR that credits #9908 for identifying the defect and cubic for
   identifying the desynchronization. Keep the downstream backport until the
   pinned UIFlow LVGL revision contains an accepted fix.
5. If the `m5sync` migration can be reproduced between two official UIFlow
   versions, open a separate issue with the old/new firmware identifiers,
   preserved `/flash/boot.py`, `sys.path`, and safe migration proposal.

No issue, fork, or PR had been created by this repository at the time of this
record.

## Release verification checklist

- [ ] Checkout and all dependencies match the recorded commits.
- [ ] UIFlow `make submodules` and `make patch` complete before the RTL patch.
- [ ] RTL patcher reports ABI 2, all four fonts, shaping, BiDi, and the BiDi
      alignment backport present.
- [ ] LVGL host suite `test_bidi` passes under ASan/UBSan, and is confirmed to
      fail against an unpatched `lv_bidi.c`.
- [ ] CoreS3 `pack_all` succeeds; application fits with the recorded headroom,
      and the roughly 1% margin is explicitly reviewed.
- [ ] Artifact sizes and SHA-256 hashes are recorded for the release.
- [ ] Physical and build partition tables compare byte-for-byte.
- [ ] `/flash/res/config.json`, apps, logs, and any transcripts are backed up
      without printing secrets.
- [ ] `micropython.bin` and its matching `fs-system.bin` are flashed as a pair.
- [ ] Preserved `/flash/boot.py` is compatible or safely staged/replaced with a
      `.bak` rollback copy.
- [ ] Normal reset reaches the UIFlow launcher; screen is not black.
- [ ] `make info` reports CoreS3, UIFlow2 2.5.1, and MicroPython 1.27.0.
- [ ] Capability probe reports ABI 2 and every required font/capability.
- [ ] Every generated font's `bitmap_format` is paired correctly with
      `LV_USE_FONT_COMPRESSED`. A compressed font with the decompressor off
      draws nothing and hard-resets the board.
- [ ] Each font is proven by *rendering* a shaped two-character Arabic and
      Hebrew string, not merely by the symbol being present.
- [ ] Font coverage is read from the generated `cmaps` array, not from the
      generator range, and `make check` passes so every `renderable_text()`
      substitution target is confirmed present in all four subsets.
- [ ] A live translation is read on screen for placeholder boxes. Each box is
      one missing code point; decode it rather than guessing.
- [ ] Arabic/Hebrew render stress probe passes repeatedly without reset or
      memory decline.
- [ ] Wi-Fi QR connects the intended 2.4 GHz hotspot and Translator reuses it.
- [ ] Current Translator app and private config are uploaded after firmware.
- [ ] Automatic routing and real translation work in both directions for
      Arabic and Hebrew.
- [ ] Arabic shaping, RTL ordering, mixed numbers/Latin, wrapping, scrolling,
      font hierarchy, and exit/reset behavior are visually checked.
- [ ] Translator logs contain no abrupt truncation, native reset, Python
      exception, `schedule queue full`, or repeated TLS/capture failure.
- [ ] OFL text/font notices ship with distributed firmware sources/artifacts.
- [ ] #9908 and the M5Stack issues above are rechecked before release, in
      case an accepted upstream fix supersedes our backport.

## Rollback and recovery

Keep the exact official image for the board and backups of `/flash` before any
firmware experiment. Prefer targeted recovery:

- If the new app crashes but UIFlow boots, reinstall a known-good
  `/flash/apps/translator.py`; firmware flashing is unnecessary.
- If the launcher fails on an import from `/flash/boot.py`, restore
  `/flash/boot.py.bak` or stage the boot file matching the currently installed
  UIFlow application/system pair.
- If the app and `/system` are mismatched, reflash a known matching pair at
  `0x10000` and `0x9a0000`; do not touch `vfs`.
- If the board only exposes the ROM bootloader, single-click RESET for normal
  boot. Hold RESET only when deliberately entering download mode.
- Use a full official M5Burner recovery only when targeted recovery is
  impossible. Treat full erase, `fs-user.bin`, `flash_all`, or a combined
  `pack_all` image as destructive to `/flash` and NVS unless independently
  proven otherwise. Restore app/config from backup afterward.

Never erase or overwrite a broad flash range just because the screen is black.
Read serial boot output, identify the failing partition or boot script, and
change the smallest recoverable target.

## Secrets and evidence handling

The real configuration belongs only in ignored
`device/config.json` locally and `/flash/res/config.json` on the board. Never
commit, paste, log, screenshot, or include its OpenAI key or Wi-Fi password in
an issue. [The committed example](../device/config.example.json) is the only
configuration safe to publish.

Before sharing logs, redact authorization headers, API keys, SSIDs if sensitive,
transcript content, and unique device/cloud identifiers. Publish firmware
hashes, partition tables, reset causes, stack traces, and bounded synthetic
Arabic/Hebrew probe strings instead. Rotate any credential that was ever
exposed in a terminal capture, chat, issue, or repository history.
