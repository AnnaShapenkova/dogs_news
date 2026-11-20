"""AITunnel only. Never infer credentials or models from legacy environment names."""

import os

PROVIDER = 'aitunnel'
ENDPOINT = 'https://api.aitunnel.ru/v1/chat/completions'
DRAFT_MODEL = os.environ.get('DRAFT_MODEL', 'glm-4.7').strip()
REVIEW_MODEL = os.environ.get('REVIEW_MODEL', 'claude-haiku-5.5').strip()


def configured_models():
    for value in (DRAFT_MODEL, REVIEW_MODEL):
        if not value or len(value) > 200 or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError('Invalid DRAFT_MODEL/REVIEW_MODEL configuration')
    return dict(provider=PROVIDER, draft_model=DRAFT_MODEL, review_model=REVIEW_MODEL)


def api_key():
    return os.environ.get('AITUNNEL_API_KEY', '').strip()


def model_for(stage):
    configured_models()
    return {'draft': DRAFT_MODEL, 'review': REVIEW_MODEL}[stage]
