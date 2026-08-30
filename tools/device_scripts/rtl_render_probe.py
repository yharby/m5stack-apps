"""Stress LVGL's multibyte BiDi line breaking on an RTL-enabled CoreS3."""

import gc
import time

import lvgl as lv
import M5
import m5ui
import translator_rtl

ARABIC = (
    "هذه تجربة ترجمة عربية طويلة مع أرقام 12345 وعبارة UIFlow2 "
    "للتأكد من التفاف السطور وعرض الحروف بشكل صحيح وآمن."
)
HEBREW = "זוהי בדיקת תרגום ארוכה עם מספרים 12345 וטקסט UIFlow2 לבדיקת כיווניות ושבירת שורות."
WIDTHS = (57, 73, 91, 117, 149, 191, 247, 304)


def style_rtl(label, font, color):
    label.set_style_base_dir(lv.BASE_DIR.RTL, 0)
    label.set_style_text_align(lv.TEXT_ALIGN.RIGHT, 0)
    label.set_style_text_font(font, 0)
    label.set_style_text_color(lv.color_hex(color), 0)
    label.set_long_mode(lv.label.LONG_MODE.WRAP)


def pump(milliseconds=8):
    lv.tick_inc(milliseconds)
    lv.task_handler()
    M5.update()
    time.sleep_ms(milliseconds)


def main():
    assert translator_rtl.ABI_VERSION == 2
    assert translator_rtl.BIDI and translator_rtl.ARABIC_SHAPING
    m5ui.init()
    loop = None
    try:
        loop = m5ui.event_loop.get_instance()
        loop.timer.deinit()
    except Exception:
        loop = None

    screen = lv.obj()
    screen.set_style_bg_color(lv.color_hex(0x000000), 0)
    title = lv.label(screen)
    title.set_text("RTL render stress")
    title.set_pos(8, 6)
    title.set_style_text_color(lv.color_hex(0x00CFFF), 0)
    title.set_style_text_font(lv.font_montserrat_16, 0)

    arabic = lv.label(screen)
    arabic.set_pos(8, 36)
    style_rtl(arabic, lv.translator_cairo_24, 0x40FF70)
    hebrew = lv.label(screen)
    hebrew.set_pos(8, 142)
    style_rtl(hebrew, lv.translator_noto_hebrew_24, 0xE0E0E0)
    lv.screen_load(screen)

    cases = 0
    for text, label in ((ARABIC, arabic), (HEBREW, hebrew)):
        for end in range(1, len(text) + 1):
            sample = text[:end]
            for width in WIDTHS:
                label.set_width(width)
                label.set_text(sample)
                pump()
                cases += 1
        print(
            "rtl_render_probe: completed %s cases=%d"
            % ("Arabic" if text is ARABIC else "Hebrew", cases)
        )

    gc.collect()
    title.set_text("RTL render PASS")
    arabic.set_width(304)
    arabic.set_text("العربية: ناجح")
    hebrew.set_width(304)
    hebrew.set_text("עברית: הצלחה")
    for _ in range(25):
        pump(20)
    print("rtl_render_probe: PASS cases=%d mem_free=%d" % (cases, gc.mem_free()))

    if loop is not None:
        loop.scheduled = 0
    M5.Lcd.lvgl_deinit()
    lv.mp_lv_deinit_gc()


main()
