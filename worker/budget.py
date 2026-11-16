"""Read-only UTC budget reporting shared by worker and bot."""

MAX_PUBLICATIONS = 3
MAX_ATTEMPTS = 30
MAX_MODEL_CALLS = 30


class BudgetLimit(Exception):
    pass


def budget_status(conn):
    row = conn.execute("""
        WITH day AS (
          SELECT date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS start
        )
        SELECT
          (SELECT count(*) FROM findings,day WHERE publication_status='published'
            AND published_at>=day.start AND published_at<day.start+interval '1 day'),
          (SELECT count(*) FROM runs,day WHERE service='worker_attempt'
            AND started_at>=day.start AND started_at<day.start+interval '1 day'),
          (SELECT count(*) FROM runs,day WHERE service='worker_model_call'
            AND started_at>=day.start AND started_at<day.start+interval '1 day'),
          (SELECT start+interval '1 day' FROM day)
        """).fetchone()
    blocked = []
    for used, limit, name in zip(row[:3], (MAX_PUBLICATIONS, MAX_ATTEMPTS, MAX_MODEL_CALLS),
                                  ('publication_daily_limit', 'attempt_daily_limit', 'model_daily_limit')):
        if used >= limit:
            blocked.append(name)
    if MAX_MODEL_CALLS - row[2] == 1:
        blocked.append('stage_model_budget')
    return dict(publications=row[0], attempts=row[1], model_calls=row[2], reset_at=row[3], blocked=blocked)


def budget_text(state):
    from datetime import timezone
    reset = state['reset_at'].astimezone(timezone.utc).strftime('%d.%m %H:%M UTC')
    text = (f"Бюджет суток UTC: публикации {state['publications']}/{MAX_PUBLICATIONS}; "
            f"попытки {state['attempts']}/{MAX_ATTEMPTS}; "
            f"вызовы модели {state['model_calls']}/{MAX_MODEL_CALLS}. Обновление: {reset}.")
    names = {'publication_daily_limit': 'публикаций', 'attempt_daily_limit': 'попыток',
             'model_daily_limit': 'вызовов модели', 'stage_model_budget': 'пары вызовов для новой ступени'}
    if state['blocked']:
        text += ' Обработка остановлена: исчерпан суточный лимит ' + ', '.join(names[k] for k in state['blocked']) + '.'
    return text
