# Translator firmware fonts

These generated LVGL bitmap fonts are intentionally embedded in the custom
CoreS3 firmware. They give heard text a compact 16 px face and translated text
a readable 24 px face without runtime font files or an SD-card dependency.

## Sources

- Cairo 3.130 for Arabic and Latin, pinned from Google Fonts commit
  `d2528f6d1f43e7d9d0d2e1794afe2ad6fd7d56ba`.
- Noto Sans Hebrew for Hebrew and Latin, pinned from Google Fonts commit
  `4c1913251a6dd1ba34a6ef4b7a630178d01b88ff`.

Both font families are licensed under SIL Open Font License 1.1. The generated
subsets remain font software under that license. See [OFL-1.1.txt](OFL-1.1.txt).
The application and UIFlow2 retain their own licenses; the fonts are not
relicensed under either of them.

## Reproduction

The files were generated with `lv_font_conv` 1.5.3. Cairo includes printable
ASCII, Arabic, and Arabic presentation forms required by LVGL contextual
shaping. Noto Sans Hebrew includes printable ASCII, Hebrew, and Hebrew
presentation forms. The 16 px sources use 2 bpp; the 24 px translations use
4 bpp. Kerning is disabled to minimize firmware work on the embedded target.

The complete generator arguments are retained in the header of each `.c`
file. `tools/patch_uiflow_rtl.py` copies and verifies these exact artifacts in
a prepared UIFlow2 checkout.
