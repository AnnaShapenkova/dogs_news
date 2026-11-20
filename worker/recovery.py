"""Conservative migration of known legacy drafts; never replay an unknown paid call."""

from copy import deepcopy

from worker import editorial as rules
from worker.config import configured_models

LEGACY_RULES = ('dogs-pilot-v5', 'dogs-pilot-v6', 'dogs-pilot-v7')


def migrate(previous, calls, prompt_version):
    entries = previous.get('stages', [])
    if (not isinstance(entries, list) or len(entries) > 3
            or any(not isinstance(e, dict) or type(e.get('stage')) is not int
                   or e['stage'] != i + 1 for i, e in enumerate(entries))):
        raise rules.Hold('uncertain_stage')
    by_stage = {e.get('stage'): e for e in entries}
    if len(by_stage) != len(entries):
        raise rules.Hold('uncertain_stage')
    for _, call in calls:
        entry = by_stage.get(call.get('format_stage'))
        phase = call.get('phase')
        if entry is None or phase not in ('draft', 'review'):
            raise rules.Hold('uncertain_stage')
        # Finished transport alone does not prove the paid response was checkpointed.
        known = (isinstance(entry.get('response'), dict) or isinstance(entry.get('draft'), dict)) if phase == 'draft' else isinstance(entry.get('review'), dict)
        if not known:
            raise rules.Hold('uncertain_stage')
    for entry in entries:
        if (entry.get('reason') == 'uncertain_stage'
                or entry.get('outcome') not in ('pending', 'response_ready', 'draft_ready', 'review_ready', 'approved', 'rejected')):
            raise rules.Hold('uncertain_stage')
    result = dict(input_hash=previous.get('input_hash'), prompt_version=prompt_version,
                  model_config=configured_models(), migration_history=deepcopy(previous),
                  next_stage=1, stages=[], stage=0)
    if not entries:
        return result
    entry = entries[-1]
    if entry.get('outcome') == 'rejected':
        raise rules.Hold('legacy_checkpoint_incompatible')
    draft = entry.get('draft')
    response = entry.get('response')
    fit = previous.get('editorial_fit')
    if draft is None and isinstance(response, dict):
        if set(response) == {'editorial_fit', 'article'}:
            fit, draft = response['editorial_fit'], response['article']
        else:
            draft = response
    if not isinstance(draft, dict) or not isinstance(fit, dict):
        raise rules.Hold('legacy_checkpoint_incompatible')
    origin = dict(provider='openrouter', model=previous.get('model'))
    if not isinstance(origin['model'], str) or not origin['model']:
        raise rules.Hold('legacy_checkpoint_incompatible')
    # run_stages validates this fit/draft against freshly fetched evidence before review.
    result.update(editorial_fit=deepcopy(fit), stages=[dict(stage=1, outcome='draft_ready',
                  draft=deepcopy(draft), draft_origin=origin)])
    return result
