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

RULES_VERSION = "dogs-pilot-v7"
STAGES = {
    1: dict(words=(60, 120), paragraphs=(1, 3), claims=(1, 3)),
    2: dict(words=(40, 100), paragraphs=(1, 3), claims=(1, 3)),
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
    payload = material.get('raw_payload') or {}
    if not isinstance(payload, dict):
        raise Hold('insufficient_content')
    if payload.get('provider') in ('rkf', 'pets'):
        from worker.evidence import verify_page
        return verify_page(material, fetch)
    if payload.get('provider') == 'plos':
        from worker.evidence import verify_plos
        return verify_plos(material, fetch)
    from worker.evidence import enrich, CLINICAL
    evidence = verify_europepmc(material)
    if CLINICAL.search(evidence['title']):
        raise Hold('specialist_clinical')
    return enrich(evidence)


def verify_europepmc(material):
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
    if any(word in type_text for word in ("retract", "erratum", "correct", "preprint", "withdraw", "concern")) or record.get("commentCorrectionList"):
        raise Hold("retracted_source" if "retract" in type_text or "withdraw" in type_text else "unknown_source_status")
    if not any(t in type_text for t in ("journal article", "review", "meta-analysis")) or record.get("publicationStatus") not in ("ppublish", "epublish", "aheadofprint", "published"):
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
            "source_language": "ru" if record.get('language') == 'rus' else 'en',
            "publication_type": type_text,
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
             'focus': ('behavior', 'learning', 'welfare', 'nutrition', 'clinical_methods', 'other'),
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


from worker.contracts import validate_draft, validate_review_contract, validate_review, publication_payload
