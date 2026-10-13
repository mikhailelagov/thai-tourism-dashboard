import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import send_telegram as t  # noqa: E402


class SanitizeTest(unittest.TestCase):
    def test_unsupported_tags_and_stray_symbols(self):
        self.assertEqual(
            t.sanitize('<h3>Заг</h3><p>5 < 7 & B&B</p><br><b>жир <i>курсив</b>'),
            "Заг5 &lt; 7 &amp; B&amp;B\n<b>жир <i>курсив</i></b>")

    def test_only_web_links_survive(self):
        self.assertEqual(
            t.sanitize('<a href="javascript:alert(1)">x</a> <a href="https://a.th/?q=1&r=2">y</a>'),
            'x <a href="https://a.th/?q=1&amp;r=2">y</a>')

    def test_chunks_fit_and_stay_balanced(self):
        text = ("<b>" + "слово " * 900 + "</b>\n\n") * 3
        parts = t.chunk(text, t.MESSAGE_LIMIT)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), t.MESSAGE_LIMIT)
            self.assertEqual(part.count("<b>"), part.count("</b>"))

    def test_plain_drops_tags(self):
        self.assertEqual(t.plain('<b>a</b> &lt;5% <a href="x">b</a>'), "a <5% b")


class FallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.saved = (t.POST_ERR, t.OFFICIAL)
        t.POST_ERR, t.OFFICIAL = root / "post_error.txt", root / "official.json"

    def tearDown(self):
        t.POST_ERR, t.OFFICIAL = self.saved
        self.tmp.cleanup()

    def payload(self):
        today = datetime.now(t.BANGKOK).date()
        days = [{"iso": (today + timedelta(days=i)).isoformat(), "score": 20 + 5 * i,
                 "tmax": 31, "mm": None if i == 0 else 2.0} for i in range(8)]
        return {"built_at": f"{today.isoformat()}T01:00:00+00:00", "fx": {"THB": 33.5},
                "regions": {"pattaya": days}}

    def test_error_reason_never_reaches_the_channel(self):
        t.POST_ERR.write_text("BadRequestError: 400 credit balance is too low req_x", encoding="utf-8")
        post = t.fallback_post(self.payload())
        self.assertNotIn("Причина", post)
        self.assertNotIn("credit", post)
        self.assertNotIn("спрос на", post.lower().replace("не измерение спроса", ""))
        self.assertIn("осадки н/д", post)
        self.assertIn("Паттайя", post)

    def test_measured_figures_lead_when_collected(self):
        t.OFFICIAL.write_text(json.dumps({
            "arrivals": {"period": "2026-09", "source_url": "https://www.mots.go.th/news/category/817",
                         "total": {"value": 2000339, "yoy_pct": -10.53}},
            "provinces": {"period": "2026-08", "source_url": "https://www.mots.go.th/news/category/820",
                          "rows": {"pattaya": {"label_ru": "Чонбури (включая Паттайю)", "occupancy_pct": 74.99,
                                               "occupancy_change_pp": -2.71}}},
        }), encoding="utf-8")
        post = t.fallback_post(self.payload())
        self.assertIn("<b>2 000 339</b> (-10.5% г/г)", post)
        self.assertIn("Чонбури (включая Паттайю), 2026-08: <b>75.0%</b> (-2.7 п.п. г/г)", post)
        self.assertIn('<a href="https://www.mots.go.th/news/category/817">MOTS</a>', post)


if __name__ == "__main__":
    unittest.main()
