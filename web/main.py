"""Web: one page with the latest findings and the map of the system."""

import html
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
from publication import public_payload, safe_url

SERVICE = "web"

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ближе к собаке</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; background: #f6f5f1; color: #1a1a1a; }}
  main {{ max-width: 960px; margin: 0 auto; padding: 24px 16px; }}
  h1 {{ font-size: 28px; margin: 0 0 4px; }}
  .muted {{ color: #6b6b6b; font-size: 14px; }}
  .map {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 24px 0; }}
  .block {{ background: #fff; border: 1px solid #1a1a1a; padding: 12px 14px; min-width: 150px; }}
  .block b {{ display: block; font-size: 16px; }}
  .block span {{ font-size: 13px; color: #6b6b6b; }}
  .arrow {{ font-size: 22px; }}
  .stats {{ display: flex; gap: 24px; flex-wrap: wrap; margin: 8px 0 24px; font-size: 15px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; font-size: 14px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #e3e1da; vertical-align: top; }}
  th {{ font-weight: 600; background: #ecebe5; }}
  a {{ color: #1a1a1a; }}
  .tag {{ background: #e8f5c8; padding: 1px 6px; white-space: nowrap; }}
  article {{ background: #fff; padding: 20px; margin: 16px 0; border: 1px solid #e3e1da; }}
  article p {{ line-height: 1.65; }}
</style></head>
<body><main>
<h1>Ближе к собаке</h1>
<div class="muted">Проверяемые объяснения исследований о поведении, обучении и благополучии собак.</div>

<div class="map">
  <div class="block"><b>Сбор</b><span>collector: обходит источники</span></div><div class="arrow">&rarr;</div>
  <div class="block"><b>База</b><span>Postgres: хранит все</span></div><div class="arrow">&rarr;</div>
  <div class="block"><b>Обработка</b><span>worker: модель отбирает и пишет</span></div><div class="arrow">&rarr;</div>
  <div class="block"><b>Страница</b><span>web: показывает находки</span></div>
</div>

<div class="stats">
  <div>Источников: <b>{sources}</b></div>
  <div>Материалов: <b>{materials}</b></div>
  <div>Находок: <b>{findings}</b></div>
  <div>Последний сбор: <b>{last_run}</b></div>
</div>

{rows}
</main></body></html>"""

def article_html(summary, payload, url, published, source):
    article = public_payload(payload)
    out = [f'<article><p class="muted">{published:%d.%m.%Y} UTC · {html.escape(source)}</p>',
           f'<h2>{html.escape(summary)}</h2>']
    if article:
        out.append(f'<span class="tag">{html.escape(article["topic_label"])}</span>')
        if article.get('stage'):
            out.append(f'<span class="tag">{html.escape(article["format_label"])}</span>')
        if article.get('context_note'):
            out.append(f'<p class="muted">{html.escape(article["context_note"])}</p>')
        for paragraph in article['paragraphs']:
            kind = 'Результат исследования' if paragraph['kind'] == 'result' else 'Как это понимать'
            reference = html.escape(article['citations'][0]['url'], quote=True)
            out.append(f'<p class="muted">{kind}</p><p>{html.escape(paragraph["text"])} <a href="{reference}" target="_blank" rel="noopener noreferrer">[1]</a></p>')
        out.append(f'<h3>Что важно учитывать</h3><p>{html.escape(article["limitations"])}</p>')
        out.append('<p class="muted">Основано на аннотации, полный текст не проверен</p><h3>Научный источник</h3>')
        for citation in article['citations']:
            label = '. '.join(citation[key] for key in ('authors', 'title', 'journal', 'year') if citation[key])
            out.append(f'<p><a href="{html.escape(citation["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(label)}</a></p>')
    else:
        out.append('<p class="muted">Краткая запись прежнего формата</p>')
    if safe_url(url):
        out.append(f'<p><a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">Исходная запись</a></p>')
    return ''.join(out) + '</article>'


def render(conn):
    sources, materials, findings = conn.execute(
        "SELECT (SELECT count(*) FROM sources), (SELECT count(*) FROM materials),"
        " (SELECT count(*) FROM findings WHERE publication_status = 'published')"
    ).fetchone()
    last = conn.execute(
        "SELECT status, started_at FROM runs WHERE service = 'collector' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_run = f"{last[1]:%d.%m %H:%M} UTC, {last[0]}" if last else "еще не было"
    rows = conn.execute(
        """SELECT coalesce(f.published_at,f.created_at), s.name, f.publication_payload, f.summary, m.url
           FROM findings f
           JOIN materials m ON m.id = f.material_id
           JOIN sources s ON s.id = m.source_id
           WHERE f.publication_status = 'published'
           ORDER BY coalesce(f.published_at,f.created_at) DESC,f.id DESC LIMIT 100"""
    ).fetchall()
    rows_html = (
        "\n".join(
            article_html(summary, payload, url, published, source)
            for published, source, payload, summary, url in rows
        )
        or '<p>Находок пока нет — материалы появятся после проверки и допуска.</p>'
    )
    return PAGE.format(
        sources=sources,
        materials=materials,
        findings=findings,
        last_run=last_run,
        rows=rows_html,
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            # healthy only when the database answers too
            try:
                with db.connect() as conn:
                    conn.execute("SELECT 1")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            except Exception as error:
                db.log(SERVICE, f"проверка упала: {error}")
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"database unavailable")
            return
        try:
            with db.connect() as conn:
                body = render(conn).encode()
            self.send_response(200)
        except Exception as error:
            db.log(SERVICE, f"страница упала: {error}")
            body = "Страница временно недоступна".encode()
            self.send_response(500)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        db.log(SERVICE, f"запрос {self.command} {self.path}")


def main():
    with db.connect() as conn:
        db.init_schema(conn)
    port = int(os.environ.get("PORT", 8000))
    db.log(SERVICE, f"страница открыта на порту {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
