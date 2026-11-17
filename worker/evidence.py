"""Verification of explicitly configured page sources and open PLOS research."""

import hashlib
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from collector.sources import SourceHTTP, canonical, page_snapshot, plos_url

UNSAFE = re.compile(
    r'\b(?:швырн\w*|од[её]рн\w*|ударьте|рывками|рывок поводка|электроошейник|'
    r'удушающий ошейник|сырые кости|кости в сыром виде|raw bones|shock collar|choke collar|'
    r'(?:дайте|давайте|назначьте|применяйте|добавьте|можно давать|следует давать|give|administer)'
    r'[^.!?;]{0,70}(?:лекарств\w*|препарат\w*|ибупрофен\w*|антибиотик\w*|дозиров\w*|лечебн\w* диет\w*|drug\w*)|'
    r'(?:увеличьте|увеличить|уменьшите|уменьшить)[^.!?;]{0,25}(?:порци\w*|доз\w*))\b', re.I)
CLINICAL = re.compile(r'\b(?:auscultation|echocardiograph\w*|murmur grading|cardiac murmur\w*|'
                      r'аускультац\w*|эхокардиограф\w*|стадирован\w*|шумы? в сердце|диагностическ\w* тест\w*)\b', re.I)


def unsafe_actions(text):
    return [m.group(0) for m in UNSAFE.finditer(text)
            if not re.search(r'(?:\bне\b|\bбез\b|\bavoid\b|\bnever\b)[^.!?;]{0,35}$', text[max(0, m.start()-40):m.start()], re.I)]


def enrich(evidence, kind='research', original_url=None, publisher=None):
    evidence.update(kind=kind, excerpt=evidence['abstract'], excerpt_hash=hashlib.sha256(evidence['abstract'].encode()).hexdigest(),
                    source_url=evidence['url'], original_url=original_url or evidence['url'],
                    source_language=evidence.get('source_language', 'en'),
                    publisher=publisher or evidence.get('journal', ''), origin_urls=[evidence['url']], status_check='verified')
    return evidence


def verify_page(material, fetcher):
    from worker import editorial as r
    payload = material['raw_payload']
    url = canonical(material['url'])
    host = urllib.parse.urlsplit(url).hostname
    expected = 'rkf' if host == 'rkf.org.ru' else 'pets' if host == 'vetandlife.ru' else None
    if expected is None or payload.get('provider') != expected or material.get('content_scope') != 'page_excerpt':
        raise r.Hold('unverified_provider')
    if not urllib.parse.urlsplit(url).path.startswith('/poleznye-materialy-o-sobakah/' if expected == 'rkf' else '/pets/'):
        raise r.Hold('unverified_provider')
    try:
        live = page_snapshot(SourceHTTP(fetcher).get(url), url)
    except (ValueError, OSError, UnicodeError) as error:
        raise r.Failure('source_unavailable') from error
    if (live['content_hash'] != payload.get('content_hash') or live['text'] != material.get('text')
            or r.normal(live['title']) != r.normal(material['title'])):
        raise r.Hold('source_changed')
    if live['published_at'] and live['published_at'] > datetime.now(timezone.utc):
        raise r.Hold('unknown_source_status')
    if r.INJECTION.search(live['safety_text']): raise r.Hold('untrusted_source_instructions')
    if len(live['safety_text']) >= 60000: raise r.Hold('insufficient_content')
    if unsafe_actions(live['safety_text']): raise r.Hold('unsafe_advice')
    if CLINICAL.search(live['title']): raise r.Hold('specialist_clinical')
    if not r.DOG_WORDS.search(live['title'] + ' ' + live['text']): raise r.Hold('outside_topics')
    if re.search(r'\berid\b|реклама|партн[её]рский материал', live['safety_text'], re.I):
        raise r.Hold('commercial_content')
    contexts = [live['safety_text'][max(0,m.start()-140):m.end()+180] for m in r.RISK.finditer(live['safety_text'])][:10]
    safety_context = live['text'] + ('\nКонтекст рисков исходника:\n' + '\n'.join(contexts) if contexts else '')
    moment = live['published_at']
    date_text = moment.date().isoformat() if moment else ''
    evidence = dict(source_id='url:' + url, evidence_key='url:' + url, title=live['title'], abstract=live['text'],
                    url=url, authors=live['author'], journal=live['publisher'], year=str(moment.year) if moment else '',
                    doi='', request_url=url, evidence_scope='page_excerpt', research_date=date_text,
                    is_evergreen=bool(moment and (datetime.now(timezone.utc)-moment).days >= 90),
                    risk_signals=r.risk_signals(live['safety_text']), source_language='ru',
                    source_context=safety_context, content_hash=live['content_hash'],
                    truncated=live['truncated'], origin_links=live['links'])
    # An explicit scientific identifier is resolved through the scientific path, never guessed by headline.
    ids = re.findall(r'(?:doi\.org/|doi\s*:\s*)(10\.\d{4,9}/[^\s<>"?#&]+)',
                     ' '.join(live['links']) + ' ' + live['text'], re.I)
    if ids:
        unique = {r.doi_value(x.rstrip('.,;')) for x in ids}
        if len(unique) != 1: raise r.Hold('ambiguous_source')
        doi = unique.pop()
        xml = ET.Element('item'); ET.SubElement(xml, 'description').text = 'https://doi.org/' + doi
        scientific = dict(material, content_scope='feed_excerpt', raw_payload=dict(provider='rss', record=ET.tostring(xml, encoding='unicode')))
        verified = r.verify_source(scientific)
        verified.update(original_url=url, source_language='ru', publisher=live['publisher'], secondary_source_id='url:' + url)
        return verified
    if payload.get('material_type_hint') == 'news':
        if not moment or (datetime.now(timezone.utc)-moment).days >= 90:
            raise r.Hold('outside_news_window')
        primary_links = []
        for link in live['links']:
            try:
                checked = canonical(link)
                if urllib.parse.urlsplit(checked).hostname == 'rkf.org.ru' and checked != url:
                    primary_links.append(checked)
            except ValueError:
                continue
        if not primary_links:
            raise r.Hold('missing_primary_event')
        try:
            primary = page_snapshot(SourceHTTP(fetcher).get(primary_links[0]), primary_links[0])
        except (OSError, ValueError) as error:
            raise r.Failure('source_unavailable') from error
        if unsafe_actions(primary['safety_text']) or r.INJECTION.search(primary['safety_text']):
            raise r.Hold('unsafe_advice')
        evidence.update(source_id='url:' + primary_links[0], evidence_key='url:' + primary_links[0], url=primary_links[0], abstract=primary['text'],
                        title=primary['title'], authors=primary['author'], source_context=primary['text'])
        return enrich(evidence, 'news', url, live['publisher'])
    return enrich(evidence, 'guide', url, live['publisher'])


def verify_plos(material, fetcher):
    from worker import editorial as r
    record = material['raw_payload'].get('record')
    doi = r.doi_value(record.get('id')) if isinstance(record, dict) else ''
    if not doi.startswith('10.1371/') or material.get('content_scope') != 'abstract':
        raise r.Hold('invalid_source_identity')
    if not isinstance(record.get('abstract'), list) or any(not isinstance(t,str) for t in record['abstract']):
        raise r.Hold('invalid_source_metadata')
    if canonical(material['url']) != canonical(plos_url(doi)):
        raise r.Hold('source_mismatch')
    http = SourceHTTP(fetcher)
    try:
        query = urllib.parse.urlencode(dict(q='id:"' + doi + '"', wt='json', rows=2,
                                           fl='id,title,abstract,publication_date,author,journal,article_type'))
        docs = json.loads(http.get('https://api.plos.org/search?' + query))['response']['docs']
        if len(docs) != 1 or docs[0].get('id', '').lower() != doi: raise r.Hold('source_mismatch')
        live = docs[0]
        if not isinstance(live.get('abstract'), list) or any(not isinstance(t,str) for t in live['abstract']):
            raise r.Hold('invalid_source_metadata')
        xml_url = plos_url(doi, manuscript=True)
        root = ET.fromstring(http.get(xml_url))
    except r.Hold:
        raise
    except Exception as error:
        raise r.Failure('source_unavailable') from error
    meta = root.find('./front/article-meta')
    if meta is None: raise r.Hold('unknown_source_status')
    notice = ' '.join(' '.join(x.itertext()) for x in meta.findall('article-categories') + meta.findall('related-article') + meta.findall('article-notes'))
    kind = root.get('article-type', '')
    if re.search(r'retract|withdraw|expression of concern', kind + ' ' + notice, re.I): raise r.Hold('retracted_source')
    if re.search(r'correct|erratum', kind + ' ' + notice, re.I): raise r.Hold('unknown_source_status')
    if kind not in ('research-article', 'review-article'): raise r.Hold('unknown_source_status')
    xml_dois = [x.text.lower() for x in meta.findall('article-id') if x.get('pub-id-type') == 'doi' and x.text]
    if doi not in xml_dois: raise r.Hold('source_mismatch')
    licenses = list(meta.iter('license'))
    if not any(any('creativecommons.org/licenses/by/' in value for value in x.attrib.values()) for x in licenses):
        raise r.Hold('source_license_unverified')
    abstract = r.normal(r.plain_text(' '.join(live.get('abstract', []))))
    title = r.plain_text(live.get('title'))
    original = r.normal(r.plain_text(' '.join(record.get('abstract', []))))
    if abstract != original or r.normal(title) != r.normal(material['title']): raise r.Hold('source_changed')
    if len(abstract.split()) < 100 or len(abstract) > 20000: raise r.Hold('insufficient_content')
    if not r.DOG_WORDS.search(title) or not r.has_animal_topic(title, abstract): raise r.Hold('outside_topics')
    if CLINICAL.search(title): raise r.Hold('specialist_clinical')
    if r.INJECTION.search(title + ' ' + abstract): raise r.Hold('untrusted_source_instructions')
    moment = datetime.fromisoformat(live['publication_date'].replace('Z', '+00:00'))
    if moment > datetime.now(timezone.utc): raise r.Hold('unknown_source_status')
    return enrich(dict(source_id='doi:' + doi, evidence_key='doi:' + doi, doi=doi, title=title, abstract=abstract,
                       url=plos_url(doi), authors=', '.join(live.get('author', [])),
                       journal=live.get('journal', ''), year=str(moment.year), request_url=xml_url, evidence_scope='abstract',
                       research_date=moment.date().isoformat(), is_evergreen=(datetime.now(timezone.utc)-moment).days >= 90,
                       risk_signals=r.risk_signals(title + ' ' + abstract), publication_type=kind))
