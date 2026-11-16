"""Actual PostgreSQL T03 acceptance tests; never use the application's DATABASE_URL."""

import http.client
import json
import os
import re
import threading
import unittest
from copy import deepcopy
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

import db
from bot import main as bot
from web import main as web
from worker import main as worker
from worker import editorial as rules
from worker import requeue
from worker import provider
from editorial_fixtures import source_record, material, draft as make_draft, review


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Set local TEST_DATABASE_URL for PostgreSQL editorial tests')
class EditorialDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ['TEST_DATABASE_URL']
        config = conninfo_to_dict(cls.dsn)
        if (config.get('host') not in ('localhost', '127.0.0.1', '::1')
                or config.get('dbname') != 'project_dog_t01_test'
                or any(config.get(k) for k in ('hostaddr', 'options', 'service'))):
            raise RuntimeError('Use the separate local project_dog_t01_test database')

    def setUp(self):
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.conn.close)
        self.schema = 't03_' + uuid4().hex
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(lambda: self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema))))
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        db.init_schema(self.conn)
        self.records = {}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'OPENROUTER_API_KEY': 'synthetic-test-key'}).start()
        patch.object(db, 'log').start()
        patch('urllib.request.urlopen', side_effect=AssertionError('External HTTP forbidden')).start()
        patch.object(db, 'connect', side_effect=self.connect).start()
        patch.object(rules, 'fetch', side_effect=self.fetch).start()
        self.model = patch.object(worker, 'ask_model', side_effect=self.model_answer).start()
        self.telegram = patch.object(bot, 'api', return_value={'username': 'test_bot'}).start()

    def connect(self):
        conn = psycopg.connect(self.dsn, autocommit=True)
        conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        return conn

    def fetch(self, url):
        self.assertTrue(url.startswith(rules.API + '?'))
        query = parse_qs(urlsplit(url).query)['query'][0]
        pmid = re.search(r'EXT_ID:([0-9]+)', query)
        doi = re.search(r'DOI:"([^"]+)"', query)
        found = [record for key, record in self.records.items()
                 if (not pmid or str(key) == pmid.group(1)) and (not doi or record['doi'] == doi.group(1))]
        return json.dumps({'hitCount': len(found), 'resultList': {'result': found}}).encode()

    def model_answer(self, stage, evidence, submitted=None):
        return make_draft(int(evidence['source_id'].split(':')[1])) if stage == 'draft' else review()

    def candidate(self, number=999999991, secondary=False):
        value = material(number)
        self.records[number] = source_record(number)
        if secondary:
            value.update(url=f'https://example.invalid/news/{number}', content_scope='feed_excerpt',
                         raw_payload={'provider': 'rss', 'record': f'<item><description>DOI:{source_record(number)["doi"]}</description></item>'})
        source_id = self.conn.execute('SELECT id FROM sources ORDER BY id LIMIT 1').fetchone()[0]
        return self.conn.execute(
            "INSERT INTO materials (source_id,url,title,text,raw_payload,content_scope,status) VALUES (%s,%s,%s,%s,%s,%s,'collected') RETURNING id",
            (source_id, value['url'], value['title'], value['text'], Jsonb(value['raw_payload']), value['content_scope']),
        ).fetchone()[0]

    def messages(self):
        return [call.args[1]['text'] for call in self.telegram.call_args_list if call.args[0] == 'sendMessage']

    def count_runs(self, service):
        return self.conn.execute('SELECT count(*) FROM runs WHERE service=%s', (service,)).fetchone()[0]

    def http_page(self, handler):
        with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                client.request('GET', '/')
                response = client.getresponse()
                return response.status, response.read().decode()
            finally:
                client.close()
                server.shutdown()
                thread.join(timeout=5)

    def test_full_publication_and_http_telegram_without_audit_leaks(self):
        mid = self.candidate()
        self.conn.execute("UPDATE materials SET raw_payload=raw_payload || %s WHERE id=%s", (Jsonb({'private': 'SECRET_RAW'}), mid))
        self.assertEqual(worker.process_new(self.conn), 1)
        status, audit = self.conn.execute('SELECT status,editorial_result FROM materials WHERE id=%s', (mid,)).fetchone()
        self.assertEqual(status, 'done')
        self.assertEqual(audit['reason'], 'published')
        self.assertIn('draft', audit)
        self.assertIn('review', audit)
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(self.count_runs('worker_model_call'), 2)
        self.assertEqual(self.count_runs('worker_attempt'), 1)
        page = bot.page_data(self.conn)
        self.assertEqual(len(page['findings']), 1)
        self.assertEqual(page['findings'][0]['article']['topic_label'], 'Обучение')
        self.assertNotIn('SECRET_RAW', json.dumps(page))
        for handler in (bot.Handler, web.Handler):
            http_status, body = self.http_page(handler)
            self.assertEqual(http_status, 200)
            self.assertIn(make_draft()['headline'], body)
            self.assertIn('Научный источник', body)
            self.assertNotIn('SECRET_RAW', body)
            self.assertNotIn('private_quote', body)
        bot.handle(self.conn, {'chat': {'id': 42, 'type': 'private'}, 'text': '/start'})
        self.assertIn(make_draft()['headline'], self.messages()[0])
        self.telegram.reset_mock()
        # Neither an old AI new nor held/collected backlog blocks the published message.
        sid = self.conn.execute('SELECT id FROM sources LIMIT 1').fetchone()[0]
        for state in ('new', 'held', 'collected'):
            self.conn.execute('INSERT INTO materials (source_id,url,title,status) VALUES (%s,%s,%s,%s)',
                              (sid, f'https://example.invalid/{state}', 'SECRET_HIDDEN', state))
        bot.send_new(self.conn)
        self.assertEqual(len(self.messages()), 1)
        self.assertIn(make_draft()['headline'], self.messages()[0])
        self.assertNotIn('SECRET_HIDDEN', self.messages()[0])
        bot.send_new(self.conn)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(worker.process_new(self.conn), 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM findings').fetchone()[0], 1)

    def test_reviewer_refusal_is_held_without_finding(self):
        mid = self.candidate()
        bad_review = review()
        bad_review.update(approved=False, reasons=['Unsupported inference'])
        self.model.side_effect = [make_draft(), bad_review]
        self.assertEqual(worker.process_new(self.conn), 0)
        self.assertEqual(self.conn.execute('SELECT status,editorial_result->>\'reason\' FROM materials WHERE id=%s', (mid,)).fetchone(),
                         ('held', 'review_not_approved'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM findings').fetchone()[0], 0)
        self.assertIn('проверка текста не пройдена', bot.status_text(self.conn))

    def test_publication_daily_cap_and_resume_do_not_repeat_model_calls(self):
        for number in range(999999991, 999999995):
            self.candidate(number)
        self.assertEqual(worker.process_new(self.conn), 3)
        self.assertEqual(self.model.call_count, 6)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials WHERE status='collected'").fetchone()[0], 1)
        self.assertEqual(worker.process_new(self.conn), 0)
        self.assertEqual(self.model.call_count, 6)

    def test_attempt_limit_is_durable_for_unlinked_secondary_inputs(self):
        for number in range(16):
            sid = self.conn.execute('SELECT id FROM sources LIMIT 1').fetchone()[0]
            self.conn.execute("INSERT INTO materials (source_id,url,title,text,raw_payload,content_scope,status) VALUES (%s,%s,'Dog learning','summary',%s,'feed_excerpt','collected')",
                              (sid, f'https://example.invalid/{number}', Jsonb({'provider': 'rss', 'record': '<item/>'})))
        self.assertEqual(worker.process_new(self.conn), 0)
        self.assertEqual(self.count_runs('worker_attempt'), 15)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials WHERE status='held'").fetchone()[0], 15)
        self.assertEqual(worker.process_new(self.conn), 0)
        self.assertEqual(self.count_runs('worker_attempt'), 15)
        runs_before = self.count_runs('worker')
        self.assertEqual(worker.process_new(self.conn), 0)
        self.assertEqual(self.count_runs('worker'), runs_before)
        self.assertIn('исчерпан суточный лимит попыток', bot.status_text(self.conn))
        self.model.assert_not_called()

    def test_model_call_limit_defers_without_calling_provider(self):
        mid = self.candidate()
        self.conn.execute("INSERT INTO runs(service,status) SELECT 'worker_model_call','failed' FROM generate_series(1,30)")
        self.assertEqual(worker.process_new(self.conn), 0)
        self.model.assert_not_called()
        self.assertEqual(self.count_runs('worker_model_call'), 30)
        self.assertEqual(self.conn.execute('SELECT status FROM materials WHERE id=%s', (mid,)).fetchone()[0], 'collected')

    def test_utc_budget_ignores_previous_day_and_counts_failed_reservations(self):
        self.conn.execute("SET TIME ZONE 'Pacific/Honolulu'")
        self.conn.execute("INSERT INTO runs(service,status,started_at) SELECT 'worker_model_call','failed', (date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')-interval '1 second' FROM generate_series(1,30)")
        with self.connect() as audit_conn:
            budget = worker.Budget(audit_conn)
            for i in range(30):
                self.assertIsNotNone(budget.reserve('worker_model_call', 30, 1, 'test'))
            self.assertIsNone(budget.reserve('worker_model_call', 30, 1, 'test'))
        self.assertEqual(self.count_runs('worker_model_call'), 60)

    def test_duplicate_research_and_popular_input_publish_once(self):
        self.candidate()
        self.candidate(secondary=True)
        self.assertEqual(worker.process_new(self.conn), 1)
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM findings').fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials WHERE editorial_result->>'reason'='duplicate_evidence'").fetchone()[0], 1)

    def test_rollback_removes_publication_but_keeps_paid_call_reservations(self):
        mid = self.candidate()
        real_insert = worker.insert_publication
        def crash(*args):
            real_insert(*args)
            raise RuntimeError('simulated process failure before commit')
        with patch.object(worker, 'insert_publication', side_effect=crash), self.assertRaises(RuntimeError):
            worker.process_new(self.conn)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM findings').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT status,editorial_result FROM materials WHERE id=%s', (mid,)).fetchone(), ('collected', None))
        self.assertEqual(self.count_runs('worker_model_call'), 2)
        self.assertEqual(self.count_runs('worker_attempt'), 1)
        self.assertEqual(worker.process_new(self.conn), 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM findings').fetchone()[0], 1)

    def test_changed_input_cannot_publish_the_old_draft(self):
        mid = self.candidate()
        def answer(stage, evidence, submitted=None):
            if stage == 'review':
                self.conn.execute("UPDATE materials SET text=text || ' changed' WHERE id=%s", (mid,))
            return self.model_answer(stage, evidence, submitted)
        self.model.side_effect = answer
        self.assertEqual(worker.process_new(self.conn), 0)
        self.assertEqual(self.conn.execute("SELECT editorial_result->>'reason' FROM materials WHERE id=%s", (mid,)).fetchone()[0], 'input_changed')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM findings').fetchone()[0], 0)

    def test_two_workers_do_not_publish_the_same_candidate(self):
        self.candidate()
        entered = threading.Event()
        release = threading.Event()
        results, errors = [], []
        def answer(stage, evidence, submitted=None):
            if stage == 'draft':
                entered.set()
                if not release.wait(10): raise RuntimeError('test timeout')
            return self.model_answer(stage, evidence, submitted)
        self.model.side_effect = answer
        def first():
            try:
                with self.connect() as conn:
                    results.append(worker.process_new(conn))
            except Exception as error: errors.append(error)
        thread = threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            with self.connect() as second:
                self.assertEqual(worker.process_new(second), 0)
        finally:
            release.set()
            thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [1])
        self.assertEqual(self.model.call_count, 2)

    def test_upgrade_t02_and_repeat_preserve_rows(self):
        # A second isolated schema is initialized from the exact pre-T03 shape.
        older = 't03_upgrade_' + uuid4().hex
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(older)))
        self.addCleanup(lambda: self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(older))))
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(older)))
        baseline = Path(__file__).with_name('fixtures').joinpath('schema_t02.sql').read_text(encoding='utf-8')
        self.conn.execute(baseline)
        mid = self.candidate()
        self.conn.execute("INSERT INTO findings(material_id,summary,model,prompt_version,publication_status) VALUES (%s,'legacy','test','v1','published')", (mid,))
        self.conn.execute("INSERT INTO subscribers(chat_id,name) VALUES (7,'preserved')")
        db.init_schema(self.conn)
        db.init_schema(self.conn)
        self.assertEqual(self.conn.execute('SELECT publication_payload,published_at FROM findings').fetchone(), ({}, None))
        self.assertEqual(self.conn.execute('SELECT raw_payload,content_scope FROM materials').fetchone()[0]['provider'], 'europepmc')
        self.assertEqual(self.conn.execute('SELECT name FROM subscribers WHERE chat_id=7').fetchone()[0], 'preserved')

    def old_hold(self, mid, reason='risk_requires_review'):
        old = dict(rules_version='dogs-pilot-v1', reason=reason, outcome='held', private='SECRET_OLD')
        self.conn.execute("UPDATE materials SET status='held',editorial_result=%s WHERE id=%s", (Jsonb(old), mid))
        return old

    def test_sensitive_source_requeue_to_publication_and_telegram(self):
        mid = self.candidate()
        record = self.records[999999991]
        record['abstractText'] += ' Aggression and pain were observed in the learning task.'
        self.conn.execute('UPDATE materials SET raw_payload=%s WHERE id=%s',
                          (Jsonb({'provider': 'europepmc', 'record': record}), mid))
        old = self.old_hold(mid)
        self.assertTrue(requeue.requeue(self.conn, [mid])[0]['eligible'])
        self.assertEqual(self.count_runs('worker_requeue'), 0)
        self.assertEqual(self.conn.execute('SELECT editorial_result FROM materials WHERE id=%s', (mid,)).fetchone()[0], old)
        self.assertTrue(requeue.requeue(self.conn, [mid], apply=True)[0]['requeued'])
        self.assertEqual(worker.process_new(self.conn), 1)
        audit = self.conn.execute('SELECT editorial_result FROM materials WHERE id=%s', (mid,)).fetchone()[0]
        self.assertIn('aggression', audit['risk_signals'])
        self.assertEqual(json.loads(self.conn.execute("SELECT note FROM runs WHERE service='worker_requeue'").fetchone()[0])['previous_result'], old)
        bot.handle(self.conn, {'chat': {'id': 42, 'type': 'private'}, 'text': '/start'})
        self.telegram.reset_mock()
        bot.send_new(self.conn)
        self.assertEqual(len(self.messages()), 1)
        self.assertNotIn('SECRET_OLD', self.messages()[0])
        self.assertNotIn('risk_signals', json.dumps(bot.page_data(self.conn)))
        self.assertEqual(self.count_runs('worker_attempt'), 1)
        self.assertEqual(self.count_runs('worker_model_call'), 2)

    def test_requeue_is_idempotent_and_never_erases_daily_budget(self):
        mid = self.candidate()
        self.old_hold(mid)
        self.conn.execute("INSERT INTO runs(service,status) SELECT 'worker_attempt','finished' FROM generate_series(1,15)")
        self.assertTrue(requeue.requeue(self.conn, [mid], apply=True)[0]['requeued'])
        self.assertFalse(requeue.requeue(self.conn, [mid], apply=True)[0]['eligible'])
        self.old_hold(mid)  # even an old-looking audit cannot bypass durable history
        self.assertEqual(requeue.requeue(self.conn, [mid], apply=True)[0]['reason'], 'already_requeued_for_version')
        self.assertEqual(self.count_runs('worker_requeue'), 1)
        self.assertEqual(worker.process_new(self.conn), 0)
        self.model.assert_not_called()
        self.assertEqual(self.count_runs('worker_attempt'), 15)

    def test_requeue_batch_with_ineligible_row_makes_no_changes(self):
        first, second = self.candidate(), self.candidate(999999992)
        old = self.old_hold(first)
        self.old_hold(second, 'publication_requires_review')
        result = requeue.requeue(self.conn, [first, second], apply=True)
        self.assertFalse(all(row['eligible'] for row in result))
        self.assertEqual(self.count_runs('worker_requeue'), 0)
        self.assertEqual(self.conn.execute('SELECT status,editorial_result FROM materials WHERE id=%s', (first,)).fetchone(), ('held', old))

    def test_requeue_rejects_retracted_snapshot_and_duplicate_evidence(self):
        first = self.candidate()
        self.assertEqual(worker.process_new(self.conn), 1)
        second = self.candidate(999999992)
        record = source_record()
        self.conn.execute('UPDATE materials SET raw_payload=%s WHERE id=%s',
                          (Jsonb({'provider': 'europepmc', 'record': record}), second))
        self.old_hold(second)
        self.assertEqual(requeue.requeue(self.conn, [second])[0]['reason'], 'duplicate_evidence')
        record['pubTypeList'] = {'pubType': ['Retracted Publication']}
        self.conn.execute('UPDATE materials SET raw_payload=%s WHERE id=%s',
                          (Jsonb({'provider': 'europepmc', 'record': record}), second))
        self.assertEqual(requeue.requeue(self.conn, [second], apply=True)[0]['reason'], 'publication_requires_review')
        self.assertEqual(self.count_runs('worker_requeue'), 0)

    def test_model_failure_diagnostics_persist_without_provider_secret(self):
        mid = self.candidate()
        self.model.side_effect = rules.Failure('model_truncated', {'finish_reason': 'length', 'usage': {'completion_tokens': 2400}})
        self.assertEqual(worker.process_new(self.conn), 0)
        status, audit = self.conn.execute('SELECT status,editorial_result FROM materials WHERE id=%s', (mid,)).fetchone()
        self.assertEqual(status, 'error')
        self.assertEqual(audit['reason'], 'model_truncated')
        self.assertEqual(audit['model_responses'][0]['finish_reason'], 'length')
        self.assertIn('оборван', bot.status_text(self.conn))
        self.assertEqual(bot.page_data(self.conn)['findings'], [])
        self.assertEqual(self.count_runs('worker_model_call'), 1)

    def test_evergreen_publication_is_labelled_in_mini_app_start_and_delivery(self):
        mid = self.candidate()
        record = self.records[999999991]
        record['firstPublicationDate'] = '2023-05-01'
        self.conn.execute('UPDATE materials SET raw_payload=%s WHERE id=%s',
                          (Jsonb({'provider': 'europepmc', 'record': record, 'discovery_window': 'evergreen'}), mid))
        self.assertEqual(worker.process_new(self.conn), 1)
        article = bot.page_data(self.conn)['findings'][0]['article']
        self.assertEqual(article['format'], 'explainer')
        self.assertIn('2023-05-01', article['context_note'])
        bot.handle(self.conn, {'chat': {'id': 42, 'type': 'private'}, 'text': '/start'})
        self.assertIn('Познавательный разбор', self.messages()[0])
        self.telegram.reset_mock()
        bot.send_new(self.conn)
        self.assertIn('исследование от 2023-05-01', self.messages()[0])
        self.assertNotIn('Свежие:', self.messages()[0])

    def test_payment_failure_pauses_after_first_and_resume_preserves_counts(self):
        first, second = self.candidate(), self.candidate(999999992)
        self.model.side_effect = rules.Failure('model_http_error', {'http_status': 402, 'limit_source': 'openrouter_credits'})
        self.assertEqual(worker.process_new(self.conn), 0)
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(self.conn.execute('SELECT status FROM materials WHERE id=%s', (first,)).fetchone()[0], 'error')
        self.assertEqual(self.conn.execute('SELECT status,editorial_result FROM materials WHERE id=%s', (second,)).fetchone(), ('collected', None))
        self.assertEqual(self.count_runs('worker_attempt'), 1)
        self.assertEqual(self.count_runs('worker_model_call'), 1)
        count = self.count_runs(provider.SERVICE)
        self.assertTrue(provider.resume_provider(self.conn)['paused'])  # preview only
        self.assertEqual(self.count_runs(provider.SERVICE), count)
        self.assertIn('ограничение оплаты/кредитов', bot.status_text(self.conn))
        with self.connect() as another:
            self.assertTrue(provider.provider_status(another)['paused'])
            self.assertEqual(worker.process_new(another), 0)
        self.assertEqual(self.model.call_count, 1)
        self.assertTrue(provider.resume_provider(self.conn, apply=True)['changed'])
        count = self.count_runs(provider.SERVICE)
        self.assertFalse(provider.resume_provider(self.conn, apply=True)['changed'])
        self.assertEqual(self.count_runs(provider.SERVICE), count)
        self.assertEqual(self.count_runs('worker_attempt'), 1)
        self.model.side_effect = self.model_answer
        self.assertEqual(worker.process_new(self.conn), 1)
        self.assertEqual(self.count_runs('worker_attempt'), 2)
        self.assertEqual(self.count_runs('worker_model_call'), 3)
        self.assertEqual(self.conn.execute('SELECT status FROM materials WHERE id=%s', (first,)).fetchone()[0], 'error')
        self.assertNotIn('limit_source', json.dumps(bot.page_data(self.conn)))

    def test_review_payment_failure_also_stops_next_candidate(self):
        first, second = self.candidate(), self.candidate(999999992)
        self.model.side_effect = [make_draft(), rules.Failure('model_http_error', {'http_status': 402})]
        self.assertEqual(worker.process_new(self.conn), 0)
        self.assertEqual(self.model.call_count, 2)
        self.assertTrue(provider.provider_status(self.conn)['paused'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM findings').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT status FROM materials WHERE id=%s', (second,)).fetchone()[0], 'collected')
        audit = self.conn.execute('SELECT editorial_result FROM materials WHERE id=%s', (first,)).fetchone()[0]
        self.assertEqual(audit['model_responses'][-1]['stage'], 'review')

    def test_durable_payment_pause_survives_parent_rollback_and_daily_reset(self):
        mid = self.candidate()
        self.model.side_effect = rules.Failure('model_http_error', {'http_status': 402})
        persist = worker.pause_provider
        def crash(conn, diagnostic):
            persist(conn, diagnostic)
            raise RuntimeError('synthetic crash after durable pause')
        with patch.object(worker, 'pause_provider', side_effect=crash), self.assertRaises(RuntimeError):
            worker.process_new(self.conn)
        self.assertEqual(self.conn.execute('SELECT status,editorial_result FROM materials WHERE id=%s', (mid,)).fetchone(), ('collected', None))
        self.assertTrue(provider.provider_status(self.conn)['paused'])
        self.assertEqual(self.count_runs('worker_model_call'), 1)
        self.conn.execute("UPDATE runs SET started_at=started_at-interval '1 day' WHERE service IN ('worker_attempt','worker_model_call')")
        with self.connect() as another:
            self.assertEqual(worker.process_new(another), 0)
        self.assertEqual(self.model.call_count, 1)

    def test_resuming_payment_pause_does_not_reset_exhausted_daily_attempts(self):
        self.candidate()
        self.conn.execute("INSERT INTO runs(service,status) SELECT 'worker_attempt','finished' FROM generate_series(1,15)")
        provider.pause_provider(self.conn, {'http_status': 402})
        provider.resume_provider(self.conn, apply=True)
        self.assertFalse(provider.provider_status(self.conn)['paused'])
        self.assertEqual(worker.process_new(self.conn), 0)
        self.model.assert_not_called()
        self.assertEqual(self.count_runs('worker_attempt'), 15)

    def test_new_rules_allow_selected_old_provider_errors_to_requeue(self):
        mid = self.candidate()
        old = {'rules_version': 'dogs-pilot-v3', 'reason': 'model_truncated', 'outcome': 'error'}
        self.conn.execute("UPDATE materials SET status='error',editorial_result=%s WHERE id=%s", (Jsonb(old), mid))
        self.assertTrue(requeue.requeue(self.conn, [mid], apply=True)[0]['requeued'])
        self.assertEqual(worker.process_new(self.conn), 1)
        previous = json.loads(self.conn.execute("SELECT note FROM runs WHERE service='worker_requeue'").fetchone()[0])
        self.assertEqual(previous['previous_result'], old)


if __name__ == '__main__':
    unittest.main()
