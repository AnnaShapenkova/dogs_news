"""T18 isolated transport/recovery tests. No live keys, model calls or database."""

import io
import json
import unittest
import urllib.error
from copy import deepcopy
from unittest.mock import MagicMock, patch

from worker import main as worker, config, editorial as rules, provider, stages
from worker.recovery import migrate
from editorial_fixtures import draft, review, editorial_fit, material, source_record
from test_t15 import FakeBudget


def response(value):
    obj = MagicMock()
    obj.__enter__.return_value.read.return_value = json.dumps(dict(choices=[dict(
        finish_reason='stop', message=dict(content=json.dumps(value)))])).encode()
    return obj


class AITunnelTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'AITUNNEL_API_KEY': 'synthetic',
                              'OPENROUTER_API_KEY': 'unused-secret', 'MODEL': 'unused-model'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_transport_routes_both_models_and_caps_tokens(self):
        with patch('urllib.request.urlopen', side_effect=[response(draft()), response(review())]) as http:
            for phase, expected, cap in (('draft', config.DRAFT_MODEL, 6000), ('review', config.REVIEW_MODEL, 2400)):
                result = worker.ask_model(phase, {}, draft() if phase == 'review' else None)
                req = http.call_args.args[0]
                body = json.loads(req.data)
                self.assertEqual(req.full_url, config.ENDPOINT)
                self.assertEqual(req.get_header('Authorization'), 'Bearer synthetic')
                self.assertEqual((body['model'], body['max_tokens']), (expected, cap))
                self.assertEqual(body['response_format'], {'type': 'json_object'})
                self.assertEqual(result.diagnostics['provider'], 'aitunnel')
                self.assertEqual(result.diagnostics['model'], expected)
                self.assertNotIn('unused', json.dumps(body))

    def test_only_old_key_cannot_start_worker_or_direct_call(self):
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'old'}, clear=True), patch('urllib.request.urlopen') as http:
            conn = MagicMock()
            self.assertEqual(worker.process_new(conn), 0)
            conn.execute.assert_not_called()
            with self.assertRaisesRegex(rules.Failure, 'model_not_configured'):
                worker.ask_model('draft', {})
            http.assert_not_called()

    def test_invalid_models_prevent_any_database_or_paid_reservation(self):
        for name in ('DRAFT_MODEL', 'REVIEW_MODEL'):
            with patch.object(config, name, ''), patch('urllib.request.urlopen') as http:
                conn = MagicMock()
                with self.assertRaises(ValueError): worker.process_new(conn)
                conn.execute.assert_not_called()
                conn.autocommit = True
                budget = worker.Budget(conn)
                with patch.object(budget, 'reserve') as reserve:
                    with self.assertRaises(ValueError): budget.model(1, 'draft', {})
                    reserve.assert_not_called()
                http.assert_not_called()

    def test_payment_type_allowlist_and_pause_preserve_old_state(self):
        value = {'error': {'type': 'insufficient_balance', 'message': 'SECRET', 'metadata': {'key': 'SECRET'}}}
        error = urllib.error.HTTPError(config.ENDPOINT, 402, 'SECRET', {}, io.BytesIO(json.dumps(value).encode()))
        with patch('urllib.request.urlopen', side_effect=error) as http:
            with self.assertRaises(rules.Failure) as caught: worker.ask_model('review', {})
        self.assertEqual(caught.exception.diagnostics,
                         dict(http_status=402, provider='aitunnel', error_type='insufficient_balance'))
        self.assertEqual(http.call_count, 1)
        self.assertNotIn('SECRET', json.dumps(caught.exception.diagnostics))
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (7, 'paused', json.dumps(dict(version=1, event='paused',
            diagnostics={'http_status': 402, 'limit_source': 'openrouter_key_limit'})))
        state = provider.provider_status(conn)
        self.assertTrue(state['paused'])
        self.assertIn('историческое событие', provider.provider_text(state))
        self.assertIn('автоматически паузу не снимают', provider.provider_text(state))

    def test_other_http_errors_and_timeouts_have_no_fallback(self):
        for code in (400, 401, 403, 429, 500):
            err = urllib.error.HTTPError(config.ENDPOINT, code, 'SECRET', {}, io.BytesIO(b'SECRET'))
            with patch('urllib.request.urlopen', side_effect=err) as http:
                with self.assertRaises(rules.Failure) as caught: worker.ask_model('draft', {})
            self.assertEqual(caught.exception.diagnostics, {'http_status': code})
            self.assertEqual(http.call_count, 1)
        with patch('urllib.request.urlopen', side_effect=TimeoutError) as http:
            with self.assertRaisesRegex(rules.Failure, 'model_timeout'): worker.ask_model('draft', {})
            self.assertEqual(http.call_count, 1)

    def test_reservation_records_phase_provider_and_actual_model(self):
        conn = MagicMock(autocommit=True)
        conn.execute.return_value.fetchone.return_value = (8,)
        for phase in ('draft', 'review'):
            worker.Budget(conn).reserve('worker_model_call', 80, 1, phase, 1)
            note = json.loads(conn.execute.call_args.args[1][1])
            self.assertEqual((note['provider'], note['model'], note['phase']),
                             ('aitunnel', config.model_for(phase), phase))
            self.assertEqual(conn.execute.call_args.args[1][-1], 80)

    def test_findings_records_actual_generator_on_legacy_draft(self):
        conn = MagicMock()
        worker.insert_publication(conn, 1, draft(), {}, 'legacy-haiku')
        self.assertEqual(conn.execute.call_args.args[1][2], 'legacy-haiku')


class RecoveryTests(unittest.TestCase):
    def old(self, outcome='approved'):
        return dict(model='anthropic/claude-haiku-5.5', input_hash='same',
                    editorial_fit=editorial_fit(), stages=[dict(stage=1, outcome=outcome,
                    draft=draft(), review=review())])

    def calls(self, phase='draft'):
        return [('finished', dict(rules='dogs-pilot-v7', format_stage=1, phase=phase))]

    def run_controller(self, audit, budget):
        with patch.object(rules, 'fetch', return_value=json.dumps(dict(hitCount=1,
                resultList=dict(result=[source_record()]))).encode()):
            return stages.run_stages(material(), audit, budget, 1)

    def test_known_v7_draft_is_reviewed_once_without_generation(self):
        old = self.old()
        untouched = deepcopy(old)
        migrated = migrate(old, self.calls() + self.calls('review'), worker.PROMPT_VERSION)
        self.assertEqual(old, untouched)
        self.assertEqual(migrated['migration_history'], old)
        self.assertNotIn('review', migrated['stages'][0])
        budget = FakeBudget(lambda phase, *args: review() if phase == 'review' else self.fail('Repeated draft'))
        self.run_controller(migrated, budget)
        self.assertEqual(budget.used, 1)
        self.assertEqual(migrated['stages'][0]['draft_origin']['provider'], 'openrouter')
        self.assertEqual(migrated['stages'][0]['review_origin']['model'], config.REVIEW_MODEL)
        self.run_controller(migrated, budget)
        self.assertEqual(budget.used, 1)

    def test_first_saved_wrapper_can_be_migrated(self):
        old = self.old('response_ready')
        entry = old['stages'][0]
        entry['response'] = dict(editorial_fit=old.pop('editorial_fit'), article=entry.pop('draft'))
        entry.pop('review')
        self.assertEqual(migrate(old, self.calls(), worker.PROMPT_VERSION)['stages'][0]['draft'], draft())

    def test_unknown_paid_results_never_restart(self):
        for phase in ('draft', 'review'):
            old = self.old('generating' if phase == 'draft' else 'reviewing')
            with self.assertRaisesRegex(rules.Hold, 'uncertain_stage'):
                migrate(old, self.calls(phase), worker.PROMPT_VERSION)
        old = self.old()
        old['stages'][0].pop('review')
        with self.assertRaisesRegex(rules.Hold, 'uncertain_stage'):
            migrate(old, self.calls('review'), worker.PROMPT_VERSION)

    def test_missing_fit_or_unsupported_old_draft_is_not_regenerated(self):
        old = self.old()
        old.pop('editorial_fit')
        with self.assertRaisesRegex(rules.Hold, 'legacy_checkpoint_incompatible'):
            migrate(old, self.calls(), worker.PROMPT_VERSION)
        old = self.old()
        old['stages'][0]['draft']['claims'][0]['quote'] = 'Unsupported fabricated quote absent from source'
        audit = migrate(old, self.calls(), worker.PROMPT_VERSION)
        budget = FakeBudget()
        # Migration must not permit regenerating an incompatible saved draft.
        with self.assertRaises(rules.Hold): self.run_controller(audit, budget)
        self.assertEqual(budget.used, 0)

    def test_malformed_stage_history_is_held(self):
        for invalid in ([], True, {}, 9):
            old = self.old()
            old['stages'][0]['stage'] = invalid
            with self.assertRaisesRegex(rules.Hold, 'uncertain_stage'):
                migrate(old, self.calls(), worker.PROMPT_VERSION)

    def test_current_checkpoint_is_returned_without_second_migration(self):
        conn = MagicMock(autocommit=True)
        budget = worker.Budget(conn)
        audit = migrate(self.old(), self.calls(), worker.PROMPT_VERSION)
        budget.entries = MagicMock(return_value=[('finished', {'checkpoint': audit})])
        self.assertIs(budget.previous(1), audit)
        self.assertEqual(budget.entries.call_count, 1)


if __name__ == '__main__': unittest.main()
