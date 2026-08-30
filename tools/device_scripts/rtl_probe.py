"""Bounded, read-only LVGL/RTL capability probe for Translator firmware."""

import lvgl as lv

print("lvgl: available")
for name in (
    "AlibabaSans_JP24",
    "AlibabaSans_KR24",
    "AlibabaPuHuiTi_CN24",
    "font_dejavu_16_persian_hebrew",
    "translator_cairo_16",
    "translator_cairo_24",
    "translator_noto_hebrew_16",
    "translator_noto_hebrew_24",
):
    print("font:%s=%s" % (name, hasattr(lv, name)))

try:
    import translator_rtl

    print("rtl_abi:%s" % translator_rtl.ABI_VERSION)
    print("rtl_bidi:%s" % translator_rtl.BIDI)
    print("rtl_arabic_shaping:%s" % translator_rtl.ARABIC_SHAPING)
    print("rtl_font_license:%s" % translator_rtl.FONT_LICENSE)
except Exception as error:
    print("rtl_abi:missing (%r)" % error)
