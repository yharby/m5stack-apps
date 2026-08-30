"""Report which code points the embedded font subsets cannot draw.

An empty box on screen is LVGL's missing-glyph placeholder, not a shaping
fault. `lv_font_get_glyph_dsc_internal()` walks the font and its fallback
chain, and when nothing supplies the code point it sets `is_placeholder` and
returns a box half a line high. This probe asks the running firmware that same
question directly, so the answer comes from the fonts actually linked into the
image rather than from the generator arguments, which only describe what was
requested.

Bounded and read only. It creates no widgets and writes no files.
"""

import lvgl as lv
import m5ui

m5ui.init()

FONTS = (
    ("translator_cairo_16", "translator_cairo_16"),
    ("translator_cairo_24", "translator_cairo_24"),
    ("translator_noto_hebrew_16", "translator_noto_hebrew_16"),
    ("translator_noto_hebrew_24", "translator_noto_hebrew_24"),
    ("font_dejavu_16_persian_hebrew", "font_dejavu_16_persian_hebrew"),
)

# Groups worth naming in the output. Everything else is swept by range below.
GROUPS = (
    ("ascii", range(0x20, 0x7F)),
    ("latin-1", range(0xA0, 0x100)),
    ("general-punct", range(0x2000, 0x2070)),
    ("arabic", range(0x600, 0x700)),
    ("arabic-pres-a", range(0xFB50, 0xFE00)),
    ("arabic-pres-b", range(0xFE70, 0xFF00)),
    ("hebrew", range(0x590, 0x600)),
    ("hebrew-pres", range(0xFB1D, 0xFB50)),
)


def missing(font, codes):
    """Code points the font itself cannot supply.

    The struct callback takes the font as its first argument and reports the
    answer in its return value. `is_placeholder` is set by the outer
    `lv_font_get_glyph_dsc_internal()` wrapper after the whole fallback chain
    has been tried, so it stays clear here and must not be used as the test.
    """
    dsc = lv.font_glyph_dsc_t()
    out = []
    for code in codes:
        try:
            found = font.get_glyph_dsc(font, dsc, code, 0)
        except Exception:
            out.append(code)
            continue
        if not found:
            out.append(code)
    return out


def main():
    for label, attr in FONTS:
        font = getattr(lv, attr, None)
        if font is None:
            print("font %s: ABSENT" % label)
            continue
        print("font %s: line_height=%d" % (label, font.line_height))
        for name, codes in GROUPS:
            gaps = missing(font, codes)
            total = len(codes)
            if not gaps:
                print("  %-14s %3d/%3d ok" % (name, total, total))
                continue
            shown = " ".join("%04X" % c for c in gaps[:24])
            more = "" if len(gaps) <= 24 else " +%d more" % (len(gaps) - 24)
            print(
                "  %-14s %3d/%3d ok, %d missing: %s%s"
                % (name, total - len(gaps), total, len(gaps), shown, more)
            )
    print("rtl_glyph_probe: DONE")


main()
