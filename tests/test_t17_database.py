"""T17 PostgreSQL acceptance. Separate loopback test DB only; all external I/O mocked."""

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import patch

import db
from psycopg.types.json import Jsonb
from worker import main as worker
from worker import editorial as rules
from collector.sources import cursor_write
from publication import public_payload
import test_editorial_database as existing
from editorial_fixtures import first_draft, review


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Separate local TEST_DATABASE_URL required for T17 PostgreSQL')
class T17DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): existing.EditorialDatabaseTests.setUpClass.__func__(cls)
    def setUp(self):
        existing.EditorialDatabaseTests.setUp(self)
        patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('External HTTP forbidden')).start()
    connect=existing.EditorialDatabaseTests.connect
    fetch=existing.EditorialDatabaseTests.fetch
    model_answer=existing.EditorialDatabaseTests.model_answer
    candidate=existing.EditorialDatabaseTests.candidate

    def test_named_old_scope_constraint_upgrade_repeated_and_data_preserved(self):
        mid=self.candidate()
        self.conn.execute('ALTER TABLE materials DROP CONSTRAINT materials_content_scope_v7_check')
        self.conn.execute("ALTER TABLE materials ADD CONSTRAINT arbitrary_old_check CHECK(content_scope IN ('abstract','feed_excerpt','metadata_only','legacy_unknown'))")
        self.conn.execute("UPDATE sources SET active=false WHERE name='Pets: Learning'")
        for _ in range(2): db.init_schema(self.conn)
        self.assertEqual(self.conn.execute('SELECT id FROM materials WHERE id=%s',(mid,)).fetchone()[0],mid)
        self.conn.execute("UPDATE materials SET content_scope='page_excerpt' WHERE id=%s",(mid,))
        self.assertFalse(self.conn.execute("SELECT active FROM sources WHERE name='Pets: Learning'").fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sources WHERE name='PLOS: Dogs'").fetchone()[0],1)

    def test_atomic_material_and_cursor_rollback(self):
        sid=self.conn.execute('SELECT id FROM sources ORDER BY id LIMIT 1').fetchone()[0]
        with self.assertRaises(RuntimeError):
            with self.conn.transaction():
                self.conn.execute("INSERT INTO materials(source_id,url,title,status) VALUES (%s,'https://rkf.org.ru/test','test','collected')",(sid,))
                cursor_write(self.conn,dict(source='test',position=dict(offset=1)))
                raise RuntimeError('synthetic crash')
        self.assertEqual(self.conn.execute("SELECT count(*) FROM materials WHERE url='https://rkf.org.ru/test'").fetchone()[0],0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM runs WHERE service='collector_cursor'").fetchone()[0],0)

    def test_two_connections_cannot_reserve_eighty_first_model_call(self):
        self.conn.execute("INSERT INTO runs(service,status) SELECT 'worker_model_call','finished' FROM generate_series(1,79)")
        def reserve(_):
            with self.connect() as conn:
                return worker.Budget(conn).reserve('worker_model_call',80,1,'draft',1)
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(reserve,range(2)))
        self.assertEqual(sum(x is not None for x in results),1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM runs WHERE service='worker_model_call'").fetchone()[0],80)

    def test_real_processing_selects_varied_topics_and_keeps_eight_ceiling(self):
        topics={}
        for index in range(12):
            number=999999970+index
            mid=self.candidate(number)
            topics[str(number)]=('learning','behavior','welfare','nutrition')[index%4]
            self.conn.execute("UPDATE materials SET raw_payload=raw_payload || %s WHERE id=%s",
                              (Jsonb(dict(topic_hint=topics[str(number)],source_language='ru')),mid))
        def answer(phase,evidence,submitted=None):
            topic=topics[evidence['source_id'].split(':')[-1]]
            if phase=='review': result=review();result['topic']=topic;return result
            result=first_draft();result['article']['topic']=topic;result['editorial_fit']['focus']=topic
            for claim in result['article']['claims']:claim['source_id']=evidence['source_id']
            return result if '_fit' not in evidence else result['article']
        self.model.side_effect=answer
        count=worker.process_new(self.conn)
        self.assertGreaterEqual(count,5);self.assertLessEqual(count,8)
        rows=self.conn.execute("SELECT publication_payload FROM findings WHERE publication_status='published'").fetchall()
        self.assertGreaterEqual(len({p['topic'] for p, in rows}),3)
        self.assertTrue(all(public_payload(p)['version']==2 for p, in rows))
        # The second cycle cannot bypass the daily publication ceiling.
        self.assertEqual(worker.process_new(self.conn),0)

    def test_v6_unknown_paid_stage_is_not_replayed(self):
        mid=self.candidate()
        checkpoint=dict(stages=[dict(stage=1,outcome='generating')],input_hash='old',prompt_version='dogs-v6')
        note=dict(version=2,material_id=mid,rules='dogs-pilot-v6',phase='editorial',checkpoint=checkpoint)
        self.conn.execute("INSERT INTO runs(service,status,note) VALUES ('worker_attempt','started',%s)",(json.dumps(note),))
        call=dict(version=2,material_id=mid,rules='dogs-pilot-v6',phase='draft',format_stage=1)
        self.conn.execute("INSERT INTO runs(service,status,note) VALUES ('worker_model_call','started',%s)",(json.dumps(call),))
        worker.process_new(self.conn)
        self.model.assert_not_called()
        state,audit=self.conn.execute('SELECT status,editorial_result FROM materials WHERE id=%s',(mid,)).fetchone()
        self.assertEqual(state,'held');self.assertEqual(audit['reason'],'uncertain_stage')
