"""Entirely synthetic editorial inputs. None are real research findings."""

from copy import deepcopy


def editorial_fit():
    return dict(audience='general_owner', focus='learning',
                owner_question='Как собаки выбирают знакомые изображения?',
                source_quote='The dogs selected the matching image more often than the alternative image.',
                decision='eligible', reason='owner_interest')


def first_draft(article=None):
    return dict(editorial_fit=editorial_fit(), article=article if article is not None else draft())


def stage_draft(stage, number=999999991):
    value = draft(number)
    if stage == 2:
        value['paragraphs'] = [dict(kind='result', claim_ids=['c1', 'c2'],
                                    text=' '.join(' '.join(p['text'] for p in value['paragraphs']).split()[:95]))]
    elif stage == 3:
        value['paragraphs'] = [dict(kind='result', claim_ids=['c1'], text=(
            'В лабораторной задаче собаки выбирали совпадающее изображение чаще, чем альтернативное. '
            'Исследователи показывали животным небольшой набор знакомых и незнакомых картинок '
            'и непосредственно записывали их ответы в одинаковых условиях. Этот результат относится '
            'к конкретной задаче с изображениями и ограниченной выборке; он не устанавливает общее '
            'понимание картинок в повседневной жизни.'))]
        value['claims'] = value['claims'][:1]
    return value

ABSTRACT = (
    "Domestic dogs were observed in a visual matching task using familiar and unfamiliar images. "
    "The study included 24 dogs whose responses were recorded under controlled conditions. "
    "The dogs selected the matching image more often than the alternative image. "
    "Each dog was shown the same small set of pictures in a consistent laboratory setting. "
    "The researchers recorded the animals' choices rather than asking owners to rate their ability. "
    "The task examined responses to a small set of images under controlled conditions. "
    "The observations describe performance in this task and do not establish a general understanding "
    "of images across everyday situations. The sample was limited and the findings may not generalize "
    "to other dogs or other visual materials. Additional studies would be needed to examine whether "
    "similar patterns occur with a broader range of pictures and observation settings. "
    "The results concern learning and visual choices rather than instructions for owners."
)


def source_record(number=999999991):
    return {"id": str(number), "source": "MED", "doi": f"10.9999/synthetic-dog-{number}",
            "title": "Dog learning in a visual matching task", "abstractText": ABSTRACT,
            "pubTypeList": {"pubType": ["Journal Article"]}, "publicationStatus": "epublish",
            "language": "eng", "authorString": "Synthetic Author", "pubYear": "2026",
            "journalInfo": {"journal": {"title": "Synthetic Test Journal"}}}


def material(number=999999991):
    return {"id": number, "title": source_record(number)["title"], "text": ABSTRACT,
            "url": f"https://europepmc.org/article/MED/{number}", "content_scope": "abstract",
            "raw_payload": {"provider": "europepmc", "record": source_record(number)}}


def draft(number=999999991):
    return {"about_dogs": True, "topic": "learning", "format": "research_explainer", "risk": "low",
            "headline": "Как собаки выбирали знакомое изображение в лабораторной задаче",
            "paragraphs": [
                {"kind": "result", "claim_ids": ["c1"], "text":
                 "В исследовании собаки участвовали в задаче на сопоставление изображений. Им показывали знакомые и незнакомые картинки, а исследователи записывали выбор животных в одинаковых лабораторных условиях. Совпадающее изображение собаки выбирали чаще, чем альтернативное. Авторы наблюдали ответы животных непосредственно, а не собирали только оценки владельцев. Такой подход описывает результат конкретной задачи с небольшим набором картинок. Он позволяет обсуждать наблюдавшиеся ответы, но не означает, что исследование охватило все способы восприятия изображений домашними собаками."},
                {"kind": "interpretation", "claim_ids": ["c2"], "text":
                 "Это наблюдение интересно как пример изучения обучения и зрительного выбора. Его смысл ограничен условиями, в которых проводилась работа. Маленький набор изображений и ограниченная выборка оставляют открытым вопрос о том, повторится ли похожий результат в другой обстановке. Полученные данные не превращают успешное выполнение задачи в общую оценку понимания картинок. Для более широких выводов авторам потребовались бы дополнительные исследования с другими изображениями и условиями наблюдения, а описанная работа показывает только один конкретный результат."}
            ],
            "limitations": "Разбор основан только на аннотации: полный текст не проверен. Выборка и набор изображений ограничены; перенос результата на других собак и бытовые ситуации пока не установлен.",
            "claims": [
                {"id": "c1", "text": "В контролируемой задаче собаки чаще выбирали совпадающее изображение.",
                 "quote": "The dogs selected the matching image more often than the alternative image.", "source_id": f"MED:{number}"},
                {"id": "c2", "text": "Результат ограничен задачей, набором изображений и выборкой.",
                 "quote": "The sample was limited and the findings may not generalize to other dogs or other visual materials.", "source_id": f"MED:{number}"}],
            "needs_full_text": False, "contains_practical_advice": False}


def review():
    return {"approved": True, "risk": "low", "about_dogs": True, "topic": "learning",
            "all_claims_supported": True, "no_practical_advice": True, "limitations_adequate": True,
            "no_overclaim": True, "source_sufficient": True, "owner_relevant": True, "plain_language": True, "reasons": []}
