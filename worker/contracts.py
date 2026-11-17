"""Strict v7 article contracts. Sources and model statements remain untrusted."""

import re
from publication import TOPICS
from worker import editorial as r
from worker.evidence import unsafe_actions


def validate_draft(draft, evidence, stage=None):
    r.require_keys(draft, ('headline', 'topic', 'material_type', 'paragraphs', 'claims', 'advice_class', 'risk', 'caveat'))
    if not isinstance(draft['topic'], str) or draft['topic'] not in TOPICS: raise r.Hold('outside_topics')
    if draft['material_type'] not in ('research', 'news', 'guide') or draft['material_type'] != evidence.get('kind', 'research'):
        raise r.Hold('source_kind_mismatch')
    if draft['risk'] != 'low' or draft['advice_class'] not in ('none', 'safe_general'):
        raise r.Hold('unsafe_advice')
    if draft['advice_class'] == 'safe_general' and draft['material_type'] != 'guide': raise r.Hold('unsafe_advice')
    if evidence.get('_fit') and draft['topic'] != evidence['_fit']['focus']: raise r.Hold('editorial_focus_mismatch')
    headline = r.text_field(draft['headline'], 8, 140)
    caveat = r.text_field(draft['caveat'], 0, 600)
    if not isinstance(draft['paragraphs'], list) or not 1 <= len(draft['paragraphs']) <= 3: raise r.Hold('invalid_article_length')
    if not isinstance(draft['claims'], list) or not 1 <= len(draft['claims']) <= 3: raise r.Hold('invalid_stage_claim_count')
    claim_ids = set()
    for claim in draft['claims']:
        r.require_keys(claim, ('id', 'text', 'quote', 'source_id'))
        ident = r.text_field(claim['id'], 2, 2)
        if not re.fullmatch(r'c[1-3]', ident) or ident in claim_ids or claim['source_id'] != evidence['source_id']:
            raise r.Hold('unsupported_claim')
        r.text_field(claim['text'], 10, 1000)
        quote = r.text_field(claim['quote'], 15, 1200)
        if len(quote.split()) < 4 or r.normal(quote) not in r.normal(evidence['abstract']): raise r.Hold('unsupported_claim')
        claim_ids.add(ident)
    texts, used = [], set()
    for p in draft['paragraphs']:
        r.require_keys(p, ('kind', 'text', 'claim_ids'))
        if p['kind'] not in ('result', 'interpretation', 'guidance') or not isinstance(p['claim_ids'], list):
            raise r.Hold('invalid_model_contract')
        if not p['claim_ids'] or any(not isinstance(x, str) or x not in claim_ids for x in p['claim_ids']):
            raise r.Hold('unsupported_claim')
        if p['kind'] == 'guidance' and (draft['material_type'] != 'guide' or draft['advice_class'] != 'safe_general'):
            raise r.Hold('unsafe_advice')
        texts.append(r.text_field(p['text'], 20, 2500)); used.update(p['claim_ids'])
    if used != claim_ids: raise r.Hold('unsupported_claim')
    if not 30 <= len(' '.join(texts).split()) <= 160: raise r.Hold('invalid_article_length')
    public = ' '.join([headline, *texts, caveat])
    if r.INJECTION.search(public): raise r.Hold('untrusted_draft_instructions')
    if unsafe_actions(public): raise r.Hold('unsafe_advice')
    if r.OVERCLAIM.search(r.SAFE_NEGATION.sub(' ', public)): raise r.Hold('overclaim')
    if draft['material_type'] != 'guide' and r.INSTRUCTIONS.search(r.LOGICAL_INFERENCE.sub(' ', public)):
        raise r.Hold('unexpected_advice')
    if 'http' in public.lower() or re.search(r'<\s*/?\s*(?:script|iframe|img|a)\b', public, re.I): raise r.Hold('unverified_link')
    cyrillic = len(re.findall(r'[а-яё]', public, re.I))
    if cyrillic < len(re.findall(r'[a-zа-яё]', public, re.I)) / 2: raise r.Hold('not_russian')
    allowed = set(re.findall(r'\d+', evidence['abstract'] + ' ' + evidence['title'] + ' ' + str(evidence.get('year', ''))))
    if set(re.findall(r'\d+', public)) - allowed: raise r.Hold('unverified_number')
    if evidence.get('is_evergreen') and re.search(r'\b(?:новое исследование|новое открытие|свежая новость|прорыв)\b', public, re.I):
        raise r.Hold('evergreen_as_news')


def validate_review_contract(review):
    flags = ('approved', 'about_dogs', 'owner_relevant', 'plain_language', 'source_sufficient',
             'all_claims_supported', 'no_overclaim', 'advice_safe')
    r.require_keys(review, (*flags, 'topic', 'material_type', 'risk', 'reasons'))
    if (any(type(review[k]) is not bool for k in flags) or not isinstance(review['topic'], str)
            or review['topic'] not in TOPICS or review['material_type'] not in ('research', 'news', 'guide')
            or review['risk'] not in ('low', 'unknown', 'high') or not isinstance(review['reasons'], list)
            or len(review['reasons']) > 6 or any(not isinstance(x, str) or not 1 <= len(x) <= 100 for x in review['reasons'])):
        raise r.Hold('invalid_review_contract')


def validate_review(review, draft):
    validate_review_contract(review)
    if not review['owner_relevant']: raise r.Hold('audience_mismatch')
    if not review['advice_safe'] or review['risk'] != 'low': raise r.Hold('unsafe_advice')
    if not review['source_sufficient'] or not review['all_claims_supported']: raise r.Hold('unsupported_claim')
    if not review['no_overclaim']: raise r.Hold('overclaim')
    if not review['plain_language']: raise r.Hold('readability_failed')
    if (not review['approved'] or not review['about_dogs'] or review['topic'] != draft['topic']
            or review['material_type'] != draft['material_type'] or review['reasons']):
        raise r.Hold('review_not_approved')


def publication_payload(draft, evidence, stage=None):
    from collector.sources import canonical
    original = canonical(evidence.get('original_url', evidence['url']))
    text = [dict(kind=p['kind'], text=p['text']) for p in draft['paragraphs']]
    # Material caveats, when present, are prose; not a mandatory reader warning block.
    if draft['caveat']: text[-1]['text'] += ' ' + draft['caveat']
    citations = [{key: str(evidence.get(key) or '') for key in ('title', 'authors', 'journal', 'year', 'doi', 'url')}]
    if original != evidence['url']:
        citations.append(dict(title=evidence['title'], authors='', journal=evidence.get('publisher', ''), year='', doi='', url=original))
    attribution = evidence.get('publisher', evidence.get('journal', ''))
    if urllib_host(original) == 'rkf.org.ru': attribution = 'Источник: Официальный сайт Российской Кинологической Федерации RKF.org.ru'
    research_format = 'research_review' if re.search(r'review|meta-analysis', evidence.get('publication_type',''), re.I) else 'explainer'
    return dict(version=2, evidence_key=evidence['evidence_key'], topic=draft['topic'],
                format=research_format if draft['material_type'] == 'research' else draft['material_type'],
                material_type=draft['material_type'], source_language=evidence.get('source_language', 'en'),
                original_url=original, evidence_scope=evidence['evidence_scope'], attribution=attribution,
                paragraphs=text, citations=citations, limitations='', stage=stage,
                research_date=evidence.get('research_date', ''), is_evergreen=evidence.get('is_evergreen', False))


def urllib_host(url):
    from urllib.parse import urlsplit
    return urlsplit(url).hostname
