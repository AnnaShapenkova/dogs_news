"""T12 contracts with synthetic data; all external I/O is mocked."""

import json
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from worker import main as worker, editorial as rules
from worker.budget import budget_status, budget_text
from worker.requeue import validate_ids
from editorial_fixtures import source_record, material, draft, review


def envelope(content, finish='stop'):
    return json.dumps({'choices': [{'finish_reason': finish, 'message': {'content': content}}],
                       'usage': {'prompt_tokens': 120, 'completion_tokens': 30, 'total_tokens': 150,
                                 'private': 'SECRET'}}).encode()


class ResponseTests(unittest.TestCase):
    def test_clean_and_single_fenced_json_keep_contract_and_private_usage(self):
        for text in (json.dumps(review()), '```json\n' + json.dumps(review()) + '\n```'):
            with self.subTest(text=text):
                value = worker.parse_model_response(envelope(text))
                self.assertEqual(value, review())
                rules.validate_review(value, draft())
                self.assertEqual(value.diagnostics['usage']['total_tokens'], 150)
                self.assertNotIn('SECRET', json.dumps(value.diagnostics))
                self.assertNotIn('usage', value)

    def test_prose_multiple_fences_and_non_json_are_not_salvaged(self):
        for text in ('Answer: ' + json.dumps(review()), '```json\n{}\n```\n```json\n{}\n```',
                     '{"approved":true', '{"approved":NaN}'):
            with self.subTest(text=text), self.assertRaisesRegex(rules.Failure, 'model_invalid_json'):
                worker.parse_model_response(envelope(text))

    def test_empty_truncated_and_incomplete_responses_are_distinct(self):
        for content, finish, reason in (('', 'stop', 'model_empty_content'), (None, 'stop', 'model_empty_content'),
                                        ('{}', 'length', 'model_truncated'), ('{}', 'content_filter', 'model_incomplete')):
            with self.subTest(reason=reason), self.assertRaisesRegex(rules.Failure, reason):
                worker.parse_model_response(envelope(content, finish))

    def test_missing_envelope_fields_or_wrong_contract_never_approve(self):
        for data in (b'{}', b'null', b'[]', b'{"choices":[]}',
                     b'{"choices":[{"message":{"content":"{}"}}]}'):
            with self.subTest(data=data), self.assertRaisesRegex(rules.Failure, 'model_invalid_envelope'):
                worker.parse_model_response(data)
        for content in ('[]', 'true', 'null'):
            with self.assertRaisesRegex(rules.Failure, 'model_invalid_contract'):
                worker.parse_model_response(envelope(content))
        value = worker.parse_model_response(envelope('{"approved":true}'))
        with self.assertRaisesRegex(rules.Hold, 'invalid_model_contract'):
            rules.validate_review(value, draft())

    def test_response_size_is_bounded(self):
        with self.assertRaisesRegex(rules.Failure, 'model_response_too_large'):
            worker.parse_model_response(b' ' * (128 * 1024 + 1))

    def test_network_errors_do_not_expose_secrets(self):
        errors = [(urllib.error.HTTPError('https://example.invalid', 401, 'SECRET', {}, None), 'model_http_error'),
                  (TimeoutError('SECRET'), 'model_timeout'),
                  (urllib.error.URLError('SECRET'), 'model_connection_error')]
        for error, reason in errors:
            with self.subTest(reason=reason), patch.dict('os.environ', {'OPENROUTER_API_KEY': 'SECRET'}), \
                    patch('urllib.request.urlopen', side_effect=error):
                with self.assertRaisesRegex(rules.Failure, reason) as result:
                    worker.ask_model('draft', {})
                self.assertNotIn('SECRET', str(result.exception) + json.dumps(result.exception.diagnostics))
                if reason == 'model_http_error':
                    self.assertEqual(result.exception.diagnostics, {'http_status': 401})


class PolicyTests(unittest.TestCase):
    def evidence(self):
        record = source_record()
        record['abstractText'] += ' Aggression and pain were observed without testing treatments.'
        current = material()
        current['raw_payload']['record'] = record
        with patch.object(rules, 'fetch', return_value=json.dumps(
                {'hitCount': 1, 'resultList': {'result': [record]}}).encode()):
            return rules.verify_source(current)

    def test_sensitive_description_can_reach_contextual_review(self):
        evidence = self.evidence()
        value = draft()
        value['headline'] = 'Агрессия и обучение собак: границы результата исследования'
        # Structural validator permits the topic; semantic support still requires the reviewer.
        rules.validate_draft(value, evidence)
        rules.validate_review(review(), value)
        self.assertIn('aggression', evidence['risk_signals'])

    def test_dangerous_or_indirect_advice_cannot_pass_reviewer(self):
        for phrase in ('Дайте собаке препарат.', 'Применяйте наказание.', 'Используйте электроошейник.',
                       'Назначьте лечебную диету.', 'Попробуйте упражнение с укусами.'):
            value = draft()
            value['paragraphs'][0]['text'] += ' ' + phrase
            with self.subTest(phrase=phrase), self.assertRaises(rules.Hold):
                rules.validate_draft(value, self.evidence())
        value = review()
        value.update(approved=False, no_practical_advice=False, reasons=['Indirect advice'])
        with self.assertRaisesRegex(rules.Hold, 'review_not_approved'):
            rules.validate_review(value, draft())

    def test_unknown_risk_and_false_evidence_flags_are_not_approval(self):
        for key, invalid in (('risk', 'unknown'), ('source_sufficient', False), ('all_claims_supported', False)):
            value = review()
            value[key] = invalid
            with self.subTest(key=key), self.assertRaises(rules.Hold):
                rules.validate_review(value, draft())


class BudgetReportingTests(unittest.TestCase):
    def test_all_limits_and_utc_reset_are_explained(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (3, 15, 30, datetime(2026, 11, 17, tzinfo=timezone.utc))
        state = budget_status(conn)
        self.assertEqual(len(state['blocked']), 3)
        text = budget_text(state)
        for expected in ('3/3', '15/15', '30/30', '17.11 00:00 UTC', 'исчерпан'):
            self.assertIn(expected, text)

    def test_exhausted_budget_creates_no_empty_worker_run(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (True,)
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'synthetic'}), \
                patch.object(worker, 'budget_status', return_value={'blocked': ['attempt_daily_limit']}), \
                patch.object(worker.db, 'start_run') as start, patch.object(worker.db, 'connect') as connect:
            self.assertEqual(worker.process_new(conn), 0)
            self.assertEqual(worker.process_new(conn), 0)
            start.assert_not_called()
            connect.assert_not_called()

    def test_requeue_ids_are_explicit_unique_and_bounded(self):
        for ids in ([], [1, 1], [0], [True], ['1'], list(range(1, 17))):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                validate_ids(ids)
        validate_ids([1, 2, 3])


if __name__ == '__main__':
    unittest.main()
