"""T14 provider handling; synthetic data and mocked HTTP only."""

import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from worker import main as worker, editorial as rules, provider
from editorial_fixtures import draft, review


def payment_error(metadata=None, retry=None, body=None):
    body = body if body is not None else json.dumps({'error': {'message': 'SECRET', 'metadata': metadata or {}}}).encode()
    return urllib.error.HTTPError('https://openrouter.ai/SECRET', 402, 'SECRET',
                                  {'Retry-After': retry} if retry is not None else {}, io.BytesIO(body))


class PaymentDiagnosticsTests(unittest.TestCase):
    def test_payment_metadata_is_allowlisted_and_bounded(self):
        cases = [('openrouter_credits', 'weight_exceeds_budget'),
                 ('openrouter_key_limit', 'key_limit_exceeded'),
                 ('openrouter_in_flight_budget', 'in_flight_budget_exhausted')]
        for source, reason in cases:
            error = payment_error({'limit_source': source, 'reason': reason, 'remedy_hint': 'SECRET',
                                   'api_key': 'SECRET'}, '12')
            value = worker.http_error_diagnostics(error)
            self.assertEqual(value, dict(http_status=402, limit_source=source,
                                         payment_reason=reason, retry_after_seconds=12))
            self.assertNotIn('SECRET', json.dumps(value))
            self.assertTrue(error.closed)

    def test_unknown_metadata_invalid_body_and_retry_never_leak(self):
        for body in (b'not-json SECRET', b'null', b'[]', b'{"error":"SECRET"}', b'x' * 40000):
            with self.subTest(body=body[:20]):
                value = worker.http_error_diagnostics(payment_error(body=body, retry='SECRET'))
                self.assertEqual(value, {'http_status': 402})
        for retry in ('999999999999999', '-1', 'tomorrow', '86401', 'SECRET', '1.2'):
            self.assertEqual(worker.http_error_diagnostics(payment_error(
                {'limit_source': 'SECRET', 'reason': 'SECRET'}, retry)), {'http_status': 402})
        self.assertEqual(provider.safe_diagnostics({'http_status': True, 'retry_after_seconds': True,
                                                   'limit_source': {}, 'payment_reason': []}), {})

    def test_http_402_is_not_retried_inside_model_call(self):
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'SECRET'}), \
                patch('urllib.request.urlopen', side_effect=payment_error()) as network:
            with self.assertRaisesRegex(rules.Failure, 'model_http_error') as result:
                worker.ask_model('draft', {})
        self.assertEqual(network.call_count, 1)
        self.assertEqual(result.exception.diagnostics, {'http_status': 402})
        self.assertNotIn('SECRET', str(result.exception))


class StageOutputTests(unittest.TestCase):
    def test_limits_are_stage_specific_without_changing_contract_or_reasoning(self):
        for stage, limit, answer in (('draft', 6000, draft()), ('review', 2400, review())):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(
                {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}]}).encode()
            with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'SECRET'}), \
                    patch('urllib.request.urlopen', return_value=response) as network:
                self.assertEqual(worker.ask_model(stage, {}, draft() if stage == 'review' else None), answer)
            body = json.loads(network.call_args.args[0].data)
            self.assertEqual(body['max_tokens'], limit)
            self.assertEqual(body['model'], worker.MODEL)
            self.assertEqual(body['temperature'], 0.2)
            self.assertNotIn('reasoning', body)

    def test_valid_json_with_length_finish_is_never_accepted(self):
        data = json.dumps({'choices': [{'finish_reason': 'length', 'message': {'content': json.dumps(draft())}}],
                           'usage': {'completion_tokens': 6000}}).encode()
        with self.assertRaisesRegex(rules.Failure, 'model_truncated') as result:
            worker.parse_model_response(data)
        self.assertEqual(result.exception.diagnostics['usage']['completion_tokens'], 6000)


class PauseControlsTests(unittest.TestCase):
    def test_payment_failure_records_pause_on_durable_budget_connection(self):
        conn = MagicMock()
        conn.autocommit = True
        budget = worker.Budget(conn)
        failure = rules.Failure('model_http_error', {'http_status': 402})
        with patch.object(worker, 'provider_status', return_value={'paused': False}), \
                patch.object(budget, 'reserve', return_value=7), patch.object(budget, 'finish'), \
                patch.object(worker, 'ask_model', side_effect=failure), patch.object(worker, 'pause_provider') as pause:
            with self.assertRaises(rules.Failure):
                budget.model(1, 'draft', {})
        pause.assert_called_once_with(conn, {'http_status': 402})

    def test_existing_pause_prevents_budget_reservation_and_paid_call(self):
        conn = MagicMock()
        conn.autocommit = True
        budget = worker.Budget(conn)
        with patch.object(worker, 'provider_status', return_value={'paused': True}), \
                patch.object(budget, 'reserve') as reserve, patch.object(worker, 'ask_model') as model:
            with self.assertRaisesRegex(worker.BudgetLimit, 'provider_payment_paused'):
                budget.model(1, 'draft', {})
            reserve.assert_not_called()
            model.assert_not_called()

    def test_paused_worker_creates_no_empty_runs_or_audit_connections(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (True,)
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'synthetic'}), \
                patch.object(worker, 'budget_status', return_value={'blocked': []}), \
                patch.object(worker, 'provider_status', return_value={'paused': True}), \
                patch.object(worker.db, 'start_run') as start, patch.object(worker.db, 'connect') as connect:
            self.assertEqual(worker.process_new(conn), 0)
            self.assertEqual(worker.process_new(conn), 0)
            start.assert_not_called()
            connect.assert_not_called()

    def test_unreadable_provider_history_is_fail_closed(self):
        conn = MagicMock()
        for row in ((1, 'paused', 'SECRET'), (1, 'unknown', '{}'),
                    (1, 'resumed', '{"version":true,"event":"resumed"}')):
            conn.execute.return_value.fetchone.return_value = row
            state = provider.provider_status(conn)
            self.assertTrue(state['paused'])
            self.assertTrue(state['state_unreadable'])
            self.assertNotIn('SECRET', json.dumps(state))
            with self.assertRaisesRegex(RuntimeError, 'unreadable'):
                provider.resume_provider(conn, apply=True)

    def test_payment_status_does_not_promise_daily_reset_will_fix_payment(self):
        text = provider.provider_text({'paused': True, 'limit_source': 'openrouter_key_limit'})
        self.assertIn('лимит API-ключа', text)
        self.assertIn('python -m worker.provider --resume', text)
        self.assertIn('автоматически паузу не снимают', text)


if __name__ == '__main__':
    unittest.main()
