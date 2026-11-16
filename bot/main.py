"""Bot: Telegram bot and a Mini App with the findings.

- remembers everyone who pressed Start (table subscribers);
- after each collection is sorted by the worker, sends one summary to subscribers and marks findings.sent_at;
- answers /start and /status;
- serves the Mini App page on PORT, the bot opens it inside Telegram.
"""

import html
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import db
from publication import public_payload, safe_url, TOPICS

SERVICE = "bot"
HERE = Path(__file__).resolve().parent
SEND_EVERY_SECONDS = 60
LATEST = 5  # findings listed in the summary, the rest are in the feed

COMMANDS = [
    {"command": "start", "description": "подписаться на находки"},
    {"command": "status", "description": "что было в последних прогонах"},
]


def setting(name):
    return os.environ.get(name, "").strip()


def page_url():
    """Address of the Mini App: set by hand or taken from the platform (Railway, Coolify)."""
    url = (
        setting("MINI_APP_URL")
        or setting("RAILWAY_PUBLIC_DOMAIN")
        or setting("COOLIFY_URL")
    )
    if not url:
        return ""
    url = url.split(",")[0].strip()  # Coolify lists several domains separated by commas
    if not url.startswith("http"):
        url = f"https://{url}"
    return url.rstrip("/") + "/"


# ---------- Telegram ----------


def api(method, payload=None, timeout=20):
    base = setting("TELEGRAM_API") or "https://api.telegram.org"
    request = urllib.request.Request(
        f"{base}/bot{setting('TELEGRAM_TOKEN')}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())["result"]


def say(chat_id, text, button=True):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if button and page_url():
        payload["reply_markup"] = {
            "inline_keyboard": [
                [{"text": "Открыть ленту", "web_app": {"url": page_url()}}]
            ]
        }
    api("sendMessage", payload)


def finding_lines(rows):
    lines = []
    for row in rows:
        summary, entity, url, source = row[:4]
        article = public_payload(row[4]) if len(row) > 4 else {}
        context = article.get('context_note', '')
        link = safe_url(url)
        heading = f'<a href="{html.escape(link)}">{html.escape(summary)}</a>' if link else html.escape(summary)
        line = f'• {heading}\n  {html.escape(source)}'
        if entity:
            line += f" · {html.escape(entity)}"
        if context:
            line += '\n  ' + html.escape(context)
        if article.get('stage'):
            line += '\n  ' + html.escape(article['format_label'])
        lines.append(line)
    return lines


def setup(conn):
    """Menu commands, the permanent page button, and a starting point for sending."""
    api("setMyCommands", {"commands": COMMANDS})
    if page_url():
        api(
            "setChatMenuButton",
            {
                "menu_button": {
                    "type": "web_app",
                    "text": "Лента",
                    "web_app": {"url": page_url()},
                }
            },
        )
    # before the first subscriber, old findings count as sent: the bot pushes only what comes later
    if not conn.execute("SELECT 1 FROM subscribers LIMIT 1").fetchone():
        marked = conn.execute(
            "UPDATE findings SET sent_at = now()"
            " WHERE sent_at IS NULL AND publication_status = 'published'"
        ).rowcount
        if marked:
            db.log(
                SERVICE,
                f"подписчиков нет, {marked} старых находок лежат в ленте, рассылать их не буду",
            )
    me = api("getMe")
    db.log(SERVICE, f"@{me['username']} на связи, лента {page_url() or 'без адреса'}")


def send_new(conn):
    """One message per collection: waits until the worker has sorted it, then sends a summary to every subscriber."""
    # Unprocessed/held inputs and the legacy AI queue must not block published content.
    busy = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM runs WHERE service = 'worker' AND status IN ('started', 'running')"
        "            AND started_at > now() - interval '30 minutes')"
    ).fetchone()[0]
    if busy:
        return
    subscribers = [row[0] for row in conn.execute("SELECT chat_id FROM subscribers")]
    ids = [
        row[0] for row in conn.execute(
            "SELECT id FROM findings WHERE sent_at IS NULL AND publication_status = 'published'"
        )
    ]
    if not subscribers or not ids:
        return
    latest = conn.execute(
        "SELECT f.summary, f.publication_payload->>'topic', coalesce(f.publication_payload->'citations'->0->>'url',m.url), s.name, f.publication_payload FROM findings f"
        " JOIN materials m ON m.id = f.material_id JOIN sources s ON s.id = m.source_id"
        " WHERE f.id = ANY(%s) AND f.publication_status = 'published' ORDER BY f.id DESC LIMIT %s",
        (ids, LATEST),
    ).fetchall()
    latest = [(summary, TOPICS.get(topic, ''), url, source, payload) for summary, topic, url, source, payload in latest]
    text = f"<b>Новых находок: {len(ids)}</b>\nБлиже к собаке"
    text += "\n\nПоследние публикации:\n\n" + "\n\n".join(finding_lines(latest))
    if len(ids) > LATEST:
        text += f"\n\nЕще {len(ids) - LATEST} в ленте."
    run_id = db.start_run(conn, SERVICE)
    delivered = 0
    for chat_id in subscribers:
        try:
            say(chat_id, text)
            delivered += 1
        except urllib.error.HTTPError as error:
            if error.code == 403:  # the person blocked the bot
                conn.execute("DELETE FROM subscribers WHERE chat_id = %s", (chat_id,))
            db.log(SERVICE, f"чат {chat_id}: не доставлено, ответ {error.code}")
    conn.execute(
        "UPDATE findings SET sent_at = now() WHERE id = ANY(%s) AND publication_status = 'published'",
        (ids,),
    )
    db.finish_run(conn, SERVICE, run_id, len(ids), f"сводка по {len(ids)} находкам, получили {delivered} из {len(subscribers)}")


def status_text(conn):
    lines = []
    for service, name in (
        ("collector", "Сбор"),
        ("worker", "Разбор"),
        ("bot", "Рассылка"),
    ):
        run = conn.execute(
            "SELECT started_at, status, note FROM runs WHERE service = %s ORDER BY id DESC LIMIT 1",
            (service,),
        ).fetchone()
        if run:
            state = {"finished": "завершен", "failed": "упал"}.get(run[1], "идет")
            lines.append(
                f"{name}: {run[0].astimezone(timezone.utc):%d.%m %H:%M} UTC, {state}. {html.escape(run[2] or '')}"
            )
        else:
            lines.append(f"{name}: еще не было")
    findings, subscribers = conn.execute(
        "SELECT (SELECT count(*) FROM findings WHERE publication_status = 'published'),"
        " (SELECT count(*) FROM subscribers)"
    ).fetchone()
    lines.append(f"\nВсего находок {findings}, подписчиков {subscribers}.")
    states = dict(conn.execute("SELECT status,count(*) FROM materials GROUP BY status").fetchall())
    lines.append(f"Ожидают проверки: {states.get('collected', 0)}; удержаны: {states.get('held', 0)}; ошибки: {states.get('error', 0)}.")
    reasons = conn.execute(
        "SELECT editorial_result->>'reason',count(*) FROM materials WHERE status IN ('held','error')"
        " AND editorial_result IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC LIMIT 8"
    ).fetchall()
    reason_names = {
        'insufficient_content': 'недостаточно содержания', 'unlinked_secondary': 'нет явного первоисточника',
        'ambiguous_source': 'неоднозначный источник', 'risk_requires_review': 'риск требует проверки',
        'review_not_approved': 'проверка текста не пройдена', 'source_unavailable': 'источник недоступен',
        'unverified_claim': 'вывод не подтверждён фрагментом', 'publication_requires_review': 'статус работы требует проверки',
        'outside_pilot': 'тема вне пилота', 'duplicate_evidence': 'исследование уже опубликовано',
        'model_unavailable_or_invalid_json': 'ошибка ответа модели', 'source_changed': 'содержание источника изменилось',
    }
    reason_names.update({
        'evergreen_as_news': 'давнее исследование представлено новой новостью',
        'invalid_model_contract': 'ответ модели нарушает формат данных',
        'model_invalid_contract': 'модель вернула не объект JSON',
        'model_http_error': 'ошибка HTTP при обращении к модели',
        'model_connection_error': 'нет соединения с моделью',
        'model_timeout': 'истекло время ожидания модели',
        'model_invalid_envelope': 'неверная структура ответа провайдера',
        'model_empty_content': 'модель вернула пустой текст',
        'model_truncated': 'ответ модели оборван по лимиту длины',
        'model_incomplete': 'генерация не завершена обычным образом',
        'model_invalid_json': 'текст модели не является допустимым JSON',
        'model_response_too_large': 'превышен размер ответа модели',
    })
    for reason, count in reasons:
        lines.append(f"• {reason_names.get(reason, 'нужна дополнительная проверка')}: {count}")
    from worker.budget import budget_status, budget_text
    lines.append(budget_text(budget_status(conn)))
    from worker.provider import provider_status, provider_text
    lines.append(provider_text(provider_status(conn)))
    from worker.editorial import RULES_VERSION
    decisions = conn.execute("""
        SELECT coalesce(m.editorial_result->>'stage','0'),m.status,
               m.editorial_result->>'reason',m.editorial_result->>'phase',count(*)
        FROM materials m JOIN runs r ON r.id::text=m.editorial_result->>'attempt_id'
        WHERE r.service='worker_attempt' AND m.editorial_result->>'rules_version'=%s
          AND r.started_at >= (date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
          AND r.started_at < (date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')+interval '1 day'
        GROUP BY 1,2,3,4 ORDER BY 1,2,3,4
    """, (RULES_VERSION,)).fetchall()
    lines.append('Последние решения текущей версии за сутки UTC:')
    labels = {'0': 'Источник', '1': 'Разбор', '2': 'Короткое объяснение', '3': 'Научная заметка'}
    phases = {'source': 'источник', 'generation': 'генерация', 'draft_check': 'проверка черновика',
              'review': 'рецензия', 'publication': 'публикация'}
    outcomes = {'done': 'опубликовано', 'held': 'удержано', 'error': 'ошибка', 'collected': 'отложено'}
    details = {'published': 'допущено', 'all_stages_held': 'не прошли все три ступени',
               'uncertain_stage': 'неизвестен результат прежнего запроса',
               'stage_model_budget': 'нужны два свободных вызова',
               'model_daily_limit': 'исчерпан бюджет модели', 'input_changed': 'исходник изменился',
               'invalid_stage_claim_count': 'неверное число выводов',
               'unsupported_interpretation': 'для формата нужна только информация о результате'}
    for stage, state, reason, phase, count in decisions:
        text = details.get(reason, reason_names.get(reason, 'требуется проверка'))
        lines.append(f"• {labels.get(stage, 'Материал')} / {phases.get(phase, 'проверка')}: "
                     f"{outcomes.get(state, 'проверяется')} {count} — {text}.")
    if not decisions:
        lines.append('Завершённых решений этой версии сегодня пока нет.')
    return "\n".join(lines)


def handle(conn, message):
    chat = message["chat"]
    if chat.get("type") != "private":
        return
    words = (message.get("text") or "").split()
    command = words[0].split("@")[0] if words else ""
    if command == "/start":
        name = chat.get("username") or chat.get("first_name") or ""
        conn.execute(
            "INSERT INTO subscribers (chat_id, name) VALUES (%s, %s) ON CONFLICT (chat_id) DO NOTHING",
            (chat["id"], name),
        )
        latest = conn.execute(
            "SELECT f.summary, f.publication_payload->>'topic', coalesce(f.publication_payload->'citations'->0->>'url',m.url), s.name, f.publication_payload FROM findings f"
            " JOIN materials m ON m.id = f.material_id JOIN sources s ON s.id = m.source_id"
            " WHERE f.publication_status = 'published' ORDER BY f.id DESC LIMIT 5"
        ).fetchall()
        latest = [(summary, TOPICS.get(topic, ''), url, source, payload) for summary, topic, url, source, payload in latest]
        text = "Вы подписаны на «Ближе к собаке». Здесь будут проверенные объяснения исследований о поведении, обучении и благополучии собак."
        if latest:
            text += "\n\nПоследние:\n\n" + "\n\n".join(finding_lines(latest))
        text += "\n\nВсе находки в ленте: кнопка ниже или «Лента» рядом с полем ввода. /status покажет, как работают блоки."
        say(chat["id"], text)
        db.log(SERVICE, f"новый подписчик {name or chat['id']}")
    elif command == "/status":
        say(chat["id"], status_text(conn))
    else:
        say(chat["id"], "Я понимаю /start и /status. Находки в ленте:")


def poll_forever():
    """Long polling: asks Telegram for new messages and answers them."""
    offset = None
    while True:
        try:
            updates = api(
                "getUpdates",
                {"timeout": 30, "offset": offset, "allowed_updates": ["message"]},
                timeout=40,
            )
            if updates:
                with db.connect() as conn:
                    for update in updates:
                        offset = update["update_id"] + 1
                        if "message" in update:
                            handle(conn, update["message"])
        except urllib.error.HTTPError as error:
            if error.code == 409:
                # Telegram gives updates to one program at a time
                db.log(SERVICE, "409: этот токен уже слушает другая программа, бот ждет, пока она остановится")
            else:
                db.log(SERVICE, f"ошибка связи с Telegram: ответ {error.code}")
            time.sleep(5)
        except Exception as error:
            db.log(SERVICE, f"ошибка связи с Telegram: {error}")
            time.sleep(5)


def send_forever():
    while True:
        try:
            with db.connect() as conn:
                send_new(conn)
        except Exception as error:
            db.log(SERVICE, f"рассылка не прошла: {error}")
        time.sleep(SEND_EVERY_SECONDS)


# ---------- Mini App page ----------


def iso(moment):
    return (
        moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if moment
        else None
    )


def page_data(conn):
    findings = []
    for row in conn.execute(
        "SELECT f.id,f.summary,f.publication_payload,coalesce(f.published_at,f.created_at),"
        " m.title,m.url,m.published_at,m.run_id,s.name"
        " FROM findings f JOIN materials m ON m.id=f.material_id JOIN sources s ON s.id=m.source_id"
        " WHERE f.publication_status='published' ORDER BY coalesce(f.published_at,f.created_at) DESC,f.id DESC LIMIT 300"
    ):
        fid, summary, payload, published, title, url, source_date, run_id, source = row
        article = public_payload(payload)
        findings.append({"id": fid, "summary": summary, "date": iso(published),
                         "title": title, "url": safe_url(url), "source_date": iso(source_date),
                         "run_id": run_id, "source": source, "article": article,
                         "reason": article.get("topic_label", ""), "topic": article.get("topic", "")})

    run = conn.execute(
        "SELECT id, finished_at, status, items FROM runs WHERE service = 'collector' AND status <> 'started'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    run_info, sources = None, []
    if run:
        statuses = dict(
            conn.execute(
                "SELECT status, count(*) FROM materials WHERE run_id = %s GROUP BY status",
                (run[0],),
            ).fetchall()
        )
        added = sum(statuses.values())
        kept = conn.execute(
            "SELECT count(*) FROM findings f JOIN materials m ON m.id = f.material_id"
            " WHERE m.run_id = %s AND f.publication_status = 'published'",
            (run[0],),
        ).fetchone()[0]
        sent = conn.execute(
            "SELECT count(*) FROM findings f JOIN materials m ON m.id = f.material_id"
            " WHERE m.run_id = %s AND f.sent_at IS NOT NULL AND f.publication_status = 'published'",
            (run[0],),
        ).fetchone()[0]
        run_info = {
            "id": run[0],
            "finished_at": iso(run[1]),
            "status": run[2],
            "added": added,
            "processed": statuses.get("done", 0) + statuses.get("held", 0) + statuses.get("error", 0)
            + statuses.get("skipped", 0) + statuses.get("failed", 0),
            "kept": kept,
            "skipped": statuses.get("skipped", 0),
            "failed": statuses.get("failed", 0),
            "waiting": statuses.get("collected", 0),
            "held": statuses.get("held", 0),
            "errors": statuses.get("error", 0),
            "sent": sent,
        }
        sources = [
            {"name": name, "new": new}
            for name, new in conn.execute(
                "SELECT s.name, count(m.id) FROM sources s"
                " LEFT JOIN materials m ON m.source_id = s.id AND m.run_id = %s WHERE s.active GROUP BY s.id ORDER BY s.id",
                (run[0],),
            )
        ]
    totals = dict(
        zip(
            ("sources", "materials", "findings", "runs", "subscribers"),
            conn.execute(
                "SELECT (SELECT count(*) FROM sources WHERE active), (SELECT count(*) FROM materials),"
                " (SELECT count(*) FROM findings WHERE publication_status = 'published'),"
                " (SELECT count(*) FROM runs), (SELECT count(*) FROM subscribers)"
            ).fetchone(),
        )
    )
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "totals": totals,
        "run": run_info,
        "sources": sources,
        "findings": findings,
    }


def render_page(conn):
    page = (
        (HERE / "template.html")
        .read_text(encoding="utf-8")
        .replace("/*BRAND*/", (HERE / "brand.css").read_text(encoding="utf-8"))
    )
    payload = json.dumps(page_data(conn), ensure_ascii=False).replace("</", "<\\/")
    return page.replace("/*DATA*/null/*END*/", payload)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            with db.connect() as conn:
                if path == "/health":
                    conn.execute("SELECT 1")
                    body, status, kind = b"ok", 200, "text/plain"
                elif path == "/":
                    body, status, kind = (
                        render_page(conn).encode("utf-8"),
                        200,
                        "text/html; charset=utf-8",
                    )
                else:
                    body, status, kind = b"not found", 404, "text/plain"
        except Exception as error:
            db.log(SERVICE, f"страница не собралась: {error}")
            body, status, kind = b"database unavailable", 503, "text/plain"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    with db.connect() as conn:
        db.init_schema(conn)
        if setting("TELEGRAM_TOKEN"):
            try:
                setup(conn)
            except Exception as error:
                db.log(SERVICE, f"не настроил бота: {error}")
    if setting("TELEGRAM_TOKEN"):
        threading.Thread(target=poll_forever, daemon=True).start()
        threading.Thread(target=send_forever, daemon=True).start()
    else:
        db.log(
            SERVICE,
            "переменная TELEGRAM_TOKEN пустая: бот молчит, работает только лента",
        )
    port = int(os.environ.get("PORT", "8080"))
    db.log(SERVICE, f"лента открыта на порту {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
