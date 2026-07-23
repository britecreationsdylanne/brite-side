"""
The BriteSide - Internal Monthly Newsletter Generator
Flask backend API for generating BriteCo's internal employee newsletter
"""

import os
import sys
import json
import html as html_mod
import secrets
import time
import traceback
import requests as http_requests
import uuid
import re
from datetime import datetime

import pytz

# Chicago timezone for timestamps
CHICAGO_TZ = pytz.timezone('America/Chicago')

from flask import Flask, request, jsonify, send_from_directory, redirect, session, url_for, Response
from flask_cors import CORS
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix

# SendGrid for email
try:
    import sendgrid
    from sendgrid.helpers.mail import Mail
    SENDGRID_AVAILABLE = True
except ImportError:
    SENDGRID_AVAILABLE = False
    print("[WARNING] SendGrid not installed. Email functionality disabled.")

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Add backend to path for imports
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))

# Import Claude client
from backend.integrations.claude_client import ClaudeClient

# Import BigQuery employee sync
from backend.integrations import user_sync

# Import config
from config.briteside_config import (
    EMPLOYEES as CONFIG_EMPLOYEES,
    MONTH_NAMES,
    BRITESIDE_SYSTEM_PROMPT,
    AI_PROMPTS,
    EMAIL_TEMPLATE_CONFIG,
    SENDGRID_CONFIG,
    GCS_CONFIG,
)

# Employees live in Firestore (see EMPLOYEE DATA LAYER section below).
# CONFIG_EMPLOYEES is only used as a first-run seed fallback.


# ============================================================================
# APP INITIALIZATION
# ============================================================================

app = Flask(__name__, static_folder=None)
# CORS is intentionally NOT enabled app-wide: the SPA is served from the same
# origin as the API, and wide-open CORS combined with cookie auth invites
# cross-origin calls. Add specific trusted origins here only if a separate
# front-end is ever introduced.

# Fix for running behind Cloud Run's proxy
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Session configuration for OAuth
flask_key = os.environ.get('FLASK_SECRET_KEY')
if flask_key:
    app.secret_key = flask_key
    print("[OK] Flask secret key loaded from environment")
else:
    app.secret_key = secrets.token_hex(32)
    print("[WARNING] Flask secret key auto-generated - sessions will not persist across restarts. Set FLASK_SECRET_KEY env var.")

app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 7  # 7 days

# OAuth configuration
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

ALLOWED_DOMAIN = 'brite.co'

# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
# Two roles:
#   - contributor: any signed-in @brite.co user. Can submit spotlight/update/
#     culture/correction entries and manage their own submissions.
#   - editor: the newsletter team. Can build/send the newsletter, manage the
#     roster, run AI generation, and curate submissions.
# EDITOR_EMAILS is a comma-separated allow-list (env). It falls back to a small
# default so the tool is usable on first deploy; set the env var in prod.
_DEFAULT_EDITORS = 'dylanne.crugnale@brite.co,dove@brite.co'
# `or _DEFAULT_EDITORS` so a set-but-EMPTY env var (Cloud Build passes
# EDITOR_EMAILS="" when the _EDITOR_EMAILS substitution is unset) still falls
# back to the default list instead of leaving nobody as an editor.
EDITOR_EMAILS = {
    e.strip().lower()
    for e in (os.environ.get('EDITOR_EMAILS') or _DEFAULT_EDITORS).split(',')
    if e.strip()
}

# Machine-to-machine secret for Cloud Scheduler job endpoints (/api/jobs/*).
JOB_SECRET = os.environ.get('JOB_SECRET', '')

# Dev auth bypass is OPT-IN only (DEV_AUTH_MODE=true). It must never turn on
# just because GOOGLE_CLIENT_ID is missing — that previously served the whole
# app to anyone as a fake "Local Developer" if a prod substitution was dropped.
DEV_AUTH_MODE = os.environ.get('DEV_AUTH_MODE', '').lower() == 'true'


def get_current_user():
    """Get current authenticated user from session (dict with email/name/...).

    In DEV_AUTH_MODE (opt-in only) a stand-in editor user is returned so the app
    is usable locally without OAuth. This never fires in production.
    """
    user = session.get('user')
    if user:
        return user
    if DEV_AUTH_MODE:
        # DEV_AUTH_NAME lets local work (e.g. seeding feed content) carry a
        # real display name instead of the stand-in marker.
        return {'email': 'dylanne.crugnale@brite.co',
                'name': os.environ.get('DEV_AUTH_NAME') or 'Local Developer (dev)',
                'picture': ''}
    return None


# Editors granted from the app UI live in Firestore (app_config/extra_editors)
# on top of the env allow-list, so Dylanne/Dove can promote someone (e.g.
# Allison) without a Cloud Build substitution change. Env-list editors are
# "locked": they can never be demoted from the UI. The extra list is cached
# in-process briefly so is_editor() doesn't cost a Firestore read per request.
_EXTRA_EDITORS_DOC = ('app_config', 'extra_editors')
_EXTRA_EDITORS_TTL = 60  # seconds
_extra_editors_cache = {'emails': set(), 'fetched_at': 0.0}


def _extra_editors(force=False):
    """Return the Firestore-managed editor set. On any error, fall back to the
    last cached value (and ultimately to the env allow-list alone)."""
    now = time.time()
    if not force and (now - _extra_editors_cache['fetched_at']) < _EXTRA_EDITORS_TTL:
        return _extra_editors_cache['emails']
    if not firestore_client:
        return _extra_editors_cache['emails']
    try:
        snap = firestore_client.collection(_EXTRA_EDITORS_DOC[0]).document(_EXTRA_EDITORS_DOC[1]).get()
        emails = {
            (e or '').strip().lower()
            for e in ((snap.to_dict() or {}).get('emails') or [])
            if (e or '').strip()
        } if snap.exists else set()
        _extra_editors_cache['emails'] = emails
        _extra_editors_cache['fetched_at'] = now
    except Exception as e:
        print(f"[WARNING] extra editors read failed (using cache): {e}")
    return _extra_editors_cache['emails']


def _set_extra_editor(email, grant):
    """Add/remove an email on the Firestore editor list. Returns the new set."""
    email = (email or '').strip().lower()
    current = set(_extra_editors(force=True))
    if grant:
        current.add(email)
    else:
        current.discard(email)
    firestore_client.collection(_EXTRA_EDITORS_DOC[0]).document(_EXTRA_EDITORS_DOC[1]).set(
        {'emails': sorted(current)})
    _extra_editors_cache['emails'] = current
    _extra_editors_cache['fetched_at'] = time.time()
    return current


def is_editor(user):
    """True when the user is on the newsletter-team allow-list (env) or has
    been granted editor access from the app (Firestore)."""
    if not user:
        return False
    email = (user.get('email') or '').strip().lower()
    return email in EDITOR_EMAILS or email in _extra_editors()


# ============================================================================
# INITIALIZE CLAUDE CLIENT
# ============================================================================

claude_client = None

try:
    claude_client = ClaudeClient()
    print("[OK] Claude initialized")
except Exception as e:
    print(f"[WARNING] Claude not available: {e}")


# ============================================================================
# INITIALIZE GOOGLE CLOUD STORAGE
# ============================================================================

GCS_DRAFTS_BUCKET = GCS_CONFIG['drafts_bucket']
# Public bucket for newsletter media. Images/gifs/videos are hotlinked from sent
# emails, so this bucket — and ONLY this bucket — is world-readable. Defaults to
# the drafts bucket for backward compatibility; set GCS_MEDIA_BUCKET to a
# dedicated public bucket so drafts, published issues, saved games, and the
# employee roster (config/employees.json) are NOT exposed to the internet.
# `or GCS_DRAFTS_BUCKET` so a set-but-EMPTY env var (Cloud Build passes
# GCS_MEDIA_BUCKET="" when _GCS_MEDIA_BUCKET is unset) falls back to the drafts
# bucket instead of becoming an empty bucket name — which broke media uploads
# and skipped photo caching during the sync.
GCS_MEDIA_BUCKET = os.environ.get('GCS_MEDIA_BUCKET') or GCS_DRAFTS_BUCKET
gcs_client = None

try:
    from google.cloud import storage as gcs_storage
    gcs_client = gcs_storage.Client()
    print("[OK] GCS initialized")

    # Grant public read ONLY on a dedicated media bucket. We never auto-grant
    # public access on the drafts bucket (it holds drafts + employee PII), and we
    # never re-apply a public grant the app previously forced (the old
    # "self-healing" misconfig). When no dedicated media bucket is configured,
    # media stays in the drafts bucket and we do NOT touch its IAM — move media
    # to a public bucket and make the drafts bucket private to close the exposure.
    if GCS_MEDIA_BUCKET != GCS_DRAFTS_BUCKET:
        try:
            _mb = gcs_client.bucket(GCS_MEDIA_BUCKET)
            _policy = _mb.get_iam_policy(requested_policy_version=3)
            _has_public = any(
                b.get('role') == 'roles/storage.objectViewer' and 'allUsers' in b.get('members', set())
                for b in _policy.bindings
            )
            if not _has_public:
                _policy.bindings.append({'role': 'roles/storage.objectViewer', 'members': {'allUsers'}})
                _mb.set_iam_policy(_policy)
                print(f"[OK] Public read enabled on media bucket '{GCS_MEDIA_BUCKET}'")
        except Exception as iam_err:
            print(f"[WARNING] Could not set media bucket IAM policy: {iam_err}")
    else:
        print(
            "[WARNING] GCS_MEDIA_BUCKET not set: media shares the drafts bucket. "
            "Set a dedicated public media bucket so drafts and employee PII stay private."
        )
except Exception as e:
    print(f"[WARNING] GCS not available: {e}")


# ============================================================================
# EMPLOYEE DATA LAYER (Firestore-backed)
# ============================================================================
# Single source of truth. Each employee is one doc in the `employees`
# collection, keyed by lowercased email. No in-memory cache — every read
# hits Firestore so multi-instance Cloud Run stays consistent.

EMPLOYEES_COLLECTION = 'employees'
EMPLOYEES_GCS_KEY = 'config/employees.json'  # kept only for initial seed

firestore_client = None
try:
    from google.cloud import firestore
    firestore_client = firestore.Client()
    print("[OK] Firestore initialized")
except Exception as e:
    print(f"[WARNING] Firestore not available: {e}")


def _emp_key(email):
    return (email or '').strip().lower()


def list_employees(strict=False):
    """Return all employees sorted by name (case-insensitive).

    On a Firestore error: raise DataUnavailable when strict=True (so read
    endpoints can return 503 rather than an empty roster that reads as "no
    employees"); otherwise return [] so internal callers degrade gracefully."""
    if not firestore_client:
        return list(CONFIG_EMPLOYEES)
    try:
        docs = firestore_client.collection(EMPLOYEES_COLLECTION).stream()
        employees = [doc.to_dict() for doc in docs if doc.exists]
        employees.sort(key=lambda e: (e.get('name') or '').lower())
        return employees
    except Exception as e:
        print(f"[WARNING] Firestore list failed: {e}")
        if strict:
            raise DataUnavailable("Employee directory is temporarily unavailable") from e
        return []


def get_employee(email):
    if not firestore_client:
        return None
    try:
        doc = firestore_client.collection(EMPLOYEES_COLLECTION).document(_emp_key(email)).get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        print(f"[WARNING] Firestore get failed: {e}")
        return None


def upsert_employee(email, data):
    if not firestore_client:
        return False
    try:
        firestore_client.collection(EMPLOYEES_COLLECTION).document(_emp_key(email)).set(data)
        return True
    except Exception as e:
        print(f"[WARNING] Firestore upsert failed: {e}")
        return False


def delete_employee(email):
    if not firestore_client:
        return False
    try:
        firestore_client.collection(EMPLOYEES_COLLECTION).document(_emp_key(email)).delete()
        return True
    except Exception as e:
        print(f"[WARNING] Firestore delete failed: {e}")
        return False


def _seed_from_gcs_or_config():
    """Return the list of employees to seed Firestore with on first run."""
    if gcs_client:
        try:
            bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
            blob = bucket.blob(EMPLOYEES_GCS_KEY)
            if blob.exists():
                raw = json.loads(blob.download_as_text())
                if isinstance(raw, dict) and 'employees' in raw:
                    return raw['employees']
                if isinstance(raw, list):
                    return raw
        except Exception as e:
            print(f"[WARNING] Could not read GCS seed: {e}")
    return list(CONFIG_EMPLOYEES)


def seed_employees_if_empty():
    """If the Firestore collection is empty, populate it from GCS (preferred)
    or from the config defaults. Runs once on startup."""
    if not firestore_client:
        return
    try:
        existing = list(firestore_client.collection(EMPLOYEES_COLLECTION).limit(1).stream())
        if existing:
            return
    except Exception as e:
        print(f"[WARNING] Could not check Firestore seed state: {e}")
        return

    seed = _seed_from_gcs_or_config()
    print(f"[SEED] Populating Firestore with {len(seed)} employees...")
    batch = firestore_client.batch()
    written = 0
    for emp in seed:
        key = _emp_key(emp.get('email', ''))
        if not key:
            continue
        ref = firestore_client.collection(EMPLOYEES_COLLECTION).document(key)
        batch.set(ref, emp)
        written += 1
    if written:
        batch.commit()
    print(f"[SEED] Seeded {written} employees into Firestore")


seed_employees_if_empty()


# ============================================================================
# HELPERS
# ============================================================================

def esc(text):
    """HTML-escape user-generated text to prevent broken rendering.
    html.escape escapes &, <, >, " and ' — safe for both element text and
    double/single-quoted attribute values."""
    if not text:
        return text or ''
    return html_mod.escape(str(text))


_ALLOWED_INLINE_TAGS = ('strong', 'b', 'em', 'i', 'u')


def sanitize_basic_html(text):
    """Escape all HTML, then restore a small allow-list of *attribute-free*
    formatting tags (<br>, <strong>, <b>, <em>, <i>, <u>). Lets AI/code-built
    content (e.g. the game's numbered "<strong>1.</strong> ... <br>" list) render
    with intended formatting while still neutralizing scripts, event handlers and
    attribute-based injection — anything carrying an attribute stays escaped."""
    s = html_mod.escape(str(text or ''))
    s = s.replace('&lt;br&gt;', '<br>').replace('&lt;br/&gt;', '<br>').replace('&lt;br /&gt;', '<br>')
    for tag in _ALLOWED_INLINE_TAGS:
        s = s.replace('&lt;' + tag + '&gt;', '<' + tag + '>')
        s = s.replace('&lt;/' + tag + '&gt;', '</' + tag + '>')
    return s


_SAFE_URL_SCHEMES = ('http://', 'https://', 'mailto:')


def safe_url(url):
    """Attribute-safe URL for src/href interpolation.

    Returns the escaped URL when it uses an allowed scheme (http/https/mailto)
    or is root-relative ('/static/...'), otherwise ''. Blocks javascript:,
    data:, and any value trying to break out of the attribute with a quote."""
    u = (url or '').strip()
    if not u:
        return ''
    low = u.lower()
    if low.startswith(_SAFE_URL_SCHEMES) or u.startswith('/'):
        return esc(u)
    return ''


def _as_bday_int(value):
    """Coerce a birthday month/day to a plain int, mapping null / '' / bad input
    to 0 (= unknown). Guards the birthdays endpoint against a TypeError when a
    None or string value would otherwise be compared to an int while sorting."""
    if value is None or value == '':
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class DataUnavailable(Exception):
    """Raised when a backing datastore read genuinely failed (as opposed to
    returning no rows), so callers can answer 503 instead of masking an outage
    as an empty result."""


def _validate_gcs_key(filename, allowed_prefixes):
    """Return filename when it sits under one of allowed_prefixes with no path
    traversal, else None. Stops a client from reading/deleting/publishing an
    arbitrary object (e.g. config/employees.json) via the draft endpoints."""
    if not filename or not isinstance(filename, str):
        return None
    if '..' in filename or filename.startswith('/'):
        return None
    if not any(filename.startswith(p) for p in allowed_prefixes):
        return None
    return filename


def _strip_code_fences(text):
    """Remove a leading ```json / ``` fence and a trailing ``` from model output
    so the JSON inside can be parsed."""
    t = (text or '').strip()
    if t.startswith('```'):
        t = t.split('\n', 1)[1] if '\n' in t else ''
        if t.rstrip().endswith('```'):
            t = t.rstrip()[:-3]
    return t.strip()


def safe_print(text):
    """Safe print for Unicode characters"""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode('ascii', 'replace').decode('ascii'))


def is_local_dev():
    """Dev auth bypass — OPT-IN only via DEV_AUTH_MODE=true. Never enabled just
    because OAuth env vars happen to be missing (that would fail open in prod)."""
    return DEV_AUTH_MODE


# Paths reachable without a signed-in session.
_PUBLIC_EXACT = {'/', '/health', '/auth/login', '/auth/callback', '/auth/logout'}


@app.before_request
def _auth_gate():
    """Global authentication + authorization gate for every request.

    - Public: '/', /health, the OAuth routes, and /static/* (the logo is
      embedded in sent emails and the preview iframe, so it must be anonymously
      fetchable).
    - '/api/jobs/*': machine endpoints that authenticate via the X-Job-Secret
      header inside the handler — allowed past the session gate here.
    - Contributor endpoints (/api/submit*, /api/me*): any signed-in @brite.co user.
    - Everything else under /api/: newsletter-team (editor) only.
    - Any other page (e.g. /templates/*): requires a signed-in session.

    Dev mode (DEV_AUTH_MODE=true) bypasses enforcement; handlers still see the
    stand-in editor user from get_current_user().
    """
    if is_local_dev():
        return

    path = request.path
    if path in _PUBLIC_EXACT or path.startswith('/static/'):
        return
    if path.startswith('/api/jobs/'):
        return  # authenticated by X-Job-Secret in the handler

    user = get_current_user()
    if not user:
        if path.startswith('/api/'):
            return jsonify({'success': False, 'error': 'Authentication required'}), 401
        return redirect('/auth/login')

    if path.startswith('/api/'):
        # Contributor-accessible surface: any signed-in @brite.co user.
        # (upload-media is shared so contributors can attach files to
        # submissions and feed posts; og-preview powers feed link cards and is
        # SSRF-guarded by _safe_fetch; /api/feed/* and /api/directory do their
        # own editor checks internally where needed.)
        if (path == '/api/me' or path.startswith('/api/me/')
                or path.startswith('/api/submit') or path == '/api/upload-media'
                or path.startswith('/api/feed/') or path == '/api/directory'
                or path == '/api/media/og-preview'):
            return
        # Editor-only for everything else (roster, AI, render, send, drafts, ...).
        if not is_editor(user):
            return jsonify({'success': False, 'error': 'Editor access required'}), 403


def require_job_secret():
    """Validate the X-Job-Secret header for Cloud Scheduler job endpoints.
    Returns None when authorized, or a (response, status) tuple to abort with.
    Fails closed: a missing/blank JOB_SECRET rejects every request."""
    provided = request.headers.get('X-Job-Secret', '')
    if not JOB_SECRET or not secrets.compare_digest(provided, JOB_SECRET):
        return jsonify({'success': False, 'error': 'Invalid or missing job secret'}), 401
    return None


def find_employee(name):
    """Look up an employee by name (case-insensitive partial match)"""
    name_lower = name.lower().strip()
    employees = list_employees()
    for emp in employees:
        if emp.get('name', '').lower() == name_lower:
            return emp
    for emp in employees:
        if name_lower in emp.get('name', '').lower():
            return emp
    return None


# ============================================================================
# OAUTH AUTHENTICATION ROUTES
# ============================================================================

@app.route('/auth/login')
def auth_login():
    """Redirect to Google OAuth"""
    if get_current_user():
        return redirect('/')
    redirect_uri = url_for('auth_callback', _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route('/auth/callback')
def auth_callback():
    """Handle OAuth callback from Google"""
    try:
        token = google.authorize_access_token()
        user_info = token.get('userinfo')

        if not user_info:
            return 'Failed to get user info', 400

        email = user_info.get('email', '')

        if not email.endswith(f'@{ALLOWED_DOMAIN}'):
            return f'''
            <html>
            <head><title>Access Denied</title></head>
            <body style="font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; background: #272D3F;">
                <div style="text-align: center; color: white; padding: 2rem;">
                    <h1 style="color: #FC883A;">Access Denied</h1>
                    <p>Only @{ALLOWED_DOMAIN} email addresses are allowed.</p>
                    <p style="color: #A9C1CB;">You tried to sign in with: {email}</p>
                    <a href="/auth/login" style="color: #31D7CA;">Try again with a different account</a>
                </div>
            </body>
            </html>
            ''', 403

        session.permanent = True
        session['user'] = {
            'email': email,
            'name': user_info.get('name', ''),
            'picture': user_info.get('picture', '')
        }

        return redirect('/')

    except Exception as e:
        print(f"[AUTH ERROR] OAuth callback failed: {e}")
        return f'Authentication failed: {str(e)}', 500


@app.route('/auth/logout')
def auth_logout():
    """Clear session and redirect to login"""
    session.pop('user', None)
    return redirect('/auth/login')


# ============================================================================
# ROUTES - STATIC FILES & HEALTH
# ============================================================================

def _serve_app(entry_mode=False):
    """Serve the SPA. '/' is the newsletter builder; '/entry' shows the
    submission form. Auth is enforced by the _auth_gate before_request."""
    user = get_current_user()
    if not user:
        return redirect('/auth/login')

    try:
        with open('index.html', 'r', encoding='utf-8') as f:
            html = f.read()
    except FileNotFoundError:
        return 'index.html not found', 404

    auth_user = {
        'email': user.get('email', ''),
        'name': user.get('name', ''),
        'picture': user.get('picture', ''),
        'is_editor': is_editor(user),
    }
    user_script = (
        '<script>\n'
        '    window.AUTH_USER = ' + json.dumps(auth_user) + ';\n'
        '    window.SUBMIT_ENTRY_MODE = ' + ('true' if entry_mode else 'false') + ';\n'
        '    </script>\n</head>'
    )
    html = html.replace('</head>', user_script, 1)
    return Response(html, mimetype='text/html')


@app.route('/')
def serve_index():
    """Main page: the newsletter builder (editors) or, for contributors, the
    submission form (the frontend redirects them to the submit view)."""
    return _serve_app(entry_mode=False)


@app.route('/entry')
def serve_entry():
    """The employee submission form."""
    return _serve_app(entry_mode=True)


@app.route('/health')
def health_check():
    """Simple health check endpoint"""
    return jsonify({
        "status": "healthy",
        "app": "The BriteSide - Internal Newsletter",
        "timestamp": datetime.now(CHICAGO_TZ).isoformat(),
        "claude_available": claude_client is not None,
        "sendgrid_available": SENDGRID_AVAILABLE,
    })


# ============================================================================
# ROUTES - EMPLOYEE DATA
# ============================================================================

@app.route('/api/employees')
def get_employees():
    """Return all employees for dropdowns (name, email, department, title)"""
    try:
        employees = [
            {
                "name": emp.get("name", ""),
                "email": emp.get("email", ""),
                "department": emp.get("department", ""),
                "title": emp.get("title", ""),
                "birthday_month": _as_bday_int(emp.get("birthday_month")),
                "birthday_day": _as_bday_int(emp.get("birthday_day")),
                "active": emp.get("active", True),
                "photo_url": emp.get("photo_url", ""),
            }
            for emp in list_employees(strict=True)
        ]
        return jsonify({"success": True, "employees": employees})

    except DataUnavailable as e:
        safe_print(f"[API] Employee directory unavailable: {e}")
        return jsonify({"success": False, "error": str(e)}), 503
    except Exception as e:
        safe_print(f"[API] Error fetching employees: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/employees/birthdays')
def get_birthdays():
    """Return employees with birthday in the given month, sorted by day"""
    try:
        month = request.args.get('month', type=int)
        if not month or month < 1 or month > 12:
            return jsonify({"success": False, "error": "Valid month parameter (1-12) required"}), 400

        birthday_employees = [
            {
                "name": emp.get("name", ""),
                "email": emp.get("email", ""),
                "department": emp.get("department", ""),
                "title": emp.get("title", ""),
                "birthday_day": _as_bday_int(emp.get("birthday_day")),
                "birthday_month": _as_bday_int(emp.get("birthday_month")),
                "image_url": emp.get("photo_url", ""),
            }
            for emp in list_employees(strict=True)
            if _as_bday_int(emp.get("birthday_month")) == month and emp.get("active", True)
        ]

        # Sort by day of month (values coerced to int above, so no None/str crash)
        birthday_employees.sort(key=lambda x: x["birthday_day"])

        month_name = MONTH_NAMES.get(month, str(month))
        safe_print(f"[API] Found {len(birthday_employees)} birthdays in {month_name}")

        return jsonify({
            "success": True,
            "month": month,
            "month_name": month_name,
            "birthdays": birthday_employees,
        })

    except DataUnavailable as e:
        safe_print(f"[API] Employee directory unavailable: {e}")
        return jsonify({"success": False, "error": str(e)}), 503
    except Exception as e:
        safe_print(f"[API] Error fetching birthdays: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/employees/anniversaries')
def get_anniversaries():
    """Return active employees with a work anniversary in the given month.

    `years` = current year − anniversary_year (0 when we have no start year, e.g.
    a first-run seed record). Someone in their first year shows as 0 and the UI
    can treat that as a plain anniversary rather than an "N years" milestone."""
    try:
        month = request.args.get('month', type=int)
        if not month or month < 1 or month > 12:
            return jsonify({"success": False, "error": "Valid month parameter (1-12) required"}), 400

        current_year = datetime.now(CHICAGO_TZ).year
        anniversary_employees = []
        for emp in list_employees(strict=True):
            if _as_bday_int(emp.get("anniversary_month")) != month or not emp.get("active", True):
                continue
            yr = _as_bday_int(emp.get("anniversary_year"))
            anniversary_employees.append({
                "name": emp.get("name", ""),
                "email": emp.get("email", ""),
                "department": emp.get("department", ""),
                "title": emp.get("title", ""),
                "anniversary_day": _as_bday_int(emp.get("anniversary_day")),
                "anniversary_month": _as_bday_int(emp.get("anniversary_month")),
                "anniversary_year": yr,
                "years": (current_year - yr) if yr else 0,
                "image_url": emp.get("photo_url", ""),
            })

        anniversary_employees.sort(key=lambda x: x["anniversary_day"])
        month_name = MONTH_NAMES.get(month, str(month))
        safe_print(f"[API] Found {len(anniversary_employees)} anniversaries in {month_name}")
        return jsonify({
            "success": True,
            "month": month,
            "month_name": month_name,
            "anniversaries": anniversary_employees,
        })

    except DataUnavailable as e:
        safe_print(f"[API] Employee directory unavailable: {e}")
        return jsonify({"success": False, "error": str(e)}), 503
    except Exception as e:
        safe_print(f"[API] Error fetching anniversaries: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/employees/add', methods=['POST'])
def add_employee():
    """Add a new employee to the list"""
    try:
        data = request.json
        name = data.get('name', '').strip()
        email = data.get('email', '').strip()

        if not name:
            return jsonify({"success": False, "error": "Employee name is required"}), 400
        if not email:
            return jsonify({"success": False, "error": "Employee email is required"}), 400

        if get_employee(email):
            return jsonify({"success": False, "error": f"Employee with email {email} already exists"}), 409

        new_employee = {
            "name": name,
            "email": email,
            "birthday_month": _as_bday_int(data.get('birthday_month')),
            "birthday_day": _as_bday_int(data.get('birthday_day')),
            "department": (data.get('department') or '').strip(),
            "title": (data.get('title') or '').strip(),
        }

        if not upsert_employee(email, new_employee):
            return jsonify({"success": False, "error": "Database write failed"}), 500

        total = len(list_employees())
        safe_print(f"[API] Added employee: {name} ({email})")
        return jsonify({"success": True, "employee": new_employee, "total": total})

    except Exception as e:
        safe_print(f"[API] Error adding employee: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/employees/remove', methods=['DELETE'])
def remove_employee():
    """Remove an employee from the list"""
    try:
        data = request.json
        email = data.get('email', '').strip().lower()

        if not email:
            return jsonify({"success": False, "error": "Employee email is required"}), 400

        if not get_employee(email):
            return jsonify({"success": False, "error": f"Employee with email {email} not found"}), 404

        if not delete_employee(email):
            return jsonify({"success": False, "error": "Database delete failed"}), 500

        safe_print(f"[API] Removed employee: {email}")
        return jsonify({"success": True, "total": len(list_employees())})

    except Exception as e:
        safe_print(f"[API] Error removing employee: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/employees/update', methods=['PUT'])
def update_employee():
    """Update an employee's details"""
    try:
        data = request.json
        email = data.get('email', '').strip().lower()

        if not email:
            return jsonify({"success": False, "error": "Employee email is required"}), 400

        emp = get_employee(email)
        if not emp:
            return jsonify({"success": False, "error": f"Employee with email {email} not found"}), 404

        new_email_raw = data.get('new_email', '')
        new_email = new_email_raw.strip().lower() if new_email_raw else None

        if new_email and new_email != email:
            if get_employee(new_email):
                return jsonify({"success": False, "error": f"Email {new_email} is already in use"}), 400

        if 'name' in data:
            emp['name'] = (data.get('name') or '').strip()
        if 'department' in data:
            emp['department'] = (data.get('department') or '').strip()
        if 'title' in data:
            emp['title'] = (data.get('title') or '').strip()
        if 'birthday_month' in data:
            emp['birthday_month'] = _as_bday_int(data.get('birthday_month'))
        if 'birthday_day' in data:
            emp['birthday_day'] = _as_bday_int(data.get('birthday_day'))
        if 'anniversary_month' in data:
            emp['anniversary_month'] = _as_bday_int(data.get('anniversary_month'))
        if 'anniversary_day' in data:
            emp['anniversary_day'] = _as_bday_int(data.get('anniversary_day'))
        if 'anniversary_year' in data:
            emp['anniversary_year'] = _as_bday_int(data.get('anniversary_year'))
        if 'photo_url' in data:
            emp['photo_url'] = (data.get('photo_url') or '').strip()
            # A manually chosen photo wins: photo_cached=True stops the BigQuery
            # sync from overwriting it with the Google thumbnail.
            emp['photo_cached'] = True

        if new_email and new_email != email:
            # Write the NEW record first, then remove the old key. A failure
            # partway through leaves the employee under the old email rather than
            # deleting them outright (the previous delete-then-set order could
            # permanently lose the record on a transient write error).
            emp['email'] = new_email
            if not upsert_employee(new_email, emp):
                return jsonify({"success": False, "error": "Database write failed"}), 500
            if not delete_employee(email):
                return jsonify({
                    "success": True,
                    "employee": emp,
                    "warning": f"Saved as {new_email}, but the old record {email} could not be removed",
                })
        else:
            if not upsert_employee(email, emp):
                return jsonify({"success": False, "error": "Database write failed"}), 500

        safe_print(f"[API] Updated employee: {emp.get('name', '')} ({emp.get('email', '')})")
        return jsonify({"success": True, "employee": emp})

    except Exception as e:
        safe_print(f"[API] Error updating employee: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/employees/set-active', methods=['POST'])
def set_employee_active():
    """Deactivate / reactivate an employee. A deactivated person STAYS in the
    roster (so the sync won't re-create them), is excluded from the newsletter
    audience and birthdays, and is NEVER auto-reactivated by the BigQuery sync."""
    try:
        data = request.json or {}
        email = (data.get('email') or '').strip().lower()
        active = bool(data.get('active', True))
        if not email:
            return jsonify({"success": False, "error": "Employee email is required"}), 400
        emp = get_employee(email)
        if not emp:
            return jsonify({"success": False, "error": "Employee not found"}), 404
        emp['active'] = active
        if not upsert_employee(email, emp):
            return jsonify({"success": False, "error": "Database write failed"}), 500
        safe_print(f"[API] Set {email} active={active}")
        return jsonify({"success": True, "email": email, "active": active})
    except Exception as e:
        safe_print(f"[API] Error setting active: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/editors', methods=['GET'])
def list_editors():
    """All current editors: env allow-list entries are locked (not demotable
    from the UI), Firestore-granted ones can be toggled. Editor-only via gate."""
    extras = _extra_editors(force=True)
    editors = ([{'email': e, 'locked': True} for e in sorted(EDITOR_EMAILS)] +
               [{'email': e, 'locked': False} for e in sorted(extras - EDITOR_EMAILS)])
    return jsonify({'success': True, 'editors': editors})


@app.route('/api/editors', methods=['POST'])
def set_editor():
    """Grant or revoke editor access for a @brite.co address (Firestore list).
    Env allow-list editors can never be demoted here."""
    if not firestore_client:
        return jsonify({'success': False, 'error': 'Firestore not available'}), 503
    data = request.json or {}
    email = (data.get('email') or '').strip().lower()
    grant = bool(data.get('editor'))
    if not email or not email.endswith('@' + ALLOWED_DOMAIN):
        return jsonify({'success': False, 'error': f'A @{ALLOWED_DOMAIN} email is required'}), 400
    if not grant and email in EDITOR_EMAILS:
        return jsonify({'success': False, 'error': 'This editor is locked (set via EDITOR_EMAILS)'}), 400
    try:
        _set_extra_editor(email, grant)
        safe_print(f"[API] Editor {'granted to' if grant else 'revoked from'} {email} by {_current_email()}")
        return jsonify({'success': True, 'email': email, 'editor': grant or email in EDITOR_EMAILS})
    except Exception as e:
        safe_print(f"[API] Error setting editor: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# ROUTES - AI GENERATION
# ============================================================================

@app.route('/api/generate-joke', methods=['POST'])
def generate_joke():
    """Generate 3 joke/pun options for the newsletter opener"""
    try:
        if not claude_client:
            return jsonify({"success": False, "error": "Claude AI is not available"}), 503

        data = request.json
        month = data.get('month', datetime.now(CHICAGO_TZ).strftime('%B'))
        theme = data.get('theme', 'jewelry and insurance')

        safe_print(f"[API] Generating jokes for {month}, theme: {theme}")

        prompt = AI_PROMPTS['generate_joke'].format(month=month, theme=theme)

        response = claude_client.generate_content(
            prompt=prompt,
            system_prompt=BRITESIDE_SYSTEM_PROMPT,
            max_tokens=400,
            temperature=0.85,
        )

        jokes_text = response.get('content', '').strip()

        safe_print(f"[API] Jokes generated ({response.get('tokens', 0)} tokens, {response.get('latency_ms', 0)}ms)")

        return jsonify({
            "success": True,
            "jokes": jokes_text,
            "model": response.get('model', ''),
            "tokens": response.get('tokens', 0),
            "cost_estimate": response.get('cost_estimate', ''),
            "latency_ms": response.get('latency_ms', 0),
        })

    except Exception as e:
        safe_print(f"[API] Error generating jokes: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/generate-spotlight', methods=['POST'])
def generate_spotlight():
    """Generate an employee spotlight blurb"""
    try:
        if not claude_client:
            return jsonify({"success": False, "error": "Claude AI is not available"}), 503

        data = request.json
        name = data.get('name', '')
        fun_facts = data.get('fun_facts', '')

        if not name:
            return jsonify({"success": False, "error": "Employee name is required"}), 400

        # Look up employee in the EMPLOYEES list
        employee = find_employee(name)
        if not employee:
            return jsonify({"success": False, "error": f"Employee '{name}' not found"}), 404

        safe_print(f"[API] Generating spotlight for {employee['name']} ({employee['department']})")

        prompt = AI_PROMPTS['generate_spotlight'].format(
            name=employee['name'],
            title=employee['title'],
            department=employee['department'],
            fun_facts=fun_facts if fun_facts else 'No fun facts provided',
        )

        response = claude_client.generate_content(
            prompt=prompt,
            system_prompt=BRITESIDE_SYSTEM_PROMPT,
            max_tokens=300,
            temperature=0.7,
        )

        spotlight_text = response.get('content', '').strip()

        safe_print(f"[API] Spotlight generated ({response.get('tokens', 0)} tokens, {response.get('latency_ms', 0)}ms)")

        return jsonify({
            "success": True,
            "spotlight": spotlight_text,
            "employee": {
                "name": employee['name'],
                "title": employee['title'],
                "department": employee['department'],
            },
            "model": response.get('model', ''),
            "tokens": response.get('tokens', 0),
            "cost_estimate": response.get('cost_estimate', ''),
            "latency_ms": response.get('latency_ms', 0),
        })

    except Exception as e:
        safe_print(f"[API] Error generating spotlight: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/rewrite-content', methods=['POST'])
def rewrite_content():
    """General-purpose content rewrite endpoint"""
    try:
        if not claude_client:
            return jsonify({"success": False, "error": "Claude AI is not available"}), 503

        data = request.json
        content = data.get('content', '')
        tone = data.get('tone', 'fun and punny')

        if not content:
            return jsonify({"success": False, "error": "Content is required"}), 400

        safe_print(f"[API] Rewriting content ({len(content)} chars, tone: {tone})")

        prompt = (
            f"Rewrite the following text in a {tone} tone, keeping the same meaning and key information.\n\n"
            f"Original text:\n{content}\n\n"
            f"Requirements:\n"
            f"- Maintain the core message and facts\n"
            f"- Apply the requested tone consistently\n"
            f"- Keep it concise — aim for 2-3 sentences max, no filler\n"
            f"- Return ONLY the rewritten text, nothing else"
        )

        response = claude_client.generate_content(
            prompt=prompt,
            system_prompt=BRITESIDE_SYSTEM_PROMPT,
            max_tokens=500,
            temperature=0.7,
        )

        rewritten = response.get('content', content).strip()

        safe_print(f"[API] Content rewritten ({response.get('tokens', 0)} tokens, {response.get('latency_ms', 0)}ms)")

        return jsonify({
            "success": True,
            "rewritten": rewritten,
            "original_length": len(content),
            "rewritten_length": len(rewritten),
            "model": response.get('model', ''),
            "tokens": response.get('tokens', 0),
            "cost_estimate": response.get('cost_estimate', ''),
            "latency_ms": response.get('latency_ms', 0),
        })

    except Exception as e:
        safe_print(f"[API] Error rewriting content: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================================
# ROUTES - MEDIA UPLOAD
# ============================================================================

ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
ALLOWED_VIDEO_TYPES = {'video/mp4', 'video/quicktime', 'video/webm'}
MAX_IMAGE_SIZE = 10 * 1024 * 1024   # 10MB
MAX_VIDEO_SIZE = 50 * 1024 * 1024   # 50MB
MAX_GIF_SIZE = 3 * 1024 * 1024      # 3MB hard cap for GIFs (no resize)

# Pillow + python-magic for the media section pipeline
try:
    from PIL import Image
    import io as _io
    PIL_AVAILABLE = True
except Exception as _pil_err:
    PIL_AVAILABLE = False
    print(f"[WARNING] Pillow not available: {_pil_err}")

try:
    import magic as _magic
    MAGIC_AVAILABLE = True
except Exception as _magic_err:
    MAGIC_AVAILABLE = False
    print(f"[WARNING] python-magic not available: {_magic_err}")


def _detect_mime(file_bytes):
    """Detect MIME from magic bytes. Returns content-type header as fallback."""
    if MAGIC_AVAILABLE:
        try:
            return _magic.from_buffer(file_bytes[:2048], mime=True)
        except Exception:
            pass
    return None


def _optimize_image(file_bytes, detected_mime):
    """Resize static images to max 1200x800, strip EXIF, re-encode.
    GIFs are passed through untouched (to preserve animation).
    Returns (processed_bytes, output_mime, ext).
    """
    if not PIL_AVAILABLE:
        return file_bytes, detected_mime, None

    if detected_mime == 'image/gif':
        return file_bytes, 'image/gif', '.gif'

    try:
        img = Image.open(_io.BytesIO(file_bytes))
        img.load()
        has_alpha = img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info)

        # Resize if larger than 1200x800 (preserve aspect)
        max_w, max_h = 1200, 800
        if img.width > max_w or img.height > max_h:
            img.thumbnail((max_w, max_h), Image.LANCZOS)

        out = _io.BytesIO()
        if has_alpha:
            img.save(out, format='PNG', optimize=True)
            return out.getvalue(), 'image/png', '.png'
        else:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(out, format='JPEG', quality=85, optimize=True, progressive=True)
            return out.getvalue(), 'image/jpeg', '.jpg'
    except Exception as e:
        safe_print(f"[MEDIA] Pillow optimize failed, using original: {e}")
        return file_bytes, detected_mime, None


@app.route('/api/upload-media', methods=['POST'])
def upload_media():
    """Upload an image, gif, or video to GCS and return its public URL.
    Images are auto-resized and re-encoded via Pillow; GIFs are capped at
    3MB and passed through untouched; videos are stored as-is."""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400

        file = request.files['file']
        if not file.filename:
            return jsonify({'success': False, 'error': 'Empty filename'}), 400

        file_data = file.read()
        if not file_data:
            return jsonify({'success': False, 'error': 'Empty file'}), 400

        # Prefer magic-bytes detection over client-supplied content type
        detected = _detect_mime(file_data) or (file.content_type or '')
        is_image = detected in ALLOWED_IMAGE_TYPES
        is_video = detected in ALLOWED_VIDEO_TYPES
        is_gif = detected == 'image/gif'

        if not is_image and not is_video:
            return jsonify({'success': False, 'error': f'Unsupported file type: {detected}'}), 400

        # Size gates
        if is_gif and len(file_data) > MAX_GIF_SIZE:
            return jsonify({'success': False, 'error': f'GIF too large. Max {MAX_GIF_SIZE // (1024*1024)}MB.'}), 400
        max_size = MAX_IMAGE_SIZE if is_image else MAX_VIDEO_SIZE
        if len(file_data) > max_size:
            limit_mb = max_size // (1024 * 1024)
            return jsonify({'success': False, 'error': f'File too large. Max {limit_mb}MB.'}), 400

        # Pillow optimize for static images (not gifs, not videos)
        final_data = file_data
        final_mime = detected
        final_ext = None
        if is_image and not is_gif:
            final_data, final_mime, final_ext = _optimize_image(file_data, detected)

        if not final_ext:
            final_ext = os.path.splitext(file.filename)[1].lower()
            if not final_ext:
                final_ext = '.jpg' if is_image else '.mp4'

        month_prefix = datetime.now(CHICAGO_TZ).strftime('%Y-%m')
        unique_name = f"{uuid.uuid4().hex}{final_ext}"
        blob_path = f"media/{month_prefix}/{unique_name}"

        bucket = gcs_client.bucket(GCS_MEDIA_BUCKET)
        blob = bucket.blob(blob_path)
        blob.upload_from_string(final_data, content_type=final_mime)

        public_url = f"https://storage.googleapis.com/{GCS_MEDIA_BUCKET}/{blob_path}"
        safe_print(f"[MEDIA] Uploaded {blob_path} ({len(final_data)} bytes, was {len(file_data)})")

        return jsonify({
            'success': True,
            'url': public_url,
            'filename': unique_name,
            'type': 'gif' if is_gif else ('image' if is_image else 'video'),
            'size': len(final_data),
            'original_size': len(file_data),
            'mime': final_mime,
        })

    except Exception as e:
        safe_print(f"[MEDIA UPLOAD ERROR] {str(e)}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Helpers for OG / YouTube preview (SSRF-safe URL fetching)
# ---------------------------------------------------------------------------

import ipaddress as _ipaddress
import socket as _socket
from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs

try:
    from bs4 import BeautifulSoup as _BeautifulSoup
    BS4_AVAILABLE = True
except Exception as _bs4_err:
    BS4_AVAILABLE = False
    print(f"[WARNING] beautifulsoup4 not available: {_bs4_err}")

MEDIA_FETCH_TIMEOUT = 5
MEDIA_FETCH_MAX_BYTES = 2 * 1024 * 1024


def _is_public_host(hostname):
    """Resolve hostname and confirm every returned IP is publicly routable."""
    if not hostname:
        return False
    try:
        infos = _socket.getaddrinfo(hostname, None)
    except _socket.gaierror:
        return False
    for info in infos:
        ip = info[4][0]
        try:
            addr = _ipaddress.ip_address(ip)
        except ValueError:
            return False
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast or addr.is_unspecified:
            return False
    return True


def _safe_fetch(url):
    """Fetch an external URL with SSRF + timeout + size limits.
    Returns (status_code, content_bytes, content_type) or raises ValueError."""
    parsed = _urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError('Only http(s) URLs are allowed')
    if not _is_public_host(parsed.hostname or ''):
        raise ValueError('Host is not publicly routable')

    resp = http_requests.get(
        url,
        timeout=MEDIA_FETCH_TIMEOUT,
        allow_redirects=False,
        stream=True,
        headers={'User-Agent': 'BriteSide-Preview/1.0 (+https://brite.co)'},
    )
    # Redirect handling: only follow redirect if new host is still public
    if resp.is_redirect or resp.is_permanent_redirect:
        loc = resp.headers.get('Location', '')
        if not loc:
            raise ValueError('Redirect without Location header')
        new_parsed = _urlparse(loc)
        if new_parsed.scheme and not _is_public_host(new_parsed.hostname or ''):
            raise ValueError('Redirect target is not publicly routable')
        resp = http_requests.get(
            loc, timeout=MEDIA_FETCH_TIMEOUT, allow_redirects=False, stream=True,
            headers={'User-Agent': 'BriteSide-Preview/1.0 (+https://brite.co)'},
        )

    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=16384):
        buf.extend(chunk)
        if len(buf) > MEDIA_FETCH_MAX_BYTES:
            break
    return resp.status_code, bytes(buf), resp.headers.get('Content-Type', '')


def _extract_og(html_text, base_url):
    """Extract Open Graph and basic metadata tags from an HTML page."""
    result = {'title': '', 'description': '', 'image': '', 'site_name': '', 'url': base_url}
    if not BS4_AVAILABLE:
        return result
    try:
        soup = _BeautifulSoup(html_text, 'lxml')
    except Exception:
        soup = _BeautifulSoup(html_text, 'html.parser')

    def meta(prop_or_name):
        tag = soup.find('meta', attrs={'property': prop_or_name}) or soup.find('meta', attrs={'name': prop_or_name})
        return (tag.get('content') or '').strip() if tag else ''

    result['title'] = meta('og:title') or (soup.title.string.strip() if soup.title and soup.title.string else '')
    result['description'] = meta('og:description') or meta('description') or meta('twitter:description')
    result['image'] = meta('og:image') or meta('twitter:image')
    result['site_name'] = meta('og:site_name')
    og_url = meta('og:url')
    if og_url:
        result['url'] = og_url
    return result


@app.route('/api/media/og-preview')
def media_og_preview():
    """Fetch a URL and return its Open Graph / meta preview data."""
    url = (request.args.get('url') or '').strip()
    if not url:
        return jsonify({'success': False, 'error': 'url parameter is required'}), 400
    try:
        status, body, ctype = _safe_fetch(url)
        if status >= 400:
            return jsonify({'success': False, 'error': f'Source returned {status}'}), 502
        if 'text/html' not in (ctype or '').lower():
            return jsonify({'success': False, 'error': 'URL is not an HTML page'}), 400
        data = _extract_og(body.decode('utf-8', errors='replace'), url)
        return jsonify({'success': True, 'og': data})
    except ValueError as ve:
        return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        safe_print(f"[MEDIA OG ERROR] {e}")
        return jsonify({'success': False, 'error': 'Preview failed'}), 500


def _extract_youtube_id(url):
    """Pull the 11-char YouTube video ID out of any of the common URL shapes."""
    if not url:
        return None
    try:
        parsed = _urlparse(url)
    except Exception:
        return None
    host = (parsed.hostname or '').lower()
    if host in ('youtu.be',):
        return parsed.path.lstrip('/').split('/')[0] or None
    if 'youtube.com' in host:
        qs = _parse_qs(parsed.query)
        if 'v' in qs and qs['v']:
            return qs['v'][0]
        # /embed/<id>, /shorts/<id>, /live/<id>
        parts = [p for p in parsed.path.split('/') if p]
        if len(parts) >= 2 and parts[0] in ('embed', 'shorts', 'live', 'v'):
            return parts[1]
    return None


def _resolve_youtube_thumbnail(video_id):
    """Return the best available YouTube thumbnail URL. Falls back when maxres is missing."""
    for variant in ('maxresdefault', 'hqdefault', 'mqdefault', '0'):
        candidate = f"https://i.ytimg.com/vi/{video_id}/{variant}.jpg"
        try:
            head = http_requests.head(candidate, timeout=MEDIA_FETCH_TIMEOUT, allow_redirects=True)
            if head.status_code == 200:
                return candidate
        except Exception:
            continue
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


@app.route('/api/media/youtube')
def media_youtube():
    """Resolve a YouTube URL to a thumbnail + title via oEmbed."""
    url = (request.args.get('url') or '').strip()
    if not url:
        return jsonify({'success': False, 'error': 'url parameter is required'}), 400
    video_id = _extract_youtube_id(url)
    if not video_id:
        return jsonify({'success': False, 'error': 'Not a recognizable YouTube URL'}), 400
    try:
        title = ''
        try:
            oembed = http_requests.get(
                'https://www.youtube.com/oembed',
                params={'url': f'https://www.youtube.com/watch?v={video_id}', 'format': 'json'},
                timeout=MEDIA_FETCH_TIMEOUT,
            )
            if oembed.status_code == 200:
                title = oembed.json().get('title', '')
        except Exception:
            pass
        thumbnail = _resolve_youtube_thumbnail(video_id)
        return jsonify({
            'success': True,
            'video_id': video_id,
            'title': title,
            'thumbnail': thumbnail,
            'url': f'https://www.youtube.com/watch?v={video_id}',
        })
    except Exception as e:
        safe_print(f"[MEDIA YT ERROR] {e}")
        return jsonify({'success': False, 'error': 'YouTube lookup failed'}), 500


# ============================================================================
# ROUTES - GAME / PUZZLE
# ============================================================================

@app.route('/api/generate-game', methods=['POST'])
def generate_game():
    """Generate a monthly game/puzzle using AI"""
    try:
        if not claude_client:
            return jsonify({"success": False, "error": "Claude AI is not available"}), 503

        data = request.json
        game_type = data.get('type', 'word_scramble')
        context = data.get('context', '')
        month = data.get('month', datetime.now(CHICAGO_TZ).strftime('%B'))

        safe_print(f"[API] Generating {game_type} game for {month}")

        prompt = AI_PROMPTS.get('generate_game', '').format(
            game_type=game_type,
            context=context,
            month=month,
        )

        response = claude_client.generate_content(
            prompt=prompt,
            system_prompt=BRITESIDE_SYSTEM_PROMPT,
            max_tokens=800,
            temperature=0.8,
        )

        game_text = response.get('content', '').strip()

        # The model is asked for raw JSON but sometimes wraps it in ```json fences
        # or truncates at the token cap. Strip fences and validate here so the
        # frontend gets clean JSON (or a clear parse_ok=false) instead of silently
        # saving a blank game.
        cleaned = _strip_code_fences(game_text)
        parsed = None
        parse_ok = False
        try:
            parsed = json.loads(cleaned)
            parse_ok = True
        except (ValueError, TypeError):
            pass

        safe_print(f"[API] Game generated ({response.get('tokens', 0)} tokens, parse_ok={parse_ok})")

        return jsonify({
            "success": True,
            "game_content": cleaned,
            "game": parsed,
            "parse_ok": parse_ok,
            "model": response.get('model', ''),
            "tokens": response.get('tokens', 0),
            "cost_estimate": response.get('cost_estimate', ''),
        })

    except Exception as e:
        safe_print(f"[API] Error generating game: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/save-game-answer', methods=['POST'])
def save_game_answer():
    """Save game answer to GCS for next month's reveal"""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503
    try:
        data = request.json
        month = data.get('month', '').lower()
        year = data.get('year', datetime.now(CHICAGO_TZ).year)
        answer = data.get('answer', '')
        game_type = data.get('type', '')

        blob_name = f"games/{month}-{year}.json"
        game_data = {
            'month': month,
            'year': year,
            'type': game_type,
            'answer': answer,
            'savedAt': datetime.now(CHICAGO_TZ).isoformat(),
        }

        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blob = bucket.blob(blob_name)
        blob.upload_from_string(json.dumps(game_data), content_type='application/json')

        safe_print(f"[GAME] Saved answer for {month} {year}")
        return jsonify({'success': True, 'file': blob_name})

    except Exception as e:
        safe_print(f"[GAME SAVE ERROR] {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/get-previous-game', methods=['GET'])
def get_previous_game():
    """Load previous month's game answer from GCS"""
    if not gcs_client:
        return jsonify({'success': True, 'game': None})
    try:
        month_num = request.args.get('month', type=int)
        year = request.args.get('year', type=int, default=datetime.now(CHICAGO_TZ).year)

        if not month_num:
            return jsonify({'success': False, 'error': 'Month parameter required'}), 400

        # Calculate previous month
        if month_num == 1:
            prev_month_num = 12
            prev_year = year - 1
        else:
            prev_month_num = month_num - 1
            prev_year = year

        prev_month_name = MONTH_NAMES.get(prev_month_num, '').lower()
        blob_name = f"games/{prev_month_name}-{prev_year}.json"

        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blob = bucket.blob(blob_name)

        if not blob.exists():
            return jsonify({'success': True, 'game': None})

        game_data = json.loads(blob.download_as_text())
        return jsonify({'success': True, 'game': game_data})

    except Exception as e:
        safe_print(f"[GAME LOAD ERROR] {str(e)}")
        return jsonify({'success': True, 'game': None})


# ============================================================================
# ROUTES - EMAIL RENDERING & SENDING
# ============================================================================

def _build_media_html(media, FONT):
    """Render the optional Intro-addon media block.
    Returns a full <tr>...</tr> block, or '' if disabled/empty."""
    if not media or not media.get('enabled'):
        return ''

    kind = (media.get('type') or '').lower()
    if not kind:
        return ''

    alt = esc(media.get('alt_text', '')) or esc(media.get('og', {}).get('title', '')) or 'Featured media'
    link_url = (media.get('link_url') or '').strip()
    source_url = (media.get('og', {}).get('source_url') or media.get('og', {}).get('url') or '').strip()
    image_url = (media.get('image_url') or '').strip()
    og = media.get('og') or {}
    header_text = esc((media.get('header') or '').strip())
    intro_text = esc((media.get('intro_text') or '').strip())

    header_html = (
        f'<p style="margin:0 0 8px 0; font-family:{FONT}; font-size:13px; font-weight:800; '
        f'color:#31D7CA; text-transform:uppercase; letter-spacing:2px; text-align:center;">{header_text}</p>'
    ) if header_text else ''
    intro_html = (
        f'<p style="margin:0 0 14px 0; font-family:{FONT}; font-size:15px; color:#272D3F; '
        f'line-height:22px; text-align:center;">{intro_text}</p>'
    ) if intro_text else ''

    img_style = (
        'display:block; width:100%; max-width:600px; height:auto; '
        'border:0; border-radius:12px; margin:0 auto;'
    )

    def _wrap(inner_html):
        divider = (
            '<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="margin:0 0 20px 0;">'
            '<tr><td style="height:2px; background-color:#31D7CA; font-size:1px; line-height:1px; border-radius:1px;">&nbsp;</td></tr>'
            '</table>'
        )
        return (
            '<tr style="display: {{MEDIA_DISPLAY_INNER}};">'
            '<td style="padding: 8px 40px 16px 40px;" class="mobile-padding">'
            + divider +
            '<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">'
            '<tr><td>' + inner_html + '</td></tr>'
            '</table></td></tr>'
        )

    # Simple image/gif/meme/social-screenshot — full-width responsive image
    if kind in ('image', 'gif', 'meme', 'social'):
        safe_image = safe_url(image_url)
        if not safe_image:
            return ''
        href = safe_url(link_url or source_url)
        img_tag = f'<img src="{safe_image}" alt="{alt}" width="600" style="{img_style}">'
        media_el = f'<a href="{href}" target="_blank" style="text-decoration:none;">{img_tag}</a>' if href else img_tag
        inner = header_html + intro_html + media_el
        return _wrap(inner).replace('{{MEDIA_DISPLAY_INNER}}', 'table-row')

    # YouTube — thumbnail + (editable) title + Watch button
    if kind == 'youtube':
        thumb = safe_url((og.get('image') or '').strip())
        yt_url = safe_url((og.get('source_url') or og.get('url') or '').strip())
        title = esc(og.get('title') or 'Watch on YouTube')
        if not thumb or not yt_url:
            return ''
        media_el = (
            f'<a href="{yt_url}" target="_blank" style="text-decoration:none; color:inherit;">'
            f'<div style="line-height:0;"><img src="{thumb}" alt="{alt}" width="600" style="{img_style}"></div>'
            f'<p style="margin:12px 0 4px 0; font-family:{FONT}; font-size:17px; font-weight:700; color:#272D3F;">{title}</p>'
            f'<p style="margin:0; font-family:{FONT}; font-size:13px; font-weight:600; color:#31D7CA; text-transform:uppercase; letter-spacing:1px;">Watch on YouTube &rarr;</p>'
            f'</a>'
        )
        inner = header_html + intro_html + media_el
        return _wrap(inner).replace('{{MEDIA_DISPLAY_INNER}}', 'table-row')

    # News / article — hotlinked OG card (title + desc editable upstream)
    if kind == 'news':
        title = esc(og.get('title') or '')
        desc = esc((og.get('description') or '')[:200])
        site = esc(og.get('site_name') or '')
        img = safe_url((og.get('image') or '').strip())
        url_out = safe_url((og.get('source_url') or og.get('url') or '').strip())
        if not url_out or not title:
            return ''

        img_html = (
            f'<img src="{img}" alt="{alt}" width="600" style="{img_style}">'
            if img else ''
        )
        media_el = (
            f'<a href="{url_out}" target="_blank" style="text-decoration:none; color:inherit;">'
            + (f'<div style="line-height:0; margin-bottom:14px;">{img_html}</div>' if img_html else '')
            + (f'<p style="margin:0 0 4px 0; font-family:{FONT}; font-size:12px; font-weight:700; color:#31D7CA; text-transform:uppercase; letter-spacing:1px;">{site}</p>' if site else '')
            + f'<p style="margin:0 0 6px 0; font-family:{FONT}; font-size:18px; font-weight:700; color:#272D3F; line-height:24px;">{title}</p>'
            + (f'<p style="margin:0 0 10px 0; font-family:{FONT}; font-size:14px; color:#6b7280; line-height:20px;">{desc}</p>' if desc else '')
            + f'<p style="margin:0; font-family:{FONT}; font-size:13px; font-weight:700; color:#31D7CA; text-transform:uppercase; letter-spacing:1px;">Read more &rarr;</p>'
            + '</a>'
        )
        inner = header_html + intro_html + media_el
        return _wrap(inner).replace('{{MEDIA_DISPLAY_INNER}}', 'table-row')

    return ''


@app.route('/api/render-email', methods=['POST'])
def render_email():
    """Render the newsletter email template with provided content"""
    try:
        data = request.json

        # Extract content fields from request body
        month = data.get('month', datetime.now(CHICAGO_TZ).strftime('%B'))
        year = data.get('year', datetime.now(CHICAGO_TZ).year)
        joke = data.get('joke', '')
        birthdays = data.get('birthdays', [])
        birthday_headings = data.get('birthday_headings') or {}
        spotlight = data.get('spotlight', {})
        spotlights = data.get('spotlights', [])
        updates = data.get('updates', [])
        updates_enabled = data.get('updates_enabled', True)
        special_section = data.get('special_section', {})
        welcome_hires = data.get('welcome_hires', [])
        welcome_enabled = data.get('welcome_enabled', False)
        game = data.get('game', {})
        media = data.get('media', {}) or {}

        # Template selection (default to classic)
        template_file = data.get('template', 'briteside-email.html')
        allowed_templates = [
            'briteside-email.html',
            'briteside-email-playful.html',
            'briteside-email-teal.html',
        ]
        if template_file not in allowed_templates:
            template_file = 'briteside-email.html'

        safe_print(f"[API] Rendering email template '{template_file}' for {month} {year}")

        # Read the email template
        template_path = os.path.join(os.path.dirname(__file__), 'templates', template_file)
        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                html = f.read()
        except FileNotFoundError:
            return jsonify({"success": False, "error": f"Template not found: {template_path}"}), 404

        # Consistent font family for all dynamically generated HTML
        FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif"

        # Build birthday HTML rows (grouped by month if multiple months supplied)
        birthday_html = ''
        primary_month_display = str(month).capitalize() if month else ''
        primary_month_num = data.get('month_num') or 0

        def _month_abbrev(month_num, fallback_full):
            full = MONTH_NAMES.get(int(month_num)) if month_num else None
            full = full or fallback_full
            return (full or '')[:3]

        def _initials(name):
            parts = [p for p in (name or '').strip().split() if p]
            if not parts:
                return '?'
            if len(parts) == 1:
                return parts[0][0].upper()
            return (parts[0][0] + parts[-1][0]).upper()

        # Stable palette for initials circles
        _AVATAR_COLORS = ['#31D7CA', '#5B6CF7', '#F59E0B', '#EF4444', '#8B5CF6', '#10B981', '#EC4899']

        def _avatar_cell(bday):
            img_url = (bday.get('image_url') or '').strip()
            name = bday.get('name', '')
            if img_url:
                avatar = (
                    f'<img src="{esc(img_url)}" width="60" height="60" alt="{esc(name)}" '
                    f'style="display:block; width:60px; height:60px; border-radius:50%; object-fit:cover; border:0;">'
                )
            else:
                initials = esc(_initials(name))
                color = _AVATAR_COLORS[(sum(ord(c) for c in name) if name else 0) % len(_AVATAR_COLORS)]
                avatar = (
                    f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="60" height="60" '
                    f'style="width:60px; height:60px; border-radius:50%; background-color:{color};">'
                    f'<tr><td align="center" valign="middle" '
                    f'style="font-family:{FONT}; font-size:22px; font-weight:700; color:#ffffff; line-height:60px;">{initials}</td></tr>'
                    f'</table>'
                )
            return (
                f'<td width="76" valign="middle" style="width:76px; padding: 10px 12px 10px 0;">{avatar}</td>'
            )

        def _row_for(bday, month_num_for_row):
            bday_name = esc(bday.get('name', ''))
            bday_day = esc(bday.get('birthday_day', ''))
            bday_dept = esc(bday.get('department', ''))
            mon_abbrev = esc(_month_abbrev(month_num_for_row, primary_month_display))
            meta_bits = []
            if mon_abbrev and bday_day:
                meta_bits.append(f'{mon_abbrev} {bday_day}')
            if bday_dept:
                meta_bits.append(bday_dept)
            meta = ' &middot; '.join(meta_bits)
            return (
                '<tr>'
                + _avatar_cell(bday)
                + f'<td valign="middle" style="padding: 10px 0;">'
                f'<div style="font-family:{FONT}; font-size:15px; font-weight:600; color:#272D3F; text-align:left;">{bday_name}</div>'
                f'<div style="font-family:{FONT}; font-size:14px; color:#6b7280; text-align:left;">{meta}</div>'
                f'</td>'
                '</tr>'
            )

        def _subheading_row(label):
            return (
                f'<tr><td colspan="2" style="padding: 18px 0 6px 0; '
                f'font-family:{FONT}; font-size:15px; font-weight:700; color:#272D3F; text-align:center;">{esc(label)}</td></tr>'
            )

        if birthdays:
            # Group by month_num; treat missing month_num as primary
            primary_group = []
            secondary_groups = {}  # month_num -> list
            for bday in birthdays:
                m = bday.get('month_num') or primary_month_num or 0
                if not primary_month_num or m == primary_month_num:
                    primary_group.append(bday)
                else:
                    secondary_groups.setdefault(int(m), []).append(bday)

            has_secondary = any(secondary_groups.values())
            user_primary_heading = (birthday_headings.get('primary') or '').strip()
            user_secondary_heading = (birthday_headings.get('secondary') or '').strip()

            items = []
            if has_secondary and primary_group:
                primary_label = user_primary_heading or f'{primary_month_display} Birthdays'
                items.append(_subheading_row(primary_label))
            for bday in primary_group:
                items.append(_row_for(bday, primary_month_num))
            for sec_month_num in sorted(secondary_groups.keys()):
                sec_name = MONTH_NAMES.get(sec_month_num, '')
                # Only use the user's secondary heading for the first secondary month;
                # subsequent ones (rare — user can only pick one in the UI today) fall back to the default.
                if has_secondary and sec_month_num == sorted(secondary_groups.keys())[0]:
                    sec_label = user_secondary_heading or f'Also celebrating in {sec_name}'
                else:
                    sec_label = f'Also celebrating in {sec_name}'
                items.append(_subheading_row(sec_label))
                for bday in secondary_groups[sec_month_num]:
                    items.append(_row_for(bday, sec_month_num))

            birthday_html = '\n'.join(items)

        # Build work anniversary rows (auto-pulled for the issue month)
        anniversaries = data.get('anniversaries', [])
        anniversaries_enabled = data.get('anniversaries_enabled', True)
        anniversary_html = ''
        if anniversaries_enabled and anniversaries:
            ann_items = []
            for a in anniversaries:
                a_name = esc(a.get('name', ''))
                a_dept = esc(a.get('department', ''))
                try:
                    yrs = int(a.get('years') or 0)
                except (TypeError, ValueError):
                    yrs = 0
                a_day = esc(a.get('anniversary_day', ''))
                mon_abbrev = esc(_month_abbrev(a.get('anniversary_month') or primary_month_num, primary_month_display))
                bits = []
                if yrs >= 1:
                    bits.append(f"{yrs} year" + ("s" if yrs != 1 else ""))
                if mon_abbrev and a_day:
                    bits.append(f"{mon_abbrev} {a_day}")
                if a_dept:
                    bits.append(a_dept)
                meta = ' &middot; '.join(bits)
                ann_items.append(
                    '<tr>'
                    + _avatar_cell(a)
                    + f'<td valign="middle" style="padding: 10px 0;">'
                    f'<div style="font-family:{FONT}; font-size:15px; font-weight:600; color:#272D3F; text-align:left;">{a_name}</div>'
                    f'<div style="font-family:{FONT}; font-size:14px; color:#6b7280; text-align:left;">&#127881; {meta}</div>'
                    f'</td>'
                    '</tr>'
                )
            anniversary_html = '\n'.join(ann_items)

        # Build welcome hires HTML
        welcome_html = ''
        if welcome_enabled and welcome_hires:
            hire_items = []
            for hire in welcome_hires:
                h_name = esc(hire.get('name', ''))
                h_role = esc(hire.get('role', ''))
                h_fact = esc(hire.get('fun_fact', ''))
                hire_items.append(
                    f'<tr><td align="center" style="padding: 14px 0; border-bottom: 1px solid #e8f5f5; font-family: {FONT}; text-align: center;">'
                    f'<p style="margin: 0 0 2px 0; font-family: {FONT}; font-size: 17px; font-weight: 700; color: #272D3F;">{h_name}</p>'
                    f'<p style="margin: 0; font-family: {FONT}; font-size: 14px; color: #31D7CA; font-style: italic;">{h_role}</p>'
                    + (f'<p style="margin: 4px 0 0 0; font-family: {FONT}; font-size: 13px; color: #6b7280;">Fun fact: {h_fact}</p>' if h_fact else '')
                    + '</td></tr>'
                )
            welcome_html = '\n'.join(hire_items)

        # Build spotlight section HTML (supports 1-3 spotlights)
        # Use spotlights array if available, fall back to single spotlight
        spotlight_list = spotlights if spotlights else ([spotlight] if spotlight and spotlight.get('name') else [])
        spotlight_section_html = ''
        valid_spotlights = [sp for sp in spotlight_list if sp and sp.get('name')]
        for sp_idx, sp in enumerate(valid_spotlights):
            sp_name = esc(sp.get('name', ''))
            sp_title = esc(sp.get('title', ''))
            sp_blurb = esc(sp.get('blurb', ''))
            sp_fun_facts = esc(sp.get('fun_facts', ''))
            sp_image_url = safe_url(sp.get('image_url', ''))

            # Add separator between multiple spotlights
            if sp_idx > 0:
                spotlight_section_html += (
                    f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">'
                    f'<tr><td style="height: 1px; padding: 28px 0; font-size: 1px; line-height: 1px;">'
                    f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">'
                    f'<tr><td style="height: 0; border-top: 2px dashed #31D7CA; font-size: 1px; line-height: 1px;">&nbsp;</td></tr>'
                    f'</table></td></tr></table>'
                )

            spotlight_section_html += '<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">'

            sp_image_html = ''
            if sp_image_url:
                spotlight_section_html += (
                    f'<tr><td align="center" style="padding-bottom: 16px;">'
                    f'<!--[if !mso]><!-->'
                    f'<img src="{sp_image_url}" width="120" alt="{sp_name}" '
                    f'style="width: 120px; height: 120px; border-radius: 50%; object-fit: cover; display: block;">'
                    f'<!--<![endif]-->'
                    f'<!--[if mso]>'
                    f'<img src="{sp_image_url}" width="120" alt="{sp_name}" '
                    f'style="width: 120px; height: auto; display: block;">'
                    f'<![endif]-->'
                    f'</td></tr>'
                )

            # Name and title
            spotlight_section_html += (
                f'<tr><td align="center" style="padding-bottom: 2px;">'
                f'<p style="margin: 0; font-family: {FONT}; font-size: 20px; font-weight: 800; color: #272D3F;">{sp_name}</p>'
                f'</td></tr>'
                f'<tr><td align="center" style="padding-bottom: 16px;">'
                f'<p style="margin: 0; font-family: {FONT}; font-size: 13px; font-weight: 700; color: #31D7CA; text-transform: uppercase; letter-spacing: 1px;">{sp_title}</p>'
                f'</td></tr>'
            )

            # Build Q&A HTML if present — centered
            sp_qa = sp.get('qa', [])
            qa_html = ''
            for pair in sp_qa:
                q_text = esc(pair.get('q', ''))
                a_text = esc(pair.get('a', ''))
                if q_text and a_text:
                    qa_html += (
                        f'<tr><td align="center" style="padding: 6px 0; text-align: center;">'
                        f'<p style="margin: 0; font-family: {FONT}; font-size: 14px; color: #272D3F; font-weight: 700;">{q_text}</p>'
                        f'<p style="margin: 0; font-family: {FONT}; font-size: 15px; color: #444444;">{a_text}</p>'
                        f'</td></tr>'
                    )

            if qa_html:
                spotlight_section_html += (
                    f'<tr><td style="padding: 0 0 16px 0;">'
                    f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">'
                    f'{qa_html}</table></td></tr>'
                )

            # Show blurb if present
            if sp_blurb:
                spotlight_section_html += (
                    f'<tr><td align="center" style="padding-bottom: 8px; text-align: center;">'
                    f'<p style="margin: 0; font-family: {FONT}; font-size: 15px; line-height: 25px; color: #444444; font-style: italic;">{sp_blurb}</p>'
                    f'</td></tr>'
                )
            # Show fun facts below blurb (or on its own if no blurb)
            if sp_fun_facts:
                spotlight_section_html += (
                    f'<tr><td align="center" style="padding-bottom: 8px; text-align: center;">'
                    f'<p style="margin: 0; font-family: {FONT}; font-size: 14px; line-height: 22px; color: #6b7280;">Fun fact: {sp_fun_facts}</p>'
                    f'</td></tr>'
                )

            spotlight_section_html += '</table>'

        # For backward compat, also build single-spotlight placeholders
        if not spotlight:
            spotlight = {}
        spotlight_name = spotlight.get('name', '')
        spotlight_title_val = spotlight.get('title', '')
        spotlight_blurb = spotlight.get('blurb', '')
        spotlight_image_url = safe_url(spotlight.get('image_url', '')) if spotlight else ''
        spotlight_image_html = ''
        if spotlight_image_url:
            spotlight_image_html = (
                f'<img src="{spotlight_image_url}" width="120" alt="{spotlight_name}" '
                f'style="width: 120px; height: 120px; border-radius: 50%; object-fit: cover; display: block;" '
                f'class="mobile-img-full">'
            )

        # Build updates HTML with photos
        if not updates_enabled:
            updates = []

        # Build individual update fields
        update_1_title = ''
        update_1_body = ''
        update_1_photos_html = ''
        update_2_title = ''
        update_2_body = ''
        update_2_photos_html = ''
        update_3_title = ''
        update_3_body = ''
        update_3_photos_html = ''
        update_4_title = ''
        update_4_body = ''
        update_4_photos_html = ''
        update_5_title = ''
        update_5_body = ''
        update_5_photos_html = ''
        if updates:
            for i, u in enumerate(updates[:5]):
                if not isinstance(u, dict):
                    continue
                title = esc(u.get('title', ''))
                body = esc(u.get('body', ''))
                photos = [s for s in (safe_url(p) for p in u.get('photos', [])) if s]
                photos_html = ''
                if len(photos) == 1:
                    # Single photo: full width, fixed height with drag-position
                    photo_positions = u.get('photo_positions', [])
                    pos_y = photo_positions[0] if photo_positions else 50
                    photos_html = (
                        f'<img src="{photos[0]}" width="516" '
                        f'style="width: 100%; height: 242px; object-fit: cover; '
                        f'object-position: center {pos_y}%; border-radius: 8px; '
                        f'margin-top: 12px; display: block;" '
                        f'alt="Update photo" class="mobile-img-full">'
                    )
                elif len(photos) >= 2:
                    # Multiple photos: side-by-side in a 2-column table
                    photos_html = (
                        '<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="margin-top: 12px;">'
                        '<tr>'
                    )
                    for p_idx, photo_url in enumerate(photos):
                        # Start a new row every 2 photos
                        if p_idx > 0 and p_idx % 2 == 0:
                            photos_html += '</tr><tr>'
                        cell_width = '50%' if (len(photos) - p_idx) >= 2 or p_idx % 2 == 1 else '100%'
                        pad_right = '4px' if p_idx % 2 == 0 and (p_idx + 1) < len(photos) else '0'
                        pad_left = '4px' if p_idx % 2 == 1 else '0'
                        colspan = ' colspan="2"' if cell_width == '100%' else ''
                        photos_html += (
                            f'<td{colspan} width="{cell_width}" style="padding-right: {pad_right}; padding-left: {pad_left}; padding-bottom: 8px;" valign="top">'
                            f'<img src="{photo_url}" width="248" '
                            f'style="width: 100%; height: auto; border-radius: 8px; display: block;" '
                            f'alt="Update photo" class="mobile-img-full">'
                            f'</td>'
                        )
                    photos_html += '</tr></table>'
                if i == 0:
                    update_1_title = title
                    update_1_body = body
                    update_1_photos_html = photos_html
                elif i == 1:
                    update_2_title = title
                    update_2_body = body
                    update_2_photos_html = photos_html
                elif i == 2:
                    update_3_title = title
                    update_3_body = body
                    update_3_photos_html = photos_html
                elif i == 3:
                    update_4_title = title
                    update_4_body = body
                    update_4_photos_html = photos_html
                elif i == 4:
                    update_5_title = title
                    update_5_body = body
                    update_5_photos_html = photos_html

        # Build special section HTML (frontend sends null if disabled)
        if not special_section:
            special_section = {}
        special_title = esc(special_section.get('title', ''))
        special_body = esc(special_section.get('body', ''))

        # Build game section HTML
        if not game:
            game = {}
        game_content = sanitize_basic_html(game.get('content', ''))
        game_image_url = safe_url(game.get('image_url', ''))
        game_previous_answer = esc(game.get('previous_answer', ''))

        game_section_html = ''
        if game_content or game_image_url:
            game_section_html += '<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">'
            if game_image_url:
                game_section_html += (
                    f'<tr><td align="center" style="padding-bottom: 10px; text-align: center;">'
                    f'<img src="{game_image_url}" width="460" '
                    f'style="width: 100%; max-width: 460px; height: auto; border-radius: 8px; display: block; margin: 0 auto;" '
                    f'alt="BriteSide Brain Teaser">'
                    f'</td></tr>'
                )
            if game_content:
                game_section_html += (
                    f'<tr><td align="center" style="padding-bottom: 10px; text-align: center;">'
                    f'<p style="margin: 0; font-family: {FONT}; font-size: 15px; line-height: 24px; color: #444444; text-align: center;">{game_content}</p>'
                    f'</td></tr>'
                )
            game_section_html += (
                f'<tr><td align="center" style="text-align: center;">'
                f'<p style="margin: 0; font-family: {FONT}; font-size: 15px; font-weight: 700; color: #018181; text-align: center;">'
                'Email Dove your answer &mdash; the winner gets 100 BriteCo Bucks!</p>'
                f'</td></tr>'
            )
            if game_previous_answer:
                game_section_html += (
                    f'<tr><td align="center" style="padding-top: 10px; text-align: center;">'
                    f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin: 0 auto;">'
                    f'<tr><td style="padding: 10px 14px; background-color: #f0fdf4; border-radius: 8px; border: 1px solid #86efac; text-align: center;">'
                    f'<p style="margin: 0 0 2px 0; font-family: {FONT}; font-size: 12px; font-weight: 700; text-transform: uppercase; color: #059669; letter-spacing: 1px;">Last Month\'s Answer</p>'
                    f'<p style="margin: 0; font-family: {FONT}; font-size: 15px; color: #272D3F;">{game_previous_answer}</p>'
                    f'</td></tr></table>'
                    f'</td></tr>'
                )
            game_section_html += '</table>'

        # Joke setup/punchline split (delimiter: |)
        joke_setup = esc(str(joke))
        joke_punchline = ''
        if '|' in str(joke):
            parts = str(joke).split('|', 1)
            joke_setup = esc(parts[0].strip())
            joke_punchline = esc(parts[1].strip())

        # Conditional display values
        birthday_display = 'table-row' if birthdays else 'none'
        anniversary_display = 'table-row' if (anniversaries_enabled and anniversary_html) else 'none'
        welcome_display = 'table-row' if (welcome_enabled and welcome_hires) else 'none'
        update_2_display = 'table-row' if (update_2_title or update_2_body) else 'none'
        update_3_display = 'table-row' if (update_3_title or update_3_body) else 'none'
        update_4_display = 'table-row' if (update_4_title or update_4_body) else 'none'
        update_5_display = 'table-row' if (update_5_title or update_5_body) else 'none'
        updates_display = 'table-row' if updates_enabled and updates else 'none'
        special_display = 'table-row' if (special_title or special_body) else 'none'
        game_display = 'table-row' if game_section_html else 'none'
        punchline_display = 'table-row' if joke_punchline else 'none'

        # Build media (optional Intro add-on) section — full <tr> block or ''
        media_html = _build_media_html(media, FONT)

        # Preheader text (short preview text for email clients)
        preheader = f"The BriteSide - {month} {year}"
        if joke_setup:
            preheader = joke_setup[:100]

        # Make logo paths absolute so they work in iframe previews and email clients
        base_url = request.host_url.rstrip('/')
        html = html.replace('/static/briteco-logo-white.png', f'{base_url}/static/briteco-logo-white.png')

        # Placeholder substitution in a SINGLE regex pass. Using one pass (rather
        # than a chain of str.replace calls) means a replacement value that
        # happens to contain a literal "{{TOKEN}}" — e.g. an update mentioning
        # {{GAME_SECTION}} — is never itself re-expanded into a server-built
        # block. Unknown placeholders are left untouched.
        month_cap = str(month).capitalize() if month else ''
        spotlight_display = 'table-row' if valid_spotlights else 'none'
        replacements = {
            'MONTH': month_cap,
            'YEAR': str(year),
            'PREHEADER': esc(str(preheader)),
            'INTRO_LINE': '',
            'INTRO_DISPLAY': 'none',
            'JOKE': esc(str(joke)),
            'JOKE_SETUP': joke_setup,
            'JOKE_PUNCHLINE': joke_punchline,
            'PUNCHLINE_DISPLAY': punchline_display,
            'MEDIA_SECTION': media_html,
            'BIRTHDAY_DISPLAY': birthday_display,
            'BIRTHDAY_SECTION': birthday_html,
            'ANNIVERSARY_DISPLAY': anniversary_display,
            'ANNIVERSARY_SECTION': anniversary_html,
            'WELCOME_DISPLAY': welcome_display,
            'WELCOME_SECTION': welcome_html,
            'SPOTLIGHT_DISPLAY': spotlight_display,
            'SPOTLIGHT_SECTION': spotlight_section_html,
            'SPOTLIGHT_IMAGE': spotlight_image_html,
            'SPOTLIGHT_NAME': esc(str(spotlight_name)),
            'SPOTLIGHT_TITLE': esc(str(spotlight_title_val)),
            'SPOTLIGHT_BLURB': esc(str(spotlight_blurb)),
            'UPDATES_DISPLAY': updates_display,
            'UPDATE_1_TITLE': str(update_1_title),
            'UPDATE_1_BODY': str(update_1_body),
            'UPDATE_1_PHOTOS': update_1_photos_html,
            'UPDATE_2_TITLE': str(update_2_title),
            'UPDATE_2_BODY': str(update_2_body),
            'UPDATE_2_PHOTOS': update_2_photos_html,
            'UPDATE_2_DISPLAY': update_2_display,
            'UPDATE_3_TITLE': str(update_3_title),
            'UPDATE_3_BODY': str(update_3_body),
            'UPDATE_3_PHOTOS': update_3_photos_html,
            'UPDATE_3_DISPLAY': update_3_display,
            'UPDATE_4_TITLE': str(update_4_title),
            'UPDATE_4_BODY': str(update_4_body),
            'UPDATE_4_PHOTOS': update_4_photos_html,
            'UPDATE_4_DISPLAY': update_4_display,
            'UPDATE_5_TITLE': str(update_5_title),
            'UPDATE_5_BODY': str(update_5_body),
            'UPDATE_5_PHOTOS': update_5_photos_html,
            'UPDATE_5_DISPLAY': update_5_display,
            'SPECIAL_TITLE': str(special_title),
            'SPECIAL_BODY': str(special_body),
            'SPECIAL_SECTION_DISPLAY': special_display,
            'GAME_DISPLAY': game_display,
            'GAME_SECTION': game_section_html,
        }
        html = re.sub(
            r'\{\{(\w+)\}\}',
            lambda m: replacements.get(m.group(1), m.group(0)),
            html,
        )

        safe_print(f"[API] Email template rendered ({len(html)} chars)")

        return jsonify({
            "success": True,
            "html": html,
            "meta": {
                "month": month,
                "year": year,
                "birthday_count": len(birthdays),
                "update_count": len(updates),
                "has_spotlight": bool(spotlight_name),
                "has_special_section": bool(special_title),
            }
        })

    except Exception as e:
        safe_print(f"[API] Error rendering email: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/send-newsletter', methods=['POST'])
def send_newsletter():
    """Send rendered newsletter HTML via SendGrid"""
    try:
        data = request.json
        recipients = data.get('recipients', [])
        subject = data.get('subject', '')
        html_content = data.get('html', '')

        if not recipients:
            return jsonify({"success": False, "error": "At least one recipient is required"}), 400

        if not subject:
            return jsonify({"success": False, "error": "Email subject is required"}), 400

        if not html_content:
            return jsonify({"success": False, "error": "HTML content is required"}), 400

        safe_print(f"[API] Sending newsletter to {len(recipients)} recipient(s): {subject}")

        if not SENDGRID_AVAILABLE:
            return jsonify({"success": False, "error": "SendGrid library not installed"}), 500

        sendgrid_api_key = os.environ.get('SENDGRID_API_KEY') or os.environ.get('_SENDGRID_API_KEY')
        from_email = os.environ.get('SENDGRID_FROM_EMAIL') or SENDGRID_CONFIG['from_email']
        from_name = os.environ.get('SENDGRID_FROM_NAME') or SENDGRID_CONFIG['from_name']

        if not sendgrid_api_key:
            return jsonify({"success": False, "error": "SendGrid API key not configured"}), 500

        sg = sendgrid.SendGridAPIClient(api_key=sendgrid_api_key)

        sent_count = 0
        errors = []

        for recipient in recipients:
            try:
                message = Mail(
                    from_email=(from_email, from_name),
                    to_emails=recipient,
                    subject=subject,
                    html_content=html_content
                )
                response = sg.send(message)
                if response.status_code in [200, 201, 202]:
                    sent_count += 1
                    safe_print(f"[API] Sent to {recipient}")
                else:
                    errors.append(f"Failed for {recipient}: status {response.status_code}")
                    safe_print(f"[API] Failed for {recipient}: status {response.status_code}")
            except Exception as email_error:
                errors.append(f"Failed for {recipient}: {str(email_error)}")
                safe_print(f"[API] Email error for {recipient}: {email_error}")

        safe_print(f"[API] Newsletter sent: {sent_count}/{len(recipients)} successful")

        return jsonify({
            "success": sent_count > 0,
            "message": f"Newsletter sent to {sent_count} of {len(recipients)} recipient(s)",
            "sent_count": sent_count,
            "total_recipients": len(recipients),
            "errors": errors if errors else None,
        })

    except Exception as e:
        safe_print(f"[API] Error sending newsletter: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================================
# SLACK INTEGRATION
# ============================================================================

@app.route('/api/send-to-slack', methods=['POST'])
def send_to_slack():
    """Post a message to Slack via incoming webhook"""
    try:
        webhook_url = os.environ.get('_SLACK_WEBHOOK_URL', os.environ.get('SLACK_WEBHOOK_URL', ''))
        if not webhook_url:
            return jsonify({"success": False, "error": "Slack webhook not configured. Add SLACK_WEBHOOK_URL to your environment."}), 400

        data = request.json
        message = data.get('message', '').strip()
        if not message:
            return jsonify({"success": False, "error": "Message cannot be empty"}), 400

        payload = {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":tada: *The BriteSide is here!*\n\n{message}"
                    }
                }
            ]
        }

        resp = http_requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200:
            safe_print(f"[API] Slack message sent successfully")
            return jsonify({"success": True})
        else:
            safe_print(f"[API] Slack webhook returned {resp.status_code}: {resp.text}")
            return jsonify({"success": False, "error": f"Slack returned status {resp.status_code}"}), 502

    except Exception as e:
        safe_print(f"[API] Error sending to Slack: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================================
# EMPLOYEE SYNC (BigQuery user_master -> Firestore)
# ============================================================================

def _slack_alert(message):
    """Best-effort Slack alert for the sync circuit breaker. Never raises."""
    webhook_url = os.environ.get('SLACK_WEBHOOK_URL') or os.environ.get('_SLACK_WEBHOOK_URL')
    if not webhook_url:
        return
    try:
        http_requests.post(
            webhook_url,
            json={'text': f":rotating_light: *BriteSide employee sync:* {message}"},
            timeout=10,
        )
    except Exception as exc:
        safe_print(f"[SYNC] Slack alert failed: {exc}")


def _run_user_sync(triggered_by):
    """Run the BigQuery->Firestore employee sync and return a JSON response."""
    if not firestore_client:
        return jsonify({'success': False, 'error': 'Firestore not available'}), 503
    try:
        summary = user_sync.run(
            firestore_client,
            gcs_client=gcs_client,
            # Only cache photos into a DEDICATED public media bucket; never into
            # the private drafts bucket.
            media_bucket=GCS_MEDIA_BUCKET,
            collection=EMPLOYEES_COLLECTION,
            triggered_by=triggered_by,
            alert_fn=_slack_alert,
            now_iso=datetime.now(CHICAGO_TZ).isoformat(),
        )
        return jsonify({'success': True, 'status': 'success', 'summary': summary})
    except user_sync.SyncSafetyTripped as exc:
        # Circuit breaker fired: creates/updates/backfills were committed, no
        # deactivations applied. 409 so the scheduler surfaces "needs attention".
        return jsonify({'success': False, 'status': 'action_required', 'error': str(exc)}), 409
    except Exception as exc:
        safe_print(f"[SYNC] Employee sync failed: {exc}")
        traceback.print_exc()
        return jsonify({'success': False, 'status': 'failed', 'error': str(exc)}), 500


@app.route('/api/jobs/sync-users', methods=['POST'])
def jobs_sync_users():
    """Cloud Scheduler entrypoint — authenticated by the X-Job-Secret header
    (NOT a user session; allowed past the before_request gate)."""
    auth_error = require_job_secret()
    if auth_error:
        return auth_error
    return _run_user_sync('scheduler')


@app.route('/api/sync-users', methods=['POST'])
def admin_sync_users():
    """Manual sync trigger for the newsletter team (editor-only via the gate)."""
    user = get_current_user() or {}
    return _run_user_sync(f"admin:{user.get('email', 'unknown')}")


# ============================================================================
# SUBMISSIONS (contributor entries -> curated Firestore databases)
# ============================================================================
# Any signed-in @brite.co user submits; the newsletter team (editors) selects
# from the curated lists when building an issue. Spotlight is one canonical
# profile per person (keyed by email, updatable); updates/culture/corrections
# are queues; nominations point at a colleague.

SPOTLIGHT_COLLECTION = 'spotlight_submissions'
UPDATE_SUBMISSIONS = 'update_submissions'
CULTURE_SUBMISSIONS = 'culture_submissions'
CORRECTION_SUBMISSIONS = 'correction_submissions'
NOMINATIONS_COLLECTION = 'nominations'

SPOTLIGHT_FIELDS = ['name', 'employee_type', 'job_title', 'birth_date', 'hire_date',
                    'residence', 'residence_location', 'describe_work',
                    'work_with_others', 'photo_url', 'qa']
UPDATE_FIELDS = ['summary', 'files', 'co_contributors', 'when_featured', 'specific_month', 'notes']
CULTURE_FIELDS = ['content_types', 'files', 'content', 'why_fits']
CORRECTION_FIELDS = ['prev_category_topic', 'prev_submitted_date', 'changes', 'files',
                     'handling', 'handling_other']
NOMINATION_FIELDS = ['nominee', 'has_own_entry']

_QUEUE_COLLECTIONS = {
    'updates': UPDATE_SUBMISSIONS,
    'culture': CULTURE_SUBMISSIONS,
    'corrections': CORRECTION_SUBMISSIONS,
    'nominations': NOMINATIONS_COLLECTION,
}


def _queue_collection(sub_type):
    """Resolve a queue collection from the URL segment, accepting the singular
    forms the frontend actually sends ('update', 'correction') alongside the
    plural keys above. Before this, /api/submissions/update 404'd and the
    builder's updates/corrections panels silently showed empty."""
    key = (sub_type or '').strip().lower()
    if key not in _QUEUE_COLLECTIONS and not key.endswith('s'):
        key += 's'
    return _QUEUE_COLLECTIONS.get(key)


def _now_iso():
    return datetime.now(CHICAGO_TZ).isoformat()


def _pick(data, keys):
    """Whitelist only the expected keys from a submission payload."""
    data = data or {}
    return {k: data.get(k) for k in keys if k in data}


def _current_email():
    return ((get_current_user() or {}).get('email') or '').strip().lower()


# ============================================================================
# FEED (BriteSide social feed — see backend/feed.py)
# ============================================================================
# The feed lives in its own blueprint so app.py stays the composition root and
# the feed is a teachable, self-contained module. It never imports app.py;
# everything it needs is handed over here.

from backend.feed import register_feed

register_feed(app, {
    'firestore_client': firestore_client,
    'get_current_user': get_current_user,
    'is_editor': is_editor,
    'now_iso': _now_iso,
    'now': lambda: datetime.now(CHICAGO_TZ),
    'safe_print': safe_print,
    'list_employees': list_employees,
})


def _add_submission(collection, doc):
    """Create a queue submission with an auto-id. Returns the new id or None."""
    if not firestore_client:
        return None
    _, ref = firestore_client.collection(collection).add(doc)
    return ref.id


def _list_collection(collection):
    """Return all docs in a collection, newest first, with their id attached."""
    if not firestore_client:
        return []
    out = []
    for d in firestore_client.collection(collection).stream():
        item = d.to_dict() or {}
        item['id'] = d.id
        out.append(item)
    out.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    return out


# ---- Contributor submission endpoints (any signed-in @brite.co user) --------

@app.route('/api/me', methods=['GET'])
def me_profile():
    """The signed-in user's own roster record, for prefilling the spotlight form
    (name/title/birthday/anniversary/photo come from our records / BigQuery)."""
    user = get_current_user() or {}
    email = (user.get('email') or '').strip().lower()
    emp = get_employee(email) or {}
    return jsonify({
        'success': True,
        'email': email,
        'name': user.get('name') or emp.get('name', ''),
        'title': emp.get('title', ''),
        'department': emp.get('department', ''),
        'birthday_month': _as_bday_int(emp.get('birthday_month')),
        'birthday_day': _as_bday_int(emp.get('birthday_day')),
        'anniversary_month': _as_bday_int(emp.get('anniversary_month')),
        'anniversary_day': _as_bday_int(emp.get('anniversary_day')),
        'photo_url': emp.get('photo_url', ''),
    })


@app.route('/api/submit/spotlight', methods=['POST'])
def submit_spotlight():
    """Create/update the submitter's OWN spotlight profile (keyed by their email
    from the session, so nobody can submit as someone else)."""
    if not firestore_client:
        return jsonify({'success': False, 'error': 'Firestore not available'}), 503
    email = _current_email()
    if not email:
        return jsonify({'success': False, 'error': 'Not signed in'}), 401
    user = get_current_user() or {}
    doc = _pick(request.json, SPOTLIGHT_FIELDS)
    doc['email'] = email
    doc.setdefault('name', user.get('name') or email.split('@')[0])
    doc['submitted_by'] = email
    doc['status'] = 'submitted'
    try:
        ref = firestore_client.collection(SPOTLIGHT_COLLECTION).document(email)
        snap = ref.get()
        doc['created_at'] = (snap.to_dict() or {}).get('created_at', _now_iso()) if snap.exists else _now_iso()
        doc['updated_at'] = _now_iso()
        ref.set(doc, merge=True)
        # If they gave a birthday and we don't have one on file, fill it in the
        # roster (OUR Firestore DB — never BigQuery). Non-destructive: an existing
        # birthday is never overwritten, and the sync won't clobber it either.
        bmonth = _as_bday_int((request.json or {}).get('birthday_month'))
        bday = _as_bday_int((request.json or {}).get('birthday_day'))
        if bmonth and bday:
            rec = get_employee(email)
            if rec and not _as_bday_int(rec.get('birthday_month')):
                rec['birthday_month'] = bmonth
                rec['birthday_day'] = bday
                upsert_employee(email, rec)
                safe_print(f"[SUBMIT] Filled missing birthday for {email} from spotlight entry")
        safe_print(f"[SUBMIT] Spotlight profile saved for {email}")
        return jsonify({'success': True, 'id': email})
    except Exception as e:
        safe_print(f"[SUBMIT] Spotlight error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def _submit_queue(collection, fields):
    email = _current_email()
    if not email:
        return jsonify({'success': False, 'error': 'Not signed in'}), 401
    if not firestore_client:
        return jsonify({'success': False, 'error': 'Firestore not available'}), 503
    doc = _pick(request.json, fields)
    doc['submitted_by'] = email
    doc['submitter_name'] = (get_current_user() or {}).get('name', '')
    doc['status'] = 'new'
    doc['created_at'] = _now_iso()
    try:
        new_id = _add_submission(collection, doc)
        return jsonify({'success': True, 'id': new_id})
    except Exception as e:
        safe_print(f"[SUBMIT] {collection} error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/submit/update', methods=['POST'])
def submit_update():
    return _submit_queue(UPDATE_SUBMISSIONS, UPDATE_FIELDS)


@app.route('/api/submit/culture', methods=['POST'])
def submit_culture():
    return _submit_queue(CULTURE_SUBMISSIONS, CULTURE_FIELDS)


@app.route('/api/submit/correction', methods=['POST'])
def submit_correction():
    return _submit_queue(CORRECTION_SUBMISSIONS, CORRECTION_FIELDS)


@app.route('/api/submit/nomination', methods=['POST'])
def submit_nomination():
    return _submit_queue(NOMINATIONS_COLLECTION, NOMINATION_FIELDS)


@app.route('/api/me/submissions', methods=['GET'])
def my_submissions():
    """List the current user's own submissions so they can review/update them."""
    email = _current_email()
    if not firestore_client:
        return jsonify({'success': True, 'submissions': {}})
    try:
        snap = firestore_client.collection(SPOTLIGHT_COLLECTION).document(email).get()
        result = {'spotlight': (snap.to_dict() if snap.exists else None)}
        for key, coll in _QUEUE_COLLECTIONS.items():
            items = []
            for d in firestore_client.collection(coll).where('submitted_by', '==', email).stream():
                it = d.to_dict() or {}
                it['id'] = d.id
                items.append(it)
            items.sort(key=lambda x: x.get('created_at', ''), reverse=True)
            result[key] = items
        return jsonify({'success': True, 'submissions': result})
    except Exception as e:
        safe_print(f"[SUBMIT] my_submissions error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---- Editor curation endpoints (editor-only via the gate) -------------------

@app.route('/api/submissions/spotlight', methods=['GET'])
def list_spotlight_submissions():
    """All spotlight profiles for the builder's 'select a spotlight' picker."""
    return jsonify({'success': True, 'submissions': _list_collection(SPOTLIGHT_COLLECTION)})


@app.route('/api/submissions/spotlight/<path:email>', methods=['GET'])
def get_spotlight_submission(email):
    if not firestore_client:
        return jsonify({'success': False, 'error': 'Firestore not available'}), 503
    snap = firestore_client.collection(SPOTLIGHT_COLLECTION).document(email.strip().lower()).get()
    if not snap.exists:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    data = snap.to_dict() or {}
    data['id'] = email.strip().lower()
    return jsonify({'success': True, 'submission': data})


@app.route('/api/submissions/<sub_type>', methods=['GET'])
def list_queue_submissions(sub_type):
    """List a curated queue (updates / culture / corrections / nominations)."""
    collection = _queue_collection(sub_type)
    if not collection:
        return jsonify({'success': False, 'error': 'Unknown submission type'}), 404
    return jsonify({'success': True, 'submissions': _list_collection(collection)})


@app.route('/api/submissions/<sub_type>/<sub_id>/status', methods=['POST'])
def set_submission_status(sub_type, sub_id):
    """Mark a queued submission used/archived so it drops out of the picker."""
    collection = _queue_collection(sub_type)
    if not collection:
        return jsonify({'success': False, 'error': 'Unknown submission type'}), 404
    if not firestore_client:
        return jsonify({'success': False, 'error': 'Firestore not available'}), 503
    status = ((request.json or {}).get('status') or '').strip()
    if status not in ('new', 'used', 'approved', 'archived'):
        return jsonify({'success': False, 'error': 'Invalid status'}), 400
    try:
        firestore_client.collection(collection).document(sub_id).set(
            {'status': status, 'status_updated_at': _now_iso()}, merge=True)
        return jsonify({'success': True})
    except Exception as e:
        safe_print(f"[SUBMIT] set status error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# DRAFT SAVE / LOAD ROUTES
# ============================================================================

@app.route('/api/save-draft', methods=['POST'])
def save_draft():
    """Save newsletter draft to GCS"""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503
    try:
        data = request.json or {}
        month = (data.get('month') or 'unknown').lower()
        year = data.get('year', datetime.now(CHICAGO_TZ).year)
        saved_by = (data.get('savedBy') or 'unknown').split('@')[0].replace('.', '-')
        blob_name = f"drafts/{month}-{year}-{saved_by}.json"

        # Persist the FULL editor state rather than a hand-maintained whitelist,
        # so every field (extra birthday months, custom headings, additional
        # special sections, ...) round-trips on resume instead of being silently
        # dropped. Media is stored as GCS URLs, so payloads stay small.
        draft = dict(data)
        draft['month'] = month
        draft['year'] = year
        draft['lastSavedBy'] = data.get('savedBy', 'unknown')
        draft['lastSavedAt'] = datetime.now(CHICAGO_TZ).isoformat()

        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blob = bucket.blob(blob_name)
        blob.upload_from_string(json.dumps(draft), content_type='application/json')
        safe_print(f"[DRAFT] Saved {blob_name}")
        return jsonify({'success': True, 'file': blob_name})

    except Exception as e:
        safe_print(f"[DRAFT SAVE ERROR] {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# One-shot AI newsletter build
# ---------------------------------------------------------------------------

_AUTO_IMG_RE = re.compile(r'\.(jpe?g|png|gif|webp)(\?|$)', re.IGNORECASE)

_AUTO_FALLBACK_JOKES = [
    "Why did the diamond go to school?|To get a little more brilliant.",
    "What did the ring say to the finger?|I've got you covered.",
    "Why don't gems ever get lost?|They always know their setting.",
]


def _auto_split_update(summary):
    """Turn a queue summary into (title, body). Feed twins arrive as
    'title\\n\\nbody'; plain entries become body under a generic title."""
    text = (summary or '').strip()
    if '\n\n' in text:
        first, rest = text.split('\n\n', 1)
        first = first.strip()
        if first and len(first) <= 120 and '\n' not in first:
            return first, rest.strip()
    return 'Company Update', text


@app.route('/api/auto-build', methods=['POST'])
def auto_build_newsletter():
    """Compose a complete draft for a month in one shot: birthdays and
    anniversaries from the roster, up to 5 company updates pulled from the
    submission queue (feed posts included, photos carried over), the latest
    spotlight profile, and a Claude-written joke. The result is saved as the
    caller's draft for that month (same file the builder auto-saves to) and
    returned so the frontend can resume it straight into the preview step.
    Queue statuses are NOT touched — nothing is marked used until the editor
    actually keeps it."""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503
    data = request.json or {}
    month_name = (data.get('month') or '').strip()
    month_num = data.get('month_num')
    year = data.get('year') or datetime.now(CHICAGO_TZ).year
    try:
        month_num = int(month_num)
    except (TypeError, ValueError):
        month_num = 0
    if not month_name or not (1 <= month_num <= 12):
        return jsonify({'success': False, 'error': 'month and month_num (1-12) are required'}), 400

    try:
        current_year = int(year)
        employees = list_employees()

        # Birthdays / anniversaries — same shapes the step-3 endpoints return.
        birthdays = sorted([
            {
                'name': emp.get('name', ''),
                'email': emp.get('email', ''),
                'department': emp.get('department', ''),
                'title': emp.get('title', ''),
                'birthday_day': _as_bday_int(emp.get('birthday_day')),
                'birthday_month': _as_bday_int(emp.get('birthday_month')),
                'image_url': emp.get('photo_url', ''),
            }
            for emp in employees
            if _as_bday_int(emp.get('birthday_month')) == month_num and emp.get('active', True)
        ], key=lambda x: x['birthday_day'])

        anniversaries = []
        for emp in employees:
            if _as_bday_int(emp.get('anniversary_month')) != month_num or not emp.get('active', True):
                continue
            yr = _as_bday_int(emp.get('anniversary_year'))
            anniversaries.append({
                'name': emp.get('name', ''),
                'email': emp.get('email', ''),
                'department': emp.get('department', ''),
                'title': emp.get('title', ''),
                'anniversary_day': _as_bday_int(emp.get('anniversary_day')),
                'anniversary_month': _as_bday_int(emp.get('anniversary_month')),
                'anniversary_year': yr,
                'years': (current_year - yr) if yr else 0,
                'image_url': emp.get('photo_url', ''),
            })
        anniversaries.sort(key=lambda x: x['anniversary_day'])

        # Company updates: newest unused queue entries (feed twins included),
        # up to the 5 slots the builder now has. Photos carry over (images only).
        updates = []
        for sub in _list_collection(UPDATE_SUBMISSIONS):
            if (sub.get('status') or 'new') != 'new':
                continue
            title, body = _auto_split_update(sub.get('summary'))
            if not body and not title:
                continue
            photos = [f for f in (sub.get('files') or [])
                      if isinstance(f, str) and _AUTO_IMG_RE.search(f)][:3]
            updates.append({
                'title': title,
                'body': body,
                'photos': photos,
                'photo_positions': [50] * len(photos),
            })
            if len(updates) >= 5:
                break

        # Spotlight: the most recently updated submitted profile, if any.
        spotlights = []
        try:
            if firestore_client:
                subs = []
                for d in firestore_client.collection(SPOTLIGHT_COLLECTION).stream():
                    s = d.to_dict() or {}
                    if s.get('status') in ('submitted', None, ''):
                        subs.append(s)
                subs.sort(key=lambda s: s.get('updated_at', ''), reverse=True)
                if subs:
                    s = subs[0]
                    qa = [q for q in (s.get('qa') or []) if isinstance(q, dict)][:3]
                    while len(qa) < 3:
                        qa.append({'q': '', 'a': ''})
                    emp_match = get_employee((s.get('email') or '').strip().lower()) or {}
                    spotlights = [{
                        'employee': {
                            'name': s.get('name') or emp_match.get('name', ''),
                            'title': s.get('job_title') or emp_match.get('title', ''),
                            'department': emp_match.get('department', ''),
                            'email': s.get('email', ''),
                        },
                        'display_title': s.get('job_title') or emp_match.get('title', ''),
                        'blurb': s.get('describe_work', ''),
                        'fun_facts': s.get('work_with_others', ''),
                        'image_url': s.get('photo_url') or emp_match.get('photo_url', ''),
                        'video_url': '',
                        'qa': qa,
                    }]
        except Exception as spot_err:
            safe_print(f"[AUTO-BUILD] Spotlight pick failed (continuing without): {spot_err}")
        if not spotlights:
            spotlights = [{'employee': None, 'fun_facts': '', 'blurb': '', 'image_url': '',
                           'video_url': '', 'display_title': '',
                           'qa': [{'q': '', 'a': ''}, {'q': '', 'a': ''}, {'q': '', 'a': ''}]}]

        # Joke: one Claude pun in setup|punchline form, canned fallback offline.
        joke = _AUTO_FALLBACK_JOKES[month_num % len(_AUTO_FALLBACK_JOKES)]
        joke_is_ai = False
        if claude_client:
            try:
                resp = claude_client.generate_content(
                    prompt=(f"Write one short, wholesome, workplace-safe pun for a company "
                            f"newsletter opener for the month of {month_name}. Theme: jewelry "
                            f"and insurance. Return EXACTLY this format with no other text: "
                            f"setup|punchline"),
                    system_prompt=BRITESIDE_SYSTEM_PROMPT,
                    max_tokens=120,
                    temperature=0.9,
                )
                text = (resp.get('content') or '').strip().strip('"')
                if '|' in text and 10 < len(text) < 300 and '\n' not in text:
                    joke = text
                    joke_is_ai = True
            except Exception as joke_err:
                safe_print(f"[AUTO-BUILD] Joke generation failed (using fallback): {joke_err}")

        user = get_current_user() or {}
        saved_by = user.get('email', 'unknown')
        prefix = saved_by.split('@')[0].replace('.', '-')
        blob_name = f"drafts/{month_name.lower()}-{current_year}-{prefix}.json"

        draft = {
            'month': month_name.lower(),
            'year': current_year,
            'currentStep': 9,  # land on the preview
            'joke': joke,
            'jokeOptions': [joke],
            'selectedJokeIndex': 0,
            'birthdays': birthdays,
            'secondary_month_num': 0,
            'extra_month_nums': [],
            'birthday_primary_heading': '',
            'birthday_secondary_heading': '',
            'spotlight': spotlights[0],
            'spotlights': spotlights,
            'updates': updates,
            'updatesEnabled': bool(updates),
            'welcomeHires': [],
            'welcomeEnabled': False,
            'anniversaries': anniversaries,
            'anniversariesEnabled': bool(anniversaries),
            'game': {'enabled': False, 'type': '', 'data': None,
                     'imageUrl': '', 'answer': '', 'previousAnswer': ''},
            'specialSection': {'enabled': False, 'title': '', 'body': '',
                               'image_url': '', 'placement': 'after-updates'},
            'specialSections': [],
            'media': {},
            'subject': f"The BriteSide · {month_name} {current_year}",
            'savedBy': saved_by,
            'lastSavedBy': saved_by,
            'lastSavedAt': datetime.now(CHICAGO_TZ).isoformat(),
            'autoBuilt': True,
        }

        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        bucket.blob(blob_name).upload_from_string(
            json.dumps(draft), content_type='application/json')
        safe_print(f"[AUTO-BUILD] {blob_name}: {len(birthdays)} bdays, "
                   f"{len(anniversaries)} annivs, {len(updates)} updates, "
                   f"spotlight={bool(spotlights[0].get('employee'))}, ai_joke={joke_is_ai}")
        return jsonify({
            'success': True,
            'file': blob_name,
            'summary': {
                'birthdays': len(birthdays),
                'anniversaries': len(anniversaries),
                'updates': len(updates),
                'spotlight': bool(spotlights[0].get('employee')),
                'ai_joke': joke_is_ai,
            },
        })
    except Exception as e:
        safe_print(f"[AUTO-BUILD ERROR] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/list-drafts', methods=['GET'])
def list_drafts():
    """List all saved drafts from GCS"""
    if not gcs_client:
        return jsonify({'success': True, 'drafts': []})
    try:
        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blobs = bucket.list_blobs(prefix='drafts/')
        drafts = []
        for blob in blobs:
            if not blob.name.endswith('.json'):
                continue
            try:
                data = json.loads(blob.download_as_text())
            except Exception as blob_err:
                # One corrupt/partial object must not blank the whole list.
                safe_print(f"[DRAFT LIST] Skipping unreadable {blob.name}: {blob_err}")
                continue
            drafts.append({
                'month': data.get('month'),
                'year': data.get('year'),
                'currentStep': data.get('currentStep'),
                'lastSavedBy': data.get('lastSavedBy'),
                'lastSavedAt': data.get('lastSavedAt'),
                'filename': blob.name,
            })
        drafts.sort(key=lambda d: d.get('lastSavedAt', ''), reverse=True)
        return jsonify({'success': True, 'drafts': drafts})
    except Exception as e:
        safe_print(f"[DRAFT LIST ERROR] {str(e)}")
        return jsonify({'success': True, 'drafts': []})


@app.route('/api/load-draft', methods=['GET'])
def load_draft():
    """Load a specific draft from GCS"""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503
    try:
        filename = _validate_gcs_key(request.args.get('file'), ('drafts/',))
        if not filename:
            return jsonify({'success': False, 'error': 'Invalid draft file'}), 400
        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blob = bucket.blob(filename)
        if not blob.exists():
            return jsonify({'success': False, 'error': 'Draft not found'}), 404
        data = json.loads(blob.download_as_text())
        return jsonify({'success': True, 'draft': data})
    except Exception as e:
        safe_print(f"[DRAFT LOAD ERROR] {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/delete-draft', methods=['DELETE'])
def delete_draft():
    """Delete a draft from GCS"""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503
    try:
        filename = _validate_gcs_key((request.json or {}).get('file'), ('drafts/',))
        if not filename:
            return jsonify({'success': False, 'error': 'Invalid draft file'}), 400
        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blob = bucket.blob(filename)
        if blob.exists():
            blob.delete()
        safe_print(f"[DRAFT] Deleted {filename}")
        return jsonify({'success': True})
    except Exception as e:
        safe_print(f"[DRAFT DELETE ERROR] {str(e)}")
        return jsonify({'success': False, 'error': 'Delete failed'}), 500


@app.route('/api/publish-draft', methods=['POST'])
def publish_draft():
    """Move draft to published in GCS"""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503
    try:
        filename = _validate_gcs_key((request.json or {}).get('file'), ('drafts/',))
        if not filename:
            return jsonify({'success': False, 'error': 'Invalid draft file'}), 400
        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        source_blob = bucket.blob(filename)
        if not source_blob.exists():
            return jsonify({'success': False, 'error': 'Draft not found'}), 404
        # filename is guaranteed to start with 'drafts/' here, so published_name
        # is always distinct — no risk of copying an object onto itself and then
        # deleting it (which previously destroyed non-drafts files).
        published_name = 'published/' + filename[len('drafts/'):]
        bucket.copy_blob(source_blob, bucket, published_name)
        source_blob.delete()
        safe_print(f"[DRAFT] Published {filename} -> {published_name}")
        return jsonify({'success': True, 'file': published_name})
    except Exception as e:
        safe_print(f"[DRAFT PUBLISH ERROR] {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/list-published', methods=['GET'])
def list_published():
    """List all published newsletters from GCS"""
    if not gcs_client:
        return jsonify({'success': True, 'newsletters': []})
    try:
        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blobs = list(bucket.list_blobs(prefix='published/'))
        newsletters = []
        for blob in blobs:
            if not blob.name.endswith('.json'):
                continue
            try:
                data = json.loads(blob.download_as_text())
            except Exception as blob_err:
                safe_print(f"[PUBLISHED LIST] Skipping unreadable {blob.name}: {blob_err}")
                continue
            newsletters.append({
                'filename': blob.name,
                'month': data.get('month'),
                'year': data.get('year'),
                'lastSavedBy': data.get('lastSavedBy'),
                'lastSavedAt': data.get('lastSavedAt'),
            })
        newsletters.sort(key=lambda d: d.get('lastSavedAt', ''), reverse=True)
        return jsonify({'success': True, 'newsletters': newsletters})
    except Exception as e:
        safe_print(f"[PUBLISHED LIST ERROR] {str(e)}")
        return jsonify({'success': True, 'newsletters': []})


@app.route('/api/load-published', methods=['GET'])
def load_published():
    """Load a specific published newsletter from GCS"""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503
    try:
        filename = _validate_gcs_key(request.args.get('file'), ('published/',))
        if not filename:
            return jsonify({'success': False, 'error': 'Invalid file'}), 400
        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blob = bucket.blob(filename)
        if not blob.exists():
            return jsonify({'success': False, 'error': 'Not found'}), 404
        data = json.loads(blob.download_as_text())
        return jsonify({'success': True, 'draft': data})
    except Exception as e:
        safe_print(f"[PUBLISHED LOAD ERROR] {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/delete-published', methods=['DELETE'])
def delete_published():
    """Delete a published newsletter from GCS"""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503
    try:
        filename = _validate_gcs_key((request.json or {}).get('file'), ('published/',))
        if not filename:
            return jsonify({'success': False, 'error': 'Invalid file'}), 400
        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blob = bucket.blob(filename)
        if blob.exists():
            blob.delete()
        return jsonify({'success': True})
    except Exception as e:
        safe_print(f"[PUBLISHED DELETE ERROR] {str(e)}")
        return jsonify({'success': False, 'error': 'Delete failed'}), 500


# ============================================================================
# STATIC FILE SERVING
# ============================================================================

@app.route('/static/<path:filename>')
def serve_static(filename):
    """Serve static files"""
    return send_from_directory('static', filename)


@app.route('/templates/<path:filename>')
def serve_template(filename):
    """Serve template files"""
    return send_from_directory('templates', filename)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5002))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'

    print(f"\n{'='*60}")
    print(f"  The BriteSide - Internal Newsletter Generator")
    print(f"  Running on http://localhost:{port}")
    print(f"{'='*60}")
    print(f"  Claude:   {'Available' if claude_client else 'Not available'}")
    print(f"  SendGrid: {'Available' if SENDGRID_AVAILABLE else 'Not available'}")
    print(f"  GCS:      {'Available' if gcs_client else 'Not available'}")
    print(f"  OAuth:    {'Configured' if not is_local_dev() else 'Skipped (local dev)'}")
    print(f"{'='*60}\n")

    app.run(host='0.0.0.0', port=port, debug=debug)
