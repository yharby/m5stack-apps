import ast
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "device" / "apps" / "translator.py"
FONT_SOURCES = sorted((ROOT / "firmware" / "fonts").glob("*/*.c"))


def font_codepoints(path):
    """Return the code points an lv_font_conv generated subset can actually draw.

    The cmaps array is the same table LVGL consults at runtime, so a code point
    absent here is one that takes the placeholder path and draws an empty box.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lists = {
        name: [int(v, 0) for v in re.findall(r"0x[0-9a-fA-F]+|\b\d+\b", body)]
        for name, body in re.findall(
            r"static const uint16_t (\w+)\[\]\s*=\s*\{(.*?)\};", text, re.S
        )
    }
    body = re.search(r"cmaps\[\]\s*=\s*\{(.*?)\n\};", text, re.S)
    if body is None:
        raise AssertionError(f"no cmaps array in {path}")
    covered = set()
    for entry in re.findall(r"\{(.*?)\}", body.group(1), re.S):

        def field(key, entry=entry):
            found = re.search(r"\." + key + r"\s*=\s*([^,\n]+)", entry)
            return found.group(1).strip() if found else None

        start = int(field("range_start"), 0)
        if "SPARSE" in (field("type") or ""):
            covered.update(start + v for v in lists.get(field("unicode_list"), []))
        else:
            covered.update(range(start, start + int(field("range_length"), 0)))
    return covered


def load_translator():
    """Load device logic without importing hardware modules or calling run()."""
    tree = ast.parse(APP.read_text(), filename=str(APP))
    terminal = tree.body[-1]
    if not (
        isinstance(terminal, ast.Expr)
        and isinstance(terminal.value, ast.Call)
        and isinstance(terminal.value.func, ast.Name)
        and terminal.value.func.id == "run"
    ):
        raise AssertionError("translator.py must end in exactly one run() call")
    tree.body.pop()
    ast.fix_missing_locations(tree)

    fake_m5 = types.ModuleType("M5")
    fake_m5.BtnPWR = types.SimpleNamespace(wasClicked=lambda: False)
    fake_lv = types.ModuleType("lvgl")
    fake_m5ui = types.ModuleType("m5ui")
    fake_network = types.ModuleType("network")
    fake_requests = types.ModuleType("requests2")
    modules = {
        "M5": fake_m5,
        "lvgl": fake_lv,
        "m5ui": fake_m5ui,
        "network": fake_network,
        "requests2": fake_requests,
    }
    namespace = {"__name__": "translator_under_test"}
    with mock.patch.dict(sys.modules, modules):
        exec(compile(tree, str(APP), "exec"), namespace)
    namespace["screen_mode"] = "test"
    return namespace


class TranslatorLanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_translator()

    def setUp(self):
        self.app["CFG"]["language_pair"] = ["en", "ar"]
        self.app["CFG"]["source_mode"] = "auto"
        self.app["CFG"]["history_turns"] = 3
        self.app["turns"].clear()
        self.app["active_turn"] = None
        self.app["follow_latest"] = True
        self.app["new_turns_while_scrolled"] = 0
        self.app["feed_revision"] = 0
        self.app["exit_requested"] = False
        self.app["stop_requested"] = False
        self.app["ui_action"] = None

    def test_aliases_cover_supported_languages(self):
        normalize = self.app["normalize_language_code"]
        expected = {
            "English": "en",
            "ja-JP": "ja",
            "kor": "ko",
            "cmn-Hans": "zh",
            "ara": "ar",
            "heb": "he",
            "mon": "mn",
        }
        for value, code in expected.items():
            with self.subTest(value=value):
                self.assertEqual(normalize(value), code)

    def test_script_detection(self):
        detect = self.app["script_language"]
        samples = {
            "English speech about STAC": "en",
            "مرحبا بالعالم": "ar",
            "שלום עולם": "he",
            "한국어 번역": "ko",
            "これは日本語です": "ja",
            "中文翻译": "zh",
            "Монгол хэл": "mn",
        }
        for text, code in samples.items():
            with self.subTest(text=text):
                self.assertEqual(detect(text), code)

    def test_route_uses_script_and_api_metadata(self):
        route = self.app["resolve_route"]
        self.assertEqual(route("مرحبا STAC", "en"), ("ar", "en"))
        self.assertEqual(route("hello", "en-US"), ("en", "ar"))
        self.assertEqual(route("שלום", "he"), ("", ""))

        self.app["CFG"]["language_pair"] = ["ja", "en"]
        self.assertEqual(route("English must not follow pair order", ""), ("en", "ja"))

        self.app["CFG"]["language_pair"] = ["ja", "zh"]
        self.assertEqual(route("衛星画像", "ja"), ("ja", "zh"))
        self.assertEqual(route("卫星图像", "zh"), ("zh", "ja"))

    def test_fixed_source_mode_is_respected(self):
        self.app["CFG"]["source_mode"] = "ar"
        self.assertEqual(self.app["resolve_route"]("EPSG:4326", "en"), ("ar", "en"))

    def test_turn_text_stays_logical_and_history_is_bounded(self):
        begin = self.app["begin_turn"]
        complete = self.app["complete_turn"]
        logical = "مرحبا STAC (EPSG:4326)"
        first = begin(logical, "ar", "en")
        complete(first, "Hello STAC (EPSG:4326)")
        self.assertEqual(first["source_text"], logical)

        begin("اثنان", "ar", "en")
        begin("ثلاثة", "ar", "en")
        begin("أربعة", "ar", "en")
        self.assertEqual(len(self.app["turns"]), 3)
        self.assertNotIn(first, self.app["turns"])

    def test_paused_follow_counts_only_new_turns(self):
        self.app["follow_latest"] = False
        turn = self.app["begin_turn"]("مرحبا", "ar", "en")
        self.assertEqual(self.app["new_turns_while_scrolled"], 1)
        self.app["complete_turn"](turn, "Hello")
        self.assertEqual(self.app["new_turns_while_scrolled"], 1)

    def test_rtl_pair_requires_explicit_firmware_abi_and_font(self):
        self.app["rtl_firmware_abi"] = 0
        self.app["font_ar_source"] = object()
        self.app["font_ar_translation"] = object()
        self.assertEqual(self.app["pair_render_error"](), "RTL firmware required")
        self.app["rtl_firmware_abi"] = 2
        self.assertEqual(self.app["pair_render_error"](), "")

    def test_rtl_translation_fonts_are_larger_distinct_faces(self):
        ar_small, ar_large = object(), object()
        he_small, he_large = object(), object()
        self.app["font_ar_source"] = ar_small
        self.app["font_ar_translation"] = ar_large
        self.app["font_he_source"] = he_small
        self.app["font_he_translation"] = he_large
        self.assertIs(self.app["font_for_language"]("ar", False), ar_small)
        self.assertIs(self.app["font_for_language"]("ar", True), ar_large)
        self.assertIs(self.app["font_for_language"]("he", False), he_small)
        self.assertIs(self.app["font_for_language"]("he", True), he_large)

    def test_every_main_control_has_a_44_pixel_touch_target(self):
        for name in ("EXIT_BOX", "LANGUAGE_BOX", "RUN_BOX", "LIVE_BOX", "GEAR_HIT_BOX"):
            x, y, width, height = self.app[name]
            with self.subTest(control=name):
                self.assertGreaterEqual(width, 44)
                self.assertGreaterEqual(height, 44)
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + width, self.app["W"])
                self.assertLessEqual(y + height, self.app["H"])

    def test_exit_callback_only_latches_cleanup_request(self):
        self.app["_on_exit"](None)
        self.assertTrue(self.app["exit_requested"])
        self.assertTrue(self.app["stop_requested"])
        self.assertEqual(self.app["ui_action"], ("exit",))

    def test_english_translation_uses_larger_font_than_heard_text(self):
        small = object()
        large = object()
        self.app["font_label"] = small
        self.app["font_trans_en"] = large
        self.assertIs(self.app["font_for_language"]("en", False), small)
        self.assertIs(self.app["font_for_language"]("en", True), large)


class RenderableTextTest(unittest.TestCase):
    """The embedded subsets stop at U+007E, so unmapped punctuation draws a box."""

    @classmethod
    def setUpClass(cls):
        cls.app = load_translator()

    def test_typographic_punctuation_folds_to_ascii(self):
        fold = self.app["renderable_text"]
        self.assertEqual(fold("don\u2019t"), "don't")
        self.assertEqual(fold("\u201cquoted\u201d"), '"quoted"')
        self.assertEqual(fold("a\u2014b"), "a-b")
        self.assertEqual(fold("wait\u2026"), "wait...")
        self.assertEqual(fold("a\u00a0b"), "a b")

    def test_invisible_formatting_codes_are_dropped(self):
        fold = self.app["renderable_text"]
        self.assertEqual(fold("a\u200fb\u202ac\ufeff"), "abc")

    def test_script_text_is_passed_through_unchanged(self):
        fold = self.app["renderable_text"]
        for sample in (
            "\u0645\u0631\u062d\u0628\u0627",
            "\u05e9\u05dc\u05d5\u05dd",
            "\u3053\u3093\u306b\u3061\u306f",
            "plain ascii",
        ):
            with self.subTest(sample=sample):
                self.assertEqual(fold(sample), sample)

    def test_empty_and_none_are_returned_unchanged(self):
        fold = self.app["renderable_text"]
        self.assertEqual(fold(""), "")
        self.assertIsNone(fold(None))

    def test_every_substitution_target_is_drawable_by_both_fonts(self):
        covered = [font_codepoints(path) for path in FONT_SOURCES]
        self.assertTrue(covered, "no generated fonts found")
        targets = set()
        for replacement in self.app["GLYPH_SUBSTITUTIONS"].values():
            targets.update(ord(ch) for ch in replacement)
        for path, cps in zip(FONT_SOURCES, covered, strict=True):
            missing = sorted(c for c in targets if c not in cps)
            with self.subTest(font=path.name):
                self.assertEqual(
                    missing,
                    [],
                    "substitution targets absent from the subset: "
                    + " ".join(f"U+{c:04X}" for c in missing),
                )


if __name__ == "__main__":
    unittest.main()
