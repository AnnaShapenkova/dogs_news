"""Explicit, bounded requeue; preview by default. Never calls providers or Telegram.

python -m worker.requeue --ids 1 2
python -m worker.requeue --ids 1 2 --apply
"""

import argparse
import json

from psycopg.types.json import Jsonb

import db
from worker import editorial as rules
from worker.main import WORKER_LOCK

RETRY_REASONS = {
    'all_stages_held', 'evergreen_as_news',
    'risk_requires_review', 'review_not_approved', 'draft_not_eligible',
    'invalid_model_contract', 'invalid_article_length', 'unverified_claim',
    'model_unavailable_or_invalid_json', 'source_unavailable',
    'model_http_error', 'model_connection_error', 'model_timeout',
    'model_invalid_envelope', 'model_empty_content', 'model_truncated',
    'model_incomplete', 'model_invalid_json', 'model_invalid_contract',
    'model_response_too_large',
    'unsupported_claim', 'invalid_review_contract', 'unexpected_advice', 'readability_failed', 'overclaim',
}


def validate_ids(ids):
    if not ids or len(ids) > 15 or any(type(mid) is not int or mid <= 0 for mid in ids):
        raise ValueError('Choose 1 to 15 positive material IDs')
    if len(set(ids)) != len(ids):
        raise ValueError('Repeated material IDs are not allowed')


def preview(conn, ids, lock=False):
    validate_ids(ids)
    rows = conn.execute(
        'SELECT id,title,status,editorial_result,raw_payload FROM materials WHERE id=ANY(%s) ORDER BY id'
        + (' FOR UPDATE' if lock else ''), (ids,),
    ).fetchall()
    previous = set()
    for (note,) in conn.execute("SELECT note FROM runs WHERE service='worker_requeue'").fetchall():
        try:
            entry = json.loads(note)
            previous.add((entry['material_id'], entry['rules_version']))
        except (ValueError, TypeError, KeyError):
            # Unknown history must not be bypassed by creating another reservation.
            raise RuntimeError('Invalid requeue history; inspect it before retrying') from None
    published_ids = {row[0] for row in conn.execute(
        "SELECT material_id FROM findings WHERE publication_status='published' AND material_id=ANY(%s)", (ids,)).fetchall()}
    published_keys = {row[0] for row in conn.execute(
        "SELECT publication_payload->>'evidence_key' FROM findings WHERE publication_status='published'"
    ).fetchall()}
    output = []
    for mid, title, status, audit, payload in rows:
        audit = audit if isinstance(audit, dict) else {}
        reason = 'eligible'
        if mid in published_ids or status not in ('held', 'error'):
            reason = 'not_held_or_error'
        elif (mid, rules.RULES_VERSION) in previous:
            reason = 'already_requeued_for_version'
        elif audit.get('rules_version') == rules.RULES_VERSION:
            reason = 'already_checked_with_current_rules'
        elif audit.get('reason') not in RETRY_REASONS:
            reason = 'reason_not_retryable'
        else:
            record = payload.get('record') if isinstance(payload, dict) else None
            if not isinstance(record, dict) or payload.get('provider') != 'europepmc':
                reason = 'research_snapshot_required'
            else:
                types = record.get('pubTypeList')
                types = types.get('pubType') if isinstance(types, dict) else None
                if (not isinstance(types, list) or not types or any(not isinstance(t, str) for t in types)
                        or any(word in ' '.join(types).lower() for word in
                               ('retract', 'erratum', 'correct', 'preprint', 'withdraw', 'concern', 'review', 'meta-analysis'))
                        or record.get('commentCorrectionList')):
                    reason = 'publication_requires_review'
                else:
                    try:
                        doi = rules.doi_value(record.get('doi'))
                        key = 'doi:' + doi if doi else 'MED:' + str(record.get('id'))
                        if key in published_keys:
                            reason = 'duplicate_evidence'
                    except rules.Hold:
                        reason = 'invalid_source_identity'
        output.append(dict(id=mid, title=title, status=status, previous_reason=audit.get('reason'),
                           eligible=reason == 'eligible', reason=reason))
    missing = set(ids) - {r['id'] for r in output}
    output.extend(dict(id=mid, eligible=False, reason='not_found') for mid in sorted(missing))
    return output


def requeue(conn, ids, apply=False):
    if not apply:
        return preview(conn, ids)
    with conn.transaction():
        # Same key as the worker session lock: no requeue while publishing.
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (WORKER_LOCK,))
        results = preview(conn, ids, lock=True)
        if not all(row['eligible'] for row in results):
            return results  # all-or-nothing, no partial update
        for row in results:
            mid = row['id']
            old = conn.execute('SELECT editorial_result FROM materials WHERE id=%s', (mid,)).fetchone()[0]
            note = dict(material_id=mid, rules_version=rules.RULES_VERSION, previous_result=old)
            conn.execute("INSERT INTO runs(service,status,items,note,finished_at) VALUES ('worker_requeue','finished',1,%s,clock_timestamp())",
                         (json.dumps(note, ensure_ascii=False),))
            conn.execute("UPDATE materials SET status='collected',editorial_result=%s WHERE id=%s",
                         (Jsonb(dict(version=1, outcome='requeued', target_rules_version=rules.RULES_VERSION,
                                     previous_reason=row['previous_reason'])), mid))
            row['requeued'] = True
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ids', type=int, nargs='+', required=True)
    parser.add_argument('--apply', action='store_true', help='Explicitly change the selected rows')
    args = parser.parse_args()
    validate_ids(args.ids)
    with db.connect() as conn:
        print(json.dumps(dict(mode='apply' if args.apply else 'preview',
                              materials=requeue(conn, args.ids, args.apply)), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
