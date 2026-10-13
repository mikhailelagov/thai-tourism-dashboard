import json
import pathlib
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import write_post as w  # noqa: E402

TODAY = date(2026, 10, 13)


def block(kind, **kw):
    return NS(type=kind, **kw)


class Files(unittest.TestCase):
    """Point the module at temporary copies of its input files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.saved = {k: getattr(w, k) for k in ("OFFICIAL", "NEWS", "INDICATORS", "LAST_POST",
                                                  "HISTORY", "FOOTFALL")}
        for k in self.saved:
            setattr(w, k, root / f"{k.lower()}.json")

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(w, k, v)
        self.tmp.cleanup()

    def write(self, name, data):
        getattr(w, name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def weather(built, start=TODAY):
    days = [{"iso": (start + timedelta(days=i)).isoformat(), "score": 30 + i, "mm": None if i == 2 else 3.0,
             "tmax": 31} for i in range(8)]
    return {"built_at": built, "regions": {"pattaya": days}}


class DigestTest(Files):
    def test_official_figures_carry_period_and_source(self):
        self.write("OFFICIAL", {
            "arrivals": {"period": "2026-09", "status": "preliminary", "published_at": "2026-10-01T03:40:30+00:00",
                         "source_url": "https://www.mots.go.th/news/category/817",
                         "file_url": "https://www.mots.go.th/images/v2022_x.xlsx",
                         "total": {"value": 2000339, "prev_year": 2235852, "yoy_pct": -10.53},
                         "markets": [{"name": "China", "ru": "Китай", "value": 349182, "yoy_pct": 8.67},
                                     {"name": "Russian Federation", "ru": "Россия", "value": 81744,
                                      "yoy_pct": 6.29}]},
            "provinces": {"period": "2026-08", "source_url": "https://www.mots.go.th/news/category/820",
                          "rows": {"pattaya": {"label_ru": "Чонбури (включая Паттайю)", "occupancy_pct": 74.99,
                                               "occupancy_change_pp": -2.71, "foreign_visitors": 804858,
                                               "foreign_yoy_pct": -4.64, "revenue_yoy_pct": -4.47}}},
            "fx": {"cbr": {"date": "2026-10-13", "rub_per_thb": 2.50337,
                           "source_url": "https://www.cbr.ru/scripts/XML_daily.asp"}},
        })
        text = w.summarize(weather(f"{TODAY.isoformat()}T01:00:00+00:00"), TODAY)
        self.assertIn("период 2026-09", text)
        self.assertIn("2 000 339", text)
        self.assertIn("-10.5%", text)
        self.assertIn("Россия: 81 744 (+6.3% г/г)", text)
        self.assertIn("Чонбури (включая Паттайю): загрузка отелей 74.99% (-2.71 п.п. г/г)", text)
        self.assertIn("https://www.mots.go.th/images/v2022_x.xlsx", text)
        self.assertIn("1 THB = 2.5034 RUB", text)

    def test_weather_is_labelled_a_model_and_missing_rain_is_not_dry(self):
        text = w.summarize(weather(f"{TODAY.isoformat()}T01:00:00+00:00"), TODAY)
        self.assertIn("НЕ измерение спроса", text)
        self.assertIn("Паттайя: сегодня 30/100", text)

    def test_stale_weather_is_withheld(self):
        text = w.summarize(weather("2026-10-01T01:00:00+00:00", date(2026, 10, 1)), TODAY)
        self.assertIn("Прогноз погоды устарел", text)
        self.assertNotIn("Паттайя: сегодня", text)

    def test_only_this_weeks_news_in_topic_order(self):
        now = datetime(2026, 10, 12, 5, tzinfo=timezone.utc)
        self.write("NEWS", {"items": [
            {"source": "The Thaiger", "title": "Old story", "url": "https://a.example/old",
             "published_at": (now - timedelta(days=12)).isoformat(), "topics": ["cannabis"]},
            {"source": "TAT Newsroom", "title": "Festival", "url": "https://a.example/fest",
             "published_at": now.isoformat(), "topics": ["events"], "official": True},
            {"source": "DTAM", "title": "Cannabis rule", "url": "https://a.example/rule",
             "published_at": (now - timedelta(days=1)).isoformat(), "topics": ["cannabis"], "official": True},
        ]})
        text = w.summarize(weather(f"{TODAY.isoformat()}T01:00:00+00:00"), TODAY)
        self.assertNotIn("Old story", text)
        self.assertLess(text.index("Cannabis rule"), text.index("Festival"))
        self.assertIn("[официальный]", text)

    def test_last_post_is_flagged_as_not_news(self):
        w.LAST_POST.write_text("<b>Прошлый</b> пост &amp; факты", encoding="utf-8")
        text = w.summarize(weather(f"{TODAY.isoformat()}T01:00:00+00:00"), TODAY)
        self.assertIn("Прошлый выпуск", text)
        self.assertIn("Прошлый пост & факты", text)


class ResponseTest(unittest.TestCase):
    def test_extract_text_skips_preamble_and_thinking(self):
        message = NS(content=[
            block("text", text="Сейчас поищу."),
            block("server_tool_use"),
            block("web_search_tool_result", content=[]),
            block("thinking", thinking=""),
            block("text", text="<b>Пост</b>"),
        ])
        self.assertEqual(w.extract_text(message), "<b>Пост</b>")

    def test_links_the_model_never_saw_are_removed(self):
        post = ('<a href="https://ok.example/a">есть</a> и <a href="https://made.up/x">нет</a> '
                '<a href="https://ok.example/b?x=1&amp;y=2">экранированная</a>')
        allowed = {"https://ok.example/a", "https://ok.example/b?x=1&y=2"}
        self.assertEqual(w.strip_unseen_links(post, allowed),
                         '<a href="https://ok.example/a">есть</a> и нет '
                         '<a href="https://ok.example/b?x=1&amp;y=2">экранированная</a>')

    def test_known_urls_drop_trailing_punctuation(self):
        self.assertEqual(w.known_urls("источник https://x.example/f.xlsx: и https://y.example."),
                         {"https://x.example/f.xlsx", "https://y.example"})

    def test_sources_seen_collects_results_fetches_and_quotes(self):
        blocks = [
            block("web_search_tool_result", content=[NS(url="https://s.example", title="S", page_age="Oct 10")]),
            block("web_fetch_tool_result", content=NS(type="web_fetch_result", url="https://f.example",
                                                      retrieved_at="2026-10-13", content=NS(title="F"))),
            block("text", text="x", citations=[NS(url="https://s.example", title="S", cited_text="цитата")]),
        ]
        seen = w.sources_seen(blocks)
        self.assertEqual(set(seen), {"https://s.example", "https://f.example"})
        self.assertEqual(seen["https://s.example"]["quotes"], ["цитата"])

    def test_trim_keeps_title_and_advice(self):
        def broken(**kw):
            raise RuntimeError("no network")
        paras = ["<b>Заголовок</b>"] + [f"Блок {i} " + "x" * 900 for i in range(6)] + ["✅ <b>Что делать</b>\n— пункт"]
        text = w.shorten(broken, "\n\n".join(paras))
        self.assertLessEqual(len(text), w.MAX_CHARS)
        self.assertTrue(text.startswith("<b>Заголовок</b>"))
        self.assertIn("Что делать", text)

    def test_usage_line_estimates_cost(self):
        usage = w.Usage()
        usage.add(NS(usage=NS(input_tokens=100000, output_tokens=10000, cache_creation_input_tokens=0,
                              cache_read_input_tokens=0,
                              server_tool_use=NS(web_search_requests=5, web_fetch_requests=2))))
        self.assertIn("about $0.65", usage.line())


if __name__ == "__main__":
    unittest.main()
