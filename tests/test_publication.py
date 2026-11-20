"""T01 acceptance tests. Only TEST_DATABASE_URL is used, never DATABASE_URL."""

import http.client
import os
import threading
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

import db
from bot import main as bot
from web import main as web
from worker import main as worker


class MessageTests(unittest.TestCase):
    def test_summary_escapes_content_and_link(self):
        line = bot.finding_lines([('<test>', 'A&B', 'https://example.invalid/"', '<source>')])[0]
        self.assertNotIn('<test>', line)
        self.assertIn('&lt;test&gt;', line)
        self.assertIn('&quot;', line)
        self.assertIn('A&amp;B', line)

    def test_say_builds_telegram_payload_without_network(self):
        with patch.dict(os.environ, {'MINI_APP_URL': 'https://example.invalid'}, clear=True):
            with patch.object(bot, 'api') as api:
                bot.say(42, 'Published summary')
        method, payload = api.call_args.args
        self.assertEqual(method, 'sendMessage')
        self.assertEqual(payload['chat_id'], 42)
        self.assertEqual(payload['text'], 'Published summary')
        self.assertEqual(payload['reply_markup']['inline_keyboard'][0][0]['web_app']['url'],
                         'https://example.invalid/')


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Set a local TEST_DATABASE_URL for PostgreSQL tests')
class PublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ['TEST_DATABASE_URL']
        config = conninfo_to_dict(cls.dsn)
        if (config.get('host') not in ('127.0.0.1', 'localhost', '::1')
                or config.get('dbname') != 'project_dog_t01_test'
                or config.get('hostaddr')
                or config.get('service')
                or config.get('options')):
            raise RuntimeError('Tests require local database project_dog_t01_test without connection overrides')

    def setUp(self):
        # A new schema per test: never truncate or modify a pre-existing application's tables.
        self.schema = 't01_' + uuid4().hex
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.conn.close)
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        db.init_schema(self.conn)
        self.source = self.conn.execute(
            "INSERT INTO sources (name, kind, url) VALUES ('fixture', 'rss', 'https://source.invalid/feed') RETURNING id"
        ).fetchone()[0]
        self.run = db.start_run(self.conn, 'collector')
        db.finish_run(self.conn, 'collector', self.run, 0, 'fixture')
        self.addCleanup(patch.stopall)
        patch('urllib.request.urlopen', side_effect=AssertionError('External HTTP is forbidden in tests')).start()
        patch.object(db, 'log').start()
        self.telegram = patch.object(bot, 'api', return_value={'username': 'test_bot'}).start()

    def drop_schema(self):
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def connect(self):
        conn = psycopg.connect(self.dsn, autocommit=True)
        conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        return conn

    def material(self, token, status='done'):
        return self.conn.execute(
            'INSERT INTO materials (source_id, run_id, url, title, text, status, published_at)'
            ' VALUES (%s, %s, %s, %s, %s, %s, now()) RETURNING id',
            (self.source, self.run, f'https://example.invalid/{token}', f'TITLE_{token}',
             f'TEXT_{token}', status),
        ).fetchone()[0]

    def finding(self, token, publication_status='draft', sent=False):
        material_id = self.material(token)
        finding_id = self.conn.execute(
            'INSERT INTO findings (material_id, entity, summary, model, prompt_version, publication_status, sent_at)'
            ' VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END) RETURNING id',
            (material_id, 'OpenAI', token, 'test-model', 'test-v1', publication_status, sent),
        ).fetchone()[0]
        return finding_id

    def mixed(self):
        return {
            'draft': self.finding('SECRET_DRAFT'),
            'draft_sent': self.finding('SECRET_DRAFT_SENT', sent=True),
            'published': self.finding('VISIBLE_PUBLISHED', 'published'),
            'published_sent': self.finding('VISIBLE_ALREADY_SENT', 'published', sent=True),
        }

    def sent_times(self):
        return dict(self.conn.execute('SELECT id, sent_at FROM findings'))

    def messages(self):
        return [call.args[1]['text'] for call in self.telegram.call_args_list if call.args[0] == 'sendMessage']

    def start(self):
        bot.handle(self.conn, {'chat': {'id': 42, 'type': 'private'}, 'text': '/start'})

    def assert_hidden(self, content):
        self.assertNotIn('SECRET_DRAFT', content)

    def test_new_database_does_not_import_props(self):
        self.assertEqual(self.conn.execute('SELECT count(*) FROM findings').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM materials').fetchone()[0], 0)

    def test_reinitialization_preserves_rows_links_and_statuses(self):
        self.mixed()
        before = {table: self.conn.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
                  for table in ('sources', 'materials', 'findings', 'runs')}
        db.init_schema(self.conn)
        after = {table: self.conn.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
                 for table in before}
        self.assertEqual(before, after)

    def test_default_and_constraints(self):
        material_id = self.material('DEFAULT')
        status = self.conn.execute(
            "INSERT INTO findings (material_id, summary, model, prompt_version) VALUES (%s, 'test', 'test', 'test')"
            ' RETURNING publication_status', (material_id,),
        ).fetchone()[0]
        self.assertEqual(status, 'draft')
        for invalid, error in [(None, psycopg.errors.NotNullViolation),
                               ('unknown', psycopg.errors.CheckViolation)]:
            with self.subTest(status=invalid):
                with self.assertRaises(error):
                    self.conn.execute('UPDATE findings SET publication_status = %s WHERE material_id = %s',
                                      (invalid, material_id))

    def test_pages_and_counters_exclude_drafts(self):
        self.mixed()
        data = bot.page_data(self.conn)
        self.assertEqual({f['summary'] for f in data['findings']},
                         {'VISIBLE_PUBLISHED', 'VISIBLE_ALREADY_SENT'})
        self.assertEqual(data['totals']['findings'], 2)
        self.assertEqual(data['run']['kept'], 2)
        self.assertEqual(data['run']['sent'], 1)
        self.assertEqual(data['totals']['materials'], 4)
        for page in (bot.render_page(self.conn), web.render(self.conn)):
            self.assert_hidden(page)
            self.assertIn('VISIBLE_PUBLISHED', page)
            self.assertIn('https://example.invalid/VISIBLE_PUBLISHED', page)
        self.assertIn('Находок: <b>2</b>', web.render(self.conn))
        self.assertIn('Всего находок 2', bot.status_text(self.conn))

    def test_start_and_status_exclude_drafts(self):
        self.mixed()
        self.start()
        self.assertIn('VISIBLE_PUBLISHED', self.messages()[0])
        self.assert_hidden(self.messages()[0])
        bot.handle(self.conn, {'chat': {'id': 42, 'type': 'private'}, 'text': '/status'})
        self.assertIn('Всего находок 2', self.messages()[-1])
        self.assert_hidden(self.messages()[-1])

    def test_send_only_published_and_preserve_draft_sent_at(self):
        ids = self.mixed()
        before = self.sent_times()
        self.start()
        self.telegram.reset_mock()
        bot.send_new(self.conn)
        self.assertEqual(len(self.messages()), 1)
        text = self.messages()[0]
        self.assertIn('Новых находок: 1', text)
        self.assertIn('VISIBLE_PUBLISHED', text)
        self.assertNotIn('VISIBLE_ALREADY_SENT', text)
        self.assert_hidden(text)
        after = self.sent_times()
        self.assertIsNotNone(after[ids['published']])
        for key in ('draft', 'draft_sent', 'published_sent'):
            self.assertEqual(before[ids[key]], after[ids[key]])
        self.telegram.reset_mock()
        bot.send_new(self.conn)
        self.assertEqual(self.messages(), [])

    def test_setup_without_subscribers_marks_only_published(self):
        ids = self.mixed()
        before = self.sent_times()
        bot.setup(self.conn)
        after = self.sent_times()
        self.assertIsNotNone(after[ids['published']])
        for key in ('draft', 'draft_sent', 'published_sent'):
            self.assertEqual(before[ids[key]], after[ids[key]])

    def test_setup_with_subscribers_does_not_mark_findings(self):
        self.mixed()
        self.start()
        before = self.sent_times()
        bot.setup(self.conn)
        self.assertEqual(before, self.sent_times())

    def test_drafts_only_no_send_and_empty_pages(self):
        self.finding('SECRET_DRAFT')
        self.finding('SECRET_DRAFT_SENT', sent=True)
        before = self.sent_times()
        self.start()
        self.assert_hidden(self.messages()[0])
        self.telegram.reset_mock()
        bot.send_new(self.conn)
        self.assertEqual(self.messages(), [])
        self.assertEqual(before, self.sent_times())
        self.assertEqual(bot.page_data(self.conn)['findings'], [])
        self.assertEqual(bot.page_data(self.conn)['totals']['findings'], 0)
        self.assertIn('Находок пока нет', web.render(self.conn))
        self.assertIn('после допуска к публикации', bot.render_page(self.conn))
        self.assert_hidden(bot.render_page(self.conn))
        self.assertIn('Всего находок 0', bot.status_text(self.conn))

    def test_latest_limit_ignores_newer_drafts(self):
        for i in range(bot.LATEST + 2):
            self.finding(f'PUBLIC_{i}', 'published')
        self.finding('SECRET_DRAFT')
        self.start()
        start_text = self.messages()[0]
        self.assertEqual(start_text.count('https://example.invalid/PUBLIC_'), 5)
        self.assert_hidden(start_text)
        self.telegram.reset_mock()
        bot.send_new(self.conn)
        text = self.messages()[0]
        self.assertIn(f'Новых находок: {bot.LATEST + 2}', text)
        self.assertEqual(text.count('https://example.invalid/PUBLIC_'), bot.LATEST)
        self.assertIn('Еще 2 в ленте', text)
        self.assert_hidden(text)

    def test_legacy_ai_queue_stays_hidden_and_is_not_processed(self):
        material_id = self.material('SECRET_DRAFT', status='new')
        with patch.object(worker, 'ask_model', return_value={
            'about_ai': True, 'entity': 'OpenAI', 'summary': 'SECRET_DRAFT',
            'publication_status': 'published',  # untrusted model output cannot grant publication
        }) as model, patch.dict(os.environ, {'AITUNNEL_API_KEY': 'test-key'}):
            worker.process_new(self.conn)
        model.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM findings WHERE material_id=%s', (material_id,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            'SELECT status FROM materials WHERE id = %s', (material_id,),
        ).fetchone()[0], 'new')
        self.assertEqual(bot.page_data(self.conn)['findings'], [])
        self.assert_hidden(web.render(self.conn))
        self.start()
        self.assert_hidden(self.messages()[0])
        self.telegram.reset_mock()
        bot.send_new(self.conn)
        self.assertEqual(self.messages(), [])

    @contextmanager
    def http_server(self, handler):
        # No application main(), polling threads, external APIs or environment DATABASE_URL.
        with patch.object(db, 'connect', side_effect=self.connect):
            with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    yield server.server_address[1]
                finally:
                    server.shutdown()
                    thread.join(timeout=5)

    def get(self, port, path='/'):
        client = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            client.request('GET', path)
            response = client.getresponse()
            return response.status, response.read().decode('utf-8')
        finally:
            client.close()

    def test_http_pages_exclude_drafts(self):
        self.mixed()
        for handler in (bot.Handler, web.Handler):
            with self.subTest(handler=handler.__module__), self.http_server(handler) as port:
                status, body = self.get(port)
                self.assertEqual(status, 200)
                self.assert_hidden(body)
                self.assertIn('VISIBLE_PUBLISHED', body)
                self.assertEqual(self.get(port, '/health'), (200, 'ok'))

    def test_http_pages_without_published_return_200(self):
        self.finding('SECRET_DRAFT')
        for handler in (bot.Handler, web.Handler):
            with self.subTest(handler=handler.__module__), self.http_server(handler) as port:
                status, body = self.get(port)
                self.assertEqual(status, 200)
                self.assert_hidden(body)
                self.assertIn('пока нет', body)


if __name__ == '__main__':
    unittest.main()
