"""Bounded source discovery and page snapshots for T17; no model or delivery I/O."""

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime

REGISTRY = {
    'RKF: Owner guides': ('owner_html', 'https://rkf.org.ru/poleznye-materialy-o-sobakah/', 10),
    'Pets: Learning': ('owner_rss', 'https://vetandlife.ru/pets/vospitanie/feed/', 4),
    'Pets: Nutrition': ('owner_rss', 'https://vetandlife.ru/pets/zoo-menu/feed/', 4),
    'Europe PMC: Dogs pilot': ('europepmc', 'https://www.ebi.ac.uk/europepmc/webservices/rest/search', 6),
    'PLOS: Dogs': ('plos', 'https://api.plos.org/search', 4),
    'ScienceDaily: Dogs': ('rss', 'https://www.sciencedaily.com/rss/plants_animals/dogs.xml', 2),
}
HOSTS = {'rkf.org.ru', 'vetandlife.ru', 'api.plos.org', 'journals.plos.org',
         'www.ebi.ac.uk', 'www.sciencedaily.com', 'europepmc.org'}
API_HOSTS = {'api.plos.org', 'www.ebi.ac.uk'}
VOID = {'br', 'hr', 'img', 'input', 'meta', 'link', 'source', 'wbr', 'area', 'base', 'embed', 'param', 'track', 'col'}
AGENT = 'closer-to-dog'
QUERY_VERSION = 7
PLOS_JOURNALS = {'pone': 'plosone', 'pbio': 'plosbiology', 'pgen': 'plosgenetics',
                 'pcbi': 'ploscompbiol', 'ppat': 'plospathogens', 'pntd': 'plosntds',
                 'pmed': 'plosmedicine', 'pclm': 'plosclimate', 'pgph': 'plosglobalpublichealth'}


def plos_url(doi, manuscript=False):
    match = re.fullmatch(r'10\.1371/journal\.([a-z]+)\.[a-zA-Z0-9_-]+', doi)
    if not match or match[1] not in PLOS_JOURNALS: raise ValueError('unsupported_plos_journal')
    path = '/article/file' if manuscript else '/article'
    params = dict(id=doi)
    if manuscript: params['type'] = 'manuscript'
    return 'https://journals.plos.org/' + PLOS_JOURNALS[match[1]] + path + '?' + urllib.parse.urlencode(params)


def canonical(url):
    p = urllib.parse.urlsplit(url)
    if (p.scheme != 'https' or p.hostname not in HOSTS or p.username or p.password
            or p.port not in (None, 443)):
        raise ValueError('unapproved_source_url')
    params = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
              if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid', 'openstat')]
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path or '/', urllib.parse.urlencode(params), ''))


class SourceHTTP:
    """One request budget per publisher, robots enforcement, no retry loop."""
    def __init__(self, fetcher, sleep=None, monotonic=time.monotonic):
        self.fetcher, self.sleep, self.clock = fetcher, sleep or time.sleep, monotonic
        self.count, self.last, self.robots, self.blocked, self.retry_until = {}, {}, {}, {}, {}

    def raw(self, url):
        url = canonical(url)
        host = urllib.parse.urlsplit(url).hostname
        if host in self.blocked:
            raise ValueError(self.blocked[host])
        if self.count.get(host, 0) >= 20:
            raise ValueError('source_request_limit')
        delay = max(1, self.robots[host].crawl_delay(AGENT) or 0) if self.robots.get(host) else 1
        if host in self.last:
            self.sleep(max(0, delay - (self.clock() - self.last[host])))
        self.count[host] = self.count.get(host, 0) + 1
        self.last[host] = self.clock()
        try:
            return self.fetcher(url)
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                retry = error.headers.get('Retry-After', '') if error.headers else ''
                self.blocked[host] = 'source_http_' + str(error.code) + (':retry_after=' + retry[:80] if retry else '')
                moment = datetime.now(timezone.utc) + timedelta(hours=6)
                try:
                    parsed = (datetime.now(timezone.utc) + timedelta(seconds=max(0, int(retry)))) if retry.isdigit() else parsedate_to_datetime(retry)
                    if parsed and parsed.tzinfo: moment = max(moment, parsed.astimezone(timezone.utc))
                except (ValueError, TypeError, OverflowError): pass
                self.retry_until[host] = moment.isoformat()
            raise

    def get(self, url):
        url = canonical(url)
        host = urllib.parse.urlsplit(url).hostname
        if host not in self.robots:
            robots = urllib.robotparser.RobotFileParser()
            try:
                body = self.raw('https://' + host + '/robots.txt')
                robots.parse(body.decode('utf-8-sig').splitlines())
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                robots.parse([])
            self.robots[host] = robots
        if not self.robots[host].can_fetch(AGENT, url):
            raise ValueError('robots_disallowed')
        return self.raw(url)


class Page(HTMLParser):
    """Extract only publisher-specific main containers, not flattened whole pages."""
    def __init__(self, host):
        super().__init__(convert_charrefs=True)
        self.host, self.stack, self.parts, self.title = host, [], [], []
        self.links, self.scripts, self.script, self.metadata = [], [], [], {}
        self.heading_done, self.author_level, self.author_parts = False, None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get('class') or '').split())
        parent = self.stack[-1] if self.stack else (False, False, False, False)
        main = parent[0] or ('post-content' in classes if self.host == 'rkf.org.ru' else bool(classes & {'fulltext', 'publ_expert'}))
        hidden = parent[1] or tag in ('script', 'style', 'nav', 'footer') or bool(classes & {
            'sharedaddy', 'related-posts', 'fusion-sharing-box', 'post-tags', 'post_img_title', 'advertisement'})
        heading = parent[2] or (tag == 'h1' and not self.heading_done)
        ld = tag == 'script' and attrs.get('type') == 'application/ld+json'
        if tag not in VOID:
            self.stack.append((main, hidden, heading, parent[3] or ld))
        if tag == 'a' and 'author' in (attrs.get('rel') or '').split():
            self.author_level = len(self.stack)
        if tag == 'meta':
            self.metadata[attrs.get('property', attrs.get('name', ''))] = attrs.get('content', '')
        if tag == 'a' and attrs.get('href'):
            self.links.append((attrs['href'], main and not hidden))
        if tag in ('p', 'li', 'h1', 'h2', 'h3', 'br') and main and not hidden:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if tag == 'h1' and self.stack and self.stack[-1][2] and not self.stack[-1][1]:
            self.heading_done = True
        if tag == 'a': self.author_level = None
        if self.stack:
            _, _, _, ld = self.stack.pop()
            if tag == 'script' and ld:
                self.scripts.append(''.join(self.script))
                self.script = []
        if tag in ('p', 'li', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_data(self, text):
        if not self.stack:
            return
        main, hidden, heading, ld = self.stack[-1]
        if ld:
            self.script.append(text)
        if self.author_level is not None and not hidden: self.author_parts.append(text)
        if heading and not hidden:
            self.title.append(text)
        if main and not hidden:
            self.parts.append(text)


def page_snapshot(body, url):
    from collector.main import DOG_WORDS
    url = canonical(url)
    host = urllib.parse.urlsplit(url).hostname
    if host not in ('rkf.org.ru', 'vetandlife.ru'):
        raise ValueError('unapproved_page_provider')
    parser = Page(host)
    parser.feed(body.decode('utf-8-sig'))
    text = ' '.join(''.join(parser.parts).split())
    title = ' '.join(''.join(parser.title).split()) or parser.metadata.get('og:title', '')
    if len(text.split()) < 35 or not title or not DOG_WORDS.search(title + ' ' + text):
        raise ValueError('insufficient_content')
    metadata = dict(parser.metadata)
    def scan(value):
        if isinstance(value, list):
            for item in value: scan(item)
        elif isinstance(value, dict):
            if value.get('@type') in ('Article', 'NewsArticle', 'WebPage'):
                for key in ('datePublished', 'dateModified', 'author'):
                    if value.get(key): metadata[key] = value[key]
            if '@graph' in value: scan(value['@graph'])
    for script in parser.scripts:
        try: scan(json.loads(script))
        except (ValueError, TypeError): pass
    author = metadata.get('author', '')
    if isinstance(author, dict): author = author.get('name', '')
    if isinstance(author, list): author = ', '.join(a.get('name', '') for a in author if isinstance(a, dict))
    if not isinstance(author, str): author = ''
    if not author: author = ' '.join(''.join(parser.author_parts).split())
    date_text = metadata.get('datePublished') or metadata.get('article:published_time', '')
    published = datetime.fromisoformat(date_text.replace('Z', '+00:00')) if date_text else None
    if published:
        published = (published.replace(tzinfo=timezone.utc) if published.tzinfo is None else published.astimezone(timezone.utc))
    excerpt = ' '.join(text.split()[:800])
    return dict(title=title, text=excerpt, published_at=published, author=author,
                source_language='ru', publisher='РКФ' if host == 'rkf.org.ru' else 'Питомцы · Ветеринария и жизнь',
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                excerpt_hash=hashlib.sha256(excerpt.encode()).hexdigest(),
                # Full extracted body is checked privately for dangerous recommendations before truncation.
                safety_text=text[:60000], truncated=len(text.split()) > 800,
                links=[urllib.parse.urljoin(url, link) for link, inside in parser.links if inside])


def topic_hint(title, text='', category=''):
    value = title + ' ' + text[:600]
    patterns = (
        ('learning', r'обуч|дрессиров|трениров|поощр|ноузворк|train|learn'),
        ('nutrition', r'питан|рацион|нутриент|корм|nutrition|nutrient|diet'),
        ('behavior', r'поведен|эмоц|общени|язык тела|сигнал|когнит|behavio|cognit|emotion'),
    )
    for topic, pattern in patterns:
        if re.search(pattern, value, re.I): return topic
    return 'nutrition' if 'zoo-menu' in category else 'welfare'


def page_candidate(body, url, discovery, today):
    snap = page_snapshot(body, url)
    published = snap['published_at']
    from collector.main import five_year_start, MAX_RECORD_BYTES
    if published and not five_year_start(today) <= published.date() <= today:
        raise ValueError('outside_window')
    host = urllib.parse.urlsplit(url).hostname
    kind = 'news' if re.search(r'\b(?:открыли|открылся|запустили|пройд[её]т|состоится|объявили|начал[аи]?сь)\b', snap['title'], re.I) else 'guide'
    payload = dict(version=2, provider='rkf' if host == 'rkf.org.ru' else 'pets',
                   canonical_url=canonical(url), discovery_url=discovery, material_type_hint=kind,
                   retrieved_at=datetime.now(timezone.utc).isoformat(), content_scope='page_excerpt',
                   rights_note='Краткая аннотация с атрибуцией и прямой ссылкой; без перепечатки статьи.',
                   **{key: value for key, value in snap.items() if key not in ('published_at', 'text', 'safety_text')})
    payload['published_at'] = published.isoformat() if published else None
    payload['excerpt'] = snap['text']
    payload['topic_hint'] = topic_hint(snap['title'], snap['text'], discovery)
    if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_RECORD_BYTES:
        raise ValueError('record_too_large')
    return dict(title=snap['title'], text=snap['text'], url=canonical(url), published_at=published,
                raw_payload=payload, content_scope='page_excerpt')


def known_url(conn, url):
    return bool(conn.execute('SELECT 1 FROM materials WHERE url=%s', (url,)).fetchone())


def owner_candidates(conn, kind, url, today, http, stats):
    from collector.main import DOG_WORDS
    body = http.get(url)
    if kind == 'owner_html':
        parser = Page('rkf.org.ru'); parser.feed(body.decode('utf-8-sig'))
        urls = [urllib.parse.urljoin(url, link) for link, _ in parser.links]
        urls = [u for u in urls if urllib.parse.urlsplit(u).path.startswith('/poleznye-materialy-o-sobakah/')
                and urllib.parse.urlsplit(u).path != '/poleznye-materialy-o-sobakah/'][:60]
    else:
        root = ET.fromstring(body)
        if root.tag != 'rss': raise ValueError('invalid_rss_response')
        urls = [(x.findtext('link') or '').strip() for x in root.findall('./channel/item')[:60]]
        # Editorial sections also contain offsite, commerce and non-owner links.
        urls = [u for u in urls if urllib.parse.urlsplit(u).hostname == 'vetandlife.ru'
                and urllib.parse.urlsplit(u).path.startswith('/pets/')]
    stats['read_ok'] = True
    for link in dict.fromkeys(urls):
        stats['scanned'] += 1
        try:
            link = canonical(link)
            if urllib.parse.urlsplit(link).hostname != urllib.parse.urlsplit(url).hostname:
                raise ValueError('unapproved_source_url')
            if known_url(conn, link):
                stats['duplicates'] += 1; continue
            item = page_candidate(http.get(link), link, url, today)
            cat_title = re.search(r'\b(?:кошк\w*|кот(?:а|у|ом|е|ы|ов|ам|ами|ах)?|cats?|feline)\b', item['title'], re.I)
            if (not DOG_WORDS.search(item['title'] + ' ' + item['text'])
                    or (cat_title and not DOG_WORDS.search(item['title']))):
                stats['skips']['outside_dog_topic'] += 1; continue
            yield item
        except (ValueError, UnicodeError) as error:
            stats['skips'][str(error)] += 1
            if str(error) in ('source_request_limit', 'robots_disallowed'):
                return


def cursor_get(conn, source, window, bounds):
    key = dict(version=QUERY_VERSION, source=source, window=window, bounds=bounds)
    row = conn.execute("SELECT note FROM runs WHERE service='collector_cursor' AND note LIKE %s ORDER BY id DESC LIMIT 1",
                       (json.dumps(key)[:-1] + ',%',)).fetchone()
    if row:
        value = json.loads(row[0]); return value['position'], key
    return {}, key


def cursor_write(conn, state):
    conn.execute("INSERT INTO runs(service,status,finished_at,note) VALUES ('collector_cursor','finished',clock_timestamp(),%s)",
                 (json.dumps(state),))


def api_candidates(conn, name, kind, url, today, http, stats):
    from collector.main import (EUROPEPMC_TERMS, candidate, publication_date, plain_text, five_year_start, InvalidRecord)
    fresh = today - timedelta(days=89)
    for window, start, end, pages in (('evergreen', five_year_start(today), fresh - timedelta(days=1), 3),
                                       ('fresh', fresh, today, 7)):
        pos, key = cursor_get(conn, name, window, [start.isoformat(), end.isoformat()])
        cursor, offset = pos.get('cursor', '*' if kind == 'europepmc' else 0), pos.get('offset', 0)
        for _ in range(pages):
            if kind == 'europepmc':
                params = dict(query=f'{EUROPEPMC_TERMS} AND FIRST_PDATE:[{start} TO {end}]', format='json',
                              resultType='core', pageSize=15, sort='FIRST_PDATE_D desc', cursorMark=cursor)
            else:
                params = dict(q=f'title:(dog OR dogs OR canine) AND publication_date:[{start}T00:00:00Z TO {end}T23:59:59Z]',
                              fl='id,title,abstract,publication_date,author,journal,article_type', rows=15,
                              sort='publication_date desc,id asc', start=cursor, wt='json')
            request = url + '?' + urllib.parse.urlencode(params)
            data = json.loads(http.get(request))
            records = data['resultList']['result'] if kind == 'europepmc' else data['response']['docs']
            if not isinstance(records, list) or len(records) > 15: raise ValueError('invalid_api_response')
            stats['read_ok'] = True
            next_cursor = data.get('nextCursorMark') if kind == 'europepmc' else int(cursor) + len(records)
            exhausted = (not records or (kind == 'europepmc' and (not next_cursor or next_cursor == cursor))
                         or (kind == 'plos' and next_cursor >= data['response']['numFound']))
            for index, record in enumerate(records[offset:], offset):
                state = dict(**key, position=dict(cursor=cursor, offset=index + 1))
                stats['scanned'] += 1
                try:
                    if kind == 'europepmc':
                        if record.get('source') != 'MED' or not re.fullmatch(r'\d+', str(record.get('id', ''))):
                            raise ValueError('invalid_identifier')
                        if record.get('language') not in ('eng', 'rus', None):
                            raise ValueError('outside_language')
                        item_url = f"https://europepmc.org/article/MED/{record['id']}"
                        title, text = record.get('title'), record.get('abstractText')
                        moment = publication_date(record.get('firstPublicationDate'))
                    else:
                        if not re.fullmatch(r'10\.1371/[a-zA-Z0-9./_-]+', record.get('id', '')):
                            raise ValueError('invalid_identifier')
                        if not isinstance(record.get('abstract'), list) or any(not isinstance(t, str) for t in record['abstract']):
                            raise ValueError('invalid_abstract')
                        item_url = plos_url(record['id'])
                        title, text = record.get('title'), ' '.join(record.get('abstract', []))
                        moment = datetime.fromisoformat(record['publication_date'].replace('Z', '+00:00'))
                    payload = dict(version=2, provider=kind, record=record, request_url=request, discovery_window=window,
                                   canonical_url=canonical(item_url), discovery_url=url, material_type_hint='research',
                                   retrieved_at=datetime.now(timezone.utc).isoformat(), content_scope='abstract',
                                   excerpt=plain_text(text), content_hash=hashlib.sha256(plain_text(text).encode()).hexdigest(),
                                   upstream_ids=dict(doi=record.get('doi') if kind == 'europepmc' else record.get('id')),
                                   source_language='ru' if record.get('language') == 'rus' else 'en',
                                   topic_hint=topic_hint(title or '', plain_text(text)), parser_version=7)
                    item = candidate(title, text, item_url, moment, payload, 'abstract', today,
                                     research=True, oldest=start, newest=end)
                    item['_cursor'] = state
                    yield item
                    if stats.pop('_archive_full', False):
                        break
                except (ValueError, TypeError, KeyError) as error:
                    stats['skips'][str(error)] += 1
                    cursor_write(conn, state)
            if window == 'evergreen' and stats.pop('_archive_stopped', False):
                break
            # A generator is resumed only after the driver saved/considered its yielded rows.
            if exhausted:
                cursor_write(conn, dict(**key, position={}))
                break
            cursor, offset = next_cursor, 0
            cursor_write(conn, dict(**key, position=dict(cursor=cursor, offset=0)))


def collect_sources(conn, today, run_id, fetcher):
    from collector.main import save_candidate, read_rss, is_evergreen
    http = SourceHTTP(fetcher)
    rows = conn.execute('SELECT id,name,kind,url FROM sources WHERE active AND name=ANY(%s) ORDER BY id',
                        (list(REGISTRY),)).fetchall()
    groups = []
    total, archive = 0, 0
    for sid, name, kind, url in rows:
        expected_kind, expected_url, quota = REGISTRY[name]
        stats = dict(id=sid, name=name, new=0, scanned=0, duplicates=0, skips=Counter(), read_ok=False, error=None)
        host = urllib.parse.urlsplit(url).hostname
        paused = conn.execute("SELECT note FROM runs WHERE service='collector_source_state' AND note LIKE %s ORDER BY id DESC LIMIT 1",
                              ('{"host": "' + host + '",%',)).fetchone()
        try:
            if paused and isinstance(paused[0], str):
                value = json.loads(paused[0])
                until = datetime.fromisoformat(value['retry_until'])
                if until > datetime.now(timezone.utc):
                    http.blocked[host] = 'source_cooldown_until:' + value['retry_until']
        except (ValueError, KeyError, TypeError):
            http.blocked[host] = 'invalid_source_cooldown'
        if (kind, url) != (expected_kind, expected_url):
            stats['error'] = 'source_configuration_mismatch'; iterator = iter(())
        elif kind in ('owner_html', 'owner_rss'):
            iterator = owner_candidates(conn, kind, url, today, http, stats)
        elif kind in ('europepmc', 'plos'):
            iterator = api_candidates(conn, name, kind, url, today, http, stats)
        else:
            def rss_iterator(url=url, stats=stats):
                # Keep the legacy parser, sharing the bounded HTTP client.
                batch = read_rss(url, today, fetcher=http.get)
                stats.update(scanned=batch.scanned, read_ok=batch.read_ok, error=batch.error)
                stats['skips'].update(batch.rejected)
                yield from batch.items
            iterator = rss_iterator()
        groups.append((stats, iter(iterator), quota))

    def take(group, target):
        nonlocal total, archive
        stats, iterator, _ = group
        while stats['new'] < target and total < 30 and not stats.get('ended'):
            try:
                item = next(iterator)
                research_old = item['content_scope'] == 'abstract' and is_evergreen(item, today)
                if research_old and archive >= 5:
                    stats['skips']['archive_quota'] += 1
                    stats['_archive_full'] = True
                    stats['_archive_stopped'] = True
                    continue
                with conn.transaction():
                    if save_candidate(conn, stats['id'], run_id, item):
                        stats['new'] += 1; total += 1; archive += int(research_old)
                    else: stats['duplicates'] += 1
                    if item.get('_cursor'): cursor_write(conn, item['_cursor'])
            except StopIteration:
                stats['ended'] = True
            except Exception as error:
                stats['error'] = type(error).__name__ + ':' + str(error)[:200]; stats['ended'] = True
    for group in groups: take(group, group[2])
    while total < 30:
        before = total
        for group in groups: take(group, group[0]['new'] + 1)
        if total == before: break
    notes = [f'новых кандидатов: {total}; очередь автообработки (collected); архивных исследований: {archive}/5']
    for stats, _, _ in groups:
        notes.append(f"{stats['name']}: просмотрено {stats['scanned']}, новых {stats['new']}, дублей {stats['duplicates']}, "
                     f"пропуски {dict(stats['skips'])}" + (f"; ошибка {stats['error']}" if stats['error'] else ''))
    for host, until in http.retry_until.items():
        conn.execute("INSERT INTO runs(service,status,finished_at,note) VALUES ('collector_source_state','finished',clock_timestamp(),%s)",
                     (json.dumps(dict(host=host, retry_until=until, reason=http.blocked[host])),))
    if groups and not any(s['read_ok'] for s, _, _ in groups): raise RuntimeError(' | '.join(notes))
    return total, ' | '.join(notes)
