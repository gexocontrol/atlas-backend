"""
ATLAS - AI-Powered Exam Platform SaaS
Complete Backend Application
Email: contact.gexoai@gmail.com
Domain: atlas-education.com
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, timedelta
import os
import re
import random
import time as _time
import json
import base64
import requests
from pathlib import Path
import stripe
from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv
import sqlite3
from io import BytesIO
import PyPDF2
import ssl
import urllib3

try:
    from supabase import create_client, Client as SupabaseClient
    _SUPABASE_PKG = True
except ImportError:
    _SUPABASE_PKG = False
    print('[Atlas] supabase package not installed — run: pip install supabase')

load_dotenv()

# Disable SSL verification (development only)
os.environ['PYTHONHTTPSVERIFY'] = '0'
ssl._create_default_https_context = ssl._create_unverified_context
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== CONFIGURATION ====================

app = Flask(__name__)
CORS(app,
     origins="*",
     methods=["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"],
     allow_headers=["Content-Type", "Authorization", "X-Requested-With"])

# Configuration
app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET', 'atlas-secret-key-change-in-production')
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(days=30)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size

jwt = JWTManager(app)

# API Keys (will be added in production)
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', 'your-gemini-api-key-here')
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY', 'your-stripe-secret-key-here')
STRIPE_PUBLISHABLE_KEY = os.getenv('STRIPE_PUBLISHABLE_KEY', 'your-stripe-publishable-key-here')
PAYSKY_API_KEY       = os.getenv('PAYSKY_API_KEY',       'your-paysky-api-key-here')

SUPABASE_URL         = os.getenv('SUPABASE_URL',         '')
SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_SERVICE_KEY', '')

stripe.api_key = STRIPE_SECRET_KEY

# Initialize Gemini client with SSL verification disabled (development only)
import httpx as _httpx
client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=genai_types.HttpOptions(
        httpxClient=_httpx.Client(verify=False),
    ),
)

# Patch httpx globally so supabase-py's internal clients also skip SSL verification
# (supabase-py v2 uses httpx, which ignores the ssl module override above)
_orig_httpx_sync  = _httpx.Client.__init__
_orig_httpx_async = _httpx.AsyncClient.__init__

def _httpx_no_ssl(self, *args, **kwargs):
    kwargs['verify'] = False   # force-override: postgrest-py passes verify=True explicitly
    _orig_httpx_sync(self, *args, **kwargs)

def _httpx_async_no_ssl(self, *args, **kwargs):
    kwargs['verify'] = False
    _orig_httpx_async(self, *args, **kwargs)

_httpx.Client.__init__      = _httpx_no_ssl
_httpx.AsyncClient.__init__ = _httpx_async_no_ssl

# Initialize Supabase client (service key bypasses Row Level Security)
_sb = None
if _SUPABASE_PKG and SUPABASE_URL and SUPABASE_SERVICE_KEY:
    try:
        _sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        print('[Atlas] Supabase connected OK')
    except Exception as _e:
        print(f'[Atlas] Supabase init failed: {_e}')

# In-memory pending email verifications: { email: { name, password_hash, code, expires_at } }
_pending_verifications = {}

# Database setup
DB_PATH = 'atlas.db'

# ==================== DATABASE SETUP ====================

def init_db():
    """Initialize database with all tables"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Users table
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        name TEXT NOT NULL,
        role TEXT DEFAULT 'teacher',
        subscription_tier TEXT DEFAULT 'free',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Subscriptions table
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        tier TEXT NOT NULL,
        exams_limit INTEGER NOT NULL,
        exams_used INTEGER DEFAULT 0,
        stripe_customer_id TEXT,
        stripe_subscription_id TEXT,
        paysky_customer_id TEXT,
        status TEXT DEFAULT 'active',
        renewal_date TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    
    # Exams table
    c.execute('''CREATE TABLE IF NOT EXISTS exams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        subject TEXT,
        difficulty TEXT,
        exam_type TEXT,
        time_limit INTEGER,
        questions_count INTEGER,
        source_pdf_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    
    # Analysis table
    c.execute('''CREATE TABLE IF NOT EXISTS analysis (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        exam_id INTEGER,
        analysis_type TEXT,
        file_url TEXT,
        gemini_response TEXT,
        score INTEGER,
        feedback TEXT,
        mistakes TEXT,
        improvements TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(exam_id) REFERENCES exams(id)
    )''')
    
    # Usage tracking
    c.execute('''CREATE TABLE IF NOT EXISTS usage_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        action TEXT,
        gemini_tokens_used INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    
    conn.commit()
    conn.close()

# Initialize database on startup
init_db()

# ==================== UTILITY FUNCTIONS ====================

def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_user_subscription(user_id):
    """Get user's current subscription"""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM subscriptions WHERE user_id = ?', (user_id,))
    sub = c.fetchone()
    conn.close()
    
    if not sub:
        return {
            'tier': 'free',
            'exams_limit': 5,
            'exams_used': 0,
            'remaining': 5
        }
    
    return {
        'tier': sub['tier'],
        'exams_limit': sub['exams_limit'],
        'exams_used': sub['exams_used'],
        'remaining': sub['exams_limit'] - sub['exams_used']
    }

def check_exam_limit(user_id):
    """Check if user can create more exams"""
    sub = get_user_subscription(user_id)
    return sub['remaining'] > 0, sub

def increment_exam_usage(user_id):
    """Increment exam usage count"""
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE subscriptions SET exams_used = exams_used + 1 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def extract_pdf_text(pdf_file):
    """Extract text from PDF"""
    try:
        pdf_reader = PyPDF2.PdfReader(pdf_file)
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text()
        return text
    except:
        return None

def analyze_with_gemini(prompt, context=""):
    """Send request to Gemini API"""
    try:
        full_prompt = f"{context}\n\n{prompt}" if context else prompt
        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=full_prompt
        )
        return response.text
    except Exception as e:
        return f"Error analyzing with Gemini: {str(e)}"

def parse_gemini_json(text):
    """Extract JSON from Gemini response (handles markdown code blocks)"""
    import re
    # Try stripping markdown code fences
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    # Find first { ... } block
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return json.loads(text)

# ==================== SUPABASE HELPERS ====================

def _get_user_email(user_id):
    """Resolve a SQLite integer user_id → email (used before Supabase writes)."""
    if not user_id:
        return None
    try:
        conn = get_db()
        c    = conn.cursor()
        c.execute('SELECT email FROM users WHERE id = ?', (user_id,))
        row  = c.fetchone()
        conn.close()
        return row['email'] if row else None
    except Exception:
        return None

def save_user(email, name, role, password_hash):
    """Upsert a user row in Supabase on signup."""
    if not _sb:
        return None
    try:
        res = _sb.table('users').upsert(
            {'email': email, 'name': name, 'role': role, 'password_hash': password_hash},
            on_conflict='email'
        ).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        print(f'[Supabase] save_user error: {e}')
        return None

def get_user(email):
    """Retrieve a user row from Supabase by email."""
    if not _sb:
        return None
    try:
        res = _sb.table('users').select('*').eq('email', email).maybe_single().execute()
        return res.data
    except Exception as e:
        print(f'[Supabase] get_user error: {e}')
        return None

_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

def user_is_registered(email):
    """Return True if email is in Supabase users table. Fails open if Supabase is unavailable."""
    if not _sb:
        return True  # can't verify — fail open
    try:
        res = _sb.table('users').select('email').eq('email', email).maybe_single().execute()
        return res.data is not None
    except Exception as e:
        print(f'[Atlas] user_is_registered error: {e}')
        return True  # fail open on transient error

def check_monthly_usage(user_email):
    """Count AI operations logged for user_email in the current calendar month."""
    if not _sb or not user_email:
        return 0
    try:
        now = datetime.utcnow()
        month_start = now.strftime('%Y-%m-01T00:00:00')
        res = (
            _sb.table('exam_results')
               .select('id', count='exact')
               .eq('user_email', user_email)
               .gte('created_at', month_start)
               .execute()
        )
        return res.count or 0
    except Exception as e:
        print(f'[Atlas] check_monthly_usage error: {e}')
        return 0

def get_plan_limit(plan, role):
    """Return monthly operation limit for a plan+role combination."""
    plan = (plan or 'free').lower()
    role = (role or 'student').lower()
    if plan in ('pro', 'plus'):
        if role == 'school':
            return 1500
        elif role == 'teacher':
            return 400
        else:
            return 300  # student pro
    return 10  # free tier

def save_exam_result(user_email, exam_type, score=None, feedback=None, subject=None):
    """Persist an exam result to Supabase. Non-fatal — never raises."""
    if not _sb or not user_email:
        return None
    try:
        res = _sb.table('exam_results').insert({
            'user_email': user_email,
            'exam_type':  exam_type,
            'score':      score,
            'feedback':   str(feedback)[:2000] if feedback else None,
            'subject':    subject,
        }).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        print(f'[Supabase] save_exam_result error: {e}')
        return None

def get_user_exams(user_email):
    """Return all exam results for a user, newest first."""
    if not _sb:
        return []
    try:
        res = (
            _sb.table('exam_results')
               .select('id,exam_type,score,feedback,subject,created_at')
               .eq('user_email', user_email)
               .order('created_at', desc=True)
               .execute()
        )
        return res.data or []
    except Exception as e:
        print(f'[Supabase] get_user_exams error: {e}')
        return []

# ==================== EMAIL HELPERS ====================

def send_verification_email(to_email, name, code):
    """Send a 6-digit verification code via Resend HTTP API."""
    resend_key = os.environ.get('RESEND_API_KEY', '')
    if not resend_key:
        print(f'[Resend] No API key — code for {to_email}: {code}')
        return
    first_name = name.split()[0] if name else 'there'
    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F4F1EA;font-family:Inter,Arial,sans-serif;">
<div style="max-width:480px;margin:40px auto;background:#fff;border-radius:16px;border:1px solid #E0DDD5;overflow:hidden;">
  <div style="background:#1A1814;padding:28px 32px;">
    <p style="color:#F4F1EA;font-size:1.1rem;font-weight:800;letter-spacing:-0.3px;margin:0;">ATLAS</p>
  </div>
  <div style="padding:32px;">
    <h2 style="color:#1A1814;font-size:1.2rem;font-weight:700;margin:0 0 8px;">Hi {first_name},</h2>
    <p style="color:#6B6560;font-size:0.9rem;line-height:1.6;margin:0 0 28px;">
      Here is your Atlas verification code. It expires in <strong>10 minutes</strong>.
    </p>
    <div style="background:#F4F1EA;border-radius:12px;padding:24px;text-align:center;margin-bottom:28px;">
      <p style="color:#1A1814;font-size:2.4rem;font-weight:800;letter-spacing:14px;margin:0;font-variant-numeric:tabular-nums;">{code}</p>
    </div>
    <p style="color:#9E9A94;font-size:0.78rem;line-height:1.5;margin:0;">
      If you did not request this, you can safely ignore this email.
    </p>
  </div>
</div>
</body>
</html>"""
    try:
        resp = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {resend_key}',
                'Content-Type':  'application/json',
            },
            json={
                'from':    'Atlas <onboarding@resend.dev>',
                'to':      [to_email],
                'subject': 'Your Atlas verification code',
                'html':    html_body,
            },
            verify=False,
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            print(f'[Resend] Failed ({resp.status_code}): {resp.text}')
        else:
            print(f'[Resend] Code sent to {to_email}')
    except Exception as e:
        print(f'[Resend] Error sending to {to_email}: {e}')


# ==================== AUTHENTICATION ROUTES ====================

@app.route('/api/auth/signup', methods=['POST'])
def signup():
    """
    Step 1 of registration: validate inputs, send verification code, store pending record.
    Does NOT create the account yet — that happens in /api/auth/verify-email.
    """
    data = request.get_json()

    if not data or not data.get('email') or not data.get('password') or not data.get('name'):
        return jsonify({'error': 'Missing required fields'}), 400

    email = data['email'].strip().lower()
    if not _EMAIL_RE.match(email):
        return jsonify({'error': 'Please enter a valid email address.'}), 400

    name     = data['name'].strip()
    password = data['password']
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters.'}), 400

    # Duplicate check: SQLite first, then Supabase (handles Railway resets)
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id FROM users WHERE email = ?', (email,))
        if c.fetchone():
            conn.close()
            return jsonify({'error': 'An account with this email already exists. Please sign in.'}), 400
        conn.close()
    except Exception:
        pass

    if _sb:
        try:
            res = _sb.table('users').select('email').eq('email', email).maybe_single().execute()
            if res.data:
                return jsonify({'error': 'An account with this email already exists. Please sign in.'}), 400
        except Exception:
            pass

    # Generate code and store pending record
    code       = str(random.randint(100000, 999999))
    expires_at = _time.time() + 600  # 10 minutes

    _pending_verifications[email] = {
        'name':          name,
        'password_hash': generate_password_hash(password),
        'code':          code,
        'expires_at':    expires_at,
    }

    send_verification_email(email, name, code)

    return jsonify({'needs_verification': True, 'email': email}), 200


@app.route('/api/auth/verify-email', methods=['POST'])
def verify_email_route():
    """
    Step 2 of registration: validate the 6-digit code and create the account.
    """
    data  = request.get_json()
    email = (data.get('email') or '').strip().lower()
    code  = str(data.get('code') or '').strip()

    if not email or not code:
        return jsonify({'error': 'Email and verification code are required.'}), 400

    pending = _pending_verifications.get(email)
    if not pending:
        return jsonify({'error': 'No pending verification found. Please sign up again.'}), 400

    if _time.time() > pending['expires_at']:
        _pending_verifications.pop(email, None)
        return jsonify({'error': 'Verification code expired. Please sign up again.'}), 400

    if pending['code'] != code:
        return jsonify({'error': 'Incorrect verification code. Please try again.'}), 400

    # Code valid — create the account
    name          = pending['name']
    password_hash = pending['password_hash']
    _pending_verifications.pop(email, None)

    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            'INSERT INTO users (email, password, name, role) VALUES (?, ?, ?, ?)',
            (email, password_hash, name, 'student')
        )
        user_id = c.lastrowid
        c.execute(
            'INSERT INTO subscriptions (user_id, tier, exams_limit) VALUES (?, ?, ?)',
            (user_id, 'free', 5)
        )
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        return jsonify({'error': 'An account with this email already exists. Please sign in.'}), 400
    except Exception as e:
        print(f'[Verify] DB error: {e}')
        return jsonify({'error': 'Account creation failed. Please try again.'}), 500

    save_user(email, name, 'student', password_hash)
    access_token = create_access_token(identity=str(user_id))

    return jsonify({
        'success':      True,
        'message':      'Email verified — account created.',
        'access_token': access_token,
        'user':         {'id': user_id, 'email': email, 'name': name, 'role': 'student'},
    }), 201


@app.route('/api/auth/resend-code', methods=['POST'])
def resend_code():
    """Regenerate and resend a verification code for a pending signup."""
    data  = request.get_json()
    email = (data.get('email') or '').strip().lower()

    pending = _pending_verifications.get(email)
    if not pending:
        return jsonify({'error': 'No pending signup for this email. Please sign up again.'}), 400

    code               = str(random.randint(100000, 999999))
    pending['code']       = code
    pending['expires_at'] = _time.time() + 600

    send_verification_email(email, pending['name'], code)

    return jsonify({'success': True, 'message': 'Verification code resent.'}), 200

@app.route('/api/auth/login', methods=['POST'])
def login():
    """User login — checks SQLite first, falls back to Supabase on Railway restarts."""
    data = request.get_json()

    if not data or not data.get('email') or not data.get('password'):
        return jsonify({'error': 'Missing email or password'}), 400

    email    = data['email']
    password = data['password']

    # ── 1. Try SQLite (primary store) ──────────────────────────────────────
    conn = get_db()
    c    = conn.cursor()
    c.execute('SELECT * FROM users WHERE email = ?', (email,))
    user = c.fetchone()
    conn.close()

    if user:
        if not check_password_hash(user['password'], password):
            return jsonify({'error': 'Invalid credentials'}), 401
        access_token = create_access_token(identity=str(user['id']))
        return jsonify({
            'success':      True,
            'access_token': access_token,
            'name':         user['name'],
            'role':         user['role'],
            'user': {
                'id':    user['id'],
                'email': user['email'],
                'name':  user['name'],
                'role':  user['role'],
            }
        }), 200

    # ── 2. SQLite miss — try Supabase (recovers after Railway restart wipes DB) ──
    sb_user = get_user(email)
    if sb_user and check_password_hash(sb_user.get('password_hash', ''), password):
        name = sb_user.get('name', email)
        role = sb_user.get('role', 'student')
        # Recreate the SQLite row so future requests don't need Supabase
        try:
            conn = get_db()
            c    = conn.cursor()
            c.execute(
                'INSERT OR IGNORE INTO users (email, password, name, role) VALUES (?, ?, ?, ?)',
                (email, sb_user['password_hash'], name, role)
            )
            if c.rowcount:
                user_id = c.lastrowid
                c.execute(
                    'INSERT OR IGNORE INTO subscriptions (user_id, tier, exams_limit) VALUES (?, ?, ?)',
                    (user_id, 'free', 5)
                )
            else:
                c.execute('SELECT id FROM users WHERE email = ?', (email,))
                user_id = c.fetchone()['id']
            conn.commit()
            conn.close()
        except Exception as _e:
            print(f'[Login] SQLite recreate error: {_e}')
            user_id = 0

        access_token = create_access_token(identity=str(user_id))
        return jsonify({
            'success':      True,
            'access_token': access_token,
            'name':         name,
            'role':         role,
            'user': {
                'id':    user_id,
                'email': email,
                'name':  name,
                'role':  role,
            }
        }), 200

    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/auth/profile', methods=['GET'])
@jwt_required()
def get_profile():
    """Get user profile"""
    user_id = get_jwt_identity()
    
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id, email, name, role, subscription_tier FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    subscription = get_user_subscription(user_id)
    
    return jsonify({
        'user': {
            'id': user['id'],
            'email': user['email'],
            'name': user['name'],
            'role': user['role'],
            'subscription': subscription
        }
    }), 200

# ==================== EXAM GENERATION ROUTES ====================

@app.route('/api/exams/generate', methods=['POST'])
@jwt_required()
def generate_exam():
    """Generate exam from PDF using Gemini"""
    user_id = get_jwt_identity()
    
    # Check exam limit
    can_create, sub = check_exam_limit(user_id)
    if not can_create:
        return jsonify({
            'error': f'Exam limit reached. Current tier: {sub["tier"]}, Limit: {sub["exams_limit"]}'
        }), 429
    
    # Get request data
    data = request.get_json()
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    title = data.get('title', 'Untitled Exam')
    subject = data.get('subject', 'General')
    difficulty = data.get('difficulty', 'medium')
    exam_type = data.get('exam_type', 'multiple_choice')
    time_limit = data.get('time_limit', 60)
    questions_count = data.get('questions_count', 10)
    
    try:
        # Extract PDF text
        pdf_text = extract_pdf_text(file)
        
        if not pdf_text:
            return jsonify({'error': 'Could not extract text from PDF'}), 400
        
        # Generate exam with Gemini
        prompt = f"""
        Create a {questions_count}-question exam from this content:
        
        {pdf_text[:3000]}  # Limit to first 3000 chars
        
        Requirements:
        - Type: {exam_type}
        - Difficulty: {difficulty}
        - Format: Return JSON array with questions
        - Each question should have: question, options (if multiple choice), correct_answer, explanation
        
        Return ONLY valid JSON, no other text.
        """
        
        gemini_response = analyze_with_gemini(prompt)
        
        # Save exam to database
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO exams (user_id, title, subject, difficulty, exam_type, time_limit, questions_count)
                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (user_id, title, subject, difficulty, exam_type, time_limit, questions_count))
        exam_id = c.lastrowid
        conn.commit()
        conn.close()
        
        # Increment usage
        increment_exam_usage(user_id)
        
        return jsonify({
            'success': True,
            'message': 'Exam generated successfully',
            'exam_id': exam_id,
            'exam_data': gemini_response,
            'remaining_exams': sub['remaining'] - 1
        }), 201
        
    except Exception as e:
        return jsonify({'error': f'Error generating exam: {str(e)}'}), 500

@app.route('/api/exams/list', methods=['GET'])
@jwt_required()
def list_exams():
    """Get user's exams"""
    user_id = get_jwt_identity()
    
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT id, title, subject, difficulty, exam_type, created_at 
                 FROM exams WHERE user_id = ? ORDER BY created_at DESC''', (user_id,))
    exams = c.fetchall()
    conn.close()
    
    return jsonify({
        'exams': [dict(exam) for exam in exams]
    }), 200

# ==================== PDF ANALYSIS ROUTES ====================

@app.route('/api/analyze/exam', methods=['POST'])
@jwt_required()
def analyze_exam():
    """Analyze student exam (teacher route)"""
    user_id = get_jwt_identity()
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    data = request.get_json()
    
    try:
        # Extract PDF text
        pdf_text = extract_pdf_text(file)
        
        if not pdf_text:
            return jsonify({'error': 'Could not extract text from PDF'}), 400
        
        # Analyze with Gemini
        prompt = f"""
        Analyze this student exam and provide:
        1. Total score (out of 100)
        2. Each answer's evaluation
        3. Mistakes identified
        4. Improvement suggestions
        
        Exam content:
        {pdf_text[:2000]}
        
        Return as JSON with: score, mistakes, improvements, feedback
        """
        
        analysis = analyze_with_gemini(prompt)
        
        # Save analysis
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO analysis (user_id, analysis_type, gemini_response)
                     VALUES (?, ?, ?)''',
                  (user_id, 'teacher_exam_analysis', analysis))
        analysis_id = c.lastrowid
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'analysis_id': analysis_id,
            'analysis': analysis
        }), 200
        
    except Exception as e:
        return jsonify({'error': f'Error analyzing exam: {str(e)}'}), 500

@app.route('/api/analyze/student-exam', methods=['POST'])
@jwt_required()
def analyze_student_exam():
    """Analyze student's own exam (student route)"""
    user_id = get_jwt_identity()
    
    # Check if premium
    subscription = get_user_subscription(user_id)
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    
    try:
        pdf_text = extract_pdf_text(file)
        
        if not pdf_text:
            return jsonify({'error': 'Could not extract text from PDF'}), 400
        
        # Different analysis based on tier
        if subscription['tier'] == 'free':
            # Free tier: Summary only
            prompt = f"""
            Provide a brief summary of this exam document:
            {pdf_text[:1500]}
            
            Return as JSON with: summary, key_topics
            """
        else:
            # Premium tier: Full analysis
            prompt = f"""
            Analyze this exam and provide detailed feedback:
            {pdf_text[:2000]}
            
            Return as JSON with: score, mistakes, how_to_improve, weak_areas, study_tips
            """
        
        analysis = analyze_with_gemini(prompt)
        
        # Save analysis
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO analysis (user_id, analysis_type, gemini_response)
                     VALUES (?, ?, ?)''',
                  (user_id, 'student_exam_analysis', analysis))
        analysis_id = c.lastrowid
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'analysis_id': analysis_id,
            'analysis': analysis,
            'tier_info': {
                'current_tier': subscription['tier'],
                'features': 'Summary only' if subscription['tier'] == 'free' else 'Full analysis with mistakes and improvements'
            }
        }), 200
        
    except Exception as e:
        return jsonify({'error': f'Error analyzing exam: {str(e)}'}), 500

# ==================== MAIN EXAM ANALYSIS (Gemini 2.5 Flash) ====================

@app.route('/api/analyze/pdf-exam', methods=['POST'])
@jwt_required(optional=True)
def analyze_pdf_exam():
    """
    Core feature: student uploads their exam for AI analysis.
    Accepts a single PDF ('file' key) OR one/more images ('files' key: JPG/PNG/WEBP/HEIC).
    All files are uploaded to the Gemini Files API and passed together for multimodal analysis.
    """
    import tempfile, time

    user_id      = get_jwt_identity()
    user_email   = request.form.get('user_email', '')
    user_plan    = request.form.get('plan', 'free')
    user_role    = request.form.get('role', 'student')
    subject      = request.form.get('subject', 'General')
    student_name = request.form.get('student_name', 'Student')

    if not user_email:
        return jsonify({'error': 'Please sign in to use this feature.'}), 401
    if not user_is_registered(user_email):
        return jsonify({'error': 'Account not found. Please sign in again.'}), 401
    limit = get_plan_limit(user_plan, user_role)
    used  = check_monthly_usage(user_email)
    if used >= limit:
        return jsonify({
            'error':         f'Monthly limit reached ({used}/{limit} operations). Upgrade your plan to continue.',
            'limit_reached': True,
            'used':          used,
            'limit':         limit,
            'plan':          user_plan,
        }), 429

    MIME_MAP = {
        '.pdf':  'application/pdf',
        '.jpg':  'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png':  'image/png',
        '.webp': 'image/webp',
        '.heic': 'image/heic',
        '.heif': 'image/heic',
    }

    # Prefer 'files' list (image mode); fall back to single 'file' (PDF mode)
    raw_files = request.files.getlist('files')
    if not raw_files or not raw_files[0].filename:
        single = request.files.get('file')
        if not single or not single.filename:
            return jsonify({'error': 'No file provided'}), 400
        raw_files = [single]

    for f in raw_files:
        ext = os.path.splitext(f.filename.lower())[1]
        if ext not in MIME_MAP:
            return jsonify({'error': f'Unsupported file: {f.filename}. Allowed: PDF, JPG, PNG, WEBP, HEIC'}), 400

    tmp_paths    = []
    gemini_files = []

    try:
        for f in raw_files:
            ext  = os.path.splitext(f.filename.lower())[1]
            mime = MIME_MAP[ext]
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                f.save(tmp.name)
                tmp_path = tmp.name
            tmp_paths.append(tmp_path)

            gf = client.files.upload(file=tmp_path, config={'mime_type': mime})
            while gf.state.name == 'PROCESSING':
                time.sleep(1)
                gf = client.files.get(name=gf.name)
            if gf.state.name != 'ACTIVE':
                return jsonify({'error': f'File processing failed for {f.filename}'}), 500
            gemini_files.append(gf)

        is_pdf = len(raw_files) == 1 and raw_files[0].filename.lower().endswith('.pdf')
        if is_pdf:
            media_note = 'The exam is provided as a PDF document (may be typed or handwritten/scanned).'
        elif len(raw_files) == 1:
            media_note = 'The exam is provided as a photographed image of a single page.'
        else:
            media_note = (
                f'The exam is provided as {len(raw_files)} photographed images, '
                f'one image per exam page, in order from first to last page.'
            )

        prompt = f"""You are an expert, encouraging exam marker for a student named {student_name} in the subject: {subject}.

{media_note} Carefully read all content across every page and provide a detailed analysis.

Return ONLY a valid JSON object with this exact structure (no markdown, no explanation outside the JSON):
{{
  "student_name": "{student_name}",
  "subject": "{subject}",
  "overall_score": <integer 0-100>,
  "grade": "<A+/A/B/C/D/F>",
  "summary": "<2-3 sentence overall assessment>",
  "questions": [
    {{
      "question_number": <int>,
      "topic": "<topic/concept tested>",
      "marks_awarded": <number>,
      "marks_available": <number>,
      "student_answer_summary": "<brief summary of what the student wrote>",
      "correct_approach": "<what the correct answer/method should be>",
      "mistakes": ["<mistake 1>", "<mistake 2>"],
      "feedback": "<specific, actionable feedback for this question>"
    }}
  ],
  "strengths": ["<strength 1>", "<strength 2>", "<strength 3>"],
  "weaknesses": ["<weakness 1>", "<weakness 2>", "<weakness 3>"],
  "study_plan": [
    {{
      "priority": "<High/Medium/Low>",
      "topic": "<topic to study>",
      "action": "<specific action to take>",
      "time_estimate": "<e.g. 2 hours>"
    }}
  ],
  "encouragement": "<warm, specific motivational message for this student>"
}}

Be rigorous but kind. Give concrete, specific feedback the student can act on immediately."""

        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=[prompt] + gemini_files
        )

        try:
            analysis_data = parse_gemini_json(response.text)
        except Exception:
            analysis_data = {'raw_response': response.text, 'parse_error': True}

        conn = get_db()
        c    = conn.cursor()
        c.execute(
            '''INSERT INTO analysis (user_id, analysis_type, gemini_response, score, feedback)
               VALUES (?, ?, ?, ?, ?)''',
            (
                user_id or 0,
                'pdf_exam_analysis_v2',
                json.dumps(analysis_data),
                analysis_data.get('overall_score'),
                analysis_data.get('summary'),
            )
        )
        analysis_id = c.lastrowid
        conn.commit()
        conn.close()

        save_exam_result(
            _get_user_email(user_id),
            'pdf_exam_analysis',
            analysis_data.get('overall_score'),
            analysis_data.get('summary'),
            subject,
        )

        return jsonify({
            'success':     True,
            'analysis_id': analysis_id,
            'analysis':    analysis_data,
        }), 200

    except Exception as e:
        return jsonify({'error': f'Analysis failed: {str(e)}'}), 500

    finally:
        for tp in tmp_paths:
            try:
                os.unlink(tp)
            except Exception:
                pass
        for gf in gemini_files:
            try:
                client.files.delete(name=gf.name)
            except Exception:
                pass

# ==================== SUMMARIZE PDF ====================

@app.route('/api/summarize/pdf', methods=['POST'])
@jwt_required(optional=True)
def summarize_pdf():
    """
    Student uploads any PDF document; Atlas returns a structured summary.
    No auth required — JWT is optional so the public page can call this directly.
    """
    user_id    = get_jwt_identity()
    user_email = request.form.get('user_email', '')
    user_plan  = request.form.get('plan', 'free')
    user_role  = request.form.get('role', 'student')

    if not user_email:
        return jsonify({'error': 'Please sign in to use this feature.'}), 401
    if not user_is_registered(user_email):
        return jsonify({'error': 'Account not found. Please sign in again.'}), 401
    limit = get_plan_limit(user_plan, user_role)
    used  = check_monthly_usage(user_email)
    if used >= limit:
        return jsonify({
            'error':         f'Monthly limit reached ({used}/{limit} operations). Upgrade your plan to continue.',
            'limit_reached': True,
            'used':          used,
            'limit':         limit,
            'plan':          user_plan,
        }), 429

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Only PDF files are accepted'}), 400

    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    gemini_file = None
    try:
        gemini_file = client.files.upload(
            file=tmp_path,
            config={'mime_type': 'application/pdf'}
        )

        import time
        while gemini_file.state.name == 'PROCESSING':
            time.sleep(1)
            gemini_file = client.files.get(name=gemini_file.name)

        if gemini_file.state.name != 'ACTIVE':
            return jsonify({'error': 'PDF processing failed'}), 500

        prompt = """You are an expert academic summarizer. Read this entire document carefully and produce a structured summary.

Return ONLY a valid JSON object with this exact structure (no markdown, no text outside the JSON):
{
  "title": "<document title or best inferred title>",
  "overview": "<2-3 sentence overview of the entire document>",
  "detailed_summary": "<comprehensive 3-5 paragraph summary covering all major sections>",
  "key_points": [
    "<key point 1>",
    "<key point 2>",
    "<key point 3>",
    "<key point 4>",
    "<key point 5>"
  ],
  "key_topics": ["<topic 1>", "<topic 2>", "<topic 3>", "<topic 4>", "<topic 5>"],
  "difficulty_level": "<Beginner / Intermediate / Advanced>",
  "estimated_read_time": "<e.g. 12 minutes>"
}

Be thorough and accurate. Key points should be specific and useful for studying."""

        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=[prompt, gemini_file]
        )

        try:
            summary_data = parse_gemini_json(response.text)
        except Exception:
            summary_data = {'raw_response': response.text, 'parse_error': True}

        conn = get_db()
        c = conn.cursor()
        c.execute(
            'INSERT INTO analysis (user_id, analysis_type, gemini_response) VALUES (?, ?, ?)',
            (user_id or 0, 'pdf_summarize', json.dumps(summary_data))
        )
        conn.commit()
        conn.close()

        save_exam_result(
            _get_user_email(user_id),
            'pdf_summarize',
            None,
            summary_data.get('overview'),
            None,
        )

        return jsonify({'success': True, 'summary': summary_data}), 200

    except Exception as e:
        return jsonify({'error': f'Summarization failed: {str(e)}'}), 500

    finally:
        os.unlink(tmp_path)
        if gemini_file:
            try:
                client.files.delete(name=gemini_file.name)
            except Exception:
                pass


# ==================== CREATE EXAM FROM PDF ====================

@app.route('/api/create-exam/pdf', methods=['POST'])
@jwt_required(optional=True)
def create_exam_from_pdf():
    """
    Student uploads a topic/chapter PDF; Atlas generates a full practice exam with mark scheme.
    No auth required — JWT is optional so the public page can call this directly.
    """
    user_id    = get_jwt_identity()
    user_email = request.form.get('user_email', '')
    user_plan  = request.form.get('plan', 'free')
    user_role  = request.form.get('role', 'student')

    if not user_email:
        return jsonify({'error': 'Please sign in to use this feature.'}), 401
    if not user_is_registered(user_email):
        return jsonify({'error': 'Account not found. Please sign in again.'}), 401
    limit = get_plan_limit(user_plan, user_role)
    used  = check_monthly_usage(user_email)
    if used >= limit:
        return jsonify({
            'error':         f'Monthly limit reached ({used}/{limit} operations). Upgrade your plan to continue.',
            'limit_reached': True,
            'used':          used,
            'limit':         limit,
            'plan':          user_plan,
        }), 429

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Only PDF files are accepted'}), 400

    subject    = request.form.get('subject', 'General')
    difficulty = request.form.get('difficulty', 'Medium')
    exam_type  = request.form.get('exam_type', 'Mixed')
    num_pages  = int(request.form.get('num_pages', 5))
    num_pages  = max(2, min(num_pages, 13))  # clamp 2–13

    # Estimate question count from pages and difficulty
    qpp = {'Easy': 7, 'Medium': 5, 'Hard': 4, 'Expert': 3}
    num_questions = num_pages * qpp.get(difficulty, 5)

    # Describe the exam type in natural language for the prompt
    type_desc = {
        'Multiple Choice':      'exclusively Multiple Choice Questions (MCQ) with 4 options each (A, B, C, D). Every question must have exactly 4 options and a single correct letter answer.',
        'Explain & Essay':      'structured Explain and Essay questions that require detailed written responses. No MCQ. Each question should demand a paragraph or more of explanation.',
        'Practical to Theory':  'questions based on practical scenarios, lab experiments, or real-world case studies that test underlying theoretical understanding. No MCQ options.',
        'Mixed':                'a balanced mix of MCQ, Short Answer, Explain, and Essay questions covering a range of cognitive levels.',
    }.get(exam_type, 'a mix of question types')

    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    gemini_file = None
    try:
        gemini_file = client.files.upload(
            file=tmp_path,
            config={'mime_type': 'application/pdf'}
        )

        import time
        while gemini_file.state.name == 'PROCESSING':
            time.sleep(1)
            gemini_file = client.files.get(name=gemini_file.name)

        if gemini_file.state.name != 'ACTIVE':
            return jsonify({'error': 'PDF processing failed'}), 500

        prompt = f"""You are an expert {difficulty}-level exam setter for {subject}. Read this document carefully and create a well-structured practice exam.

Exam specification:
- Subject: {subject}
- Difficulty: {difficulty}
- Exam type: {type_desc}
- Target length: approximately {num_pages} A4 pages (~{num_questions} questions total)

Return ONLY a valid JSON object with this exact structure (no markdown, no text outside the JSON):
{{
  "subject": "{subject}",
  "difficulty": "{difficulty}",
  "exam_type": "{exam_type}",
  "total_questions": <actual number of questions you generated>,
  "questions": [
    {{
      "number": 1,
      "type": "MCQ",
      "marks": 1,
      "question": "<full question text>",
      "options": ["A. <option>", "B. <option>", "C. <option>", "D. <option>"],
      "answer": "A",
      "mark_scheme": "<brief explanation of why this answer is correct>"
    }},
    {{
      "number": 2,
      "type": "Short Answer",
      "marks": 3,
      "question": "<full question text>",
      "options": [],
      "answer": "<model answer>",
      "mark_scheme": "<marking guidance: what earns each mark>"
    }}
  ]
}}

Rules:
- All questions must be based strictly on the content in the uploaded document
- Calibrate challenge to {difficulty} level: Easy = recall & basic understanding, Medium = application, Hard = analysis & synthesis, Expert = evaluation & novel application
- For MCQ: always provide exactly 4 options labeled A–D; answer field is just the letter (e.g. "B"); leave options empty [] for non-MCQ
- Marks per question: MCQ = 1, Short Answer = 2–4, Explain = 4–6, Essay = 6–12
- Mark schemes must be specific, detailed, and useful for self-marking or peer marking"""

        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=[prompt, gemini_file]
        )

        try:
            exam_data = parse_gemini_json(response.text)
        except Exception:
            exam_data = {'raw_response': response.text, 'parse_error': True}

        conn = get_db()
        c = conn.cursor()
        c.execute(
            'INSERT INTO analysis (user_id, analysis_type, gemini_response) VALUES (?, ?, ?)',
            (user_id or 0, 'pdf_create_exam', json.dumps(exam_data))
        )
        conn.commit()
        conn.close()

        save_exam_result(
            _get_user_email(user_id),
            'pdf_create_exam',
            None,
            f"{exam_data.get('total_questions', '?')} questions generated",
            subject,
        )

        return jsonify({'success': True, 'exam': exam_data}), 200

    except Exception as e:
        return jsonify({'error': f'Exam generation failed: {str(e)}'}), 500

    finally:
        os.unlink(tmp_path)
        if gemini_file:
            try:
                client.files.delete(name=gemini_file.name)
            except Exception:
                pass


# ==================== BULK EXAM CHECKING ====================

@app.route('/api/bulk-check', methods=['POST'])
@jwt_required(optional=True)
def bulk_check():
    """
    School admin uploads multiple student exam PDFs at once.
    Optional answer key PDF is used as the marking reference.
    Returns per-student scores, grades, pass/fail, and a class summary.
    """
    import tempfile, time
    user_id    = get_jwt_identity()
    user_email = request.form.get('user_email', '')
    user_plan  = request.form.get('plan', 'free')
    user_role  = request.form.get('role', 'school')

    if not user_email:
        return jsonify({'error': 'Please sign in to use this feature.'}), 401
    if not user_is_registered(user_email):
        return jsonify({'error': 'Account not found. Please sign in again.'}), 401
    limit = get_plan_limit(user_plan, user_role)
    used  = check_monthly_usage(user_email)
    if used >= limit:
        return jsonify({
            'error':         f'Monthly limit reached ({used}/{limit} operations). Upgrade your plan to continue.',
            'limit_reached': True,
            'used':          used,
            'limit':         limit,
            'plan':          user_plan,
        }), 429

    student_files = request.files.getlist('files')
    student_files = [f for f in student_files if f.filename]
    if not student_files:
        return jsonify({'error': 'No student exam files provided'}), 400

    student_names = request.form.getlist('student_names')
    subject       = request.form.get('subject', 'General')

    # ── Upload answer key once (optional) ──
    answer_key_gemini = None
    answer_key_tmp    = None
    if 'answer_key' in request.files:
        ak = request.files['answer_key']
        if ak.filename and ak.filename.lower().endswith('.pdf'):
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                ak.save(tmp.name)
                answer_key_tmp = tmp.name
            try:
                answer_key_gemini = client.files.upload(
                    file=answer_key_tmp,
                    config={'mime_type': 'application/pdf'}
                )
                while answer_key_gemini.state.name == 'PROCESSING':
                    time.sleep(1)
                    answer_key_gemini = client.files.get(name=answer_key_gemini.name)
                if answer_key_gemini.state.name != 'ACTIVE':
                    answer_key_gemini = None
            except Exception:
                answer_key_gemini = None

    results = []

    for i, student_file in enumerate(student_files):
        student_name = (
            student_names[i].strip()
            if i < len(student_names) and student_names[i].strip()
            else student_file.filename.rsplit('.', 1)[0].replace('_', ' ')
        )

        if not student_file.filename.lower().endswith('.pdf'):
            results.append({
                'student_name': student_name,
                'score': None, 'grade': '—',
                'status': 'Error', 'summary': 'File must be a PDF', 'error': True
            })
            continue

        tmp_path    = None
        gemini_file = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                student_file.save(tmp.name)
                tmp_path = tmp.name

            gemini_file = client.files.upload(
                file=tmp_path,
                config={'mime_type': 'application/pdf'}
            )
            while gemini_file.state.name == 'PROCESSING':
                time.sleep(1)
                gemini_file = client.files.get(name=gemini_file.name)

            if gemini_file.state.name != 'ACTIVE':
                results.append({
                    'student_name': student_name,
                    'score': None, 'grade': '—',
                    'status': 'Error', 'summary': 'PDF processing failed', 'error': True
                })
                continue

            grade_scale = 'A+ 90-100, A 80-89, B 70-79, C 60-69, D 50-59, F below 50. Pass = 50 or above.'
            json_schema = (
                '{\n'
                '  "student_name": "<name>",\n'
                '  "overall_score": <integer 0-100>,\n'
                '  "grade": "<A+/A/B/C/D/F>",\n'
                '  "pass_fail": "<Pass/Fail>",\n'
                '  "summary": "<1-2 sentence assessment>"\n'
                '}'
            )

            if answer_key_gemini:
                prompt = (
                    f'You are an expert exam marker. The FIRST document is the ANSWER KEY for a {subject} exam. '
                    f'The SECOND document is the exam paper written by {student_name}.\n'
                    f'Compare the student\'s answers against the answer key and assign a score.\n\n'
                    f'Return ONLY valid JSON (no markdown):\n{json_schema}\n\n'
                    f'Grade scale: {grade_scale}'
                )
                contents = [prompt, answer_key_gemini, gemini_file]
            else:
                prompt = (
                    f'You are an expert exam marker. Read this {subject} exam paper written by {student_name} '
                    f'and estimate a score based on correctness and quality of answers.\n\n'
                    f'Return ONLY valid JSON (no markdown):\n{json_schema}\n\n'
                    f'Grade scale: {grade_scale}'
                )
                contents = [prompt, gemini_file]

            response = client.models.generate_content(
                model='gemini-2.0-flash',
                contents=contents
            )

            try:
                data = parse_gemini_json(response.text)
                results.append({
                    'student_name': student_name,
                    'score':        data.get('overall_score', 0),
                    'grade':        data.get('grade', '—'),
                    'status':       data.get('pass_fail', '—'),
                    'summary':      data.get('summary', ''),
                    'error':        False
                })
            except Exception:
                results.append({
                    'student_name': student_name,
                    'score': None, 'grade': '—',
                    'status': 'Error', 'summary': 'Could not parse AI response', 'error': True
                })

        except Exception as e:
            results.append({
                'student_name': student_name,
                'score': None, 'grade': '—',
                'status': 'Error', 'summary': str(e), 'error': True
            })

        finally:
            if tmp_path:
                try: os.unlink(tmp_path)
                except Exception: pass
            if gemini_file:
                try: client.files.delete(name=gemini_file.name)
                except Exception: pass

    # Cleanup answer key
    if answer_key_tmp:
        try: os.unlink(answer_key_tmp)
        except Exception: pass
    if answer_key_gemini:
        try: client.files.delete(name=answer_key_gemini.name)
        except Exception: pass

    valid_scores = [r['score'] for r in results if r.get('score') is not None and not r.get('error')]
    avg_score = round(sum(valid_scores) / len(valid_scores)) if valid_scores else None

    conn = get_db()
    c    = conn.cursor()
    c.execute(
        'INSERT INTO analysis (user_id, analysis_type, gemini_response) VALUES (?, ?, ?)',
        (user_id or 0, 'bulk_check', json.dumps({'results': results, 'subject': subject}))
    )
    conn.commit()
    conn.close()

    save_exam_result(
        _get_user_email(user_id),
        'bulk_check',
        avg_score,
        f"Bulk graded {len(results)} exams in {subject}. Average: {avg_score}%",
        subject,
    )

    return jsonify({
        'success': True,
        'results': results,
        'total':   len(results),
        'summary': {
            'total_graded':  len(valid_scores),
            'average_score': avg_score,
            'highest_score': max(valid_scores) if valid_scores else None,
            'lowest_score':  min(valid_scores) if valid_scores else None,
        }
    }), 200


# ==================== USER EXAM HISTORY ====================

@app.route('/api/user/exams', methods=['GET'])
@jwt_required()
def user_exam_history():
    """Return the authenticated user's exam history from Supabase."""
    user_id = get_jwt_identity()
    email   = _get_user_email(user_id)
    if not email:
        return jsonify({'error': 'User not found'}), 404
    exams = get_user_exams(email)
    return jsonify({'exams': exams, 'total': len(exams)}), 200


# ==================== PAYMENT ROUTES ====================

@app.route('/api/payments/pricing', methods=['GET'])
def get_pricing():
    """Get subscription pricing"""
    return jsonify({
        'tiers': {
            'free': {
                'name': 'Free',
                'price': 0,
                'exams_per_month': 5,
                'features': ['Generate exams', 'Basic PDF summary']
            },
            'basic': {
                'name': 'Basic',
                'price': 9.99,
                'exams_per_month': 8,
                'features': ['Generate exams', 'Full PDF analysis', 'Detailed feedback']
            },
            'pro': {
                'name': 'Pro',
                'price': 19.99,
                'exams_per_month': 12,
                'features': ['Generate exams', 'Full PDF analysis', 'Priority support', 'Advanced analytics']
            }
        }
    }), 200

@app.route('/api/payments/create-checkout', methods=['POST'])
@jwt_required()
def create_checkout():
    """Create Stripe checkout session"""
    user_id = get_jwt_identity()
    data = request.get_json()
    tier = data.get('tier')
    payment_method = data.get('payment_method', 'stripe')  # stripe or paysky
    
    # Get user email
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT email FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    # Pricing
    pricing = {
        'basic': {'price_id': 'price_basic_monthly', 'amount': 999},  # $9.99
        'pro': {'price_id': 'price_pro_monthly', 'amount': 1999}  # $19.99
    }
    
    if tier not in pricing:
        return jsonify({'error': 'Invalid tier'}), 400
    
    try:
        if payment_method == 'stripe':
            # Create Stripe session
            session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price': pricing[tier]['price_id'],
                    'quantity': 1
                }],
                mode='subscription',
                success_url='https://atlas-education.com/success?session_id={CHECKOUT_SESSION_ID}',
                cancel_url='https://atlas-education.com/cancel',
                customer_email=user['email'],
                metadata={'user_id': user_id, 'tier': tier}
            )
            
            return jsonify({
                'checkout_url': session.url,
                'session_id': session.id
            }), 200
        
        elif payment_method == 'paysky':
            # Create Paysky payment
            paysky_data = {
                'amount': pricing[tier]['amount'],
                'currency': 'USD',
                'description': f'Atlas {tier.capitalize()} Plan',
                'customer_email': user['email'],
                'metadata': {'user_id': user_id, 'tier': tier}
            }
            
            # This would call Paysky API
            return jsonify({
                'message': 'Paysky payment initialized',
                'data': paysky_data
            }), 200
        
    except Exception as e:
        return jsonify({'error': f'Payment error: {str(e)}'}), 500

@app.route('/api/payments/webhook/stripe', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhooks"""
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, os.getenv('STRIPE_WEBHOOK_SECRET', 'your-webhook-secret')
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    
    # Handle events
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        user_id = session['metadata'].get('user_id')
        tier = session['metadata'].get('tier')
        
        # Update subscription
        conn = get_db()
        c = conn.cursor()
        c.execute('''UPDATE subscriptions SET tier = ?, exams_used = 0 
                     WHERE user_id = ?''', (tier, user_id))
        conn.commit()
        conn.close()
    
    return jsonify({'success': True}), 200

# ==================== ADMIN ROUTES ====================

@app.route('/api/admin/stats', methods=['GET'])
@jwt_required()
def get_admin_stats():
    """Get platform statistics"""
    user_id = get_jwt_identity()
    
    # Verify admin (simplified - check if first user)
    conn = get_db()
    c = conn.cursor()
    
    c.execute('SELECT COUNT(*) as count FROM users')
    user_count = c.fetchone()['count']
    
    c.execute('SELECT COUNT(*) as count FROM exams')
    exam_count = c.fetchone()['count']
    
    c.execute('SELECT COUNT(*) as count FROM analysis')
    analysis_count = c.fetchone()['count']
    
    c.execute('SELECT COUNT(*) as count FROM subscriptions WHERE tier != "free"')
    paid_users = c.fetchone()['count']
    
    conn.close()
    
    return jsonify({
        'stats': {
            'total_users': user_count,
            'total_exams': exam_count,
            'total_analysis': analysis_count,
            'paid_users': paid_users
        }
    }), 200

# ==================== HEALTH CHECK ====================

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'Atlas SaaS',
        'version': '1.0.0',
        'timestamp': datetime.now().isoformat()
    }), 200

# ==================== ERROR HANDLERS ====================

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500

# ==================== RUN ====================

if __name__ == '__main__':
    print("""
    ==========================================
         ATLAS - AI-Powered Exam Platform
                  SaaS Backend
          Email: contact.gexoai@gmail.com
          Domain: atlas-education.com
    ==========================================
    """)
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
