"""Finite editorial formats, with durable checkpoints and unchanged evidence gates."""

import json

from worker import editorial as rules
from worker.budget import BudgetLimit

SIMPLIFIABLE = {
    'invalid_article_length', 'missing_evidence_scope', 'not_russian', 'evergreen_as_news',
    'unverified_claim', 'unverified_number', 'unsupported_interpretation', 'invalid_stage_claim_count',
}


def public_text(draft):
    if not isinstance(draft, dict):
        return ''
    paragraphs = draft.get('paragraphs')
    texts = [str(p.get('text', '')) for p in paragraphs if isinstance(p, dict)] if isinstance(paragraphs, list) else []
    return ' '.join([str(draft.get('headline', '')), str(draft.get('limitations', '')), *texts])


def can_simplify(reason, draft, review=None):
    # Instructions in any model output are never fed to a further generation.
    if rules.INJECTION.search(json.dumps([draft, review], ensure_ascii=False)):
        return False
    if reason in SIMPLIFIABLE or reason == 'risk_requires_review':
        return True
    if reason == 'draft_not_eligible' and isinstance(draft, dict):
        return (draft.get('about_dogs') is True and draft.get('needs_full_text') is False
                and draft.get('risk') in ('low', 'high') and draft.get('contains_practical_advice') is True)
    if reason == 'review_not_approved':
        try:
            rules.validate_review_contract(review)
        except rules.Hold:
            return False
        return (review['about_dogs'] and review['source_sufficient'] and review['risk'] != 'unknown'
                and isinstance(draft, dict) and review['topic'] == draft.get('topic')
                and (bool(review['reasons']) or any(review[k] is False for k in
                     ('all_claims_supported', 'no_practical_advice', 'limitations_adequate', 'no_overclaim'))))
    return False


def run_stages(material, audit, budget, attempt_id):
    entries = audit.setdefault('stages', [])
    next_stage = audit.setdefault('next_stage', 1)
    if (not isinstance(entries, list) or len(entries) > 3 or type(next_stage) is not int
            or not 1 <= next_stage <= 4 or len(entries) < next_stage - 1
            or any(not isinstance(e, dict) or e.get('stage') != i + 1 for i, e in enumerate(entries))):
        raise rules.Hold('uncertain_stage')

    def save():
        budget.checkpoint(attempt_id, audit)

    for stage in range(next_stage, 4):
        audit['stage'] = stage
        audit['phase'] = 'source'
        # Re-read the original source at each new format, not the rejected article.
        evidence = rules.verify_source(material)
        audit['verified_source'] = evidence
        audit['risk_signals'] = evidence.get('risk_signals', [])
        evidence['_stage'] = stage
        evidence['_feedback'] = [e['reason'] for e in entries if e.get('outcome') == 'rejected']
        if len(entries) < stage:
            if budget.remaining_calls() < 2:
                raise BudgetLimit('stage_model_budget')
            entries.append(dict(stage=stage, outcome='pending'))
            save()
        entry = entries[stage - 1]
        if entry.get('outcome') not in ('pending', 'generating', 'draft_ready', 'reviewing', 'review_ready', 'approved', 'rejected'):
            raise rules.Hold('uncertain_stage')
        if entry['outcome'] == 'rejected':
            audit['next_stage'] = stage + 1
            save()
            continue
        draft, review = entry.get('draft'), entry.get('review')
        try:
            if entry['outcome'] in ('pending', 'generating'):
                audit['phase'] = 'generation'
                if budget.phase_started(material['id'], stage, 'draft'):
                    raise rules.Failure('uncertain_stage')
                entry['outcome'] = 'generating'
                save()
                draft = budget.model(material['id'], 'draft', evidence)
                entry.update(draft=draft, outcome='draft_ready')
                save()
            audit['draft'] = draft  # backward-compatible last generated version
            audit['phase'] = 'draft_check'
            audit['policy_signals'] = rules.policy_signals(public_text(draft))
            audit['draft_risk_signals'] = rules.risk_signals(public_text(draft))
            rules.validate_draft(draft, evidence, stage)
            if rules.INJECTION.search(json.dumps(draft, ensure_ascii=False)):
                raise rules.Hold('untrusted_draft_instructions')
            if entry['outcome'] not in ('review_ready', 'approved'):
                audit['phase'] = 'review'
                if budget.remaining_calls() < 1:
                    raise BudgetLimit('model_daily_limit')
                if budget.phase_started(material['id'], stage, 'review'):
                    raise rules.Failure('uncertain_stage')
                entry['outcome'] = 'reviewing'
                save()
                review = budget.model(material['id'], 'review', evidence, draft)
                entry.update(review=review, outcome='review_ready')
                save()
            audit['review'] = review
            audit['phase'] = 'review'
            if rules.INJECTION.search(json.dumps(review, ensure_ascii=False)):
                raise rules.Hold('untrusted_draft_instructions')
            rules.validate_review(review, draft)
            entry.update(outcome='approved', reason='approved')
            audit['next_stage'] = stage
            save()
            return draft, review, stage
        except rules.Hold as error:
            reason = str(error)
            if not can_simplify(reason, draft, review):
                entry.update(outcome='rejected', reason=reason)
                save()
                raise
            entry.update(outcome='rejected', reason=reason)
            audit['next_stage'] = stage + 1
            save()
        except rules.Failure as error:
            reason = str(error)
            if reason != 'model_truncated':
                entry.update(outcome='error', reason=reason)
                save()
                raise
            entry.update(outcome='rejected', reason=reason)
            audit['next_stage'] = stage + 1
            save()
    raise rules.Hold('all_stages_held')
