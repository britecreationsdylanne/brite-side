"""
The BriteSide - Internal Monthly Newsletter Generator
Flask backend API for generating BriteCo's internal employee newsletter
"""

import os
import sys
import json
import secrets
import traceback
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
    EMPLOYEES,
    MONTH_NAMES,
    BRITESIDE_SYSTEM_PROMPT,
    AI_PROMPTS,
    EMAIL_TEMPLATE_CONFIG,
    SENDGRID_CONFIG,
    GCS_CONFIG,
)


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
except Exception as e:
    print(f"[WARNING] GCS not available: {e}")


# ============================================================================
# HELPERS
# ============================================================================

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
                "department": emp["department"],
                "title": emp["title"],
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
            f"- Keep approximately the same length\n"
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
        updates = data.get('updates', [])
        special_section = data.get('special_section', {})

        safe_print(f"[API] Rendering email template for {month} {year}")

        # Read the email template
        template_path = os.path.join(os.path.dirname(__file__), EMAIL_TEMPLATE_CONFIG['template_file'])
        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                html = f.read()
        except FileNotFoundError:
            return jsonify({"success": False, "error": f"Template not found: {template_path}"}), 404

        # Build birthday HTML rows
        birthday_html = ''
        if birthdays:
            birthday_items = []
            for bday in birthdays:
                bday_name = bday.get('name', '')
                bday_day = bday.get('birthday_day', '')
                bday_dept = bday.get('department', '')
                birthday_items.append(
                    f'<tr><td style="padding: 6px 12px; font-weight: 600;">{bday_name}</td>'
                    f'<td style="padding: 6px 12px; color: #6b7280;">{month} {bday_day}</td>'
                    f'<td style="padding: 6px 12px; color: #6b7280;">{bday_dept}</td></tr>'
                )
            birthday_html = '\n'.join(birthday_items)

        # Build updates HTML list
        updates_html = ''
        if updates:
            update_items = []
            for update in updates:
                if isinstance(update, str):
                    update_items.append(f'<li style="margin-bottom: 8px;">{update}</li>')
                elif isinstance(update, dict):
                    title = update.get('title', '')
                    body = update.get('body', '')
                    update_items.append(
                        f'<li style="margin-bottom: 12px;"><strong>{title}</strong><br>{body}</li>'
                    )
            updates_html = '\n'.join(update_items)

        # Build spotlight HTML (frontend sends null if no spotlight selected)
        if not spotlight:
            spotlight = {}
        spotlight_name = spotlight.get('name', '')
        spotlight_title = spotlight.get('title', '')
        spotlight_blurb = spotlight.get('blurb', '')

        # Build special section HTML (frontend sends null if disabled)
        if not special_section:
            special_section = {}
        special_title = special_section.get('title', '')
        special_body = special_section.get('body', '')

        # Build spotlight image HTML
        spotlight_image_url = spotlight.get('image_url', '') if spotlight else ''
        spotlight_image_html = ''
        if spotlight_image_url:
            spotlight_image_html = (
                f'<img src="{spotlight_image_url}" width="120" alt="{spotlight_name}" '
                f'style="width: 120px; height: 120px; border-radius: 50%; object-fit: cover; display: block;" '
                f'class="mobile-img-full">'
            )

        # Build individual update fields
        update_1_title = ''
        update_1_body = ''
        update_2_title = ''
        update_2_body = ''
        if updates:
            if len(updates) >= 1:
                u1 = updates[0]
                if isinstance(u1, dict):
                    update_1_title = u1.get('title', '')
                    update_1_body = u1.get('body', '')
            if len(updates) >= 2:
                u2 = updates[1]
                if isinstance(u2, dict):
                    update_2_title = u2.get('title', '')
                    update_2_body = u2.get('body', '')

        # Conditional display values
        birthday_display = 'table-row' if birthdays else 'none'
        update_2_display = 'table-row' if (update_2_title or update_2_body) else 'none'
        special_display = 'table-row' if (special_title or special_body) else 'none'

        # Preheader text (short preview text for email clients)
        preheader = f"The BriteSide - {month} {year}"
        if joke:
            preheader = joke[:100]

        # Replace placeholders in template
        html = html.replace('{{MONTH}}', str(month))
        html = html.replace('{{YEAR}}', str(year))
        html = html.replace('{{PREHEADER}}', str(preheader))
        html = html.replace('{{JOKE}}', str(joke))
        html = html.replace('{{BIRTHDAY_DISPLAY}}', birthday_display)
        html = html.replace('{{BIRTHDAY_SECTION}}', birthday_html)
        html = html.replace('{{SPOTLIGHT_IMAGE}}', spotlight_image_html)
        html = html.replace('{{SPOTLIGHT_NAME}}', str(spotlight_name))
        html = html.replace('{{SPOTLIGHT_TITLE}}', str(spotlight_title))
        html = html.replace('{{SPOTLIGHT_BLURB}}', str(spotlight_blurb))
        html = html.replace('{{UPDATE_1_TITLE}}', str(update_1_title))
        html = html.replace('{{UPDATE_1_BODY}}', str(update_1_body))
        html = html.replace('{{UPDATE_2_TITLE}}', str(update_2_title))
        html = html.replace('{{UPDATE_2_BODY}}', str(update_2_body))
        html = html.replace('{{UPDATE_2_DISPLAY}}', update_2_display)
        html = html.replace('{{SPECIAL_TITLE}}', str(special_title))
        html = html.replace('{{SPECIAL_BODY}}', str(special_body))
        html = html.replace('{{SPECIAL_SECTION_DISPLAY}}', special_display)

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
            'updates': data.get('updates'),
            'specialSection': data.get('specialSection'),
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
