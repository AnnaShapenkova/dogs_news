"""Durable provider payment pause, including legacy events. Preview by default.

python -m worker.provider
python -m worker.provider --resume

No provider requests or Telegram calls are made by this command. A running worker
may start paid processing and publication immediately after an explicit resume.
"""

import argparse
import json

import db

WORKER_LOCK = 4208
PROVIDER_LOCK = 4211
SERVICE = 'worker_provider_state'  # ledger category, not a separate service
LIMIT_SOURCES = {'openrouter_credits', 'openrouter_key_limit', 'openrouter_in_flight_budget'}
PAYMENT_REASONS = {'in_flight_budget_exhausted', 'weight_exceeds_budget',
                   'insufficient_credits', 'key_limit_exceeded'}


def safe_diagnostics(value):
    """Only known codes and numbers; arbitrary provider strings never reach the ledger."""
    output = {}
    if not isinstance(value, dict):
        return output
    if type(value.get('http_status')) is int and 100 <= value['http_status'] <= 599:
        output['http_status'] = value['http_status']
    if value.get('provider') == 'aitunnel':
        output['provider'] = 'aitunnel'
    if value.get('error_type') == 'insufficient_balance':
        output['error_type'] = 'insufficient_balance'
    for key, allowed in (('limit_source', LIMIT_SOURCES), ('payment_reason', PAYMENT_REASONS)):
        item = value.get(key)
        if isinstance(item, str) and item in allowed:
            output[key] = item
    retry = value.get('retry_after_seconds')
    if type(retry) is int and 0 <= retry <= 86400:
        output['retry_after_seconds'] = retry
    return output


def provider_status(conn):
    row = conn.execute('SELECT id,status,note FROM runs WHERE service=%s ORDER BY id DESC LIMIT 1',
                       (SERVICE,)).fetchone()
    if not row:
        return dict(paused=False, event_id=None)
    event_id, status, note = row
    try:
        value = json.loads(note)
        valid = (isinstance(value, dict) and type(value.get('version')) is int and value.get('version') == 1
                 and value.get('event') == status and status in ('paused', 'resumed'))
    except (ValueError, TypeError):
        valid = False
        value = {}
    if not valid:
        return dict(paused=True, event_id=event_id, state_unreadable=True)
    return dict(paused=status == 'paused', event_id=event_id,
                **safe_diagnostics(value.get('diagnostics')))


def pause_provider(conn, diagnostics):
    """Use the durable audit connection so a parent rollback cannot erase the pause."""
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (PROVIDER_LOCK,))
        state = provider_status(conn)
        if state['paused']:
            return state
        note = dict(version=1, event='paused', diagnostics=safe_diagnostics(diagnostics))
        conn.execute("INSERT INTO runs(service,status,note,finished_at) VALUES (%s,'paused',%s,clock_timestamp())",
                     (SERVICE, json.dumps(note)))
        return provider_status(conn)


def resume_provider(conn, apply=False):
    if not apply:
        return provider_status(conn)
    with conn.transaction():
        # Same order as the worker: worker lock, then provider-state lock.
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (WORKER_LOCK,))
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (PROVIDER_LOCK,))
        state = provider_status(conn)
        if not state['paused']:
            return dict(**state, changed=False)
        if state.get('state_unreadable'):
            raise RuntimeError('Provider history is unreadable; inspect before resuming')
        note = dict(version=1, event='resumed', previous_event_id=state['event_id'])
        conn.execute("INSERT INTO runs(service,status,note,finished_at) VALUES (%s,'resumed',%s,clock_timestamp())",
                     (SERVICE, json.dumps(note)))
        return dict(**provider_status(conn), changed=True)


def provider_text(state):
    if not state['paused']:
        return 'Паузы оплаты провайдера нет. Текущий провайдер: AITunnel.'
    source = 'AITunnel' if state.get('provider') == 'aitunnel' else 'провайдера (историческое событие до T18)'
    text = f'Обработка приостановлена: ограничение оплаты {source} (HTTP 402).'
    names = {'openrouter_credits': 'ограничение доступных средств или резерва запроса',
             'openrouter_key_limit': 'лимит API-ключа',
             'openrouter_in_flight_budget': 'резерв под текущие или недавно завершённые запросы'}
    if state.get('state_unreadable'):
        text = 'Обработка приостановлена: требуется проверка журнала провайдера.'
    elif state.get('limit_source') in names:
        text += ' Причина: ' + names[state['limit_source']] + '.'
    text += (' Проверьте баланс и лимит рабочего ключа; затем явно возобновите обработку '
             'в терминале worker: python -m worker.provider --resume. '
             'Сброс суточного бюджета и смена ключа автоматически паузу не снимают.')
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume', action='store_true', help='Explicitly clear the payment pause; budgets are preserved')
    args = parser.parse_args()
    with db.connect() as conn:
        state = resume_provider(conn, apply=args.resume)
    print(json.dumps(dict(mode='resume' if args.resume else 'preview', **state), ensure_ascii=False, indent=2))
    print(provider_text(state))


if __name__ == '__main__':
    main()
