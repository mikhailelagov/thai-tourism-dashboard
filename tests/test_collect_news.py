"""Offline checks for the news collector: parsing, tagging, merging and failures."""

import contextlib
from datetime import datetime, timedelta, timezone
import email.message
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "collect_news.py"
SPEC = importlib.util.spec_from_file_location("collect_news", MODULE)
news = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(news)
NOW = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)  # 10:00 Bangkok
CUTOFF = NOW - timedelta(days=10)
SOURCES = {source["name"]: source for source in news.SOURCES}

# Layouts mirror the live responses; titles and links are synthetic.
TAT_JSON = json.dumps([
    {"id": 2, "date_gmt": "2026-10-12T11:34:51", "modified_gmt": "2026-10-12T11:34:52",
     "link": "https://www.tatnews.org/2026/10/tat-new-route/",
     "title": {"rendered": "TAT welcomes new Kuala Lumpur&#8211;Bangkok service"}},
    {"id": 1, "date_gmt": "2026-10-09T07:51:17",
     "link": "https://www.tatnews.org/2026/10/award/",
     "title": {"rendered": "Bangkok named World&#8217;s <em>Best</em> City"}},
]).encode()
DTAM_JSON = json.dumps([
    {"id": 9, "date": "2026-10-10T09:11:56", "date_gmt": "2026-10-10T02:11:56",
     "link": "https://www.dtam.moph.go.th/dtam-news/9/",
     "title": {"rendered": "เปิดสาระสำคัญร่าง พ.ร.บ.ควบคุมกัญชา"}},
    {"id": 8, "date": "2026-10-11T09:00:00", "date_gmt": "2026-10-11T02:00:00",
     "link": "https://www.dtam.moph.go.th/dtam-news/8/",
     "title": {"rendered": "ขมิ้นชัน ลดอาการข้อเสื่อม"}},
], ensure_ascii=False).encode()
KHAOSOD_RSS = b"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:atom="http://www.w3.org/2005/Atom">
<channel><title>Khaosod English</title>
<atom:link href="https://www.khaosodenglish.com/feed/" rel="self" type="application/rss+xml" />
<item><title>Cabinet approves 1,000 baht departure tax &amp; new visa rules</title>
<link>https://www.khaosodenglish.com/news/2026/10/12/departure-tax/</link>
<dc:creator><![CDATA[Khaosod English]]></dc:creator>
<pubDate>Mon, 12 Oct 2026 12:21:26 +0000</pubDate></item>
<item><title>Provincial governor opens new school</title>
<link>https://www.khaosodenglish.com/news/2026/10/12/school/</link>
<pubDate>Mon, 12 Oct 2026 11:56:01 +0000</pubDate></item>
<item><title>Undated cannabis story</title>
<link>https://www.khaosodenglish.com/news/undated/</link></item>
</channel></rss>"""
PHUKET_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Phuket News</title>
<item><title><![CDATA[Phuket Vegetarian Festival begins in Old Town]]></title>
<link>https://www.thephuketnews.com/vegetarian-festival-101899.php</link>
<pubDate>2026-10-12T18:41+07:00</pubDate></item>
<item><title><![CDATA[Chalong market reopens]]></title>
<link>https://www.thephuketnews.com/market-101896.php</link>
<pubDate>2026-10-12T12:58+07:00</pubDate></item>
</channel></rss>"""
NATION_SITEMAP = b"""<?xml version = "1.0" encoding = "UTF-8" ?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
 xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://www.nationthailand.com</loc><lastmod>2026-10-13</lastmod></url>
<url><loc>https://www.nationthailand.com/category/business</loc></url>
<url><loc>https://www.nationthailand.com/news/1</loc><news:news>
<news:publication><news:name>Nationthailand</news:name><news:language>th</news:language></news:publication>
<news:publication_date>2026-10-13T09:21:00+07:00</news:publication_date>
<news:title><![CDATA[ONWR warns of heavy rain as reservoirs reach 82% capacity]]></news:title>
<news:keywords><![CDATA[thailand news, pattaya, samui]]></news:keywords>
</news:news></url>
<url><loc>https://www.nationthailand.com/news/2</loc><news:news>
<news:publication_date>2026-10-13T09:36:00+07:00</news:publication_date>
<news:title><![CDATA[Global smartphone shipments fall 20% to 14-year low]]></news:title>
</news:news></url>
</urlset>"""


def headers(**values):
    message = email.message.Message()
    for key, value in values.items():
        message[key.replace("_", "-")] = value
    return message


def item(source="The Thaiger", title="Pattaya hotels report record bookings",
         url="https://thethaiger.com/news/pattaya/a", published="2026-10-12T10:00:00+00:00",
         **extra):
    value = {"source": source, "title": title, "url": url, "published_at": published,
             "lang": "en", "topics": [], "official": False}
    value.update(extra)
    return value


def fake_fetch(responses):
    """Serve canned bodies by URL prefix; anything unknown fails like a closed port."""
    calls = []

    def fetch(url, accept):
        calls.append(url)
        for prefix, result in responses.items():
            if url.startswith(prefix):
                if isinstance(result, Exception):
                    raise result
                return result, headers(X_WP_TotalPages="1")
        raise urllib.error.URLError("connection refused")
    return fetch, calls


def run(path, responses, argv=None):
    fetch, calls = fake_fetch(responses)
    out = io.StringIO()
    with patch.object(news, "fetch", fetch), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(io.StringIO()):
        code = news.main(argv or [], output_path=path, now=NOW)
    return code, json.loads(Path(path).read_text(encoding="utf-8")), out.getvalue(), calls


class TitleAndTopicTests(unittest.TestCase):
    def test_title_is_plain_text_without_emoji_but_keeps_thai_marks(self):
        self.assertEqual(news.clean_title("Bangkok named World&#8217;s <em>Best</em> City"),
                         "Bangkok named World’s Best City")
        self.assertEqual(news.clean_title("Fish &amp;amp; chips"), "Fish & chips")
        self.assertEqual(news.clean_title("\U0001f315\U0001f3dd\ufe0f เมืองพัทยาประชุมเตรียมพร้อมลอยกระทง"),
                         "เมืองพัทยาประชุมเตรียมพร้อมลอยกระทง")
        self.assertEqual(news.clean_title("Heat reaches 38\u00b0C\u200b in Bangkok"),
                         "Heat reaches 38\u00b0C in Bangkok")
        # U+1FA75 is newer than Python 3.9's Unicode tables; the title must not
        # depend on the Python version.
        self.assertEqual(news.clean_title("\u0e40\u0e2b\u0e25\u0e37\u0e2d\u0e07\u0e1f\u0e49\u0e32 \u0e40\u0e01\u0e21\u0e2a\u0e4c\U0001fa75(\u0e09\u0e1a\u0e31\u0e1a\u0e17\u0e35\u0e48 1,034/69)"),
                         "\u0e40\u0e2b\u0e25\u0e37\u0e2d\u0e07\u0e1f\u0e49\u0e32 \u0e40\u0e01\u0e21\u0e2a\u0e4c (\u0e09\u0e1a\u0e31\u0e1a\u0e17\u0e35\u0e48 1,034/69)")

    def test_cannabis_needs_cannabis_words(self):
        for title in ("Police seize kratom and ecstasy pills",
                      "Officials vow to weed out corrupt officers",
                      "Seaweed farmers expand", "Meth and kratom haul at checkpoint"):
            self.assertNotIn("cannabis", news.tag(title), title)
        for title in ("New cannabis rules for shops", "Weed shop raided on Beach Road",
                      "Marijuana dispensaries must hire doctors",
                      "ร่าง พ.ร.บ.ควบคุมกัญชา เสร็จสมบูรณ์"):
            self.assertIn("cannabis", news.tag(title), title)

    def test_word_boundaries_and_thai_compounds(self):
        self.assertEqual(news.tag("Barcelona fans visit Bangkok banks"), ["bangkok"])
        self.assertNotIn("alcohol", news.tag("Man put behind bars for fraud"))
        self.assertNotIn("alcohol", news.tag("Drinking water shortage on Samui"))
        self.assertNotIn("alcohol", news.tag("สุราษฎร์ธานี ประชุม"))
        self.assertEqual(news.tag("ท่าอากาศยานภูเก็ต"), ["flights", "phuket"])
        self.assertEqual(news.tag("เฉลิมฉลองวันเกิด"), ["events"])
        self.assertEqual(news.tag("Thai Hotels Association rejects 1,000-baht departure tax"),
                         ["entry_rules"])

    def test_enforcement_and_records_are_not_rules_or_statistics(self):
        self.assertNotIn("entry_rules", news.tag("Thai immigration arrests man wanted abroad"))
        self.assertNotIn("entry_rules", news.tag("193 Thais deported from Cambodia"))
        self.assertIn("entry_rules", news.tag("New immigration rules for long-stay visitors"))
        self.assertNotIn("statistics", news.tag("พัทยาสร้างสถิติกระโดดร่มเชิญธงชาติ"))
        self.assertIn("statistics", news.tag("สถิตินักท่องเที่ยวต่างชาติเดือนกันยายน"))
        self.assertNotIn("safety", news.tag("Brent gains after tanker attacks"))
        self.assertNotIn("events", news.tag("Music teacher returns home"))
        self.assertNotIn("alcohol", news.tag("ตรวจสถานบริการน้ำมันในพัทยา"))
        self.assertNotIn("alcohol", news.tag("พัฒนาสถานบริการสาธารณสุข"))
        self.assertIn("alcohol", news.tag("ตรวจสถานบริการย่านวอล์กกิ้งสตรีท"))
        self.assertEqual(news.tag("Kamala Harris visits Bangkok"), ["bangkok"])
        self.assertIn("phuket", news.tag("Kamala beach clean-up"))

    def test_keep_rule(self):
        self.assertTrue(news.relevant(["weather", "bangkok"], False))
        self.assertTrue(news.relevant(["pattaya"], False))
        self.assertTrue(news.relevant([], True))
        self.assertFalse(news.relevant(["phuket", "bangkok"], False))
        self.assertFalse(news.relevant([], False))

    def test_language_follows_the_script(self):
        self.assertEqual(news.language("เมืองพัทยา Smart City"), "th")
        self.assertEqual(news.language("Pattaya ลอยกระทง festival schedule"), "en")


class ParsingTests(unittest.TestCase):
    def test_times_need_a_zone_except_wordpress_gmt(self):
        self.assertEqual(news.parse_time("2026-10-12T18:41+07:00"),
                         datetime(2026, 10, 12, 11, 41, tzinfo=timezone.utc))
        self.assertEqual(news.parse_time("Mon, 12 Oct 2026 12:21:26 +0000"),
                         datetime(2026, 10, 12, 12, 21, 26, tzinfo=timezone.utc))
        self.assertEqual(news.parse_time("2026-10-12T12:00:00Z"),
                         datetime(2026, 10, 12, 12, tzinfo=timezone.utc))
        self.assertIsNone(news.parse_time("2026-10-12T11:34:51"))
        self.assertEqual(news.parse_time("2026-10-12T11:34:51", naive_utc=True),
                         datetime(2026, 10, 12, 11, 34, 51, tzinfo=timezone.utc))
        for value in ("", None, "yesterday", "Mon, 12 Oct 2026 12:21:26 -0000"):
            self.assertIsNone(news.parse_time(value), value)

    def test_rss_and_atom(self):
        entries = news.feed_entries(KHAOSOD_RSS)
        self.assertEqual(entries[0], ("Cabinet approves 1,000 baht departure tax & new visa rules",
                                      "https://www.khaosodenglish.com/news/2026/10/12/departure-tax/",
                                      datetime(2026, 10, 12, 12, 21, 26, tzinfo=timezone.utc)))
        self.assertIsNone(entries[2][2])
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Flight news</title>
<link rel="alternate" href="https://example.org/a"/><updated>2026-10-12T01:00:00Z</updated>
</entry></feed>"""
        self.assertEqual(news.feed_entries(atom), [
            ("Flight news", "https://example.org/a", datetime(2026, 10, 12, 1, tzinfo=timezone.utc))])

    def test_iso_pubdate_without_seconds(self):
        entries = news.feed_entries(PHUKET_RSS)
        self.assertEqual(entries[0][2], datetime(2026, 10, 12, 11, 41, tzinfo=timezone.utc))

    def test_sitemap_skips_pages_without_news_block(self):
        entries = news.sitemap_entries(NATION_SITEMAP)
        self.assertEqual([entry[1] for entry in entries], ["https://www.nationthailand.com/news/1",
                                                           "https://www.nationthailand.com/news/2"])
        self.assertEqual(entries[0][2], datetime(2026, 10, 13, 2, 21, tzinfo=timezone.utc))

    def test_xml_with_doctype_or_html_challenge_is_rejected(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss>&a;</rss>'
        with self.assertRaises(news.SourceError):
            news.feed_entries(bomb)
        with self.assertRaises(Exception) as caught:
            news.feed_entries(b"<html><body>Just a moment...</body></html>")
        self.assertEqual(news.error_label(caught.exception), "invalid_response")

    def test_wordpress_paging_and_partial_failure(self):
        pages = {1: (json.dumps([{"date_gmt": "2026-10-12T01:00:00", "link": "https://x.org/1",
                                  "title": {"rendered": "Airport news"}}]).encode(),
                     headers(X_WP_TotalPages="5"))}
        requested = []

        def fetch(url, accept):
            requested.append(url)
            page = int(url.rsplit("page=", 1)[1].split("&")[0])
            if page in pages:
                return pages[page]
            raise urllib.error.HTTPError(url, 503, "Unavailable", headers(), None)

        with patch.object(news, "fetch", fetch):
            entries, partial = news.collect_source(SOURCES["The Thaiger"], CUTOFF)
        self.assertEqual(len(entries), 1)
        self.assertEqual(partial, "page 2: http_503")
        self.assertIn("after=2026-10-02T03%3A00%3A00Z", requested[0])
        self.assertIn("orderby=date", requested[0])

    def test_dtam_search_is_encoded_and_titles_must_be_about_cannabis(self):
        source = SOURCES["DTAM (Ministry of Public Health)"]
        fetch, calls = fake_fetch({source["endpoint"]: DTAM_JSON})
        with patch.object(news, "fetch", fetch):
            entries, _ = news.collect_source(source, CUTOFF)
        self.assertIn("search=%E0%B8%81%E0%B8%B1%E0%B8%8D%E0%B8%8A%E0%B8%B2", calls[0])
        items = [news.make_item(source, *entry) for entry in entries]
        self.assertEqual(items[0]["published_at"], "2026-10-10T02:11:56+00:00")
        self.assertEqual(items[0]["lang"], "th")
        self.assertIsNone(items[1])

    def test_dtam_falls_back_to_rss_when_rest_is_not_json(self):
        source = SOURCES["DTAM (Ministry of Public Health)"]
        rss = ("""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
            <item><title>ประกาศควบคุมกัญชา ฉบับใหม่</title><link>https://www.dtam.moph.go.th/a/1/</link>
            <pubDate>Sat, 10 Oct 2026 02:11:56 +0000</pubDate></item>
            <item><title>ประกาศรายชื่อผู้สอบผ่าน</title><link>https://www.dtam.moph.go.th/a/2/</link>
            <pubDate>Sat, 10 Oct 2026 03:00:00 +0000</pubDate></item>
            </channel></rss>""").encode("utf-8")
        fetch, calls = fake_fetch({source["endpoint"]: b"<html>blocked</html>",
                                   source["fallback_feed"]: rss})
        with patch.object(news, "fetch", fetch):
            entries, partial = news.collect_source(source, CUTOFF)
        self.assertEqual(partial, "REST invalid_response, RSS used")
        items = [news.make_item(source, *entry) for entry in entries]
        self.assertEqual(items[0]["url"], "https://www.dtam.moph.go.th/a/1/")
        self.assertIsNone(items[1])


class FetchTests(unittest.TestCase):
    class Response:
        def __init__(self, body):
            self.body, self.headers = body, headers()

        def read(self, limit):
            return self.body[:limit]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(self, *outcomes):
        sequence = list(outcomes)
        requests = []

        class Opener:
            def open(self, request, timeout):
                requests.append((request, timeout))
                outcome = sequence.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return FetchTests.Response(outcome)
        return Opener(), requests

    def test_honest_agent_and_one_retry_for_transient_errors(self):
        opener, requests = self.opener(urllib.error.URLError("reset"), b"<rss/>")
        with patch.object(news, "OPENER", opener), patch.object(news.time, "sleep"):
            body, _ = news.fetch("https://example.org/feed", news.XML_ACCEPT)
        self.assertEqual(body, b"<rss/>")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][0].get_header("User-agent"), news.USER_AGENT)
        self.assertEqual(requests[0][1], 30)

    def test_truncated_response_is_retried_and_labelled(self):
        opener, requests = self.opener(news.http.client.IncompleteRead(b"<rss"),
                                       news.http.client.IncompleteRead(b"<rss"))
        with patch.object(news, "OPENER", opener), patch.object(news.time, "sleep"):
            with self.assertRaises(news.http.client.IncompleteRead) as caught:
                news.fetch("https://example.org/feed", news.XML_ACCEPT)
        self.assertEqual(len(requests), 2)
        self.assertEqual(news.error_label(caught.exception), "network_error")

    def test_at_most_one_retry(self):
        opener, requests = self.opener(b"  ", b"")
        with patch.object(news, "OPENER", opener), patch.object(news.time, "sleep"):
            with self.assertRaises(news.SourceError) as caught:
                news.fetch("https://example.org/feed", news.XML_ACCEPT)
        self.assertEqual(str(caught.exception), "empty_response")
        self.assertEqual(len(requests), 2)

    def test_refusals_and_challenges_are_not_retried(self):
        refused = urllib.error.HTTPError("https://x.org", 403, "Forbidden", headers(), None)
        challenge = urllib.error.HTTPError("https://x.org", 503, "Busy",
                                           headers(cf_mitigated="challenge"), None)
        for error, label in ((refused, "http_403"), (challenge, "bot_challenge")):
            opener, requests = self.opener(error, b"<rss/>")
            with patch.object(news, "OPENER", opener), patch.object(news.time, "sleep"):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    news.fetch("https://x.org/feed", news.XML_ACCEPT)
            self.assertEqual(len(requests), 1)
            self.assertEqual(news.error_label(caught.exception), label)

    def test_offsite_redirects_are_refused(self):
        handler = news.SameSiteRedirect()
        request = urllib.request.Request("https://www.nationthailand.com/sitemap/latest")
        moved = handler.redirect_request(request, None, 301, "Moved", {},
                                         "https://api.nationthailand.com/sitemap/latest")
        self.assertEqual(moved.full_url, "https://api.nationthailand.com/sitemap/latest")
        with self.assertRaises(news.SourceError) as caught:
            handler.redirect_request(request, None, 302, "Found", {},
                                     "https://paywall.example.com/pay?url=x")
        self.assertEqual(str(caught.exception), "offsite_redirect")
        # TollBit's paywall sits on the publisher's own subdomain.
        with self.assertRaises(news.SourceError) as caught:
            handler.redirect_request(request, None, 307, "Temporary Redirect", {},
                                     "https://tollbit.nationthailand.com/?url=x")
        self.assertEqual(str(caught.exception), "paywall_redirect")


class AssembleTests(unittest.TestCase):
    def test_window_future_and_url_dedupe_prefer_fresh(self):
        fresh = [item(title="Pattaya hotels report record bookings (updated)",
                      url="https://thethaiger.com/news/pattaya/a/?utm_source=x")]
        previous = [item(), item(url="https://thethaiger.com/old",
                                 published="2026-10-01T00:00:00+00:00"),
                    item(url="https://thethaiger.com/future", published="2026-10-13T05:00:00+00:00")]
        result = news.assemble(previous, [news.make_item(SOURCES["The Thaiger"], i["title"], i["url"],
                                                         news.parse_time(i["published_at"]))
                                          for i in fresh], CUTOFF, NOW)
        self.assertEqual([i["title"] for i in result], ["Pattaya hotels report record bookings (updated)"])
        self.assertEqual(result[0]["topics"], ["statistics", "pattaya"])

    def test_dropped_sources_and_irrelevant_previous_items_are_not_carried(self):
        previous = [item(source="Bangkok Post", title="Visa rules change"),
                    item(title="Provincial governor opens school", url="https://thethaiger.com/b")]
        self.assertEqual(news.assemble(previous, [], CUTOFF, NOW), [])

    def test_near_identical_titles_keep_official_but_dated_series_stay(self):
        source = SOURCES["The Thaiger"]
        stamp = news.parse_time("2026-10-12T10:00:00+00:00")
        official = news.make_item(SOURCES["TAT Newsroom"],
                                  "Thailand introduces new 30-day visa exemption rules",
                                  "https://www.tatnews.org/visa", stamp)
        copy = news.make_item(source, "Thailand introduces new 30-day visa-exemption rules!",
                              "https://thethaiger.com/visa", stamp - timedelta(hours=2))
        day12 = news.make_item(source, "Thailand weather today, 12 October: heavy rain",
                               "https://thethaiger.com/w12", stamp - timedelta(days=1))
        day13 = news.make_item(source, "Thailand weather today, 13 October: heavy rain",
                               "https://thethaiger.com/w13", stamp)
        result = news.assemble([], [copy, official, day12, day13], CUTOFF, NOW)
        self.assertEqual(sorted(i["url"] for i in result), [
            "https://thethaiger.com/w12", "https://thethaiger.com/w13", "https://www.tatnews.org/visa"])

    def test_caps_trim_outlets_before_official_and_order_newest_first(self):
        fresh = []
        start = NOW - timedelta(hours=1)
        for index in range(60):
            for name in ("The Thaiger", "The Pattaya News", "Khaosod English",
                         "Nation Thailand", "The Phuket News"):
                fresh.append(news.make_item(SOURCES[name], "%s flight %d: %s" % (name, index, "x" * (index % 7)),
                                            "https://%s.example/%d" % (name.replace(" ", ""), index),
                                            start - timedelta(hours=index)))
        official = news.make_item(SOURCES["Pattaya City Hall"], "ประชุมสภาเมือง",
                                  "https://pattaya.go.th/?p=1", NOW - timedelta(days=9))
        result = news.assemble([], fresh + [official], CUTOFF, NOW)
        self.assertEqual(len(result), news.TOTAL)
        self.assertIn(official, result)
        self.assertLessEqual(max(sum(i["source"] == n for i in result) for n in SOURCES), news.PER_SOURCE)
        stamps = [i["published_at"] for i in result]
        self.assertEqual(stamps, sorted(stamps, reverse=True))


    def test_source_cap_keeps_older_business_topics_before_newer_local_news(self):
        source = SOURCES["The Pattaya News"]
        local = [news.make_item(source, "Pattaya condo dispute number %d" % index,
                                "https://thepattayanews.com/%d" % index, NOW - timedelta(hours=index))
                 for index in range(news.PER_SOURCE + 5)]
        cannabis = news.make_item(source, "Officials discuss cannabis shop rules",
                                  "https://thepattayanews.com/cannabis", NOW - timedelta(days=6))
        result = news.assemble([], local + [cannabis], CUTOFF, NOW)
        self.assertEqual(len(result), news.PER_SOURCE)
        self.assertIn(cannabis, result)
        self.assertEqual(result[-1], cannabis)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "tourism" / "news.json"

    def tearDown(self):
        self.directory.cleanup()

    def responses(self):
        return {"https://www.tatnews.org/": TAT_JSON,
                "https://www.khaosodenglish.com/feed/": KHAOSOD_RSS,
                "https://www.thephuketnews.com/": PHUKET_RSS,
                "https://api.nationthailand.com/": NATION_SITEMAP}

    def test_contract_and_partial_success_exit_zero(self):
        code, document, printed, calls = run(self.path, self.responses())
        self.assertEqual(code, 0)
        self.assertFalse(any("bangkokpost" in url or "gdelt" in url for url in calls))
        self.assertEqual(document["built_at"], "2026-10-13T03:00:00+00:00")
        self.assertEqual(document["window_days"], 10)
        self.assertEqual([s["name"] for s in document["sources"]], list(SOURCES))
        status = {s["name"]: s for s in document["sources"]}
        self.assertEqual(status["TAT Newsroom"], {
            "name": "TAT Newsroom", "endpoint": "https://www.tatnews.org/wp-json/wp/v2/posts",
            "ok": True, "count": 2, "error": None})
        self.assertFalse(status["The Thaiger"]["ok"])
        self.assertEqual(status["The Thaiger"]["error"], "network_error")
        self.assertIn("The Thaiger: network_error", document["errors"])
        titles = [i["title"] for i in document["items"]]
        self.assertIn("Bangkok named World’s Best City", titles)
        self.assertIn("Phuket Vegetarian Festival begins in Old Town", titles)
        self.assertIn("ONWR warns of heavy rain as reservoirs reach 82% capacity", titles)
        self.assertNotIn("Provincial governor opens new school", titles)
        self.assertNotIn("Chalong market reopens", titles)
        self.assertNotIn("Global smartphone shipments fall 20% to 14-year low", titles)
        self.assertNotIn("Undated cannabis story", titles)
        for entry in document["items"]:
            self.assertEqual(set(entry), {"source", "title", "url", "published_at", "lang",
                                          "topics", "official"})
            self.assertTrue(entry["published_at"].endswith("+00:00"))
        self.assertEqual(document["items"][0]["source"], "Nation Thailand")
        tat = [i for i in document["items"] if i["source"] == "TAT Newsroom"]
        self.assertTrue(all(i["official"] for i in tat))
        self.assertIn("TAT Newsroom: ok, 2 new in window", printed)
        self.assertIn("topics:", printed)

    def test_failed_source_keeps_previous_items_and_all_failed_exits_one(self):
        run(self.path, self.responses())
        before = json.loads(self.path.read_text(encoding="utf-8"))["items"]
        responses = self.responses()
        responses["https://www.tatnews.org/"] = urllib.error.HTTPError(
            "https://www.tatnews.org", 403, "Forbidden", headers(), None)
        code, document, printed, _ = run(self.path, responses)
        self.assertEqual(code, 0)
        self.assertEqual(document["items"], before)
        self.assertIn("TAT Newsroom: FAILED http_403, 2 kept from earlier runs", printed)

        code, document, _, _ = run(self.path, {})
        self.assertEqual(code, 1)
        self.assertEqual(document["items"], before)
        self.assertTrue(all(not s["ok"] and s["count"] == 0 for s in document["sources"]))

    def test_layout_changes_fail_loudly_but_a_quiet_wordpress_window_is_ok(self):
        run(self.path, self.responses())
        before = [i for i in json.loads(self.path.read_text(encoding="utf-8"))["items"]
                  if i["source"] == "The Phuket News"]
        responses = self.responses()
        responses["https://www.thephuketnews.com/"] = b"<rss version='2.0'><channel/></rss>"
        responses["https://api.nationthailand.com/"] = NATION_SITEMAP.replace(
            b"2026-10-13T09:21:00+07:00", b"13 Oct, 9:21").replace(
            b"2026-10-13T09:36:00+07:00", b"13 Oct, 9:36")
        responses["https://www.dtam.moph.go.th/"] = b"[]"
        # A byte-order mark in front of WordPress JSON is still JSON.
        responses["https://www.tatnews.org/"] = b"\xef\xbb\xbf" + TAT_JSON
        code, document, _, _ = run(self.path, responses)
        self.assertEqual(code, 0)
        status = {s["name"]: s for s in document["sources"]}
        self.assertEqual((status["The Phuket News"]["ok"], status["The Phuket News"]["error"]),
                         (False, "no_entries"))
        self.assertEqual((status["Nation Thailand"]["ok"], status["Nation Thailand"]["error"]),
                         (False, "undated_entries"))
        self.assertEqual(status["DTAM (Ministry of Public Health)"],
                         {"name": "DTAM (Ministry of Public Health)", "ok": True, "count": 0,
                          "error": None, "endpoint": news.display_endpoint(
                              SOURCES["DTAM (Ministry of Public Health)"])})
        self.assertEqual(status["TAT Newsroom"]["count"], 2)
        self.assertEqual([i for i in document["items"] if i["source"] == "The Phuket News"], before)
        self.assertIn("ONWR warns of heavy rain as reservoirs reach 82% capacity",
                      [i["title"] for i in document["items"]])

    def test_corrupt_previous_items_are_skipped(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"items": [
            {"source": ["The Thaiger"], "title": "Visa rules", "url": "https://thethaiger.com/x",
             "published_at": "2026-10-12T00:00:00+00:00"},
            {"source": "The Thaiger", "title": "Visa rules", "url": "javascript:alert(1)",
             "published_at": "2026-10-12T00:00:00+00:00"},
            "not an item"]}), encoding="utf-8")
        code, document, _, _ = run(self.path, self.responses())
        self.assertEqual(code, 0)
        self.assertNotIn("Visa rules", [i["title"] for i in document["items"]])

    def test_short_feeds_accumulate_across_runs(self):
        run(self.path, self.responses())
        later = b"""<rss version="2.0"><channel><item><title>Patong flood warning</title>
<link>https://www.thephuketnews.com/flood-101900.php</link>
<pubDate>2026-10-13T08:00+07:00</pubDate></item></channel></rss>"""
        responses = self.responses()
        responses["https://www.thephuketnews.com/"] = later
        _, document, _, _ = run(self.path, responses)
        phuket = [i["title"] for i in document["items"] if i["source"] == "The Phuket News"]
        self.assertEqual(phuket, ["Patong flood warning", "Phuket Vegetarian Festival begins in Old Town"])

    def test_window_option_and_unreadable_previous_file(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        code, document, _, _ = run(self.path, self.responses(), ["--days", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(document["window_days"], 1)
        self.assertIn("previous news.json unreadable", document["errors"])
        self.assertNotIn("Bangkok named World’s Best City", [i["title"] for i in document["items"]])


if __name__ == "__main__":
    unittest.main()
