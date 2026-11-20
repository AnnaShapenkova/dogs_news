"""Owner editorial fit: synthetic examples, not live judgments by the model."""

import json
import unittest
from copy import deepcopy
from unittest.mock import MagicMock, patch

from collector import main as collector
from worker import editorial as rules, main as worker, stages
from publication import public_payload
from editorial_fixtures import material, source_record, stage_draft, review, editorial_fit, first_draft
from test_t15 import FakeBudget


CLINICAL = (
    'Dogs with cardiac disease underwent standardized cardiac auscultation and echocardiography. '
    'The dogs were examined by cardiology-trained veterinarians at multiple clinical centers. '
    'The study described the location and intensity of cardiac murmurs across several cardiac diagnoses. '
    'Murmur intensity was compared with cardiac disease stages during the same clinical visit. '
    'The investigators recorded their examinations using a standardized clinical protocol. '
    'The results describe associations between examination findings and the clinical classifications. '
    'The observations do not establish causal effects. The clinical sample represents dogs examined '
    'at participating centers rather than a random sample from the general population. '
    'The paper discusses the performance of clinical examination findings within the included diagnoses.'
)


class OwnerFitTests(unittest.TestCase):
    def setUp(self):
        self.input = material()
        self.record = source_record()
        self.network = patch.object(rules, 'fetch', side_effect=lambda url: json.dumps(
            {'hitCount': 1, 'resultList': {'result': [self.record]}}).encode()).start()
        self.addCleanup(patch.stopall)

    def run_controller(self, budget, audit=None):
        return stages.run_stages(self.input, audit if audit is not None else {}, budget, 1)

    def test_listen_style_clinical_training_is_not_dog_learning(self):
        self.assertGreaterEqual(len(CLINICAL.split()), 100)
        self.assertFalse(collector.has_animal_topic('Dog cardiac murmurs', CLINICAL))
        self.record.update(title='Dog cardiac murmurs and disease stages', abstractText=CLINICAL)
        self.input['raw_payload']['record'] = deepcopy(self.record)
        budget = FakeBudget()
        with self.assertRaisesRegex(rules.Hold, 'clinical_focus'):
            self.run_controller(budget)
        self.assertEqual(budget.used, 0)

    def test_staff_and_machine_training_are_not_subject_matter(self):
        for text in ('cardiology-trained veterinarians', 'training of veterinary students',
                     'обучение ветеринаров', 'deep learning', 'training models'):
            with self.subTest(text=text):
                self.assertFalse(collector.has_animal_topic('Dog examination', text))
        self.assertTrue(collector.has_animal_topic('Dog learning and behaviour',
                                                 'Observed by cardiology-trained veterinarians using machine learning.'))
        self.assertTrue(collector.has_animal_topic('Dog welfare during rest', 'Published in a veterinary journal.'))

    def test_skip_clinical_or_specialist_never_reaches_review_or_shorter_stage(self):
        for focus, audience, expected in (('clinical_methods', 'specialist', 'clinical_focus'),
                                           ('welfare', 'specialist', 'audience_specialist'),
                                           ('welfare', 'unclear', 'unclear_editorial_fit')):
            fit = editorial_fit()
            fit.update(focus=focus, audience=audience, decision='skip', reason='specialist_clinical', owner_question='')
            budget = FakeBudget(lambda *args: dict(editorial_fit=fit, article=None), auto_wrap=False)
            audit = {}
            with self.subTest(focus=focus), self.assertRaisesRegex(rules.Hold, expected):
                self.run_controller(budget, audit)
            self.assertEqual(budget.used, 1)
            self.assertEqual(len(audit['stages']), 1)

    def test_invalid_or_unsupported_fit_is_fail_closed(self):
        for change, reason in (({'owner_question': ''}, 'unclear_editorial_fit'),
                               ({'source_quote': 'Invented supporting text absent from the abstract.'}, 'unverified_editorial_fit'),
                               ({'audience': True}, 'invalid_editorial_contract'),
                               ({'focus': 'clinical_methods'}, 'clinical_focus')):
            fit = editorial_fit()
            fit.update(change)
            budget = FakeBudget(lambda *args: dict(editorial_fit=fit, article=stage_draft(1)), auto_wrap=False)
            with self.subTest(change=change), self.assertRaisesRegex((rules.Hold, rules.Failure), reason):
                self.run_controller(budget)
            self.assertEqual(budget.used, 1)
        budget = FakeBudget(lambda *args: stage_draft(1), auto_wrap=False)
        with self.assertRaisesRegex(rules.Failure, 'invalid_editorial_contract'):
            self.run_controller(budget)

    def test_skip_cannot_smuggle_an_article(self):
        fit = editorial_fit()
        fit.update(decision='skip', audience='specialist')
        budget = FakeBudget(lambda *args: dict(editorial_fit=fit, article=stage_draft(1)), auto_wrap=False)
        with self.assertRaisesRegex(rules.Failure, 'invalid_editorial_contract'):
            self.run_controller(budget)
        self.assertEqual(budget.used, 1)

    def test_reviewer_audience_refusal_cannot_fall_through_formats(self):
        def answer(phase, evidence, submitted):
            if phase == 'draft': return stage_draft(evidence['_stage'])
            value = review()
            value.update(approved=False, owner_relevant=False, reasons=['A clinical subject for specialists'])
            return value
        budget = FakeBudget(answer)
        with self.assertRaisesRegex(rules.Hold, 'audience_mismatch'):
            self.run_controller(budget)
        self.assertEqual(budget.used, 2)

    def test_readability_can_improve_without_changing_the_editorial_question(self):
        def answer(phase, evidence, submitted):
            if phase == 'draft': return stage_draft(evidence['_stage'])
            value = review()
            if evidence['_stage'] == 1:
                value.update(approved=False, plain_language=False, reasons=['Unnecessary specialist jargon'])
            return value
        budget = FakeBudget(answer)
        audit = {}
        article, assessment, stage = self.run_controller(budget, audit)
        self.assertEqual(stage, 2)
        self.assertEqual(budget.used, 4)
        self.assertEqual(audit['editorial_fit']['owner_question'], editorial_fit()['owner_question'])
        self.assertEqual(budget.calls[2][1]['_fit'], editorial_fit())
        self.assertTrue(assessment['plain_language'])
        payload = rules.publication_payload(article, audit['verified_source'], stage)
        payload.update(editorial_fit=audit['editorial_fit'], stages=audit['stages'])
        projected = public_payload(payload)
        self.assertNotIn('editorial_fit', projected)
        self.assertNotIn('owner_question', json.dumps(projected))

    def test_reviewer_new_fields_are_strict_booleans(self):
        for key in ('owner_relevant', 'plain_language'):
            value = review()
            value[key] = 'true'
            with self.assertRaises(rules.Hold): rules.validate_review(value, stage_draft(1))

    def test_accepted_focus_cannot_be_relabelled_welfare(self):
        def answer(phase, evidence, submitted):
            value = stage_draft(1)
            value['topic'] = 'welfare'
            return first_draft(value)
        budget = FakeBudget(answer, auto_wrap=False)
        with self.assertRaisesRegex(rules.Hold, 'editorial_focus_mismatch'):
            self.run_controller(budget)
        self.assertEqual(budget.used, 1)

    def test_prompt_requests_first_fit_and_reuses_fit_on_later_formats(self):
        for phase, stage, fit in (('draft', 1, None), ('draft', 2, editorial_fit()), ('review', 2, editorial_fit())):
            evidence = dict(_stage=stage, abstract='synthetic', _feedback=['readability_failed'])
            if fit: evidence['_fit'] = fit
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(
                {'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]}).encode()
            with patch.dict('os.environ', {'AITUNNEL_API_KEY': 'SECRET'}), patch('urllib.request.urlopen', return_value=response) as call:
                worker.ask_model(phase, evidence)
            content = json.loads(json.loads(call.call_args.args[0].data)['messages'][1]['content'])
            self.assertEqual(content['needs_editorial_fit'], phase == 'draft' and fit is None)
            self.assertNotIn('_fit', content['source'])
            self.assertNotIn('SECRET', json.dumps(content))


class MigrationPolicyTests(unittest.TestCase):
    def budget_with_previous(self, outcome, calls=True):
        conn = MagicMock()
        conn.autocommit = True
        budget = worker.Budget(conn)
        old = dict(input_hash='previous-hash', rules_version='dogs-pilot-v5',
                   model='anthropic/claude-haiku-5.5', editorial_fit=editorial_fit(),
                   stages=[dict(stage=1, outcome=outcome, draft=stage_draft(1))], next_stage=1)
        def entries(mid, service, any_rules=False):
            if not any_rules: return []
            if service == 'worker_attempt': return [('finished', dict(rules='dogs-pilot-v5', checkpoint=old))]
            return [('finished', dict(rules='dogs-pilot-v5', format_stage=1,
                                     phase='review' if outcome == 'reviewing' else 'draft'))] if calls else []
        budget.entries = entries
        return budget, old

    def test_known_old_draft_preserves_fit_and_old_audit_for_fresh_review(self):
        budget, old = self.budget_with_previous('draft_ready')
        value = budget.previous(1)
        self.assertEqual(value['migration_history'], old)
        self.assertEqual(value['stages'][0]['outcome'], 'draft_ready')
        self.assertEqual(value['next_stage'], 1)
        self.assertEqual(value['input_hash'], 'previous-hash')
        self.assertIn('editorial_fit', value)

    def test_unknown_old_paid_response_is_not_automatically_regenerated(self):
        for outcome in ('generating', 'reviewing'):
            budget, _ = self.budget_with_previous(outcome)
            with self.assertRaisesRegex(rules.Hold, 'uncertain_stage'): budget.previous(1)
        budget, _ = self.budget_with_previous('generating', calls=False)
        with self.assertRaisesRegex(rules.Hold, 'uncertain_stage'): budget.previous(1)
        budget, old = self.budget_with_previous('draft_ready')
        old['stages'][0].pop('draft')
        with self.assertRaisesRegex(rules.Hold, 'uncertain_stage'): budget.previous(1)


if __name__ == '__main__': unittest.main()
