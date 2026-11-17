"""Deterministic, bounded topic selection before paid drafting."""

from collections import Counter
from urllib.parse import urlsplit

from collector.sources import topic_hint

DAY = "(date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"


def distribution(conn):
    rows = conn.execute(f"""SELECT publication_payload->>'topic',
        coalesce(publication_payload->>'original_url',publication_payload->'citations'->0->>'url','')
        FROM findings WHERE publication_status='published' AND published_at>={DAY}
        AND published_at<{DAY}+interval '1 day'""").fetchall()
    return Counter(t for t, _ in rows), Counter(urlsplit(u).hostname for _, u in rows)


def rank_candidates(materials, topic_counts, publisher_counts):
    def rank(m):
        raw = m.get('raw_payload') or {}
        if not isinstance(raw, dict): raw = {}
        topic = raw.get('topic_hint') or topic_hint(m['title'], m.get('text') or '')
        publisher = urlsplit(m['url']).hostname
        n, p = topic_counts.get(topic, 0), publisher_counts.get(publisher, 0)
        ru = raw.get('source_language') == 'ru' or raw.get('provider') in ('rkf', 'pets')
        return (n >= 2 or p >= 2, n, p, not ru, -m['id'])
    return sorted(materials, key=rank)


def select_material(conn, fields):
    columns = ','.join(fields)
    rows = conn.execute(f"""SELECT {columns} FROM materials WHERE id IN (
        (SELECT id FROM materials WHERE status='collected' ORDER BY id DESC LIMIT 100)
        UNION (SELECT id FROM materials WHERE status='collected' ORDER BY id ASC LIMIT 20))
        AND status='collected'""").fetchall()
    topics, publishers = distribution(conn)
    for material in rank_candidates([dict(zip(fields, row)) for row in rows], topics, publishers):
        row = conn.execute(f"SELECT {columns} FROM materials WHERE id=%s AND status='collected' FOR UPDATE SKIP LOCKED",
                           (material['id'],)).fetchone()
        if row: return row
    return None
