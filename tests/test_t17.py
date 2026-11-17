"""T17 isolated tests: synthetic text in observed publisher structures; no paid/live I/O."""

import json
import unittest
import urllib.error
from collections import Counter
from contextlib import nullcontext
from copy import deepcopy
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from collector import main as collector, sources
from worker import editorial as rules, main as worker, stages, planning
from worker.budget import MAX_ATTEMPTS, MAX_MODEL_CALLS, MAX_PUBLICATIONS, budget_status
from worker.evidence import unsafe_actions
from publication import public_payload
from bot import main as bot
from web.main import article_html
from editorial_fixtures import ABSTRACT, draft, review, material, first_draft, source_record
from test_t15 import FakeBudget

TODAY = date(2026, 11, 16)
RKF = 'https://rkf.org.ru/poleznye-materialy-o-sobakah/test-dog/'
PETS = 'https://vetandlife.ru/pets/test-dog/'
TEXT = ('Собака может выбирать знакомые предметы и общаться с человеком во время спокойной игры. '
        'В руководстве описано наблюдение за собакой и её сигналами в привычной домашней обстановке. '
        'Автор объясняет, как отмечать интерес собаки к игрушке, делать паузы и использовать поощрение. '
        'Это объяснение обычного занятия, которое не содержит назначения лечения или индивидуального питания.')


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        moment=datetime(2026,11,16,12,tzinfo=timezone.utc)
        return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)


def page(host='rkf.org.ru', text=TEXT, extra='', title='Обучение собаки во время игры'):
    container = 'post-content' if host == 'rkf.org.ru' else 'fulltext'
    return (f'<html><nav>МЕНЮ И РЕКЛАМА</nav><h1>{title}</h1>'
            '<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-09-03T10:00:00Z",'
            '"author":{"@type":"Person","name":"Тестовый автор"}}</script>'
            f'<div class="{container}"><p>{text}</p>{extra}</div><footer>ПОСТОРОННИЙ ФУТЕР</footer></html>').encode()


class MemoryStore:
    """Collector I/O simulation, explicitly not a SQL/migration test."""
    def __init__(self, registry=None):
        self.sources = [(i+1, n, k, u) for i, (n,(k,u,q)) in enumerate((registry or sources.REGISTRY).items())]
        self.items, self.notes = {}, []
    def transaction(self): return nullcontext()
    def execute(self, sql, params=()):
        result = MagicMock(); result.fetchone.return_value = None; result.fetchall.return_value = []
        if sql.startswith('SELECT id,name,kind,url'): result.fetchall.return_value = self.sources
        elif sql.startswith('SELECT 1 FROM materials'):
            result.fetchone.return_value = (1,) if params[0] in self.items else None
        elif "SELECT note FROM runs WHERE service='collector_cursor'" in sql:
            matches = [n for n in self.notes if n.startswith(params[0][:-1])]
            result.fetchone.return_value = (matches[-1],) if matches else None
        elif sql.startswith('INSERT INTO runs'):
            self.notes.append(params[0])
        elif 'INSERT INTO materials' in sql:
            url = params[2]
            if url not in self.items:
                self.items[url] = params; result.fetchone.return_value = (len(self.items),)
        return result


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Live network forbidden')).start()
        patch('collector.sources.time.sleep').start()
        patch('collector.sources.datetime',FixedDateTime).start()
        patch('worker.evidence.datetime',FixedDateTime).start()

    def test_page_scope_metadata_and_navigation_exclusion(self):
        for host, url in (('rkf.org.ru',RKF),('vetandlife.ru',PETS)):
            with self.subTest(host=host):
                item = sources.page_candidate(page(host), url, url, TODAY)
                self.assertEqual(item['content_scope'], 'page_excerpt')
                self.assertEqual(item['text'], TEXT)
                self.assertNotIn('МЕНЮ', item['text'])
                self.assertNotIn('ФУТЕР', item['text'])
                self.assertEqual(item['raw_payload']['author'], 'Тестовый автор')
                self.assertEqual(item['raw_payload']['source_language'], 'ru')
                self.assertEqual(item['published_at'].year, 2026)
        responsive = page().replace(b'</h1>', '</h1><h1>Дублированный мобильный заголовок</h1>'.encode(),1)
        self.assertEqual(sources.page_snapshot(responsive,RKF)['title'],'Обучение собаки во время игры')

    def test_missing_main_body_is_not_replaced_by_menu_or_description(self):
        for body in (b'<h1>Dog training</h1><nav>' + page() + b'</nav>', b'<meta name="description" content="Dog training">'):
            with self.assertRaises(ValueError): sources.page_snapshot(body, RKF)

    def test_unknown_or_private_hosts_and_redirect_targets_are_rejected(self):
        for u in ('http://rkf.org.ru/x','https://127.0.0.1/','https://rkf.org.ru.evil.test/x',
                  'https://user:password@rkf.org.ru/x','https://rkf.org.ru:444/x'):
            with self.subTest(url=u),self.assertRaises(ValueError): sources.canonical(u)
        self.assertEqual(sources.canonical(RKF+'?utm_source=test&mode=one#top'),RKF+'?mode=one')

    def test_robots_budget_and_429_stop_without_retry_loop(self):
        calls=[]
        def fetch(url):
            calls.append(url)
            if url.endswith('/robots.txt'): return b'User-agent: *\nDisallow: /feed/\n'
            return b'content'
        client=sources.SourceHTTP(fetch, sleep=lambda s:None)
        with self.assertRaisesRegex(ValueError,'robots_disallowed'): client.get('https://rkf.org.ru/feed/')
        self.assertEqual(len(calls),1)
        for _ in range(19): client.get(RKF)
        with self.assertRaisesRegex(ValueError,'source_request_limit'): client.get(RKF)
        def fail(url):
            if url.endswith('/robots.txt'): return b'User-agent: *\nDisallow:\n'
            raise urllib.error.HTTPError(url,429,'rate limit',{'Retry-After':'3600'},None)
        client=sources.SourceHTTP(fail,sleep=lambda s:None)
        with self.assertRaises(urllib.error.HTTPError): client.get(RKF)
        with self.assertRaisesRegex(ValueError,'429'): client.get(RKF)

    def test_known_html_url_is_not_downloaded_again(self):
        store=MemoryStore();store.items[RKF]=True
        calls=[]
        def fetch(u):
            calls.append(u)
            if u.endswith('robots.txt'):return b'User-agent: *\nDisallow:\n'
            return f'<a href="{RKF}">Guide</a>'.encode()
        stats=dict(scanned=0,duplicates=0,skips=Counter())
        result=list(sources.owner_candidates(store,'owner_html',sources.REGISTRY['RKF: Owner guides'][1],TODAY,
                                            sources.SourceHTTP(fetch,sleep=lambda s:None),stats))
        self.assertEqual(result,[]);self.assertNotIn(RKF,calls);self.assertEqual(stats['duplicates'],1)

    def test_source_change_and_unsafe_original_cannot_be_hidden_in_excerpt(self):
        item=sources.page_candidate(page(),RKF,RKF,TODAY)
        with patch.object(rules,'fetch',side_effect=lambda u: b'User-agent: *\nDisallow:\n' if u.endswith('robots.txt') else page()):
            evidence=rules.verify_source(item)
            self.assertEqual(evidence['kind'],'guide');self.assertEqual(evidence['evidence_scope'],'page_excerpt')
        changed=page(text=TEXT+' Другой текст.')
        with patch.object(rules,'fetch',side_effect=lambda u:b'User-agent: *\nDisallow:\n' if u.endswith('robots.txt') else changed):
            with self.assertRaisesRegex(rules.Hold,'source_changed'):rules.verify_source(item)
        bad=page(text=TEXT+' Швырнуть предмет в щенка, чтобы остановить действие.')
        item=sources.page_candidate(bad,RKF,RKF,TODAY)
        with patch.object(rules,'fetch',side_effect=lambda u:b'User-agent: *\nDisallow:\n' if u.endswith('robots.txt') else bad):
            with self.assertRaisesRegex(rules.Hold,'unsafe_advice'):rules.verify_source(item)

    def test_archive_cursor_is_not_advanced_past_deferred_sixth_record(self):
        registry={'Europe PMC: Dogs pilot':sources.REGISTRY['Europe PMC: Dogs pilot']}
        store=MemoryStore(registry)
        def fetch(u):
            if u.endswith('robots.txt'):return b'User-agent: *\nDisallow:\n'
            params=parse_qs(urlsplit(u).query)
            old='TO 2026-08-18]' in params['query'][0]
            items=[dict(source_record(i), firstPublicationDate='2024-01-01') for i in range(1,16)] if old else []
            return json.dumps({'resultList':{'result':items}}).encode()
        count,_=sources.collect_sources(store,TODAY,1,fetch)
        self.assertEqual(count,5)
        notes=[json.loads(x) for x in store.notes if json.loads(x).get('window')=='evergreen']
        self.assertEqual(notes[-1]['position']['offset'],5)
        count,_=sources.collect_sources(store,TODAY,2,fetch)
        self.assertEqual(count,5)
        self.assertEqual(len(store.items),10)

    def test_news_uses_primary_message_and_missing_primary_does_not_approve(self):
        primary=RKF.replace('test-dog','primary-dog')
        title='Запустили новую игру для собаки'
        secondary=page('vetandlife.ru',extra=f'<a href="{primary}">Первичное сообщение</a>',title=title)
        item=sources.page_candidate(secondary,PETS,PETS,TODAY)
        self.assertEqual(item['raw_payload']['material_type_hint'],'news')
        def fetch(url):
            if url.endswith('robots.txt'):return b'User-agent: *\nDisallow:\n'
            return secondary if url==PETS else page(title=title)
        with patch.object(rules,'fetch',side_effect=fetch):
            evidence=rules.verify_source(item)
        self.assertEqual(evidence['kind'],'news');self.assertEqual(evidence['original_url'],PETS)
        self.assertEqual(evidence['evidence_key'],'url:'+primary)
        secondary=page('vetandlife.ru',title=title)
        item=sources.page_candidate(secondary,PETS,PETS,TODAY)
        with patch.object(rules,'fetch',side_effect=fetch):
            with self.assertRaisesRegex(rules.Hold,'missing_primary_event'):rules.verify_source(item)

    def test_http_cooldown_survives_new_collector_client(self):
        registry={'RKF: Owner guides':sources.REGISTRY['RKF: Owner guides']}
        store=MemoryStore(registry);calls=[]
        def fetch(url):
            calls.append(url)
            if url.endswith('robots.txt'):return b'User-agent: *\nDisallow:\n'
            raise urllib.error.HTTPError(url,429,'rate limit',{'Retry-After':'3600'},None)
        with self.assertRaises(RuntimeError):sources.collect_sources(store,TODAY,1,fetch)
        saved=[json.loads(n) for n in store.notes if 'retry_until' in n]
        self.assertTrue(saved)
        # Simulate the SQL lookup of the persisted publisher cooldown.
        execute=store.execute
        def lookup(sql,params=()):
            result=execute(sql,params)
            if "service='collector_source_state'" in sql:result.fetchone.return_value=(json.dumps(saved[-1]),)
            return result
        store.execute=lookup;before=len(calls)
        with self.assertRaises(RuntimeError):sources.collect_sources(store,TODAY,2,fetch)
        self.assertEqual(len(calls),before)


class EditorialV7Tests(unittest.TestCase):
    def evidence(self):
        with patch.object(rules,'fetch',return_value=json.dumps({'hitCount':1,'resultList':{'result':[source_record()]}}).encode()):
            return rules.verify_source(material())

    def test_guide_imperative_without_doi_is_allowed_but_not_medication(self):
        evidence=self.evidence();evidence.update(kind='guide',evidence_scope='page_excerpt')
        article=draft();article.update(material_type='guide',advice_class='safe_general')
        article['paragraphs'][0]['kind']='guidance';article['paragraphs'][0]['text']+=' Используйте знакомую игрушку.'
        rules.validate_draft(article,evidence)
        article['paragraphs'][0]['text']+=' Дайте собаке лекарство.'
        with self.assertRaisesRegex(rules.Hold,'unsafe_advice'):rules.validate_draft(article,evidence)
        self.assertFalse(unsafe_actions('Не используйте электроошейник.'))

    def test_no_mandatory_limitations_but_unsupported_claim_still_refused(self):
        evidence=self.evidence();article=draft()
        self.assertEqual(article['caveat'],'');rules.validate_draft(article,evidence)
        article['claims'][0]['quote']='This sentence was invented and is not in the source.'
        with self.assertRaisesRegex(rules.Hold,'unsupported_claim'):rules.validate_draft(article,evidence)

    def test_reviews_and_retractions_get_different_source_decisions(self):
        for typ, accepted in (('Review',True),('Meta-Analysis',True),('Retracted Publication',False)):
            record=source_record();record['pubTypeList']={'pubType':[typ]}
            item=material();item['raw_payload']['record']=record
            with patch.object(rules,'fetch',return_value=json.dumps({'hitCount':1,'resultList':{'result':[record]}}).encode()):
                if accepted:self.assertEqual(rules.verify_source(item)['kind'],'research')
                else:
                    with self.assertRaisesRegex(rules.Hold,'retracted_source'):rules.verify_source(item)

    def test_plos_requires_matching_doi_license_and_known_article_status(self):
        record=dict(id='10.1371/journal.pone.0300889',title='Dog learning in a visual matching task',
                    abstract=[ABSTRACT],publication_date='2024-03-21T00:00:00Z',author=['Test'],journal='PLOS ONE')
        item=dict(material(),url=sources.plos_url(record['id']),title=record['title'],raw_payload=dict(provider='plos',record=record))
        def response(kind='research-article',licensed=True):
            return (f'<article article-type="{kind}"><front><article-meta><article-id pub-id-type="doi">{record["id"]}</article-id>'
                    + ('<permissions><license href="https://creativecommons.org/licenses/by/4.0/"/></permissions>' if licensed else '')
                    + '</article-meta></front></article>').encode()
        def fetch(url):
            if url.endswith('robots.txt'):return b'User-agent: *\nDisallow:\n'
            if 'api.plos.org/search?' in url:return json.dumps({'response':{'docs':[record]}}).encode()
            return response()
        with patch('collector.sources.time.sleep'),patch.object(rules,'fetch',side_effect=fetch):
            evidence=rules.verify_source(item)
            self.assertEqual(evidence['kind'],'research');self.assertEqual(evidence['doi'],record['id'])
        for kind,licensed,reason in (('retraction',True,'retracted_source'),('research-article',False,'source_license_unverified')):
            with patch('collector.sources.time.sleep'),patch.object(rules,'fetch',side_effect=lambda u:
                b'User-agent: *\nDisallow:\n' if u.endswith('robots.txt') else
                json.dumps({'response':{'docs':[record]}}).encode() if 'api.plos.org/search?' in u else response(kind,licensed)):
                with self.assertRaisesRegex(rules.Hold,reason):rules.verify_source(item)

    def test_russian_source_priority_does_not_overwrite_missing_topic(self):
        one=material(1);one['raw_payload'].update(topic_hint='learning',source_language='en')
        two=material(2);two['raw_payload'].update(topic_hint='learning',source_language='ru')
        three=material(3);three['raw_payload'].update(topic_hint='nutrition',source_language='en')
        self.assertEqual(planning.rank_candidates([one,two],Counter(),Counter())[0]['id'],2)
        self.assertEqual(planning.rank_candidates([one,two,three],Counter(learning=2),Counter())[0]['id'],3)

    def test_budget_constants_and_one_remaining_call(self):
        self.assertEqual((MAX_PUBLICATIONS,MAX_ATTEMPTS,MAX_MODEL_CALLS),(8,60,80))
        conn=MagicMock();conn.execute.return_value.fetchone.return_value=(2,41,79,datetime.now(timezone.utc))
        self.assertEqual(budget_status(conn)['blocked'],['stage_model_budget'])

    def test_bad_review_type_unknown_risk_and_unsafe_advice_are_hard_stops(self):
        for change in ({'advice_safe':'true'},{'risk':'unknown'},{'owner_relevant':False}):
            value=review();value.update(change)
            with self.assertRaises(rules.Hold):rules.validate_review(value,draft())
            self.assertFalse(stages.can_simplify('review_not_approved',draft(),value))

    def test_v2_readers_keep_v1_and_hide_internal_audit(self):
        evidence=self.evidence();payload=rules.publication_payload(draft(),evidence,1)
        payload.update(editorial_fit='SECRET',claims='SECRET')
        public=public_payload(payload)
        self.assertEqual(public['version'],2);self.assertNotIn('SECRET',json.dumps(public))
        rendered=article_html('Заголовок',payload,evidence['url'],datetime.now(timezone.utc),'Источник')
        self.assertIn('Читать оригинал',rendered);self.assertNotIn('Что важно учитывать',rendered)
        old=deepcopy(payload);old.update(version=1,limitations='Историческое пояснение')
        self.assertEqual(public_payload(old)['version'],1)
        self.assertIn('Историческое пояснение',article_html('Старый',old,evidence['url'],datetime.now(timezone.utc),'Источник'))

    def test_telegram_large_digest_splits_without_real_delivery(self):
        lines=['<b>Заголовок</b>\n'+('Понятная история о собаке. '*30) for _ in range(8)]
        with patch.object(bot,'api') as api,patch.object(bot,'page_url',return_value=''):
            bot.say(1,'\n\n'.join(lines))
        self.assertGreater(api.call_count,1)
        for call in api.call_args_list:
            self.assertLessEqual(len(call.args[1]['text'].encode('utf-16-le')),7600)

    def test_twelve_suitable_mixed_inputs_produce_five_balanced_cards(self):
        # Real controller and ranker, fake model/source/budget. This is NOT a live editorial evaluation.
        items=[]
        for i in range(12):
            item=material(i+100);topic=('learning','behavior','nutrition','welfare')[i%4]
            item['raw_payload'].update(topic_hint=topic,source_language='ru' if i%2 else 'en')
            item['url']=('https://rkf.org.ru/' if i%2 else 'https://vetandlife.ru/')+str(i)
            items.append(item)
        topics,publishers=Counter(),Counter();accepted=[]
        while items and len(accepted)<5:
            item=planning.rank_candidates(items,topics,publishers)[0];items.remove(item)
            topic=item['raw_payload']['topic_hint']
            evidence=self.evidence();evidence.update(source_id='MED:999999991')
            def answer(phase,ev,submitted,topic=topic):
                if phase=='review':value=review();value['topic']=topic;return value
                value=first_draft();value['editorial_fit']['focus']=topic;value['article']['topic']=topic
                return value if '_fit' not in ev else value['article']
            with patch.object(rules,'verify_source',return_value=evidence):
                article,assessment,_=stages.run_stages(item,{},FakeBudget(answer,auto_wrap=False),1)
            self.assertTrue(assessment['approved']);accepted.append(article)
            topics[topic]+=1;publishers[urlsplit(item['url']).hostname]+=1
        self.assertEqual(len(accepted),5);self.assertGreaterEqual(len(topics),3)
        self.assertLessEqual(max(topics.values()),2)


if __name__=='__main__':unittest.main()
