"""T02: bounded collection, faithful input snapshots, and safe schema upgrade."""

import json
import os
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

import db
from collector import main as collector
from worker import main as worker

TODAY = date(2026, 11, 12)
API_URL = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
RSS_URL = 'https://www.sciencedaily.com/rss/plants_animals/dogs.xml'


def record(number=1, **changes):
    result = {'id': str(number), 'source': 'MED', 'language': 'eng',
              'title': f'Dog behaviour and welfare {number}',
              'abstractText': '<p>Dogs learn through training.</p>',
              'firstPublicationDate': TODAY.isoformat(), 'doi': f'10.1234/{number}'}
    result.update(changes)
    return result


def api_page(records, **extras):
    return json.dumps({'resultList': {'result': records}, **extras}).encode()


def rss_item(number=1, title=None, description='<p>Dogs learn through training.</p>',
             link=None, pubdate='Thu, 12 Nov 2026 12:00:00 +0000'):
    return (f'<item><title>{escape(title if title is not None else f"Dog learning {number}")}</title>'
            f'<link>{escape(link if link is not None else f"https://example.invalid/{number}")}</link>'
            f'<description>{escape(description)}</description>'
            f'<pubDate>{escape(pubdate)}</pubDate></item>')


def rss(*items):
    return ('<rss><channel>' + ''.join(items) + '</channel></rss>').encode()


class ReaderTests(unittest.TestCase):
    def read_api(self, records):
        with patch.object(collector, 'fetch', return_value=api_page(records)):
            return collector.read_europepmc(API_URL, TODAY, include_archive=False)

    def test_full_abstract_and_original_metadata_are_preserved(self):
        item = record(abstractText='<p>' + 'Dogs learn. ' * 200 + '</p>',
                      authorList={'author': [{'fullName': 'Test Author'}]})
        batch = self.read_api([item])
        self.assertTrue(batch.read_ok)
        self.assertIsNone(batch.error)
        result = batch.items[0]
        self.assertGreater(len(result['text']), 1500)
        self.assertNotIn('<p>', result['text'])
        self.assertEqual(result['raw_payload']['record'], item)
        self.assertEqual(result['content_scope'], 'abstract')
        self.assertEqual(result['url'], 'https://europepmc.org/article/MED/1')
        from urllib.parse import parse_qs, urlsplit
        params = parse_qs(urlsplit(result['raw_payload']['request_url']).query)
        self.assertIn('FIRST_PDATE:[2026-08-15 TO 2026-11-12]', params['query'][0])
        self.assertIn('TITLE:собак*', params['query'][0])
        self.assertEqual(params['resultType'], ['core'])
        self.assertEqual(params['pageSize'], ['15'])

    def test_no_abstract_is_metadata_only(self):
        self.assertEqual(self.read_api([record(abstractText=None)]).items[0]['content_scope'], 'metadata_only')

    def test_rss_preserves_xml_and_cleans_text_without_truncation(self):
        text = '<p>Dog training &amp; welfare.</p>' * 100 + '<script>secret()</script>'
        with patch.object(collector, 'fetch', return_value=rss(rss_item(description=text))):
            result = collector.read_rss(RSS_URL, TODAY).items[0]
        self.assertEqual(result['content_scope'], 'feed_excerpt')
        self.assertGreater(len(result['text']), 1500)
        self.assertNotIn('secret()', result['text'])
        self.assertIn('secret()', result['raw_payload']['record'])
        self.assertIn('Dog training & welfare.', result['text'])

    def test_bad_item_does_not_drop_valid_item(self):
        with patch.object(collector, 'fetch', return_value=rss(
                rss_item(1, pubdate='bad date'), rss_item(2, link='javascript:alert(1)'),
                rss_item(3, link='https://example.invalid:bad-port/'), rss_item(4))):
            batch = collector.read_rss(RSS_URL, TODAY)
        self.assertEqual(len(batch.items), 1)
        self.assertEqual(batch.rejected['invalid_date'], 1)
        self.assertEqual(batch.rejected['invalid_url'], 2)
        self.assertIsNone(batch.error)

    def test_topic_and_word_boundaries(self):
        examples = [('Cat welfare', False), ('Dogma and behaviour', False),
                    ('Dog genomic biomarkers', False), ('Dogs and cognition', True),
                    ('Canine enrichment', True), ('Обучение собак', True),
                    ('Благополучие псов', True),
                    ('Canine MRI with deep learning', False),
                    ('Canine tumours and biological behaviour', False),
                    ('Canine disease and training datasets', False),
                    ('Обнаружение опухолей собак: машинное обучение', False),
                    ('Dog behaviour measured using machine learning', True),
                    ('A dog learns', True), ('Enriching the lives of dogs', True)]
        for title, expected in examples:
            with self.subTest(title=title):
                batch = self.read_api([record(title=title, abstractText=None)])
                self.assertEqual(bool(batch.items), expected)

    def test_future_and_out_of_window_dates_are_rejected(self):
        for delta, expected in [(0, True), (-89, True), (-90, False), (1, False)]:
            with self.subTest(delta=delta):
                batch = self.read_api([record(firstPublicationDate=(TODAY + timedelta(days=delta)).isoformat())])
                self.assertEqual(bool(batch.items), expected)

    def test_rss_timezone_is_converted_before_window_check(self):
        with patch.object(collector, 'fetch', return_value=rss(
                rss_item(1, pubdate='Fri, 13 Nov 2026 00:30:00 +0200'),
                rss_item(2, pubdate='Thu, 12 Nov 2026 23:30:00 -0200'))):
            batch = collector.read_rss(RSS_URL, TODAY)
        self.assertEqual(len(batch.items), 1)
        self.assertTrue(batch.items[0]['url'].endswith('/1'))

    def test_oversize_record_is_not_silently_truncated(self):
        batch = self.read_api([record(abstractText='Dogs training ' * 30000)])
        self.assertEqual(batch.items, [])
        self.assertEqual(batch.rejected['record_too_large'], 1)

    def test_api_cursor_budget_and_partial_failure(self):
        pages = [api_page([record(1)], nextCursorMark='page2'), OSError('source unavailable')]
        with patch.object(collector, 'fetch', side_effect=pages):
            batch = collector.read_europepmc(API_URL, TODAY)
        self.assertTrue(batch.read_ok)
        self.assertEqual(len(batch.items), 1)
        self.assertIn('unavailable', batch.error)
        pages = [api_page([record(i)], nextCursorMark=f'page{i + 1}') for i in range(10)]
        with patch.object(collector, 'fetch', side_effect=pages) as fetch:
            batch = collector.read_europepmc(API_URL, TODAY)
        self.assertEqual(fetch.call_count, 10)
        self.assertTrue(batch.limited)

    def test_rss_item_budget(self):
        with patch.object(collector, 'fetch', return_value=rss(*(rss_item(i) for i in range(65)))):
            batch = collector.read_rss(RSS_URL, TODAY)
        self.assertEqual(batch.scanned, 60)
        self.assertEqual(len(batch.items), 60)
        self.assertTrue(batch.limited)

    def test_empty_and_invalid_responses_are_distinguishable(self):
        self.assertTrue(self.read_api([]).read_ok)
        with patch.object(collector, 'fetch', return_value=b'{"error": "bad request"}'):
            batch = collector.read_europepmc(API_URL, TODAY)
        self.assertFalse(batch.read_ok)
        self.assertIsNotNone(batch.error)
        with patch.object(collector, 'fetch', return_value=b'<html/>'):
            batch = collector.read_rss(RSS_URL, TODAY)
        self.assertFalse(batch.read_ok)

    def test_missing_date_identifier_title_and_bad_language_are_skipped(self):
        for changes in ({'firstPublicationDate': None}, {'id': '../bad'}, {'source': 'PPR'},
                        {'title': None}, {'language': 'fra'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.read_api([record(**changes)]).items, [])

    def test_non_finite_metadata_does_not_drop_other_records(self):
        batch = self.read_api([record(1, invalid=float('nan')), record(2)])
        self.assertEqual(len(batch.items), 1)
        self.assertEqual(batch.rejected['invalid_payload'], 1)
        self.assertIsNone(batch.error)

    def test_fetch_size_limit_and_timeout_without_network(self):
        @contextmanager
        def response(*args, **kwargs):
            class Response:
                def read(self, size):
                    self.requested = size
                    return b'x' * size
            yield Response()
        with patch('urllib.request.OpenerDirector.open', side_effect=response) as network:
            with self.assertRaisesRegex(ValueError, 'response_too_large'):
                collector.fetch(RSS_URL)
        self.assertEqual(network.call_args.kwargs['timeout'], 15)
        with patch('urllib.request.OpenerDirector.open') as network:
            with self.assertRaisesRegex(ValueError, 'invalid_source_url'):
                collector.fetch('file:///private')
            network.assert_not_called()


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Set local TEST_DATABASE_URL for PostgreSQL acceptance tests')
class CollectorDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ['TEST_DATABASE_URL']
        config = conninfo_to_dict(cls.dsn)
        if (config.get('host') not in ('localhost', '127.0.0.1', '::1')
                or config.get('dbname') != 'project_dog_t01_test'
                or any(config.get(k) for k in ('hostaddr', 'options', 'service'))):
            raise RuntimeError('Use a separate local project_dog_t01_test database')

    def setUp(self):
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.conn.close)
        self.schema = 't02_' + uuid4().hex
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(lambda: self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema))))
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.log_patch = patch.object(db, 'log').start()
        self.addCleanup(patch.stopall)
        patch('urllib.request.urlopen', side_effect=AssertionError('External HTTP forbidden in tests')).start()
        patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('External HTTP forbidden in tests')).start()
        patch('collector.sources.time.sleep').start()
        patch.dict(os.environ, {'OPENROUTER_API_KEY': ''}).start()

    def init(self):
        db.init_schema(self.conn)
        # These historical SQL allocation tests isolate the original two publishers.
        # New defaults/migration are exercised without disabling them in test_t17_database.
        self.conn.execute("UPDATE sources SET active=false WHERE name=ANY(%s)",
                          (['RKF: Owner guides','Pets: Learning','Pets: Nutrition','PLOS: Dogs'],))

    def read_network(self, science=20, articles=20, error=None):
        def fetch(url):
            if url.endswith('/robots.txt'): return b'User-agent: *\nDisallow:\n'
            if url.startswith(API_URL):
                if error == 'science':
                    raise OSError('science unavailable')
                from urllib.parse import parse_qs, urlsplit
                params = parse_qs(urlsplit(url).query)
                if 'TO ' + TODAY.isoformat() + ']' not in params['query'][0]: return api_page([])
                cursor = params['cursorMark'][0]
                offset = 0 if cursor == '*' else int(cursor)
                records = [record(i) for i in range(offset,min(science,offset+15))]
                return api_page(records, **({'nextCursorMark':str(offset+15)} if offset+15<science else {}))
            return rss(*(rss_item(i) for i in range(articles)))
        return patch.object(collector, 'fetch', side_effect=fetch)

    def test_upgrade_and_reinit_preserve_existing_data_and_custom_sources(self):
        baseline = Path(__file__).with_name('fixtures').joinpath('schema_t01.sql').read_text(encoding='utf-8')
        self.conn.execute(baseline)
        sid = self.conn.execute('SELECT id FROM sources ORDER BY id LIMIT 1').fetchone()[0]
        mid = self.conn.execute("INSERT INTO materials (source_id,url,title,text,status) VALUES (%s,'https://legacy.invalid','legacy','original','skipped') RETURNING id", (sid,)).fetchone()[0]
        self.conn.execute("INSERT INTO findings (material_id,summary,model,prompt_version,publication_status,sent_at) VALUES (%s,'old','test','v1','published',now())", (mid,))
        self.conn.execute("INSERT INTO subscribers (chat_id,name) VALUES (42,'original')")
        self.conn.execute("INSERT INTO sources (name,kind,url) VALUES ('custom','rss','https://custom.invalid')")
        before = self.conn.execute('SELECT * FROM findings').fetchall()
        self.init()
        self.assertEqual(self.conn.execute('SELECT * FROM findings').fetchall(), before)
        self.assertEqual(self.conn.execute('SELECT text,status,content_scope,raw_payload FROM materials').fetchone(),
                         ('original', 'skipped', 'legacy_unknown', None))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM sources WHERE active').fetchone()[0], 3)
        self.conn.execute('UPDATE sources SET active=false WHERE name=%s', (collector.ARTICLE_SOURCE,))
        self.init()
        self.assertEqual(self.conn.execute('SELECT active FROM sources WHERE name=%s', (collector.ARTICLE_SOURCE,)).fetchone()[0], False)
        self.assertEqual(self.conn.execute("SELECT name FROM subscribers WHERE chat_id=42").fetchone()[0], 'original')

    def test_scope_constraint_and_atomic_failure(self):
        self.init()
        sid = self.conn.execute('SELECT id FROM sources LIMIT 1').fetchone()[0]
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute("INSERT INTO materials (source_id,url,title,content_scope) VALUES (%s,'https://invalid.invalid','test','fulltext')", (sid,))
        # The whole script, including earlier writes, must roll back on failure.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'schema.sql'
            script.write_text("ALTER TABLE materials ADD COLUMN rollback_probe TEXT; UPDATE sources SET active=false; SELECT * FROM missing_t02_table;", encoding='utf-8')
            before = self.conn.execute('SELECT id,active FROM sources ORDER BY id').fetchall()
            with patch.object(db, 'SCHEMA', script), self.assertRaises(psycopg.errors.UndefinedTable):
                self.init()
            self.assertEqual(self.conn.execute('SELECT id,active FROM sources ORDER BY id').fetchall(), before)
            self.assertEqual(self.conn.execute(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema=%s AND table_name='materials' AND column_name='rollback_probe'",
                (self.schema,),
            ).fetchone()[0], 0)
        self.init()  # lock and connection remain usable after failed transaction

    def test_total_limit_quotas_and_collected_state(self):
        self.init()
        with self.read_network():
            self.assertEqual(collector.collect(self.conn, TODAY), 30)
        rows = self.conn.execute('SELECT s.name,count(*) FROM materials m JOIN sources s ON s.id=m.source_id GROUP BY s.name').fetchall()
        self.assertEqual(dict(rows), {collector.RESEARCH_SOURCE: 17, collector.ARTICLE_SOURCE: 13})
        self.assertEqual(self.conn.execute('SELECT DISTINCT status FROM materials').fetchall(), [('collected',)])
        with patch.object(worker, 'ask_model') as model:
            worker.process_new(self.conn)
            model.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM findings').fetchone()[0], 0)

    def test_repeat_keeps_first_snapshot_and_passes_duplicates(self):
        self.init()
        with self.read_network(science=15, articles=0):
            self.assertEqual(collector.collect(self.conn, TODAY), 15)
        before = self.conn.execute('SELECT url,text,raw_payload,status,collected_at FROM materials ORDER BY id').fetchall()
        with self.read_network(science=15, articles=0):
            self.assertEqual(collector.collect(self.conn, TODAY), 0)
        self.assertEqual(self.conn.execute('SELECT url,text,raw_payload,status,collected_at FROM materials ORDER BY id').fetchall(), before)
        pages = [api_page([record(i) for i in range(15)], nextCursorMark='next'), api_page([record(99)])]
        def fetch(url):
            if url.endswith('/robots.txt'): return b'User-agent: *\nDisallow:\n'
            if url.startswith(API_URL):
                if 'TO ' + TODAY.isoformat() + ']' not in parse_qs(urlsplit(url).query)['query'][0]:
                    return api_page([])
                return pages.pop(0)
            return rss()
        with patch.object(collector, 'fetch', side_effect=fetch):
            self.assertEqual(collector.collect(self.conn, TODAY), 1)

    def test_reallocation_and_single_source_failure(self):
        self.init()
        with self.read_network(error='science'):
            self.assertEqual(collector.collect(self.conn, TODAY), 20)
        status, note = self.conn.execute('SELECT status,note FROM runs ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(status, 'finished')
        self.assertIn('science unavailable', note)

    def test_both_sources_fail_but_valid_empty_run_finishes(self):
        self.init()
        with patch.object(collector, 'fetch', side_effect=OSError('unavailable')):
            with self.assertRaises(RuntimeError):
                collector.collect(self.conn, TODAY)
        self.assertEqual(self.conn.execute('SELECT status FROM runs ORDER BY id DESC LIMIT 1').fetchone()[0], 'failed')
        with self.read_network(science=0, articles=0):
            self.assertEqual(collector.collect(self.conn, TODAY), 0)
        self.assertEqual(self.conn.execute('SELECT status FROM runs ORDER BY id DESC LIMIT 1').fetchone()[0], 'finished')

    def test_disabled_and_unapproved_sources_are_not_fetched(self):
        self.init()
        self.conn.execute('UPDATE sources SET active=false')
        self.conn.execute("INSERT INTO sources (name,kind,url) VALUES ('custom','rss','https://custom.invalid')")
        with patch.object(collector, 'fetch') as fetch:
            self.assertEqual(collector.collect(self.conn, TODAY), 0)
            fetch.assert_not_called()

    def test_archive_limit_preserves_dates_and_original_snapshots(self):
        self.init()
        from urllib.parse import parse_qs, urlsplit
        def fetch(url):
            if url.endswith('/robots.txt'): return b'User-agent: *\nDisallow:\n'
            if not url.startswith(API_URL):
                return rss()
            query = parse_qs(urlsplit(url).query)['query'][0]
            if 'TO 2026-11-12]' in query:
                return api_page([record(i) for i in range(1, 11)])
            return api_page([record(i, firstPublicationDate='2023-05-01') for i in range(20, 30)])
        with patch.object(collector, 'fetch', side_effect=fetch):
            self.assertEqual(collector.collect(self.conn, TODAY), 15)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials WHERE raw_payload->>'discovery_window'='evergreen'").fetchone()[0], 5)
        self.assertEqual(self.conn.execute("SELECT DISTINCT published_at::date FROM materials WHERE raw_payload->>'discovery_window'='evergreen'").fetchall(), [(date(2023, 5, 1),)])
        before = self.conn.execute('SELECT id,text,raw_payload,collected_at FROM materials ORDER BY id').fetchall()
        with patch.object(collector, 'fetch', side_effect=fetch):
            collector.collect(self.conn, TODAY)
        for row in before:
            self.assertEqual(self.conn.execute('SELECT id,text,raw_payload,collected_at FROM materials WHERE id=%s', (row[0],)).fetchone(), row)


class AllocationTests(unittest.TestCase):
    """Exercise the real allocation loop with an in-memory store, not simulated SQL."""

    def run_collection(self, science=30, articles=30, seen=(), science_error=None, rss_error=None):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            (1, collector.RESEARCH_SOURCE, 'europepmc', API_URL),
            (2, collector.ARTICLE_SOURCE, 'rss', RSS_URL),
        ]
        stored = set(seen)
        inserted = []

        def save(conn, source_id, run_id, item):
            if item['url'] in stored:
                return False
            stored.add(item['url'])
            inserted.append((source_id, item['url']))
            return True

        def batch(prefix, count, error):
            return collector.Batch(items=[{'url': f'{prefix}/{i}', 'content_scope': 'feed_excerpt', 'published_at': datetime(2026,11,12,tzinfo=timezone.utc)} for i in range(count)],
                                   scanned=count, read_ok=error is None, error=error)
        readers = {
            'europepmc': lambda url, today: batch('research', science, science_error),
            'rss': lambda url, today: batch('article', articles, rss_error),
        }
        def science_iterator(conn, name, kind, url, today, http, stats):
            value = readers['europepmc'](url, today)
            stats.update(read_ok=value.read_ok, scanned=value.scanned, error=value.error)
            yield from value.items
        with patch('collector.sources.api_candidates', side_effect=science_iterator), patch.object(collector, 'read_rss', side_effect=lambda url,today,fetcher=None: readers['rss'](url,today)), patch.object(collector, 'save_candidate', side_effect=save), \
                patch.object(db, 'start_run', return_value=1), patch.object(db, 'finish_run') as finished, \
                patch.object(db, 'fail_run') as failed:
            try:
                total = collector.collect(conn, TODAY)
            except RuntimeError:
                self.assertTrue(failed.called)
                self.assertFalse(finished.called)
                raise
        return total, inserted, finished.call_args.args[-1]

    def test_two_groups_share_a_total_limit_of_thirty(self):
        total, inserted, note = self.run_collection()
        self.assertEqual(total, 30)
        self.assertEqual(sum(sid == 1 for sid, _ in inserted), 17)
        self.assertEqual(sum(sid == 2 for sid, _ in inserted), 13)
        self.assertIn('новых кандидатов: 30', note)

    def test_unused_research_slots_go_to_articles(self):
        total, inserted, _ = self.run_collection(science=2)
        self.assertEqual(total, 30)
        self.assertEqual(sum(sid == 2 for sid, _ in inserted), 28)

    def test_unused_article_slots_go_to_research(self):
        total, inserted, _ = self.run_collection(articles=1)
        self.assertEqual(total, 30)
        self.assertEqual(sum(sid == 1 for sid, _ in inserted), 29)

    def test_duplicates_do_not_consume_new_record_budget(self):
        seen = [f'research/{i}' for i in range(15)] + [f'article/{i}' for i in range(5)]
        total, inserted, note = self.run_collection(seen=seen)
        self.assertEqual(total, 30)
        self.assertTrue(set(url for _, url in inserted).isdisjoint(seen))
        self.assertIn('дублей 15', note)

    def test_fewer_candidates_are_not_filled_with_invalid_content(self):
        self.assertEqual(self.run_collection(science=2, articles=1)[0], 3)

    def test_one_failure_still_collects_from_other_source(self):
        total, inserted, note = self.run_collection(science=0, science_error='offline')
        self.assertEqual(total, 30)
        self.assertTrue(all(sid == 2 for sid, _ in inserted))
        self.assertIn('offline', note)

    def test_both_unreadable_sources_fail(self):
        with self.assertRaises(RuntimeError):
            self.run_collection(science=0, articles=0, science_error='offline', rss_error='offline')

    def test_empty_readable_sources_are_successful(self):
        self.assertEqual(self.run_collection(science=0, articles=0)[0], 0)


if __name__ == '__main__':
    unittest.main()
