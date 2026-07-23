"""
The BriteSide Feed - internal social feed blueprint.

Posts (quick + rich), reactions, flat comments, the role-filtered team
directory, and the birthday/anniversary auto-seeder. Registered from app.py
via register_feed(app, deps) so this module never imports app.py (no circular
imports; app.py stays the composition root).

Data model (Firestore):
  posts/{auto-id}:
    author_email, author_name        denormalized at post time
    kind: 'quick' | 'rich'           derived server-side (title/media/link => rich)
    title, body                      title only meaningful for rich posts
    media: [url, ...]                public GCS URLs from /api/upload-media
    link_card: {url,title,description,image} | None
    category: team_update | shout_out | fun_media | question | birthday | anniversary
    reactions: {name: [email, ...]}  names from ALLOWED_REACTIONS
    comment_count: int
    queue_ref: {collection, id}|None dual-write pointer into a builder queue
    auto_key: str|None               'birthday-{email}-{yyyy}' idempotency key
    created_at, updated_at           ISO strings (America/Chicago, matches app)
  posts/{id}/comments/{auto-id}:
    author_email, author_name, body, created_at
  feed_meta/seed-YYYY-MM-DD:         one marker per day so the lazy seeder
                                     runs exactly once (create() is atomic)

Dual-write: posts made with a newsletter-relevant chip also land in the
existing builder queue collections with source='feed' + post_id, so the
builder pickers see them with zero builder changes and "share of issue
sourced from the feed" stays measurable forever.
"""

from flask import Blueprint, request, jsonify

try:
    from google.cloud import firestore
    from google.api_core import exceptions as gcloud_exceptions
except ImportError:  # pragma: no cover - firestore is in requirements
    firestore = None
    gcloud_exceptions = None


feed_bp = Blueprint('feed', __name__)

# Filled by register_feed(); handlers read their app.py collaborators here.
_deps = {}

POSTS_COLLECTION = 'posts'
COMMENTS_SUBCOLLECTION = 'comments'
FEED_META_COLLECTION = 'feed_meta'

# Reaction names are stable keys; the frontend maps them to emoji.
ALLOWED_REACTIONS = ('heart', 'celebrate', 'clap', 'laugh')

# Categories a person can post with. birthday/anniversary are seeder-only.
POSTABLE_CATEGORIES = ('team_update', 'shout_out', 'fun_media', 'question')
AUTO_CATEGORIES = ('birthday', 'anniversary')

SYSTEM_AUTHOR_EMAIL = 'briteside@brite.co'
SYSTEM_AUTHOR_NAME = 'The BriteSide'

TITLE_MAX = 150
BODY_MAX = 4000
COMMENT_MAX = 1000
MEDIA_MAX = 8
PAGE_MAX = 50

# Media URLs must come from our own upload pipeline (public GCS), so the feed
# can't be used to hotlink arbitrary external content into the app.
ALLOWED_MEDIA_PREFIX = 'https://storage.googleapis.com/'

# Queue collection names mirror app.py's (kept as literals so this module
# doesn't import app.py; a rename there must be mirrored here).
UPDATE_SUBMISSIONS = 'update_submissions'
CULTURE_SUBMISSIONS = 'culture_submissions'

# Queue doc statuses that mean "already pulled into an issue" — those docs
# survive post deletion and stop receiving edits from the post.
USED_STATUSES = ('used', 'approved')


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _db():
    return _deps.get('firestore_client')


def _user():
    return _deps['get_current_user']() or {}


def _email():
    return (_user().get('email') or '').strip().lower()


def _is_editor():
    return _deps['is_editor'](_user())


def _now_iso():
    return _deps['now_iso']()


def _log(text):
    _deps['safe_print'](text)


def _no_db():
    return jsonify({'success': False, 'error': 'Firestore not available'}), 503


def _clean_str(value, max_len):
    if not isinstance(value, str):
        return ''
    return value.strip()[:max_len]


def _clean_media(value):
    """Keep only same-pipeline GCS URLs, capped at MEDIA_MAX."""
    if not isinstance(value, list):
        return []
    out = []
    for u in value:
        if isinstance(u, str) and u.startswith(ALLOWED_MEDIA_PREFIX):
            out.append(u.strip())
        if len(out) >= MEDIA_MAX:
            break
    return out


def _clean_link_card(value):
    """Whitelist link card keys; require an http(s) url or drop the card."""
    if not isinstance(value, dict):
        return None
    url = (value.get('url') or '').strip()
    if not (url.startswith('https://') or url.startswith('http://')):
        return None
    image = (value.get('image') or '').strip()
    if image and not (image.startswith('https://') or image.startswith('http://')):
        image = ''
    return {
        'url': url[:2000],
        'title': _clean_str(value.get('title') or '', 300),
        'description': _clean_str(value.get('description') or '', 500),
        'image': image[:2000],
    }


def _post_ref(post_id):
    return _db().collection(POSTS_COLLECTION).document(post_id)


def _get_post(post_id):
    """Return (ref, dict-with-id) or (ref, None) when the post doesn't exist."""
    ref = _post_ref(post_id)
    snap = ref.get()
    if not snap.exists:
        return ref, None
    data = snap.to_dict() or {}
    data['id'] = snap.id
    return ref, data


def _can_touch(post, allow_editor=True):
    """Author always; editors too unless allow_editor=False."""
    email = _email()
    if email and post.get('author_email') == email:
        return True
    return allow_editor and _is_editor()


# ---------------------------------------------------------------------------
# Dual-write mapping (post -> builder queue doc)
# ---------------------------------------------------------------------------

def _map_team_update(post):
    title = post.get('title') or ''
    body = post.get('body') or ''
    summary = (title + '\n\n' + body).strip() if title else body
    return UPDATE_SUBMISSIONS, {
        'summary': summary,
        'files': post.get('media') or [],
        'co_contributors': [],
        'when_featured': '',
        'specific_month': '',
        'notes': 'From the BriteSide feed',
    }


def _map_culture(post, content_types):
    title = post.get('title') or ''
    body = post.get('body') or ''
    content = (title + ' — ' + body).strip(' —') if title else body
    link = (post.get('link_card') or {}).get('url', '')
    if link:
        content = (content + '\n\n' + link).strip()
    return CULTURE_SUBMISSIONS, {
        'content_types': content_types,
        'files': post.get('media') or [],
        'content': content,
        'why_fits': 'From the BriteSide feed',
    }


def _queue_payload(post):
    """Return (collection, mapped fields) for a post's category, or (None, None)
    for feed-only categories (question, birthday, anniversary)."""
    category = post.get('category')
    if category == 'team_update':
        return _map_team_update(post)
    if category == 'shout_out':
        return _map_culture(post, ['Shout-out'])
    if category == 'fun_media':
        if post.get('media'):
            types = ['Media with own caption']
        elif post.get('link_card'):
            types = ['Educational/informational link']
        else:
            types = ['Other']
        return _map_culture(post, types)
    return None, None


def _dual_write(post_id, post):
    """Create the builder-queue twin of a post. Returns queue_ref or None."""
    collection, fields = _queue_payload(post)
    if not collection:
        return None
    doc = dict(fields)
    doc['submitted_by'] = post.get('author_email', '')
    doc['submitter_name'] = post.get('author_name', '')
    doc['status'] = 'new'
    doc['created_at'] = post.get('created_at') or _now_iso()
    doc['source'] = 'feed'
    doc['post_id'] = post_id
    _, ref = _db().collection(collection).add(doc)
    return {'collection': collection, 'id': ref.id}


def _queue_doc(queue_ref):
    """Return (ref, dict) for a post's queue twin, or (None, None)."""
    if not queue_ref or not queue_ref.get('collection') or not queue_ref.get('id'):
        return None, None
    ref = _db().collection(queue_ref['collection']).document(queue_ref['id'])
    snap = ref.get()
    return ref, (snap.to_dict() if snap.exists else None)


def _sync_queue_doc(post):
    """Re-map post fields onto its queue twin unless an issue already used it."""
    ref, data = _queue_doc(post.get('queue_ref'))
    if not ref or data is None:
        return
    if (data.get('status') or '') in USED_STATUSES:
        return
    _, fields = _queue_payload(post)
    if fields:
        ref.set(fields, merge=True)


def _drop_queue_doc(post):
    """Delete the queue twin unless an issue already used it."""
    ref, data = _queue_doc(post.get('queue_ref'))
    if not ref or data is None:
        return
    if (data.get('status') or '') in USED_STATUSES:
        return
    ref.delete()


# ---------------------------------------------------------------------------
# Birthday / anniversary seeder
# ---------------------------------------------------------------------------

def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _first_name(name):
    return (name or '').strip().split(' ')[0] or 'you'


def _auto_post_exists(auto_key):
    docs = list(
        _db().collection(POSTS_COLLECTION)
        .where('auto_key', '==', auto_key).limit(1).stream()
    )
    return len(docs) > 0


def _create_auto_post(category, auto_key, body):
    _db().collection(POSTS_COLLECTION).add({
        'author_email': SYSTEM_AUTHOR_EMAIL,
        'author_name': SYSTEM_AUTHOR_NAME,
        'kind': 'quick',
        'title': '',
        'body': body,
        'media': [],
        'link_card': None,
        'category': category,
        'reactions': {},
        'comment_count': 0,
        'queue_ref': None,
        'auto_key': auto_key,
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
    })


def run_seed():
    """Create today's birthday/anniversary posts. Idempotent via auto_key, so
    the manual endpoint can be re-run safely. Returns counts."""
    today = _deps['now']()
    month, day, year = today.month, today.day, today.year
    created = {'birthday': 0, 'anniversary': 0}

    for emp in _deps['list_employees']():
        if emp.get('active') is False:
            continue
        email = (emp.get('email') or '').strip().lower()
        if not email:
            continue
        name = emp.get('name') or email.split('@')[0]

        if _as_int(emp.get('birthday_month')) == month and _as_int(emp.get('birthday_day')) == day:
            key = f'birthday-{email}-{year}'
            if not _auto_post_exists(key):
                _create_auto_post(
                    'birthday', key,
                    f"It's {name}'s birthday today! \U0001F382 Drop your wishes in the comments.")
                created['birthday'] += 1

        if _as_int(emp.get('anniversary_month')) == month and _as_int(emp.get('anniversary_day')) == day:
            years = year - _as_int(emp.get('anniversary_year')) if _as_int(emp.get('anniversary_year')) else 0
            if years <= 0:
                continue  # started this year (or unknown start year): skip
            key = f'anniversary-{email}-{year}'
            if not _auto_post_exists(key):
                label = '1 year' if years == 1 else f'{years} years'
                _create_auto_post(
                    'anniversary', key,
                    f'{name} celebrates {label} at BriteCo today! \U0001F389 Congrats, {_first_name(name)}!')
                created['anniversary'] += 1

    _log(f"[FEED] Seed ran: {created['birthday']} birthday, {created['anniversary']} anniversary post(s)")
    return created


def _maybe_seed_today():
    """Lazy daily seed: first feed load of the day wins the marker-create race
    (Firestore create() fails on an existing doc) and runs the seeder. Any
    failure is swallowed — seeding must never break the feed."""
    db = _db()
    if not db:
        return
    try:
        today = _deps['now']().strftime('%Y-%m-%d')
        marker = db.collection(FEED_META_COLLECTION).document(f'seed-{today}')
        try:
            marker.create({'ran_at': _now_iso(), 'trigger': 'lazy'})
        except gcloud_exceptions.AlreadyExists:
            return  # someone else already seeded today
        run_seed()
    except Exception as e:
        _log(f'[FEED] Lazy seed failed (non-fatal): {e}')


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

@feed_bp.route('/api/feed/posts', methods=['GET'])
def list_posts():
    """Feed page, newest first. ?limit=20&before=<created_at iso> paginates.
    Also triggers the once-a-day lazy seeder and annotates each post with
    used_in_issue (its queue twin was pulled into a newsletter)."""
    if not _db():
        return _no_db()
    _maybe_seed_today()

    try:
        limit = min(max(int(request.args.get('limit', 20)), 1), PAGE_MAX)
    except (TypeError, ValueError):
        limit = 20
    before = (request.args.get('before') or '').strip()

    try:
        query = (_db().collection(POSTS_COLLECTION)
                 .order_by('created_at', direction=firestore.Query.DESCENDING))
        if before:
            query = query.where('created_at', '<', before)
        posts = []
        queue_lookups = []  # (post index, queue doc ref)
        for snap in query.limit(limit).stream():
            post = snap.to_dict() or {}
            post['id'] = snap.id
            post['used_in_issue'] = False
            qr = post.get('queue_ref') or {}
            if qr.get('collection') and qr.get('id'):
                queue_lookups.append(
                    (len(posts), _db().collection(qr['collection']).document(qr['id'])))
            posts.append(post)

        # One batched read resolves every twin's status for the badge.
        if queue_lookups:
            refs = [ref for _, ref in queue_lookups]
            statuses = {}
            for doc in _db().get_all(refs):
                if doc.exists:
                    statuses[doc.reference.path] = (doc.to_dict() or {}).get('status', '')
            for idx, ref in queue_lookups:
                posts[idx]['used_in_issue'] = statuses.get(ref.path, '') in USED_STATUSES

        return jsonify({
            'success': True,
            'posts': posts,
            'has_more': len(posts) == limit,
            'viewer': {'email': _email(), 'is_editor': _is_editor()},
        })
    except Exception as e:
        _log(f'[FEED] list_posts error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@feed_bp.route('/api/feed/posts', methods=['POST'])
def create_post():
    """Create a post; newsletter-relevant categories dual-write a queue twin."""
    if not _db():
        return _no_db()
    email = _email()
    if not email:
        return jsonify({'success': False, 'error': 'Not signed in'}), 401

    data = request.json or {}
    category = (data.get('category') or '').strip()
    if category not in POSTABLE_CATEGORIES:
        return jsonify({'success': False, 'error': 'Invalid category'}), 400

    title = _clean_str(data.get('title') or '', TITLE_MAX)
    body = _clean_str(data.get('body') or '', BODY_MAX)
    media = _clean_media(data.get('media'))
    link_card = _clean_link_card(data.get('link_card'))
    if not body and not media and not link_card:
        return jsonify({'success': False, 'error': 'Write something (or add a photo/link) first'}), 400

    post = {
        'author_email': email,
        'author_name': _user().get('name') or email.split('@')[0],
        'kind': 'rich' if (title or media or link_card) else 'quick',
        'title': title,
        'body': body,
        'media': media,
        'link_card': link_card,
        'category': category,
        'reactions': {},
        'comment_count': 0,
        'queue_ref': None,
        'auto_key': None,
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
    }
    try:
        _, ref = _db().collection(POSTS_COLLECTION).add(post)
        queue_ref = _dual_write(ref.id, post)
        if queue_ref:
            ref.set({'queue_ref': queue_ref}, merge=True)
            post['queue_ref'] = queue_ref
        post['id'] = ref.id
        post['used_in_issue'] = False
        _log(f'[FEED] Post created by {email} ({category})')
        return jsonify({'success': True, 'post': post})
    except Exception as e:
        _log(f'[FEED] create_post error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@feed_bp.route('/api/feed/posts/<post_id>', methods=['PATCH'])
def edit_post(post_id):
    """Author-only edit of title/body/media/link_card; category is fixed.
    The queue twin is re-mapped unless an issue already used it."""
    if not _db():
        return _no_db()
    try:
        ref, post = _get_post(post_id)
        if not post:
            return jsonify({'success': False, 'error': 'Post not found'}), 404
        if not _can_touch(post, allow_editor=False):
            return jsonify({'success': False, 'error': 'Only the author can edit a post'}), 403

        data = request.json or {}
        patch = {}
        if 'title' in data:
            patch['title'] = _clean_str(data.get('title') or '', TITLE_MAX)
        if 'body' in data:
            patch['body'] = _clean_str(data.get('body') or '', BODY_MAX)
        if 'media' in data:
            patch['media'] = _clean_media(data.get('media'))
        if 'link_card' in data:
            patch['link_card'] = _clean_link_card(data.get('link_card'))
        if not patch:
            return jsonify({'success': False, 'error': 'Nothing to update'}), 400

        merged = dict(post)
        merged.update(patch)
        if not (merged.get('body') or merged.get('media') or merged.get('link_card')):
            return jsonify({'success': False, 'error': 'A post needs text, a photo, or a link'}), 400
        patch['kind'] = 'rich' if (merged.get('title') or merged.get('media') or merged.get('link_card')) else 'quick'
        patch['updated_at'] = _now_iso()

        ref.set(patch, merge=True)
        merged.update(patch)
        _sync_queue_doc(merged)
        merged['id'] = post_id
        return jsonify({'success': True, 'post': merged})
    except Exception as e:
        _log(f'[FEED] edit_post error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@feed_bp.route('/api/feed/posts/<post_id>', methods=['DELETE'])
def delete_post(post_id):
    """Author or editor. Removes comments and the queue twin (unless used)."""
    if not _db():
        return _no_db()
    try:
        ref, post = _get_post(post_id)
        if not post:
            return jsonify({'success': False, 'error': 'Post not found'}), 404
        if not _can_touch(post):
            return jsonify({'success': False, 'error': 'Not allowed'}), 403

        _drop_queue_doc(post)
        for comment in ref.collection(COMMENTS_SUBCOLLECTION).stream():
            comment.reference.delete()
        ref.delete()
        _log(f'[FEED] Post {post_id} deleted by {_email()}')
        return jsonify({'success': True})
    except Exception as e:
        _log(f'[FEED] delete_post error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@feed_bp.route('/api/feed/posts/<post_id>/react', methods=['POST'])
def react_to_post(post_id):
    """Toggle the caller on/off a reaction. ArrayUnion/Remove keeps each
    toggle atomic under concurrent reactions."""
    if not _db():
        return _no_db()
    email = _email()
    if not email:
        return jsonify({'success': False, 'error': 'Not signed in'}), 401
    reaction = ((request.json or {}).get('reaction') or '').strip()
    if reaction not in ALLOWED_REACTIONS:
        return jsonify({'success': False, 'error': 'Unknown reaction'}), 400
    try:
        ref, post = _get_post(post_id)
        if not post:
            return jsonify({'success': False, 'error': 'Post not found'}), 404
        current = (post.get('reactions') or {}).get(reaction, [])
        if email in current:
            ref.update({f'reactions.{reaction}': firestore.ArrayRemove([email])})
        else:
            ref.update({f'reactions.{reaction}': firestore.ArrayUnion([email])})
        _, fresh = _get_post(post_id)
        return jsonify({'success': True, 'reactions': (fresh or {}).get('reactions', {})})
    except Exception as e:
        _log(f'[FEED] react error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

@feed_bp.route('/api/feed/posts/<post_id>/comments', methods=['GET'])
def list_comments(post_id):
    if not _db():
        return _no_db()
    try:
        ref = _post_ref(post_id)
        out = []
        for snap in (ref.collection(COMMENTS_SUBCOLLECTION)
                     .order_by('created_at').stream()):
            item = snap.to_dict() or {}
            item['id'] = snap.id
            out.append(item)
        return jsonify({'success': True, 'comments': out})
    except Exception as e:
        _log(f'[FEED] list_comments error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@feed_bp.route('/api/feed/posts/<post_id>/comments', methods=['POST'])
def add_comment(post_id):
    if not _db():
        return _no_db()
    email = _email()
    if not email:
        return jsonify({'success': False, 'error': 'Not signed in'}), 401
    body = _clean_str((request.json or {}).get('body') or '', COMMENT_MAX)
    if not body:
        return jsonify({'success': False, 'error': 'Write a comment first'}), 400
    try:
        ref, post = _get_post(post_id)
        if not post:
            return jsonify({'success': False, 'error': 'Post not found'}), 404
        comment = {
            'author_email': email,
            'author_name': _user().get('name') or email.split('@')[0],
            'body': body,
            'created_at': _now_iso(),
        }
        _, cref = ref.collection(COMMENTS_SUBCOLLECTION).add(comment)
        ref.update({'comment_count': firestore.Increment(1)})
        comment['id'] = cref.id
        return jsonify({'success': True, 'comment': comment})
    except Exception as e:
        _log(f'[FEED] add_comment error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@feed_bp.route('/api/feed/posts/<post_id>/comments/<comment_id>', methods=['DELETE'])
def delete_comment(post_id, comment_id):
    if not _db():
        return _no_db()
    try:
        ref, post = _get_post(post_id)
        if not post:
            return jsonify({'success': False, 'error': 'Post not found'}), 404
        cref = ref.collection(COMMENTS_SUBCOLLECTION).document(comment_id)
        snap = cref.get()
        if not snap.exists:
            return jsonify({'success': False, 'error': 'Comment not found'}), 404
        comment = snap.to_dict() or {}
        email = _email()
        if not (email and comment.get('author_email') == email) and not _is_editor():
            return jsonify({'success': False, 'error': 'Not allowed'}), 403
        cref.delete()
        ref.update({'comment_count': firestore.Increment(-1)})
        return jsonify({'success': True})
    except Exception as e:
        _log(f'[FEED] delete_comment error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Manual seed + directory
# ---------------------------------------------------------------------------

@feed_bp.route('/api/feed/seed', methods=['POST'])
def manual_seed():
    """Editor-only manual run of the birthday/anniversary seeder. Idempotent
    (auto_key), so re-running is always safe."""
    if not _db():
        return _no_db()
    if not _is_editor():
        return jsonify({'success': False, 'error': 'Editor access required'}), 403
    try:
        created = run_seed()
        return jsonify({'success': True, 'created': created})
    except Exception as e:
        _log(f'[FEED] manual_seed error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@feed_bp.route('/api/directory', methods=['GET'])
def directory():
    """Role-filtered roster for the Team directory tab (active employees).
    Everyone: name/email/title/department/photo. Editors also see birthday
    and anniversary fields. /api/employees stays editor-only and untouched."""
    try:
        employees = _deps['list_employees']()
    except Exception as e:
        _log(f'[FEED] directory unavailable: {e}')
        return jsonify({'success': False, 'error': 'Directory is temporarily unavailable'}), 503

    editor = _is_editor()
    out = []
    for emp in employees:
        if emp.get('active') is False:
            continue
        row = {
            'name': emp.get('name', ''),
            'email': emp.get('email', ''),
            'title': emp.get('title', ''),
            'department': emp.get('department', ''),
            'photo_url': emp.get('photo_url', ''),
        }
        if editor:
            row['birthday_month'] = _as_int(emp.get('birthday_month'))
            row['birthday_day'] = _as_int(emp.get('birthday_day'))
            row['anniversary_month'] = _as_int(emp.get('anniversary_month'))
            row['anniversary_day'] = _as_int(emp.get('anniversary_day'))
            row['anniversary_year'] = _as_int(emp.get('anniversary_year'))
        out.append(row)
    return jsonify({'success': True, 'employees': out, 'is_editor': editor})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_feed(app, deps):
    """Wire the feed blueprint into the Flask app.

    deps: firestore_client, get_current_user, is_editor, now_iso,
          now (tz-aware datetime callable), safe_print, list_employees.
    """
    _deps.update(deps)
    app.register_blueprint(feed_bp)
    deps['safe_print']('[OK] Feed blueprint registered')
