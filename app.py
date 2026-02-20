"""
The BriteSide - Internal Monthly Newsletter Generator
Flask backend API for generating BriteCo's internal employee newsletter
"""

import os
import sys
import json
import html as html_mod
import secrets
import traceback
import requests as http_requests
import uuid
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

# Import config
from config.briteside_config import (
    EMPLOYEES as CONFIG_EMPLOYEES,
    CONFIG_EMPLOYEES_VERSION,
    MONTH_NAMES,
    BRITESIDE_SYSTEM_PROMPT,
    AI_PROMPTS,
    EMAIL_TEMPLATE_CONFIG,
    SENDGRID_CONFIG,
    GCS_CONFIG,
)

# Mutable employee list (starts from config, can be updated at runtime via GCS)
EMPLOYEES = list(CONFIG_EMPLOYEES)


# ============================================================================
# APP INITIALIZATION
# ============================================================================

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

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


def get_current_user():
    """Get current authenticated user from session"""
    return session.get('user')


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
gcs_client = None

try:
    from google.cloud import storage as gcs_storage
    gcs_client = gcs_storage.Client()
    print("[OK] GCS initialized")

    # Ensure bucket allows public reads (uniform bucket-level access)
    try:
        _bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        _policy = _bucket.get_iam_policy(requested_policy_version=3)
        _has_public = any(
            b.get('role') == 'roles/storage.objectViewer' and 'allUsers' in b.get('members', set())
            for b in _policy.bindings
        )
        if not _has_public:
            _policy.bindings.append({'role': 'roles/storage.objectViewer', 'members': {'allUsers'}})
            _bucket.set_iam_policy(_policy)
            print("[OK] Bucket public read access enabled via IAM")
        else:
            print("[OK] Bucket already has public read access")
    except Exception as iam_err:
        print(f"[WARNING] Could not set bucket IAM policy: {iam_err}")
except Exception as e:
    print(f"[WARNING] GCS not available: {e}")


# ============================================================================
# EMPLOYEE LIST MANAGEMENT (GCS-backed)
# ============================================================================

EMPLOYEES_GCS_KEY = 'config/employees.json'


def load_employees_from_gcs():
    """Load employee list from GCS if available, otherwise use config defaults.
    If config version is newer than GCS version, re-sync from config."""
    global EMPLOYEES
    if not gcs_client:
        return
    try:
        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blob = bucket.blob(EMPLOYEES_GCS_KEY)
        if blob.exists():
            raw = json.loads(blob.download_as_text())
            # Support versioned format: {"version": N, "employees": [...]}
            if isinstance(raw, dict) and 'employees' in raw:
                gcs_version = raw.get('version', 1)
                gcs_employees = raw['employees']
            else:
                # Legacy format: just a list
                gcs_version = 1
                gcs_employees = raw
            if gcs_version < CONFIG_EMPLOYEES_VERSION:
                # Config has been updated — re-sync to GCS
                print(f"[OK] Config v{CONFIG_EMPLOYEES_VERSION} > GCS v{gcs_version}, re-syncing employees from config")
                save_employees_to_gcs()
            else:
                EMPLOYEES.clear()
                EMPLOYEES.extend(gcs_employees)
                print(f"[OK] Loaded {len(EMPLOYEES)} employees from GCS (v{gcs_version})")
        else:
            # First run: save config defaults to GCS
            save_employees_to_gcs()
            print(f"[OK] Initialized GCS employees from config ({len(EMPLOYEES)})")
    except Exception as e:
        print(f"[WARNING] Could not load employees from GCS: {e}")


def save_employees_to_gcs():
    """Persist current employee list to GCS with version."""
    if not gcs_client:
        return False
    try:
        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blob = bucket.blob(EMPLOYEES_GCS_KEY)
        data = {"version": CONFIG_EMPLOYEES_VERSION, "employees": EMPLOYEES}
        blob.upload_from_string(json.dumps(data, indent=2), content_type='application/json')
        return True
    except Exception as e:
        print(f"[WARNING] Could not save employees to GCS: {e}")
        return False


# Load employees from GCS on startup
load_employees_from_gcs()


# ============================================================================
# HELPERS
# ============================================================================

def esc(text):
    """HTML-escape user-generated text to prevent broken rendering"""
    if not text:
        return text or ''
    return html_mod.escape(str(text))


def safe_print(text):
    """Safe print for Unicode characters"""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode('ascii', 'replace').decode('ascii'))


def is_local_dev():
    """Check if running in local dev mode (no OAuth configured)"""
    return not os.environ.get('GOOGLE_CLIENT_ID')


def require_auth(f):
    """Decorator placeholder - auth is checked inline per the consumer-newsletter pattern"""
    pass  # Not used; auth is checked inline in routes


def find_employee(name):
    """Look up an employee by name (case-insensitive partial match)"""
    name_lower = name.lower().strip()
    for emp in EMPLOYEES:
        if emp['name'].lower() == name_lower:
            return emp
    # Partial match fallback
    for emp in EMPLOYEES:
        if name_lower in emp['name'].lower():
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

@app.route('/')
def serve_index():
    """Serve the main app with auth check"""

    # Local dev mode: skip OAuth when GOOGLE_CLIENT_ID is not set
    if is_local_dev():
        try:
            with open('index.html', 'r', encoding='utf-8') as f:
                html = f.read()

            dev_user = {
                'email': 'dev@brite.co',
                'name': 'Local Developer',
                'picture': ''
            }
            user_script = f'''<script>
    window.AUTH_USER = {json.dumps(dev_user)};
    </script>
</head>'''
            html = html.replace('</head>', user_script, 1)
            return Response(html, mimetype='text/html')

        except FileNotFoundError:
            return 'index.html not found', 404

    # Production: require auth
    user = get_current_user()
    if not user:
        return redirect('/auth/login')

    try:
        with open('index.html', 'r', encoding='utf-8') as f:
            html = f.read()
    except FileNotFoundError:
        return 'index.html not found', 404

    user_script = f'''<script>
    window.AUTH_USER = {json.dumps(user)};
    </script>
</head>'''
    html = html.replace('</head>', user_script, 1)

    return Response(html, mimetype='text/html')


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
                "name": emp["name"],
                "email": emp["email"],
                "department": emp.get("department", ""),
                "title": emp.get("title", ""),
                "birthday_month": emp.get("birthday_month", 0),
                "birthday_day": emp.get("birthday_day", 0),
            }
            for emp in EMPLOYEES
        ]
        return jsonify({"success": True, "employees": employees})

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
                "name": emp["name"],
                "email": emp["email"],
                "department": emp["department"],
                "title": emp["title"],
                "birthday_day": emp["birthday_day"],
                "birthday_month": emp["birthday_month"],
            }
            for emp in EMPLOYEES
            if emp.get("birthday_month") == month
        ]

        # Sort by day of month
        birthday_employees.sort(key=lambda x: x["birthday_day"])

        month_name = MONTH_NAMES.get(month, str(month))
        safe_print(f"[API] Found {len(birthday_employees)} birthdays in {month_name}")

        return jsonify({
            "success": True,
            "month": month,
            "month_name": month_name,
            "birthdays": birthday_employees,
        })

    except Exception as e:
        safe_print(f"[API] Error fetching birthdays: {e}")
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

        # Check for duplicates
        for emp in EMPLOYEES:
            if emp['email'].lower() == email.lower():
                return jsonify({"success": False, "error": f"Employee with email {email} already exists"}), 409

        new_employee = {
            "name": name,
            "email": email,
            "birthday_month": data.get('birthday_month', 0),
            "birthday_day": data.get('birthday_day', 0),
            "department": data.get('department', ''),
            "title": data.get('title', ''),
        }

        EMPLOYEES.append(new_employee)
        EMPLOYEES.sort(key=lambda e: e['name'].lower())
        save_employees_to_gcs()

        safe_print(f"[API] Added employee: {name} ({email})")
        return jsonify({"success": True, "employee": new_employee, "total": len(EMPLOYEES)})

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

        original_count = len(EMPLOYEES)
        EMPLOYEES[:] = [emp for emp in EMPLOYEES if emp['email'].lower() != email]

        if len(EMPLOYEES) == original_count:
            return jsonify({"success": False, "error": f"Employee with email {email} not found"}), 404

        save_employees_to_gcs()

        safe_print(f"[API] Removed employee: {email}")
        return jsonify({"success": True, "total": len(EMPLOYEES)})

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

        for emp in EMPLOYEES:
            if emp['email'].lower() == email:
                if 'name' in data:
                    emp['name'] = data['name'].strip()
                if 'department' in data:
                    emp['department'] = data['department'].strip()
                if 'title' in data:
                    emp['title'] = data['title'].strip()
                if 'birthday_month' in data:
                    emp['birthday_month'] = data['birthday_month']
                if 'birthday_day' in data:
                    emp['birthday_day'] = data['birthday_day']

                save_employees_to_gcs()
                safe_print(f"[API] Updated employee: {emp['name']} ({email})")
                return jsonify({"success": True, "employee": emp})

        return jsonify({"success": False, "error": f"Employee with email {email} not found"}), 404

    except Exception as e:
        safe_print(f"[API] Error updating employee: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


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


@app.route('/api/upload-media', methods=['POST'])
def upload_media():
    """Upload an image or video to GCS and return its public URL"""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400

        file = request.files['file']
        if not file.filename:
            return jsonify({'success': False, 'error': 'Empty filename'}), 400

        content_type = file.content_type or ''
        is_image = content_type in ALLOWED_IMAGE_TYPES
        is_video = content_type in ALLOWED_VIDEO_TYPES

        if not is_image and not is_video:
            return jsonify({'success': False, 'error': f'Unsupported file type: {content_type}'}), 400

        # Read file data and check size
        file_data = file.read()
        max_size = MAX_IMAGE_SIZE if is_image else MAX_VIDEO_SIZE
        if len(file_data) > max_size:
            limit_mb = max_size // (1024 * 1024)
            return jsonify({'success': False, 'error': f'File too large. Max {limit_mb}MB.'}), 400

        # Generate unique filename
        ext = os.path.splitext(file.filename)[1].lower() or ('.jpg' if is_image else '.mp4')
        unique_name = f"{uuid.uuid4().hex}{ext}"
        blob_path = f"media/{unique_name}"

        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blob = bucket.blob(blob_path)
        blob.upload_from_string(file_data, content_type=content_type)

        # Construct public URL directly (bucket uses uniform bucket-level access)
        public_url = f"https://storage.googleapis.com/{GCS_DRAFTS_BUCKET}/{blob_path}"
        safe_print(f"[MEDIA] Uploaded {blob_path} ({len(file_data)} bytes)")

        return jsonify({
            'success': True,
            'url': public_url,
            'filename': unique_name,
            'type': 'image' if is_image else 'video',
            'size': len(file_data),
        })

    except Exception as e:
        safe_print(f"[MEDIA UPLOAD ERROR] {str(e)}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


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

        safe_print(f"[API] Game generated ({response.get('tokens', 0)} tokens)")

        return jsonify({
            "success": True,
            "game_content": game_text,
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
        spotlight = data.get('spotlight', {})
        spotlights = data.get('spotlights', [])
        updates = data.get('updates', [])
        updates_enabled = data.get('updates_enabled', True)
        special_section = data.get('special_section', {})
        welcome_hires = data.get('welcome_hires', [])
        welcome_enabled = data.get('welcome_enabled', False)
        game = data.get('game', {})

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

        # Build birthday HTML rows
        birthday_html = ''
        if birthdays:
            birthday_items = []
            for bday in birthdays:
                bday_name = esc(bday.get('name', ''))
                bday_day = esc(bday.get('birthday_day', ''))
                bday_dept = esc(bday.get('department', ''))
                birthday_items.append(
                    f'<tr><td style="padding: 6px 12px; font-family: {FONT}; font-size: 15px; font-weight: 600; color: #272D3F;">{bday_name}</td>'
                    f'<td style="padding: 6px 12px; font-family: {FONT}; font-size: 15px; color: #6b7280;">{month} {bday_day}</td>'
                    f'<td style="padding: 6px 12px; font-family: {FONT}; font-size: 15px; color: #6b7280;">{bday_dept}</td></tr>'
                )
            birthday_html = '\n'.join(birthday_items)

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
            sp_image_url = sp.get('image_url', '')

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
        spotlight_image_url = spotlight.get('image_url', '') if spotlight else ''
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
        if updates:
            for i, u in enumerate(updates[:3]):
                if not isinstance(u, dict):
                    continue
                title = esc(u.get('title', ''))
                body = esc(u.get('body', ''))
                photos = [p for p in u.get('photos', []) if p]
                photos_html = ''
                if len(photos) == 1:
                    # Single photo: full width
                    photos_html = (
                        f'<img src="{photos[0]}" width="516" '
                        f'style="width: 100%; height: auto; border-radius: 8px; margin-top: 12px; display: block;" '
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

        # Build special section HTML (frontend sends null if disabled)
        if not special_section:
            special_section = {}
        special_title = esc(special_section.get('title', ''))
        special_body = esc(special_section.get('body', ''))

        # Build game section HTML
        if not game:
            game = {}
        game_content = game.get('content', '')
        game_image_url = game.get('image_url', '')
        game_previous_answer = game.get('previous_answer', '')

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
        welcome_display = 'table-row' if (welcome_enabled and welcome_hires) else 'none'
        update_2_display = 'table-row' if (update_2_title or update_2_body) else 'none'
        update_3_display = 'table-row' if (update_3_title or update_3_body) else 'none'
        updates_display = 'table-row' if updates_enabled and updates else 'none'
        special_display = 'table-row' if (special_title or special_body) else 'none'
        game_display = 'table-row' if game_section_html else 'none'
        punchline_display = 'table-row' if joke_punchline else 'none'

        # Preheader text (short preview text for email clients)
        preheader = f"The BriteSide - {month} {year}"
        if joke_setup:
            preheader = joke_setup[:100]

        # Make logo paths absolute so they work in iframe previews and email clients
        base_url = request.host_url.rstrip('/')
        html = html.replace('/static/briteco-logo-white.png', f'{base_url}/static/briteco-logo-white.png')

        # Replace placeholders in template
        html = html.replace('{{MONTH}}', str(month))
        html = html.replace('{{YEAR}}', str(year))
        html = html.replace('{{PREHEADER}}', str(preheader))
        html = html.replace('{{INTRO_LINE}}', '')
        html = html.replace('{{INTRO_DISPLAY}}', 'none')
        html = html.replace('{{JOKE}}', str(joke))
        html = html.replace('{{JOKE_SETUP}}', joke_setup)
        html = html.replace('{{JOKE_PUNCHLINE}}', joke_punchline)
        html = html.replace('{{PUNCHLINE_DISPLAY}}', punchline_display)
        html = html.replace('{{BIRTHDAY_DISPLAY}}', birthday_display)
        html = html.replace('{{BIRTHDAY_SECTION}}', birthday_html)
        html = html.replace('{{WELCOME_DISPLAY}}', welcome_display)
        html = html.replace('{{WELCOME_SECTION}}', welcome_html)
        html = html.replace('{{SPOTLIGHT_SECTION}}', spotlight_section_html)
        html = html.replace('{{SPOTLIGHT_IMAGE}}', spotlight_image_html)
        html = html.replace('{{SPOTLIGHT_NAME}}', str(spotlight_name))
        html = html.replace('{{SPOTLIGHT_TITLE}}', str(spotlight_title_val))
        html = html.replace('{{SPOTLIGHT_BLURB}}', str(spotlight_blurb))
        html = html.replace('{{UPDATES_DISPLAY}}', updates_display)
        html = html.replace('{{UPDATE_1_TITLE}}', str(update_1_title))
        html = html.replace('{{UPDATE_1_BODY}}', str(update_1_body))
        html = html.replace('{{UPDATE_1_PHOTOS}}', update_1_photos_html)
        html = html.replace('{{UPDATE_2_TITLE}}', str(update_2_title))
        html = html.replace('{{UPDATE_2_BODY}}', str(update_2_body))
        html = html.replace('{{UPDATE_2_PHOTOS}}', update_2_photos_html)
        html = html.replace('{{UPDATE_2_DISPLAY}}', update_2_display)
        html = html.replace('{{UPDATE_3_TITLE}}', str(update_3_title))
        html = html.replace('{{UPDATE_3_BODY}}', str(update_3_body))
        html = html.replace('{{UPDATE_3_PHOTOS}}', update_3_photos_html)
        html = html.replace('{{UPDATE_3_DISPLAY}}', update_3_display)
        html = html.replace('{{SPECIAL_TITLE}}', str(special_title))
        html = html.replace('{{SPECIAL_BODY}}', str(special_body))
        html = html.replace('{{SPECIAL_SECTION_DISPLAY}}', special_display)
        html = html.replace('{{GAME_DISPLAY}}', game_display)
        html = html.replace('{{GAME_SECTION}}', game_section_html)

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
# DRAFT SAVE / LOAD ROUTES
# ============================================================================

@app.route('/api/save-draft', methods=['POST'])
def save_draft():
    """Save newsletter draft to GCS"""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503
    try:
        data = request.json
        month = data.get('month', 'unknown').lower()
        year = data.get('year', datetime.now(CHICAGO_TZ).year)
        saved_by = data.get('savedBy', 'unknown').split('@')[0].replace('.', '-')
        blob_name = f"drafts/{month}-{year}-{saved_by}.json"

        draft = {
            'month': month,
            'year': year,
            'currentStep': data.get('currentStep'),
            'joke': data.get('joke'),
            'jokeOptions': data.get('jokeOptions'),
            'selectedJokeIndex': data.get('selectedJokeIndex'),
            'birthdays': data.get('birthdays'),
            'spotlight': data.get('spotlight'),
            'spotlights': data.get('spotlights'),
            'updates': data.get('updates'),
            'updatesEnabled': data.get('updatesEnabled', True),
            'specialSection': data.get('specialSection'),
            'welcomeHires': data.get('welcomeHires'),
            'welcomeEnabled': data.get('welcomeEnabled', False),
            'game': data.get('game'),
            'subject': data.get('subject'),
            'lastSavedBy': data.get('savedBy', 'unknown'),
            'lastSavedAt': datetime.now(CHICAGO_TZ).isoformat(),
        }

        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blob = bucket.blob(blob_name)
        blob.upload_from_string(json.dumps(draft), content_type='application/json')
        safe_print(f"[DRAFT] Saved {blob_name}")
        return jsonify({'success': True, 'file': blob_name})

    except Exception as e:
        safe_print(f"[DRAFT SAVE ERROR] {str(e)}")
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
            data = json.loads(blob.download_as_text())
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
        filename = request.args.get('file')
        if not filename:
            return jsonify({'success': False, 'error': 'No file specified'}), 400
        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blob = bucket.blob(filename)
        data = json.loads(blob.download_as_text())
        return jsonify({'success': True, 'draft': data})
    except Exception as e:
        safe_print(f"[DRAFT LOAD ERROR] {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/delete-draft', methods=['DELETE'])
def delete_draft():
    """Delete a draft from GCS"""
    if not gcs_client:
        return jsonify({'success': True})
    try:
        filename = request.json.get('file')
        if not filename:
            return jsonify({'success': False, 'error': 'No file specified'}), 400
        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blob = bucket.blob(filename)
        if blob.exists():
            blob.delete()
        safe_print(f"[DRAFT] Deleted {filename}")
        return jsonify({'success': True})
    except Exception as e:
        safe_print(f"[DRAFT DELETE ERROR] {str(e)}")
        return jsonify({'success': True})


@app.route('/api/publish-draft', methods=['POST'])
def publish_draft():
    """Move draft to published in GCS"""
    if not gcs_client:
        return jsonify({'success': False, 'error': 'GCS not available'}), 503
    try:
        filename = request.json.get('file')
        if not filename:
            return jsonify({'success': False, 'error': 'No file specified'}), 400
        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        source_blob = bucket.blob(filename)
        if not source_blob.exists():
            return jsonify({'success': False, 'error': 'Draft not found'}), 404
        published_name = filename.replace('drafts/', 'published/', 1)
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
            if blob.name.endswith('.json'):
                data = json.loads(blob.download_as_text())
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
        filename = request.args.get('file')
        if not filename:
            return jsonify({'success': False, 'error': 'No file specified'}), 400
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
        return jsonify({'success': True})
    try:
        filename = request.json.get('file')
        if not filename:
            return jsonify({'success': False, 'error': 'No file specified'}), 400
        bucket = gcs_client.bucket(GCS_DRAFTS_BUCKET)
        blob = bucket.blob(filename)
        if blob.exists():
            blob.delete()
        return jsonify({'success': True})
    except Exception as e:
        safe_print(f"[PUBLISHED DELETE ERROR] {str(e)}")
        return jsonify({'success': True})


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
