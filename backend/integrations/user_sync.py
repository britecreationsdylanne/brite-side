"""Sync employees from BigQuery user_master into The BriteSide (Firestore).

Adapted from the rewards / brite-shadow user sync, trimmed to this app's schema
(name / email / title / department / birthday / photo / active).

Non-destructive sync rules:
- New people in BigQuery are inserted with source='sync', active=True.
- Existing people are updated only where BigQuery has a non-null value.
- Birthdays are BACKFILLED ONLY when missing: BriteSide's hand-curated
  birthday_month/day (the crown jewels — not authoritative in BigQuery) are
  NEVER overwritten. A stored month of 0/None counts as "missing" and gets
  filled from user_master.birth_month/birth_day.
- `active` is NEVER auto-flipped False -> True: an editor-deactivated person
  stays deactivated even if BigQuery still lists them as eligible.
- Terminated / ineligible people are deactivated, but all deactivations are
  deferred behind a circuit breaker: if a single run would deactivate more than
  SYNC_MAX_DEACTIVATION_PCT of currently-active people, the run raises
  SyncSafetyTripped (after a best-effort alert) and applies NO status changes.
- Profile photos are cached from the thumbnail URL to the public media bucket.

google.cloud libraries are imported lazily inside run() so the app boots
locally without them / without GCP credentials.

NOTE: user_master lives in the `britecreations` project. If BriteSide deploys
to a different GCP project, its service account needs cross-project BigQuery
read on that dataset (same grant rewards' SA already has).
"""
from __future__ import annotations

import os

import requests as http_requests

BQ_PROJECT = os.environ.get('BQ_PROJECT', 'britecreations')
BQ_DATASET = os.environ.get('BQ_DATASET', 'Security_Collection')
BQ_USER_TABLE = 'user_master'
BQ_TERMINATED_TABLE = 'terminated'

SYNC_MAX_DEACTIVATION_PCT = float(os.environ.get('SYNC_MAX_DEACTIVATION_PCT', '25'))

PHOTO_PREFIX = 'user-photos'


class SyncSafetyTripped(Exception):
    """Raised when a sync run would deactivate too many people at once.

    No status changes are applied when this fires; the run aborts cleanly so an
    editor can investigate before anyone loses access."""


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _fetch_user_master():
    """Query eligible @brite.co people from BigQuery user_master."""
    from google.cloud import bigquery

    client = bigquery.Client(project=BQ_PROJECT)
    query = f"""
        SELECT email, name, title, department, birth_month, birth_day, thumbnail, eligible
        FROM `{BQ_PROJECT}.{BQ_DATASET}.{BQ_USER_TABLE}`
        WHERE email IS NOT NULL
          AND LOWER(email) LIKE '%@brite.co'
          AND LOWER(name) NOT LIKE '%util acct%'
          AND (title IS NULL OR LOWER(title) NOT LIKE '%util acct%')
          AND building_id IS NOT NULL AND TRIM(building_id) != ''
    """
    rows = client.query(query).result()
    return [dict(row) for row in rows]


def _fetch_terminated_emails():
    """Get the set of terminated employee emails."""
    from google.cloud import bigquery

    client = bigquery.Client(project=BQ_PROJECT)
    query = f"""
        SELECT DISTINCT email
        FROM `{BQ_PROJECT}.{BQ_DATASET}.{BQ_TERMINATED_TABLE}`
        WHERE email IS NOT NULL
    """
    rows = client.query(query).result()
    return {row['email'].lower() for row in rows if row['email']}


def _cache_photo(gcs_client, media_bucket, email, thumbnail_url):
    """Cache a thumbnail to the public media bucket. Returns the public URL when
    the photo is in the cache (newly uploaded or already there), else ''. Never
    raises."""
    if not (gcs_client and media_bucket and thumbnail_url):
        return ''
    blob_name = f"{PHOTO_PREFIX}/{email}.jpg"
    public_url = f"https://storage.googleapis.com/{media_bucket}/{blob_name}"
    try:
        bucket = gcs_client.bucket(media_bucket)
        blob = bucket.blob(blob_name)
        if blob.exists():
            return public_url
        resp = http_requests.get(thumbnail_url, timeout=10, allow_redirects=True)
        if resp.status_code != 200 or not resp.content:
            return ''
        blob.upload_from_string(resp.content, content_type='image/jpeg')
        return public_url
    except Exception as exc:
        print(f"[SYNC] Failed to cache photo for {email}: {exc}")
        return ''


def run(db, gcs_client=None, media_bucket=None, collection='employees',
        triggered_by='scheduler', alert_fn=None, now_iso=None):
    """Run the full employee sync from BigQuery into Firestore.

    Args:
      db: the Firestore client.
      gcs_client / media_bucket: optional, for caching profile photos.
      collection: the Firestore employees collection name.
      triggered_by: label for logging.
      alert_fn: optional callable(message) for the mass-deactivation alert.
      now_iso: ISO timestamp string to stamp synced_at with (caller supplies it
        so this module stays free of wall-clock calls).

    Returns a summary dict. Raises SyncSafetyTripped when the deactivation
    circuit breaker fires (creates/updates/backfills are committed first)."""
    print(f"[SYNC] Starting BigQuery employee sync (triggered by {triggered_by})...")

    bq_users = _fetch_user_master()
    terminated_emails = _fetch_terminated_emails()

    col = db.collection(collection)
    existing = {}
    for doc in col.stream():
        d = doc.to_dict() or {}
        key = (d.get('email') or doc.id).strip().lower()
        existing[key] = d

    # Treat a missing `active` (legacy docs) as active.
    active_count = sum(1 for d in existing.values() if d.get('active', True))

    pending_deactivations = []  # emails to deactivate, gated by the safety check
    created = updated = birthdays_filled = photos_cached = 0

    def _commit(email, data):
        col.document(email).set(data, merge=True)

    for row in bq_users:
        email = (row.get('email') or '').strip().lower()
        if not email:
            continue

        bq_name = (row.get('name') or '').strip() or None
        bq_title = (row.get('title') or '').strip() or None
        bq_department = (row.get('department') or '').strip() or None
        bq_month = _as_int(row.get('birth_month'))
        bq_day = _as_int(row.get('birth_day'))
        bq_thumb = row.get('thumbnail')
        bq_eligible = row.get('eligible')

        is_terminated = email in terminated_emails
        should_be_active = not (is_terminated or bq_eligible is False)

        current = existing.get(email)
        if current is not None:
            patch = {}
            if bq_name and bq_name != current.get('name'):
                patch['name'] = bq_name
            if bq_title and bq_title != current.get('title'):
                patch['title'] = bq_title
            if (bq_department and bq_department != current.get('department')
                    and not current.get('department_locked')):
                patch['department'] = bq_department

            # Birthday: BACKFILL ONLY. Never overwrite a curated birthday.
            has_birthday = _as_int(current.get('birthday_month')) > 0
            if not has_birthday and bq_month and bq_day:
                patch['birthday_month'] = bq_month
                patch['birthday_day'] = bq_day
                birthdays_filled += 1

            # Photo: cache once.
            if not current.get('photo_cached'):
                url = _cache_photo(gcs_client, media_bucket, email, bq_thumb)
                if url:
                    patch['photo_url'] = url
                    patch['photo_cached'] = True
                    photos_cached += 1

            if patch:
                patch['synced_at'] = now_iso
                _commit(email, patch)
                updated += 1

            # Deactivation is deferred behind the safety check below. Never
            # auto-reactivate (an editor's deactivation must stick).
            if not should_be_active and current.get('active', True):
                pending_deactivations.append(email)
        else:
            new_doc = {
                'email': email,
                'name': bq_name or email.split('@')[0],
                'title': bq_title or '',
                'department': bq_department or '',
                'birthday_month': bq_month or 0,
                'birthday_day': bq_day or 0,
                'active': should_be_active,
                'source': 'sync',
                'synced_at': now_iso,
            }
            url = _cache_photo(gcs_client, media_bucket, email, bq_thumb)
            if url:
                new_doc['photo_url'] = url
                new_doc['photo_cached'] = True
                photos_cached += 1
            _commit(email, new_doc)
            existing[email] = new_doc
            created += 1
            if bq_month and bq_day:
                birthdays_filled += 1

    # Second pass: people in the terminated table who are still active locally.
    for email, d in existing.items():
        if email in terminated_emails and d.get('active', True) and email not in pending_deactivations:
            pending_deactivations.append(email)

    # ---- SAFETY CHECK: refuse mass deactivation --------------------------------
    pending_count = len(pending_deactivations)
    pct = (pending_count / active_count * 100.0) if active_count else 0.0

    if pending_count > 0 and pct > SYNC_MAX_DEACTIVATION_PCT:
        sample = ', '.join(sorted(pending_deactivations)[:10])
        if pending_count > 10:
            sample += f", ... (+{pending_count - 10} more)"
        msg = (
            f"REFUSED to deactivate {pending_count} of {active_count} active "
            f"employees ({pct:.1f}%, threshold {SYNC_MAX_DEACTIVATION_PCT:.0f}%). "
            f"This almost always means upstream BigQuery data is broken. No status "
            f"changes were applied. Affected: {sample}."
        )
        print(f"[SYNC] {msg}")
        if alert_fn:
            try:
                alert_fn(msg)
            except Exception as alert_exc:
                print(f"[SYNC] alert delivery failed: {alert_exc}")
        raise SyncSafetyTripped(msg)

    deactivated = 0
    for email in pending_deactivations:
        _commit(email, {'active': False, 'synced_at': now_iso})
        deactivated += 1

    summary = {
        'created': created,
        'updated': updated,
        'birthdays_filled': birthdays_filled,
        'deactivated': deactivated,
        'photos': photos_cached,
        'total_in_bigquery': len(bq_users),
        'terminated': len(terminated_emails),
    }
    print(f"[SYNC] Employee sync complete: {summary}")
    return summary
