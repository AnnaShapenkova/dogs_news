"""Source verification and fail-closed editorial contracts for the narrow pilot."""

import hashlib
import json
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

from collector.main import fetch, plain_text, DOG_WORDS, has_animal_topic, STAFF_TRAINING
from publication import TOPICS, FORMATS

RULES_VERSION = "dogs-pilot-v6"
STAGES = {
    1: dict(words=(120, 220), paragraphs=(2, 4), claims=(1, 3)),
    2: dict(words=(80, 120), paragraphs=(1, 2), claims=(1, 2)),
    3: dict(words=(40, 80), paragraphs=(1, 1), claims=(1, 1)),
}
API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
RISK = re.compile(
    r"\b(?:aggress\w*|bit(?:e|es|ing)|aversive|punish\w*|shock|chok\w*|dos(?:e|es|age)|"
    r"drug\w*|medicat\w*|medicin\w*|treat(?:ment|ments|ing)|therap\w*|diagnos\w*|"
    r"diet\w*|cancer\w*|tumou?r\w*|pain|rehabilitat\w*|clinical trial|"
    r"леч[а-яё]*|лекарств[а-яё]*|доз(?:а|ы|у|ой|иров[а-яё]*)|диет[а-яё]*|диагноз[а-яё]*|"
    r"агресси[а-яё]*|укус[а-яё]*|наказан[а-яё]*|электроошейн[а-яё]*|удуш[а-яё]*|болев[а-яё]*|боль|онколо[а-яё]*)\b", re.I
)
INSTRUCTIONS = re.compile(
    r"\b(?:дайте|давайте|используйте|попробуйте|обратитесь|применяйте|следует|нужно|необходимо|"
    r"рекомендуем|рекомендуется|назначьте|administer|you should|you must|give your dog|try this)\b", re.I
)
OVERCLAIM = re.compile(r"\b(?:доказали|гарантиру[а-яё]*|всегда|все собаки|консенсус|безусловно|proves|guarantee\w*|all dogs|always)\b", re.I)
INJECTION = re.compile(r"(?:ignore (?:all |previous )?instructions|system prompt|игнорируй[а-яё]* (?:все |предыдущие )?инструкц|раскрой[а-яё]* (?:ключ|токен))", re.I)
DOI = re.compile(r"10\.\d{4,9}/[^\s<>\"?#]+", re.I)
SAFE_NEGATION = re.compile(
    r"\bне\s+(?:всегда|все\s+собаки|доказали|гарантиру[а-яё]*)\b|\bnot\s+(?:always|all\s+dogs)\b", re.I)
LOGICAL_INFERENCE = re.compile(
    r"\bиз\s+(?:этого|результат[а-яё]*|данн[а-яё]*|наблюден[а-яё]*|исследован[а-яё]*)\s+"
    r"следует(?=\s*,?\s*(?:что|вывод|предположение|заключение)\b)", re.I)


def policy_signals(text):
    # Exempt only the local safe phrase, never an entire sentence or paragraph.
    contextual = LOGICAL_INFERENCE.sub(' ', SAFE_NEGATION.sub(' ', text))
    return sorted({m.group(0).lower() for regex in (INSTRUCTIONS, OVERCLAIM, INJECTION)
                   for m in regex.finditer(contextual)})


class Hold(Exception):
    pass


class Failure(Exception):
    def __init__(self, reason, diagnostics=None):
        super().__init__(reason)
        self.diagnostics = diagnostics or {}


def risk_signals(text):
    """Private signals for contextual review; a topic word is not a verdict."""
    return sorted({match.group(0).lower() for match in RISK.finditer(text)})


def normal(value):
    return " ".join(unicodedata.normalize("NFC", value).split())


def doi_value(value):
    if not value:
        return ""
    if not isinstance(value, str):
        raise Hold("invalid_doi")
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value.strip(), flags=re.I)
    if not DOI.fullmatch(value) or len(value) > 200:
        raise Hold("invalid_doi")
    return value.lower()


def input_hash(material):
    value = {key: material.get(key) for key in ("url", "title", "text", "raw_payload", "content_scope")}
    try:
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError, UnicodeError):
        raise Hold("invalid_input") from None


def secondary_identifiers(record):
    if not isinstance(record, str):
        raise Hold("unlinked_secondary")
    try:
        item = ET.fromstring(record)
    except ET.ParseError:
        raise Hold("unlinked_secondary") from None
    content = " ".join(item.itertext())
    # Only explicit identifiers/links, never a guessed match by topic or headline.
    dois = {doi_value(m.group(1).rstrip('.,;')) for m in re.finditer(
        r'(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)(10\.\d{4,9}/[^\s<>"?#&]+)', content, re.I)}
    for element in item.iter():
        if element.tag.rsplit('}', 1)[-1].lower() == 'doi' and element.text:
            dois.add(doi_value(element.text))
    pmids = set(re.findall(r'(?:pubmed\.ncbi\.nlm\.nih\.gov/|europepmc\.org/article/MED/)([0-9]+)', content, re.I))
    if len(dois) > 1 or len(pmids) > 1:
        raise Hold("ambiguous_source")
    if not dois and not pmids:
        raise Hold("unlinked_secondary")
    return next(iter(pmids), ""), next(iter(dois), "")


def verify_source(material):
    payload = material.get("raw_payload")
    if not isinstance(payload, dict) or material.get("content_scope") not in ("abstract", "feed_excerpt"):
        raise Hold("insufficient_content")
    original = payload.get("record")
    research = payload.get("provider") == "europepmc"
    if research:
        if not isinstance(original, dict) or original.get("source") != "MED" or not re.fullmatch(r"[0-9]+", str(original.get("id", ""))):
            raise Hold("invalid_source_identity")
        pmid, doi = str(original["id"]), doi_value(original.get("doi"))
    elif payload.get("provider") == "rss":
        pmid, doi = secondary_identifiers(original)
    else:
        raise Hold("unverified_provider")
    terms = ["SRC:MED"]
    if pmid:
        terms.append(f"EXT_ID:{pmid}")
    if doi:
        terms.append(f'DOI:"{doi}"')
    request_url = API + "?" + urllib.parse.urlencode({"query": " AND ".join(terms), "format": "json", "resultType": "core", "pageSize": 2})
    try:
        data = json.loads(fetch(request_url))
        records = data["resultList"]["result"]
    except Exception as error:
        raise Failure("source_unavailable") from error
    if not isinstance(records, list) or len(records) != 1 or data.get("hitCount", 1) != 1:
        raise Hold("ambiguous_source")
    record = records[0]
    if not isinstance(record, dict) or record.get("source") != "MED" or not re.fullmatch(r"[0-9]+", str(record.get("id", ""))):
        raise Hold("invalid_source_identity")
    verified_doi = doi_value(record.get("doi"))
    if (pmid and str(record["id"]) != pmid) or (doi and verified_doi != doi):
        raise Hold("source_mismatch")
    try:
        title, abstract = plain_text(record.get("title")), plain_text(record.get("abstractText"))
    except ValueError:
        raise Hold("invalid_source_metadata") from None
    if research and (normal(title) != normal(plain_text(original.get("title"))) or
                     normal(abstract) != normal(plain_text(original.get("abstractText")))):
        raise Hold("source_changed")
    type_list = record.get("pubTypeList")
    types = type_list.get("pubType", []) if isinstance(type_list, dict) else []
    if not isinstance(types, list) or not types or any(not isinstance(t, str) for t in types):
        raise Hold("unknown_publication_status")
    type_text = " ".join(types).lower()
    if any(word in type_text for word in ("retract", "erratum", "correct", "preprint", "withdraw", "concern", "review", "meta-analysis")) or record.get("commentCorrectionList"):
        raise Hold("publication_requires_review")
    if "journal article" not in type_text or record.get("publicationStatus") not in ("ppublish", "epublish", "aheadofprint", "published"):
        raise Hold("unknown_publication_status")
    if record.get("language") not in ("eng", "rus"):
        raise Hold("unsupported_language")
    if not title or len(abstract.split()) < 100 or len(abstract) > 20000:
        raise Hold("insufficient_content")
    if not has_animal_topic(title, abstract) and STAFF_TRAINING.search(title + ' ' + abstract):
        raise Hold('clinical_focus')
    if not DOG_WORDS.search(title) or not has_animal_topic(title, abstract):
        raise Hold("outside_pilot")
    if INJECTION.search(title + " " + abstract):
        raise Hold("untrusted_source_instructions")
    journal_info = record.get("journalInfo") or {}
    journal = journal_info.get("journal") or {} if isinstance(journal_info, dict) else {}
    if not isinstance(journal, dict) or any(not isinstance(value, str) for value in
            (record.get("authorString", ""), record.get("pubYear", ""), journal.get("title", ""))):
        raise Hold("invalid_source_metadata")
    research_date = record.get('firstPublicationDate', '')
    if research_date:
        try:
            research_date = date.fromisoformat(research_date).isoformat()
            if date.fromisoformat(research_date) > datetime.now(timezone.utc).date():
                raise ValueError()
        except (ValueError, TypeError):
            raise Hold('invalid_source_metadata') from None
    elif payload.get('discovery_window') == 'evergreen':
        raise Hold('invalid_source_metadata')
    return {"source_id": "MED:" + str(record["id"]),
            "evidence_key": "doi:" + verified_doi if verified_doi else "MED:" + str(record["id"]),
            "doi": verified_doi, "title": title, "abstract": abstract,
            "url": f"https://europepmc.org/article/MED/{record['id']}",
            "authors": record.get("authorString") or "", "journal": journal.get("title") or "",
            "year": record.get("pubYear") or "", "request_url": request_url,
            "evidence_scope": "abstract", "risk_signals": risk_signals(title + " " + abstract),
            "research_date": research_date,
            "is_evergreen": bool(research_date and date.fromisoformat(research_date) <
                                 datetime.now(timezone.utc).date() - timedelta(days=89))}


def require_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise Hold("invalid_model_contract")


def text_field(value, minimum=1, maximum=5000):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum or '\x00' in value:
        raise Hold("invalid_model_contract")
    return value.strip()


def validate_editorial_fit(fit, evidence):
    """An editorial decision, never scientific evidence or a publication permission."""
    try:
        require_keys(fit, ('audience', 'focus', 'owner_question', 'source_quote', 'decision', 'reason'))
    except Hold:
        raise Failure('invalid_editorial_contract') from None
    enums = {'audience': ('general_owner', 'specialist', 'unclear'),
             'focus': ('behavior', 'learning', 'welfare', 'clinical_methods', 'other'),
             'decision': ('eligible', 'skip'),
             'reason': ('owner_interest', 'specialist_clinical', 'outside_pilot', 'unclear')}
    if (any(not isinstance(fit[key], str) or fit[key] not in options for key, options in enums.items())
            or not isinstance(fit['owner_question'], str) or len(fit['owner_question']) > 180
            or not isinstance(fit['source_quote'], str) or len(fit['source_quote']) > 1200):
        raise Failure('invalid_editorial_contract')
    if INJECTION.search(json.dumps(fit, ensure_ascii=False)):
        raise Hold('untrusted_draft_instructions')
    if fit['focus'] == 'clinical_methods':
        raise Hold('clinical_focus')
    if fit['audience'] == 'specialist':
        raise Hold('audience_specialist')
    if fit['focus'] == 'other' or fit['reason'] == 'outside_pilot':
        raise Hold('outside_pilot')
    if (fit['audience'] != 'general_owner' or fit['decision'] != 'eligible'
            or fit['reason'] != 'owner_interest' or len(fit['owner_question'].strip()) < 10):
        raise Hold('unclear_editorial_fit')
    quote = normal(fit['source_quote'])
    if len(quote.split()) < 4 or quote not in normal(evidence['abstract']):
        raise Hold('unverified_editorial_fit')


def unwrap_first_draft(value, evidence):
    if not isinstance(value, dict) or set(value) != {'editorial_fit', 'article'}:
        raise Failure('invalid_editorial_contract')
    fit = value['editorial_fit']
    # A rejected source cannot smuggle an article through its skip branch.
    if isinstance(fit, dict) and fit.get('decision') == 'skip' and value['article'] is not None:
        raise Failure('invalid_editorial_contract')
    validate_editorial_fit(fit, evidence)
    if not isinstance(value['article'], dict):
        raise Failure('invalid_editorial_contract')
    return fit, value['article']


def validate_draft(draft, evidence, stage=None):
    bounds = STAGES[stage] if stage in STAGES else dict(words=(80, 220), paragraphs=(1, 4), claims=(1, 6))
    require_keys(draft, ("about_dogs", "topic", "format", "risk", "headline", "paragraphs", "limitations",
                         "claims", "needs_full_text", "contains_practical_advice"))
    if draft["about_dogs"] is not True or draft["risk"] != "low" or draft["needs_full_text"] is not False or draft["contains_practical_advice"] is not False:
        raise Hold("draft_not_eligible")
    if not isinstance(draft["topic"], str) or not isinstance(draft["format"], str) or draft["topic"] not in TOPICS or draft["format"] not in FORMATS:
        raise Hold("outside_pilot")
    if evidence.get('_fit') and draft['topic'] != evidence['_fit']['focus']:
        raise Hold('editorial_focus_mismatch')
    headline = text_field(draft["headline"], 8, 140)
    limitations = text_field(draft["limitations"], 40, 1500)
    if not isinstance(draft['paragraphs'], list):
        raise Hold('invalid_model_contract')
    if not bounds['paragraphs'][0] <= len(draft["paragraphs"]) <= bounds['paragraphs'][1]:
        raise Hold('invalid_article_length')
    if not isinstance(draft["claims"], list) or not 1 <= len(draft['claims']) <= 6:
        raise Hold("invalid_model_contract")
    if not bounds['claims'][0] <= len(draft['claims']) <= bounds['claims'][1]:
        raise Hold('invalid_stage_claim_count')
    claim_ids = set()
    for claim in draft["claims"]:
        require_keys(claim, ("id", "text", "quote", "source_id"))
        identifier = text_field(claim["id"], 1, 20)
        if not re.fullmatch(r"c[1-6]", identifier) or identifier in claim_ids or claim["source_id"] != evidence["source_id"]:
            raise Hold("unverified_claim")
        claim_ids.add(identifier)
        text_field(claim["text"], 10, 1000)
        quote = text_field(claim["quote"], 15, 1200)
        if len(quote.split()) < 4 or normal(quote) not in normal(evidence["abstract"]):
            raise Hold("unverified_claim")
    paragraphs = []
    used_ids = set()
    for paragraph in draft["paragraphs"]:
        require_keys(paragraph, ("kind", "text", "claim_ids"))
        refs = paragraph["claim_ids"]
        if paragraph["kind"] not in ("result", "interpretation") or not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in claim_ids for r in refs):
            raise Hold("unverified_claim")
        if stage in (2, 3) and paragraph['kind'] != 'result':
            raise Hold('unsupported_interpretation')
        used_ids.update(refs)
        paragraphs.append(text_field(paragraph["text"], 20, 2500))
    if used_ids != claim_ids or not bounds['words'][0] <= len(" ".join(paragraphs).split()) <= bounds['words'][1]:
        raise Hold("invalid_article_length")
    public_text = " ".join([headline, *paragraphs, limitations])
    if evidence.get('is_evergreen') and re.search(
            r'\b(?:нов[а-яё]*\s+(?:исследован[а-яё]*|открыти[а-яё]*|новост[а-яё]*)|свеж[а-яё]*\s+(?:исследован[а-яё]*|новост[а-яё]*))\b',
            public_text, re.I):
        raise Hold('evergreen_as_news')
    if policy_signals(public_text):
        raise Hold("risk_requires_review")
    if 'http' in public_text.lower() or re.search(r'<\s*/?\s*(?:script|iframe|img|a)\b', public_text, re.I):
        raise Hold("unverified_link")
    cyrillic = len(re.findall(r'[а-яё]', public_text, re.I))
    if cyrillic < len(re.findall(r'[a-zа-яё]', public_text, re.I)) / 2:
        raise Hold("not_russian")
    allowed_numbers = set(re.findall(r'\d+', evidence["abstract"] + " " + evidence["title"] + " " + str(evidence["year"])))
    if set(re.findall(r'\d+', public_text)) - allowed_numbers:
        raise Hold("unverified_number")
    if not re.search(r'аннотаци|реферат', limitations, re.I):
        raise Hold("missing_evidence_scope")


def validate_review_contract(review):
    require_keys(review, ("approved", "risk", "about_dogs", "topic", "all_claims_supported", "no_practical_advice",
                          "limitations_adequate", "no_overclaim", "source_sufficient", "reasons", "owner_relevant", "plain_language"))
    flags = ("approved", "about_dogs", "all_claims_supported", "no_practical_advice", "limitations_adequate", "no_overclaim", "source_sufficient", "owner_relevant", "plain_language")
    if (any(type(review[key]) is not bool for key in flags)
            or not isinstance(review['risk'], str) or review['risk'] not in ('low', 'unknown', 'high')
            or not isinstance(review['topic'], str) or review['topic'] not in TOPICS
            or not isinstance(review['reasons'], list) or len(review['reasons']) > 6
            or any(not isinstance(reason, str) or not 1 <= len(reason) <= 500 for reason in review['reasons'])):
        raise Hold('review_not_approved')


def validate_review(review, draft):
    validate_review_contract(review)
    if review['owner_relevant'] is not True:
        raise Hold('audience_mismatch')
    if (review['plain_language'] is False and review['risk'] == 'low' and review['about_dogs']
            and review['source_sufficient'] and review['no_practical_advice']
            and review['all_claims_supported'] and review['no_overclaim']
            and review['limitations_adequate'] and review['topic'] == draft['topic']):
        raise Hold('readability_failed')
    flags = ("approved", "about_dogs", "all_claims_supported", "no_practical_advice", "limitations_adequate", "no_overclaim", "source_sufficient")
    if any(review[key] is not True for key in flags) or review["risk"] != "low" or review["topic"] != draft["topic"]:
        raise Hold("review_not_approved")
    if not isinstance(review["reasons"], list) or review["reasons"]:
        if review['plain_language'] is True:
            raise Hold("review_not_approved")
    if review['plain_language'] is not True:
        raise Hold('readability_failed')


def publication_payload(draft, evidence, stage=None):
    return {"version": 1, "evidence_key": evidence["evidence_key"], "topic": draft["topic"],
            "format": "explainer" if evidence.get('is_evergreen') else draft["format"],
            "research_date": evidence.get('research_date', ''), "is_evergreen": evidence.get('is_evergreen', False),
            "stage": stage if type(stage) is int and stage in STAGES else None,
            "paragraphs": [{"kind": p["kind"], "text": p["text"]} for p in draft["paragraphs"]],
            "limitations": draft["limitations"], "evidence_scope": "abstract",
            "citations": [{key: str(evidence.get(key) or "") for key in ("title", "authors", "journal", "year", "doi", "url")}]}
