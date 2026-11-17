"""Small shared public projection. Never serialize editorial audits to readers."""

from urllib.parse import urlsplit
from datetime import date

TOPICS = {"behavior": "Поведение и эмоции", "learning": "Обучение", "welfare": "Благополучие", "nutrition": "Питание"}
FORMATS = {"research_explainer": "Объяснение исследования", "discovery": "Научное открытие", "explainer": "Познавательный разбор",
           "guide": "Полезное руководство", "news": "Новость", "research_review": "Обзор исследований"}
STAGE_LABELS = {1: 'Разбор исследования', 2: 'Короткое объяснение', 3: 'Научная заметка'}


def safe_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return ""
    try:
        url = urlsplit(value)
        url.port
        if url.scheme in ("https", "http") and url.hostname and not url.username and not url.password:
            return value
    except ValueError:
        pass
    return ""


def public_payload(value):
    """An explicit allowlist also protects against accidental audit keys in JSONB."""
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] not in (1, 2):
        return {}
    v2 = value['version'] == 2
    topic = value.get("topic")
    format_name = value.get("format")
    if not isinstance(topic, str) or not isinstance(format_name, str) or topic not in TOPICS or format_name not in FORMATS:
        return {}
    paragraphs = []
    for item in value.get("paragraphs", []) if isinstance(value.get("paragraphs"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("kind") in (("result", "interpretation", "guidance") if v2 else ("result", "interpretation")):
            paragraphs.append({"text": item["text"], "kind": item["kind"]})
    citations = []
    for item in value.get("citations", []) if isinstance(value.get("citations"), list) else []:
        if not isinstance(item, dict) or not safe_url(item.get("url")):
            continue
        clean = {key: item.get(key, "") if isinstance(item.get(key, ""), str) else ""
                 for key in ("title", "authors", "journal", "year", "doi")}
        clean["url"] = safe_url(item["url"])
        citations.append(clean)
    limitations = '' if v2 else value.get("limitations")
    if not paragraphs or not citations or not isinstance(limitations, str):
        return {}
    research_date = value.get('research_date', '')
    try:
        research_date = date.fromisoformat(research_date).isoformat() if isinstance(research_date, str) and research_date else ''
    except ValueError:
        research_date = ''
    evergreen = value.get('is_evergreen') is True and bool(research_date)
    context_note = ('Познавательный разбор · исследование от ' + research_date) if evergreen else ''
    if evergreen and not v2:
        format_name = 'explainer'
    stage = value.get('stage')
    stage = stage if type(stage) is int and stage in STAGE_LABELS else None
    result = {"version": value['version'], "topic": topic, "topic_label": TOPICS[topic], "format": format_name,
            "format_label": FORMATS[format_name] if v2 else STAGE_LABELS.get(stage, FORMATS[format_name]), "paragraphs": paragraphs, "limitations": limitations,
            "evidence_scope": "abstract", "citations": citations,
            "research_date": research_date, "is_evergreen": evergreen, "context_note": context_note, "stage": stage}
    if v2:
        if (value.get('material_type') not in ('research', 'news', 'guide')
                or value.get('evidence_scope') not in ('abstract', 'page_excerpt')
                or value.get('source_language') not in ('ru', 'en') or not safe_url(value.get('original_url'))):
            return {}
        if value['original_url'] not in [c['url'] for c in citations]: return {}
        result.update(material_type=value['material_type'], evidence_scope=value['evidence_scope'],
                      source_language=value['source_language'], original_url=value['original_url'],
                      attribution=value.get('attribution', '') if isinstance(value.get('attribution'), str) else '',
                      context_note=('Материал от ' + research_date) if evergreen else '')
    return result
