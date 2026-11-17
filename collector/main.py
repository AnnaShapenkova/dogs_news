"""Collect a bounded pilot corpus; keep inputs out of the legacy AI worker."""

import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from psycopg.types.json import Jsonb

import db

SERVICE = "collector"
MAX_NEW = 30
LOOKBACK_DAYS = 90
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_RECORD_BYTES = 256 * 1024
PAGE_SIZE = 15
MAX_PAGES = 10
FRESH_PAGES = 7
MAX_EVERGREEN = 5
MAX_RSS_ITEMS = 60
RESEARCH_SOURCE = "Europe PMC: Dogs pilot"
ARTICLE_SOURCE = "ScienceDaily: Dogs"
from collector.sources import REGISTRY
QUOTAS = {name: entry[2] for name, entry in REGISTRY.items()}

DOG_WORDS = re.compile(
    r"\b(?:dogs?|canines?|canis (?:lupus )?familiaris|собак[а-яё]*|п[её]с|пс(?:а|ы|ов|у|ом|е|ам|ами|ах))\b", re.I
)
TOPIC_WORDS = re.compile(
    r"\b(?:behaviou?r(?:al|ally|s)?|cognit(?:ion|ive|ively)|emotion(?:s|al|ally)?|learn(?:ing|ed|s|t)?|"
    r"train(?:ing|ed|s|ers?|ability|able)?|welfare|well[- ]?being|enrich(?:ment|ed|ing|s)?|"
    r"поведен[а-яё]*|эмоци[а-яё]*|когнитив[а-яё]*|обуч[а-яё]*|дрессиров[а-яё]*|"
    r"благополуч[а-яё]*|обогащ[а-яё]*|питан[а-яё]*|нутриент[а-яё]*|рацион[а-яё]*|nutrition\w*|nutrient\w*|diet\w*)\b", re.I
)
# These are different meanings of "learning/behaviour", seen in live probes.
# They must not turn imaging models or tumour biology into dog-behaviour candidates.
NON_ANIMAL_TOPICS = re.compile(
    r"\b(?:machine|deep|supervised|unsupervised|statistical)\s+learning\b|"
    r"\b(?:training|trained)\s+(?:data|datasets?|sets?|cohorts?|models?|classifiers?|networks?)\b|"
    r"\b(?:models?|classifiers?|networks?|algorithms?)\s+(?:(?:was|were|are|is)\s+)?trained\b|"
    r"\b(?:biological|cellular|tumou?r|stationary)\s+behaviou?r\b|"
    r"\b(?:машинн[а-яё]*|глубок[а-яё]*)\s+обуч[а-яё]*\b", re.I
)
STAFF_TRAINING = re.compile(
    r'\b(?:[a-z]+-)*trained\s+(?:veterinarians?|clinicians?|cardiologists?|personnel|staff|veterinary\s+(?:students?|surgeons?))\b|'
    r'\btraining\s+(?:of|for)\s+(?:veterinarians?|clinicians?|cardiologists?|veterinary\s+(?:students?|surgeons?))\b|'
    r'\b(?:обучени[ея]|подготовк[аиу])\s+(?:ветеринар[а-яё]*|кардиолог[а-яё]*|врач[а-яё]*)\b', re.I)


def has_animal_topic(title, text):
    combined = title + ' ' + text
    return bool(TOPIC_WORDS.search(STAFF_TRAINING.sub(' ', NON_ANIMAL_TOPICS.sub(' ', combined))))
# Explicit, inspectable discovery query. It is not an editorial quality assessment.
EUROPEPMC_TERMS = (
    '(TITLE:dog OR TITLE:dogs OR TITLE:canine OR TITLE:"Canis familiaris" OR TITLE:"Canis lupus familiaris"'
    ' OR TITLE:собак* OR TITLE:пёс OR TITLE:пес OR TITLE:псы OR TITLE:псов)'
    ' AND (TITLE:behavio* OR ABSTRACT:behavio* OR TITLE:cogniti* OR ABSTRACT:cogniti*'
    ' OR TITLE:emotion* OR ABSTRACT:emotion* OR TITLE:learn* OR ABSTRACT:learn*'
    ' OR TITLE:train* OR ABSTRACT:train* OR TITLE:welfare OR ABSTRACT:welfare'
    ' OR TITLE:wellbeing OR ABSTRACT:wellbeing OR TITLE:"well-being" OR ABSTRACT:"well-being"'
    ' OR TITLE:enrich* OR ABSTRACT:enrich*'
    ' OR TITLE:поведен* OR ABSTRACT:поведен* OR TITLE:эмоци* OR ABSTRACT:эмоци*'
    ' OR TITLE:когнитив* OR ABSTRACT:когнитив* OR TITLE:обуч* OR ABSTRACT:обуч*'
    ' OR TITLE:дрессиров* OR ABSTRACT:дрессиров* OR TITLE:благополуч* OR ABSTRACT:благополуч*'
    ' OR TITLE:обогащ* OR ABSTRACT:обогащ* OR TITLE:nutrition* OR ABSTRACT:nutrition*'
    ' OR TITLE:nutrient* OR ABSTRACT:nutrient* OR TITLE:diet* OR ABSTRACT:diet*)'
    ' AND (LANG:eng OR LANG:rus) AND SRC:MED'
)


class InvalidRecord(ValueError):
    """A record can be skipped without losing the other records in a source."""


@dataclass
class Batch:
    items: list = field(default_factory=list)
    rejected: Counter = field(default_factory=Counter)
    scanned: int = 0
    limited: bool = False
    read_ok: bool = False
    error: str | None = None
    pages: int = 0
    archive_scanned: int = 0


def five_year_start(today):
    try:
        return today.replace(year=today.year - 5)
    except ValueError:  # February 29 in a non-leap target year
        return date(today.year - 5, 2, 28)


def is_evergreen(item, today):
    published = item.get('published_at')
    return isinstance(published, datetime) and published.date() < today - timedelta(days=LOOKBACK_DAYS - 1)


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        if not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidRecord("invalid_text")
    parser = TextOnly()
    parser.feed(value)
    return " ".join("".join(parser.parts).split())


def utc_today():
    return datetime.now(timezone.utc).date()


def valid_url(url):
    if not isinstance(url, str) or any(c.isspace() for c in url):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        parsed.port  # reject invalid/out-of-range ports as malformed input
        return parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def fetch(url):
    if not valid_url(url):
        raise ValueError("invalid_source_url")
    from collector.sources import canonical
    canonical(url)
    class ApprovedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            canonical(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    request = urllib.request.Request(url, headers={"User-Agent": "closer-to-dog/0.7"})
    with urllib.request.build_opener(ApprovedRedirect()).open(request, timeout=15) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("response_too_large")
    return body


def publication_date(value, rss=False):
    try:
        if not value:
            raise ValueError()
        if rss:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                raise ValueError()
            return moment.astimezone(timezone.utc)
        # Europe PMC supplies a calendar date, not a publication time.
        return datetime.combine(date.fromisoformat(value), datetime.min.time(), timezone.utc)
    except (ValueError, TypeError, OverflowError, AttributeError):
        raise InvalidRecord("invalid_date") from None


def candidate(title, text, url, published, payload, scope, today, research=False, oldest=None, newest=None):
    try:
        payload["record"]  # A faithful upstream record is required, not just generated metadata.
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise InvalidRecord("invalid_payload") from None
    if len(serialized.encode("utf-8")) > MAX_RECORD_BYTES:
        raise InvalidRecord("record_too_large")
    title, text = plain_text(title), plain_text(text)
    if not title:
        raise InvalidRecord("missing_title")
    if not valid_url(url):
        raise InvalidRecord("invalid_url")
    if not (oldest or today - timedelta(days=LOOKBACK_DAYS - 1)) <= published.date() <= (newest or today):
        raise InvalidRecord("outside_window")
    combined = title + " " + text
    if not DOG_WORDS.search(title if research else combined) or not has_animal_topic(title, text):
        raise InvalidRecord("outside_topic")
    return {"title": title, "text": text, "url": url, "published_at": published,
            "raw_payload": payload, "content_scope": scope if text else "metadata_only"}


def read_europepmc_window(url, today, start, end, page_limit, window):
    batch = Batch()
    cursor = "*"
    query = f"{EUROPEPMC_TERMS} AND FIRST_PDATE:[{start.isoformat()} TO {end.isoformat()}]"
    cursors = set()
    try:
        for page in range(page_limit):
            cursors.add(cursor)
            parts = urllib.parse.urlsplit(url)
            params = dict(urllib.parse.parse_qsl(parts.query))
            params.update(query=query, format="json", resultType="core", pageSize=str(PAGE_SIZE),
                          sort="FIRST_PDATE_D desc", cursorMark=cursor)
            request_url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(params)))
            batch.pages += 1
            data = json.loads(fetch(request_url))
            records = data["resultList"]["result"]
            if not isinstance(records, list) or len(records) > PAGE_SIZE or "errCode" in data:
                raise ValueError("invalid_api_response")
            batch.read_ok = True
            for record in records:
                batch.scanned += 1
                try:
                    if not isinstance(record, dict) or record.get("source") != "MED" or not re.fullmatch(r"[0-9]+", str(record.get("id", ""))):
                        raise InvalidRecord("invalid_identifier")
                    if record.get("language") not in ("eng", "rus", None):
                        raise InvalidRecord("outside_language")
                    payload = {"provider": "europepmc", "request_url": request_url, "discovery_window": window,
                               "fetched_at": datetime.now(timezone.utc).isoformat(), "record": record}
                    batch.items.append(candidate(
                        record.get("title"), record.get("abstractText"),
                        f"https://europepmc.org/article/MED/{record['id']}",
                        publication_date(record.get("firstPublicationDate")), payload, "abstract", today,
                        research=True, oldest=start, newest=end))
                except InvalidRecord as error:
                    batch.rejected[str(error)] += 1
            next_cursor = data.get("nextCursorMark")
            if not records or not next_cursor:
                break
            if next_cursor in cursors or page == page_limit - 1:
                batch.limited = True
                break
            cursor = next_cursor
    except Exception as error:
        # Keep an already read page if a later page fails; expose the partial failure.
        batch.error = f"{type(error).__name__}: {error}"
    return batch


def read_europepmc(url, today, include_archive=True):
    start = today - timedelta(days=LOOKBACK_DAYS - 1)
    batch = read_europepmc_window(url, today, start, today,
                                 FRESH_PAGES if include_archive else MAX_PAGES, 'fresh')
    if include_archive and batch.read_ok and batch.pages < MAX_PAGES:
        archive = read_europepmc_window(url, today, five_year_start(today), start - timedelta(days=1),
                                       MAX_PAGES - batch.pages, 'evergreen')
        batch.items.extend(archive.items)
        batch.rejected.update(archive.rejected)
        batch.scanned += archive.scanned
        batch.archive_scanned = archive.scanned
        batch.pages += archive.pages
        batch.limited = batch.limited or archive.limited
        if archive.error:
            batch.error = (batch.error + '; ' if batch.error else '') + 'archive: ' + archive.error
    return batch


def read_rss(url, today, fetcher=None):
    batch = Batch()
    try:
        root = ET.fromstring((fetcher or fetch)(url))
        if root.tag != "rss" or root.find("channel") is None:
            raise ValueError("invalid_rss_response")
        records = root.findall("./channel/item")
        batch.read_ok = True
        batch.limited = len(records) > MAX_RSS_ITEMS
        for item in records[:MAX_RSS_ITEMS]:
            batch.scanned += 1
            try:
                payload = {"provider": "rss", "request_url": url,
                           "fetched_at": datetime.now(timezone.utc).isoformat(),
                           "record": ET.tostring(item, encoding="unicode")}
                batch.items.append(candidate(
                    item.findtext("title"), item.findtext("description"), (item.findtext("link") or "").strip(),
                    publication_date(item.findtext("pubDate"), rss=True), payload, "feed_excerpt", today))
            except InvalidRecord as error:
                batch.rejected[str(error)] += 1
    except Exception as error:
        batch.error = f"{type(error).__name__}: {error}"
    return batch


READERS = {"europepmc": read_europepmc, "rss": read_rss}


def save_candidate(conn, source_id, run_id, item):
    return conn.execute(
        """INSERT INTO materials (source_id, run_id, url, title, text, published_at, raw_payload, content_scope, status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'collected')
           ON CONFLICT (url) DO NOTHING RETURNING id""",
        (source_id, run_id, item["url"], item["title"], item["text"], item["published_at"],
         Jsonb(item["raw_payload"]), item["content_scope"]),
    ).fetchone() is not None


def collect(conn, today=None):
    from collector.sources import collect_sources
    today = today or utc_today()
    run_id = db.start_run(conn, SERVICE)
    try:
        total, note = collect_sources(conn, today, run_id, fetch)
        db.finish_run(conn, SERVICE, run_id, total, note)
        return total
    except Exception as error:
        db.fail_run(conn, SERVICE, run_id, error)
        raise


def main():
    with db.connect() as conn:
        db.init_schema(conn)
        collect(conn)


if __name__ == "__main__":
    main()
