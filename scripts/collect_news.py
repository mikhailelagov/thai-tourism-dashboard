#!/usr/bin/env python3
"""Collect dated, attributable Thailand tourism headlines into tourism/news.json.

The weekly post used to learn about entry rules, cannabis regulation, flights and
local events from free-form model web search, so its claims carried no date or
link. This collector reads a fixed list of official sites and established outlets
through their public WordPress REST APIs, RSS feeds or Google-News sitemap, and
keeps only the headline, link, publication time and source. No article text is
stored. Topics come from keyword lists matched on the headline, so a tag means
"the headline says so", not that anyone read the story.

Every request sends an honest User-Agent, has a timeout and is retried at most
once. A source that refuses that agent, challenges it or redirects off-site is
recorded as failed; nothing tries to get around it. Bangkok Post is left out on
purpose because it charges AI bots. Items from earlier runs are merged by URL, so
a failed source keeps what it had and short feeds (The Phuket News, Nation) fill
the window over several runs. Exit 0 when at least one source answered, else 1.
"""

import argparse
import difflib
import email.utils
import html
import http.client
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tourism" / "news.json"
USER_AGENT = ("thai-tourism-dashboard/1.0 "
              "(+https://github.com/mikhailelagov/thai-tourism-dashboard)")
TIMEOUT = 30
RETRY_DELAY = 5
TRANSIENT = (429, 500, 502, 503, 504)
MAX_BYTES = 8 * 1024 * 1024
WINDOW_DAYS = 10
PER_SOURCE = 40
TOTAL = 200
WP_PAGES = 3
FUTURE_SLACK = timedelta(hours=1)
REPOST_GAP = timedelta(days=3)
JSON_ACCEPT = "application/json"
XML_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8"

SOURCES = [
    {"name": "TAT Newsroom", "kind": "wp", "official": True,
     "endpoint": "https://www.tatnews.org/wp-json/wp/v2/posts"},
    {"name": "The Pattaya News", "kind": "wp", "official": False,
     "endpoint": "https://thepattayanews.com/wp-json/wp/v2/posts"},
    {"name": "Pattaya City Hall", "kind": "wp", "official": True,
     "endpoint": "https://pattaya.go.th/wp-json/wp/v2/posts"},
    # The search also matches body text, so the headline itself must be about cannabis.
    # On 2026-10-13 GitHub's runners got a bot-protection page instead of data
    # from both the REST API and the RSS; we do not work around it. The RSS
    # fallback helps when only the API is refused.
    {"name": "DTAM (Ministry of Public Health)", "kind": "wp", "official": True,
     "endpoint": "https://www.dtam.moph.go.th/wp-json/wp/v2/posts",
     "search": "กัญชา", "require": "cannabis",
     "fallback_feed": "https://www.dtam.moph.go.th/feed/"},
    {"name": "The Thaiger", "kind": "wp", "official": False,
     "endpoint": "https://thethaiger.com/wp-json/wp/v2/posts"},
    # Khaosod's REST API sits behind a bot challenge; its RSS does not.
    {"name": "Khaosod English", "kind": "feed", "official": False,
     "endpoint": "https://www.khaosodenglish.com/feed/",
     "pages": ["https://www.khaosodenglish.com/feed/",
               "https://www.khaosodenglish.com/feed/?paged=2",
               "https://www.khaosodenglish.com/feed/?paged=3"]},
    {"name": "The Phuket News", "kind": "feed", "official": False,
     "endpoint": "https://www.thephuketnews.com/rss-xml/news.xml"},
    {"name": "Nation Thailand", "kind": "sitemap", "official": False,
     "endpoint": "https://api.nationthailand.com/sitemap/latest"},
]

TOPICS = ("cannabis", "entry_rules", "flights", "statistics", "events", "alcohol",
          "safety", "weather", "pattaya", "phuket", "bangkok", "krabi", "samui", "chiangmai")
PLACES = ("pattaya", "phuket", "bangkok", "krabi", "samui", "chiangmai")
# Topics a tourism business acts on; the caps keep these before other news.
BUSINESS = ("cannabis", "entry_rules", "flights", "statistics", "alcohol", "events")

# English patterns get word boundaries. Thai is written without spaces, so Thai
# patterns match anywhere and exclude the longer words they occur in.
KEYWORDS = {
    "cannabis": (
        [r"cannabis", r"marijuana", r"marihuana", r"ganja", r"hemp", r"weed(?!\s+out)",
         r"dispensar(?:y|ies)", r"thc", r"pre-?rolls?"],
        ["กัญชา", "กัญชง"]),
    "entry_rules": (
        [r"visas?", r"visa[- ]exempt\w*", r"e-?visas?", r"dtv", r"tdac",
         r"arrival cards?", r"proof of funds", r"re-?entry permits?", r"90-day report\w*",
         r"immigration (?:rules?|laws?|polic\w+|regulations?|requirements?|fees?|forms?)",
         r"passport (?:holders?|rules?|requirements?|validity)",
         r"overstay (?:fines?|rules?|penalt\w+)",
         r"entry (?:rules?|requirements?|fees?|tax\w*|ban\w*)",
         r"(?:departure|tourist|tourism|travel|landing|arrival) (?:tax\w*|fees?|levy|levies)",
         r"border (?:crossings?|checkpoints?|clos\w+|reopen\w*)",
         r"(?:travel|tourist) insurance"],
        ["วีซ่า", "ตรวจลงตรา", "ค่าธรรมเนียมเหยียบแผ่นดิน", "ค่าเหยียบแผ่นดิน", "ภาษีขาออก",
         "จุดผ่านแดน", "ด่านชายแดน"]),
    "flights": (
        [r"flights?", r"airlines?", r"airports?", r"airways", r"air ?asia", r"vietjet",
         r"nok ?air", r"thai lion", r"air routes?", r"aviation", r"planes?",
         r"charter flights?", r"low-cost carriers?", r"suvarnabhumi", r"don m[ue]+ang",
         r"u-?tapao", r"aot", r"caat", r"jet fuel", r"checked bags?", r"baggage",
         r"(?:new|direct|non-?stop) (?:routes?|services?)"],
        ["เที่ยวบิน", "สายการบิน", "สนามบิน", "ท่าอากาศยาน", "อู่ตะเภา", "สุวรรณภูมิ",
         "ดอนเมือง", "การบิน", "บินตรง", "เครื่องบิน", r"ทอท\."]),
    "statistics": (
        [r"statistic\w*", r"stats", r"arrivals",
         r"(?:tourist|visitor|foreign|international) arrivals?",
         r"(?:tourist|visitor|passenger) (?:numbers?|figures?|counts?)",
         r"occupancy", r"(?:tourism|tourist|hotel|airport|airline) revenues?",
         r"(?:tourism|tourist) (?:income|spending|receipts)", r"hotel (?:rates?|prices?)",
         r"million (?:tourists|visitors|arrivals|passengers|foreign\w*)",
         r"year-on-year", r"y-?o-?y", r"(?:sentiment|confidence) index",
         r"gdp", r"bookings?"],
        # สร้างสถิติ / ทำลายสถิติ mean setting or breaking a record.
        [r"(?<!สร้าง)(?<!ทำลาย)สถิติ", "จำนวนนักท่องเที่ยว", "อัตราการเข้าพัก",
         "รายได้จากการท่องเที่ยว", "รายได้ท่องเที่ยว", "ยอดจอง"]),
    "events": (
        [r"festivals?", r"loy krathong", r"krathong", r"songkran", r"y(?:i|ee) ?peng",
         r"concert\w*", r"fireworks?", r"events?", r"fest", r"expos?", r"exhibitions?",
         r"showcases?", r"things to do", r"what'?s on", r"road closures?",
         r"(?:trade|food|travel|tourism|temple|winter|book) fairs?", r"marathons?",
         r"carnivals?", r"countdown", r"new year", r"celebrat\w+", r"parades?", r"processions?",
         r"ceremon(?:y|ies)", r"cremation", r"holidays?", r"long weekend", r"halloween",
         r"tournaments?", r"championships?", r"regatta", r"full moon party"],
        ["เทศกาล", "ลอยกระทง", "สงกรานต์", "ยี่เป็ง", "คอนเสิร์ต", "ประเพณี", "มหกรรม",
         "เฉลิมฉลอง", "นิทรรศการ", "พลุ", "เคาท์ดาวน์", "เคานต์ดาวน์", "วันหยุด", "งานวัด",
         "ถนนคนเดิน", "การแข่งขัน", "พระราชพิธี", "ดอกไม้จันทน์", "กินเจ", "มาราธอน",
         "ขบวนแห่", "ปิดการจราจร", "ปิดถนน"]),
    "alcohol": (
        [r"alcohol\w*", r"liquor", r"beers?", r"wines?", r"booze", r"drunk\w*",
         r"drink-?driv\w*", r"drinking(?! water)", r"(?<!behind )bars?", r"pubs?",
         r"nightclubs?", r"night ?life", r"entertainment venues?",
         r"(?:closing|opening) (?:times?|hours)", r"dry days?", r"happy hours?"],
        ["แอลกอฮอล์", r"สุรา(?!ษ)", "เหล้า", "เบียร์", "ไวน์", "เครื่องดื่มมึนเมา",
         # สถานบริการน้ำมัน is a petrol station, สถานบริการสาธารณสุข a clinic.
         "สถานบันเทิง", r"สถานบริการ(?!น้ำมัน|สาธารณสุข|ทางการแพทย์|การแพทย์|สุขภาพ)", "ผับ", r"บาร์(?!บี)", "เมาแล้วขับ"]),
    "safety": (
        [r"safety", r"safe", r"unsafe", r"danger\w*", r"scam\w*", r"fraud\w*",
         r"con artists?", r"tourist police", r"crackdowns?", r"advisor(?:y|ies)",
         r"travel (?:warnings?|alerts?)", r"drown\w*", r"rip currents?", r"jellyfish",
         r"jet[- ]?skis?", r"robb\w+", r"theft", r"thie(?:f|ves)", r"steal\w*", r"stole\w*",
         r"pickpocket\w*", r"snatch\w*", r"shoplift\w*", r"assault\w*", r"violen\w*",
         r"stab", r"stabs", r"stabb\w+", r"shootings?", r"gunm[ae]n", r"killed", r"fatal\w*",
         r"crash\w*", r"collisions?", r"hit-and-run", r"accidents?", r"injur\w*",
         r"brawl\w*", r"extort\w*", r"overcharg\w*", r"raids?", r"blazes?", r"fires?",
         r"earthquakes?", r"tsunami", r"missing", r"rescue\w*"],
        ["ความปลอดภัย", "ตำรวจท่องเที่ยว", "มิจฉาชีพ", "หลอกลวง", "ต้มตุ๋น", "อุบัติเหตุ",
         "จมน้ำ", "ปล้น", "ชิงทรัพย์", "ลักทรัพย์", "ทำร้าย", "ยิง", "กวาดล้าง", "ไฟไหม้",
         "เพลิงไหม้", "แผ่นดินไหว", "กู้ภัย", "เตือนภัย", "สแกมเมอร์", "บัญชีม้า",
         "แก๊งคอลเซ็นเตอร์"]),
    "weather": (
        [r"weather", r"rain\w*", r"storms?", r"thunderstorms?", r"flood\w*", r"monsoon\w*",
         r"typhoons?", r"cyclones?", r"tropical depressions?", r"heat ?waves?", r"heat",
         r"hot season", r"temperatures?", r"pm ?2\.5", r"air (?:pollution|quality)",
         r"haze", r"smog", r"dust", r"waves", r"landslides?", r"drought\w*",
         r"meteorolog\w*", r"tmd", r"cold (?:snap|spell|front|weather|season)",
         r"cool season", r"winds?"],
        ["สภาพอากาศ", "ฝน", "พายุ", "น้ำท่วม", "อุทกภัย", "น้ำป่า", "ดินถล่ม", "อุตุนิยมวิทยา",
         "กรมอุตุ", "คลื่นลม", "คลื่นสูง", "ฝุ่น", "หมอกควัน", "คุณภาพอากาศ", "อากาศร้อน",
         "อากาศหนาว", "ร้อนจัด", "ภัยแล้ง", "มรสุม"]),
    "pattaya": (
        [r"pattaya", r"jomtien", r"na ?klua", r"na kluea", r"koh? larn", r"walking street",
         r"bang ?lamung", r"pratumnak", r"phra tamnak", r"wong ?amat", r"u-?tapao"],
        ["พัทยา", "จอมเทียน", "นาเกลือ", "เกาะล้าน", "บางละมุง", "อู่ตะเภา"]),
    "phuket": (
        [r"phuket", r"patong", r"kata", r"karon", r"rawai", r"chalong", r"kamala(?! harris)",
         r"bang ?tao", r"mai khao"],
        ["ภูเก็ต", "ป่าตอง", "กะตะ", "กะรน", "ราไวย์"]),
    "bangkok": (
        [r"bangkok", r"suvarnabhumi", r"don m[ue]+ang", r"silom", r"khao ?san",
         r"chatuchak", r"bkk", r"bts", r"mrt"],
        ["กรุงเทพ", r"กทม\.?", "สุวรรณภูมิ", "ดอนเมือง"]),
    "krabi": (
        [r"krabi", r"ao ?nang", r"phi ?phi", r"(?:koh? )?lanta", r"railay"],
        ["กระบี่", "อ่าวนาง", "พีพี", "เกาะลันตา"]),
    "samui": (
        [r"samui", r"(?:koh? )?pha-? ?ngan", r"koh? tao", r"full moon party"],
        ["สมุย", "พะงัน", "เกาะเต่า"]),
    "chiangmai": (
        [r"chiang ?mai", r"nimman\w*", r"doi suthep", r"y(?:i|ee) ?peng"],
        ["เชียงใหม่", "ยี่เป็ง"]),
}

TAG = re.compile(r"<[^>]*>")
ENTITY = re.compile(r"&(?:#\d+|#x[0-9a-f]+|[a-z]+);", re.IGNORECASE)
INVISIBLE = "\u200b\u200c\u200d\u2060\ufe0e\ufe0f\ufeff"
SITEMAP = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
NEWS = "{http://www.google.com/schemas/sitemap-news/0.9}"
ATOM = "{http://www.w3.org/2005/Atom}"
DUBLIN_CORE = "{http://purl.org/dc/elements/1.1/}"


class SourceError(Exception):
    """A failure with a short, controlled label that is safe to publish."""


def compile_topics():
    rules = []
    for topic in TOPICS:
        english, thai = KEYWORDS[topic]
        pattern = r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % "|".join(english)
        rules.append((topic, re.compile(pattern + "|" + "|".join(thai), re.IGNORECASE)))
    return rules


RULES = compile_topics()


def tag(title):
    return [topic for topic, rule in RULES if rule.search(title)]


def relevant(topics, official):
    return official or "pattaya" in topics or any(t not in PLACES for t in topics)


def clean_title(value):
    """Plain text: tags removed, entities decoded (twice if double-encoded), emoji dropped."""
    text = html.unescape(TAG.sub(" ", str(value or "")))
    if ENTITY.search(text):
        text = html.unescape(text)
    chars = []
    for char in text:
        if char in INVISIBLE:
            continue
        # Emoji and pictographs only; degree signs and Thai vowel marks stay.
        # Emoji newer than this Python's Unicode tables read as unassigned (Cn).
        category = unicodedata.category(char)
        if (ord(char) >= 0x2190 and category in ("So", "Sk")
                or ord(char) >= 0x1F000 and category == "Cn"):
            chars.append(" ")
        else:
            chars.append(char)
    return " ".join("".join(chars).split())


def language(title):
    thai = sum(1 for char in title if "\u0e00" <= char <= "\u0e7f")
    latin = sum(1 for char in title if char.isascii() and char.isalpha())
    return "th" if thai > latin else "en"


def parse_time(value, naive_utc=False):
    """Return an aware UTC datetime, or None when the value or its zone is unknown."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        stamp = datetime.fromisoformat(re.sub(r"[zZ]$", "+00:00", text))
    except ValueError:
        try:
            stamp = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if stamp is None or stamp.year < 2000:
        return None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        if not naive_utc:
            return None
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def url_key(url):
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), parts.path).rstrip("/")
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    return host + path + "?" + urllib.parse.urlencode(query)


def same_site(first, second):
    first, second = (first or "").lower(), (second or "").lower()
    first = first[4:] if first.startswith("www.") else first
    second = second[4:] if second.startswith("www.") else second
    return bool(first) and (first == second or first.endswith("." + second)
                            or second.endswith("." + first))


class SameSiteRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects to another site, e.g. a pay-per-crawl or challenge page.

    TollBit's AI-bot paywall lives on the publisher's own subdomain
    (tollbit.<site>), so it is refused by name as well.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = (urllib.parse.urlsplit(newurl).hostname or "").lower()
        label = None
        if "tollbit" in target.split("."):
            label = "paywall_redirect"
        elif not same_site(urllib.parse.urlsplit(req.full_url).hostname, target):
            label = "offsite_redirect"
        if label:
            if fp is not None:
                fp.close()
            raise SourceError(label)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(SameSiteRedirect)


def challenged(exc):
    return str(exc.headers.get("cf-mitigated", "")).lower() == "challenge" if exc.headers else False


def error_label(exc):
    if isinstance(exc, SourceError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return "bot_challenge" if challenged(exc) else "http_%d" % exc.code
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return "timeout"
        return "network_error"
    if isinstance(exc, (ValueError, TypeError, KeyError, ET.ParseError)):
        return "invalid_response"
    if isinstance(exc, (OSError, http.client.HTTPException)):
        return "network_error"
    return "unexpected_error"


def fetch(url, accept):
    """GET with an honest User-Agent; retry once, and only after a transient failure."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    error = None
    for attempt in (1, 2):
        try:
            with OPENER.open(request, timeout=TIMEOUT) as response:
                body = response.read(MAX_BYTES + 1)
                headers = response.headers
            if len(body) > MAX_BYTES:
                raise SourceError("response_too_large")
            # A web page where data was asked for is a bot-protection or error
            # page; label it so the log does not read as a format change.
            if "text/html" in (headers.get("Content-Type") or "") and "html" not in accept:
                raise SourceError("html_instead_of_data")
            if body.strip():
                return body, headers
            error = SourceError("empty_response")
        except urllib.error.HTTPError as exc:
            if exc.fp is not None:
                exc.close()
            if exc.code not in TRANSIENT or challenged(exc):
                raise
            error = exc
        except SourceError:
            raise
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                http.client.HTTPException) as exc:
            error = exc
        if attempt == 1:
            time.sleep(RETRY_DELAY)
    raise error


def parse_xml(body):
    # Feeds never need a DTD; refusing one avoids entity-expansion attacks.
    if re.search(rb"<!(?:DOCTYPE|ENTITY)", body, re.IGNORECASE):
        raise SourceError("invalid_response")
    return ET.fromstring(body)


def feed_entries(body):
    """(title, link, published) from RSS 2.0 or Atom."""
    root = parse_xml(body)
    if root.tag not in ("rss", ATOM + "feed"):
        raise SourceError("invalid_response")
    entries = []
    for item in root.iter("item"):
        published = item.findtext("pubDate") or item.findtext(DUBLIN_CORE + "date")
        entries.append((item.findtext("title"), item.findtext("link"), parse_time(published)))
    for entry in root.iter(ATOM + "entry"):
        links = [node.get("href") for node in entry.findall(ATOM + "link")
                 if node.get("rel", "alternate") == "alternate"]
        published = entry.findtext(ATOM + "published") or entry.findtext(ATOM + "updated")
        entries.append((entry.findtext(ATOM + "title"), links[0] if links else None,
                        parse_time(published)))
    return entries


def sitemap_entries(body):
    """(title, link, published) from a Google-News sitemap; other URLs are skipped."""
    root = parse_xml(body)
    if root.tag != SITEMAP + "urlset":
        raise SourceError("invalid_response")
    entries = []
    for node in root.findall(SITEMAP + "url"):
        news = node.find(NEWS + "news")
        if news is None:
            continue
        entries.append((news.findtext(NEWS + "title"), node.findtext(SITEMAP + "loc"),
                        parse_time(news.findtext(NEWS + "publication_date"))))
    return entries


def wp_entries(source, after):
    """Page through a WordPress REST post list; date_gmt is UTC without an offset."""
    params = {"after": after, "per_page": 100, "orderby": "date", "order": "desc",
              "_fields": "id,date_gmt,link,title"}
    if source.get("search"):
        params["search"] = source["search"]
    entries, partial, page, pages = [], None, 1, 1
    while page <= min(pages, WP_PAGES):
        params["page"] = page
        try:
            body, headers = fetch(source["endpoint"] + "?" + urllib.parse.urlencode(params),
                                  JSON_ACCEPT)
            posts = json.loads(body.decode("utf-8-sig"))
            if not isinstance(posts, list):
                raise SourceError("invalid_response")
        except Exception as exc:
            if page == 1:
                raise
            partial = "page %d: %s" % (page, error_label(exc))
            break
        for post in posts:
            if not isinstance(post, dict):
                continue
            title = post.get("title")
            title = title.get("rendered") if isinstance(title, dict) else None
            entries.append((title, post.get("link"),
                            parse_time(post.get("date_gmt"), naive_utc=True)))
        try:
            pages = int(headers.get("X-WP-TotalPages") or 0)
        except ValueError:
            pages = 0
        if not pages:
            pages = page + 1 if len(posts) >= params["per_page"] else page
        page += 1
    return entries, partial


def collect_source(source, cutoff):
    """Return (entries, partial); partial names a later page that failed.

    A layout change must not pass as a quiet day: a feed with no entries, or
    entries none of which has a readable date, is a failure.
    """
    if source["kind"] == "wp":
        # A day of margin: some sites apply "after" in local time. The window is
        # enforced on date_gmt below.
        after = (cutoff - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            entries, partial = wp_entries(source, after)
        except Exception as exc:
            if not source.get("fallback_feed"):
                raise
            body, _ = fetch(source["fallback_feed"], XML_ACCEPT)
            entries = feed_entries(body)
            partial = "REST %s, RSS used" % error_label(exc)
    else:
        parse = sitemap_entries if source["kind"] == "sitemap" else feed_entries
        entries, partial = [], None
        for index, url in enumerate(source.get("pages") or [source["endpoint"]]):
            try:
                body, _ = fetch(url, XML_ACCEPT)
                entries.extend(parse(body))
            except Exception as exc:
                if index == 0:
                    raise
                partial = "page %d: %s" % (index + 1, error_label(exc))
                break
        # WordPress filters by date, so an empty list there is a quiet window.
        if not entries:
            raise SourceError("no_entries")
    if entries and all(entry[2] is None for entry in entries):
        raise SourceError("undated_entries")
    return entries, partial


def make_item(source, title, url, published, clean=True):
    """A contract item, or None when it is incomplete or off-topic."""
    title = clean_title(title) if clean else " ".join(str(title or "").split())
    url = str(url or "").strip()
    if not title or published is None or not url.lower().startswith(("https://", "http://")):
        return None
    topics = tag(title)
    if source.get("require") and source["require"] not in topics:
        return None
    if not relevant(topics, source["official"]):
        return None
    return {"source": source["name"], "title": title, "url": url,
            "published_at": published.isoformat(timespec="seconds"),
            "lang": language(title), "topics": topics, "official": source["official"]}


def in_window(item, cutoff, now):
    published = parse_time(item["published_at"])
    return published is not None and cutoff <= published <= now + FUTURE_SLACK


def title_key(title):
    chars = [" " if unicodedata.category(c)[0] in "PSZ" else c for c in title.lower()]
    return " ".join("".join(chars).split())


def drop_reposts(items):
    """Drop near-identical headlines; official first, then the earliest copy wins.

    Headlines with different numbers ("weather today, 12 October" vs "13 October")
    or far apart in time are separate stories, not reposts.
    """
    kept = []
    for item in sorted(items, key=lambda i: (not i["official"], i["published_at"])):
        key = title_key(item["title"])
        digits = re.findall(r"\d+", key)
        when = parse_time(item["published_at"])
        repost = False
        for _, other, other_digits, other_when in kept:
            if other_digits != digits or abs(when - other_when) > REPOST_GAP:
                continue
            matcher = difflib.SequenceMatcher(None, other, key)
            if other == key or (matcher.quick_ratio() >= 0.92 and matcher.ratio() >= 0.92):
                repost = True
                break
        if not repost:
            kept.append((item, key, digits, when))
    return [entry[0] for entry in kept]


def priority(item):
    """0: business topic, 1: Pattaya, 2: other (safety, weather, untagged official)."""
    if any(topic in BUSINESS for topic in item["topics"]):
        return 0
    return 1 if "pattaya" in item["topics"] else 2


def newest_first(items):
    ordered = sorted(items, key=lambda i: (i["source"], i["title"]))
    return sorted(ordered, key=lambda i: i["published_at"], reverse=True)


def assemble(previous, fresh, cutoff, now):
    """Merge this run with earlier items by URL, then window, dedupe and cap.

    Caps keep business topics, then Pattaya, then the rest, newest first within
    each, so an older cannabis or visa story is not pushed out by daily crime news.
    """
    by_name = {source["name"]: source for source in SOURCES}
    carried = []
    for item in previous:
        name = item.get("source")
        source = by_name.get(name) if isinstance(name, str) else None
        if source is None:
            continue
        # Re-tag with today's keyword lists; titles were cleaned when first stored.
        remade = make_item(source, item.get("title"), item.get("url"),
                           parse_time(item.get("published_at")), clean=False)
        if remade is not None:
            carried.append(remade)
    merged, seen = [], set()
    for item in fresh + carried:
        key = url_key(item["url"])
        if key not in seen and in_window(item, cutoff, now):
            seen.add(key)
            merged.append(item)
    groups = {}
    for item in drop_reposts(merged):
        groups.setdefault(item["source"], []).append(item)
    capped = []
    for group in groups.values():
        capped.extend(sorted(newest_first(group), key=priority)[:PER_SOURCE])
    # The total cap trims news outlets before official notices.
    capped = sorted(newest_first(capped), key=lambda i: (not i["official"], priority(i)))[:TOTAL]
    return newest_first(capped)


def load_previous(path):
    """Earlier items; an unreadable file is reported and ignored, never trusted."""
    path = Path(path)
    if not path.exists():
        return [], None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        items = document.get("items") if isinstance(document, dict) else None
        if not isinstance(items, list):
            raise ValueError("no items")
    except (OSError, ValueError):
        return [], "previous news.json unreadable"
    return [item for item in items if isinstance(item, dict)], None


def display_endpoint(source):
    if source.get("search"):
        return source["endpoint"] + "?search=" + source["search"]
    return source["endpoint"]


def atomic_write(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", delete=False) as out:
            temporary = Path(out.name)
            json.dump(document, out, ensure_ascii=False, indent=1, allow_nan=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv=None, output_path=OUT, now=None):
    parser = argparse.ArgumentParser(description="Collect dated Thailand tourism headlines.")
    parser.add_argument("--days", type=int, default=WINDOW_DAYS,
                        help="keep items published in the last N days (default %d)" % WINDOW_DAYS)
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    cutoff = now - timedelta(days=args.days)
    previous, previous_error = load_previous(output_path)
    errors = [previous_error] if previous_error else []
    fresh, statuses = [], []
    for source in SOURCES:
        status = {"name": source["name"], "endpoint": display_endpoint(source),
                  "ok": False, "count": 0, "error": None}
        try:
            entries, partial = collect_source(source, cutoff)
        except Exception as exc:
            status["error"] = error_label(exc)
        else:
            # Feed pages can overlap when a story lands between two requests.
            unique = {}
            for item in (make_item(source, *entry) for entry in entries):
                if item is not None and in_window(item, cutoff, now):
                    unique.setdefault(url_key(item["url"]), item)
            items = list(unique.values())
            fresh.extend(items)
            status.update(ok=True, count=len(items),
                          error="partial, " + partial if partial else None)
        if status["error"]:
            errors.append("%s: %s" % (source["name"], status["error"]))
        statuses.append(status)

    items = assemble(previous, fresh, cutoff, now)
    atomic_write(output_path, {"built_at": now.isoformat(), "window_days": args.days,
                               "items": items, "sources": statuses, "errors": errors})

    for status in statuses:
        kept = sum(1 for item in items if item["source"] == status["name"])
        if status["ok"]:
            line = "%s: ok, %d new in window, %d in file" % (status["name"], status["count"], kept)
            if status["error"]:
                line += " (%s)" % status["error"]
        else:
            line = "%s: FAILED %s, %d kept from earlier runs" % (
                status["name"], status["error"], kept)
        print(line)
    if previous_error:
        print("Warning: " + previous_error + "; started a fresh file.", file=sys.stderr)
    histogram = ["%s %d" % (topic, sum(topic in item["topics"] for item in items))
                 for topic in TOPICS if any(topic in item["topics"] for item in items)]
    print("%d items, %d days; topics: %s" % (len(items), args.days, ", ".join(histogram) or "none"))
    return 0 if any(status["ok"] for status in statuses) else 1


if __name__ == "__main__":
    sys.exit(main())
