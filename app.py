# app.py
import os
import re
import base64
from collections import defaultdict

import streamlit as st
from pymongo import MongoClient
import gridfs
import fitz  # PyMuPDF
import requests

# Optional libs (only used if provided)
try:
    import stripe
except Exception:
    stripe = None

# ============== PAGE CONFIG ==============
st.set_page_config(
    page_title="AI Help Pastpapers",
    page_icon="🧠 ",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ============== READ SECRETS & ENV ==============
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))
BACKEND_URL = st.secrets.get("BACKEND_URL", os.getenv("BACKEND_URL", "http://localhost:5000"))

STRIPE_SECRET_KEY = (
    st.secrets.get("STRIPE_SECRET_KEY")
    or (st.secrets.get("stripe", {}).get("SECRET_KEY") if isinstance(st.secrets.get("stripe", {}), dict) else None)
    or os.getenv("STRIPE_SECRET_KEY", "")
)
STRIPE_PUBLISHABLE_KEY = (
    st.secrets.get("STRIPE_PUBLISHABLE_KEY")
    or (st.secrets.get("stripe", {}).get("PUBLISHABLE_KEY") if isinstance(st.secrets.get("stripe", {}), dict) else None)
    or os.getenv("STRIPE_PUBLISHABLE_KEY", "")
)

# Optional price IDs from secrets (recommended)
BASIC_PRICE_ID = (
    (st.secrets.get("stripe", {}).get("BASIC_PRICE_ID") if isinstance(st.secrets.get("stripe", {}), dict) else None)
    or os.getenv("BASIC_PRICE_ID", "price_basic_monthly")  # placeholder
)
PLUS_PRICE_ID = (
    (st.secrets.get("stripe", {}).get("PLUS_PRICE_ID") if isinstance(st.secrets.get("stripe", {}), dict) else None)
    or os.getenv("PLUS_PRICE_ID", "price_plus_monthly")  # placeholder
)
PRO_PRICE_ID = (
    (st.secrets.get("stripe", {}).get("PRO_PRICE_ID") if isinstance(st.secrets.get("stripe", {}), dict) else None)
    or os.getenv("PRO_PRICE_ID", "price_pro_monthly")  # placeholder
)

if stripe and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# ============== OPENAI CLIENT (supports new & old SDKs) ==============
_openai_mode = None
_openai_client = None
try:
    # New SDK style
    from openai import OpenAI
    if OPENAI_API_KEY:
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
        _openai_mode = "new"
except Exception:
    pass

if _openai_mode is None:
    # Try old SDK as a fallback
    try:
        import openai  # old SDK
        if OPENAI_API_KEY:
            openai.api_key = OPENAI_API_KEY
            _openai_mode = "old"
    except Exception:
        pass

# ============== MongoDB Setup ==============
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017/")
try:
    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=2000)
    db = client["data"]
    fs = gridfs.GridFS(db)
    _mongo_ok = True
    client.server_info()  # quick ping
except Exception:
    _mongo_ok = False
    fs = None

DATA_DIR = "data"
TEMP_DIR = "temp_pdfs"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# ============== Session State Initialization ==============
def init_session_state():
    st.session_state.setdefault("search_count", 0)
    st.session_state.setdefault("locked", False)
    st.session_state.setdefault("show_plans", False)
    st.session_state.setdefault("user_email", "")
    st.session_state.setdefault("subscription_status", None)
    st.session_state.setdefault("current_page", "home")
    st.session_state.setdefault("show_subscription_popup", False)

init_session_state()

# ============== Upload Local PDFs to MongoDB ==============
def upload_pdfs_to_mongo(folder_path: str) -> int:
    """Upload all PDFs in DATA_DIR to GridFS if not already present."""
    if not _mongo_ok:
        return 0
    uploaded = 0
    for filename in os.listdir(folder_path):
        if not filename.lower().endswith(".pdf"):
            continue
        try:
            if fs.find_one({"filename": filename}):
                continue
            file_path = os.path.join(folder_path, filename)
            with open(file_path, "rb") as f:
                fs.put(f.read(), filename=filename)
                uploaded += 1
        except Exception as e:
            st.warning(f" Failed to upload {filename}: {e}")
    return uploaded

if _mongo_ok:
    upload_pdfs_to_mongo(DATA_DIR)

# ============== Utility Functions ==============

def get_qp_files_by_session():
    """Return {session_name: [qp_filenames...]} with newest sessions first."""
    if not _mongo_ok:
        return {}
    session_dict = defaultdict(list)
    try:
        for file in fs.find():
            name = file.filename
            if "_qp_" in name:
                parts = name.split("_")
                if len(parts) >= 3:
                    # name format example: 9702_m24_qp_22.pdf
                    code, session, _ = parts[:3]
                    if len(session) >= 2:
                        year = "20" + session[1:3]
                        month_code = session[0].lower()
                        month_map = {"m": "March", "s": "May–June", "w": "Oct–Nov"}
                        month = month_map.get(month_code, session)
                        session_name = f"{year}-{month}"
                        session_dict[session_name].append(name)
    except Exception as e:
        st.error(f"Mongo read error: {e}")
        return {}
    # newest first (string sort is fine due to YYYY- prefix)
    return dict(sorted(session_dict.items(), key=lambda x: x[0], reverse=True))


def get_pdf_file(filename: str):
    if not _mongo_ok:
        return None
    try:
        file = fs.find_one({"filename": filename})
        if file:
            path = os.path.join(TEMP_DIR, filename)
            with open(path, "wb") as f:
                f.write(file.read())
            return path
    except Exception as e:
        st.error(f"Failed to fetch {filename}: {e}")
    return None


def extract_text(path: str) -> str:
    text = ""
    try:
        with fitz.open(path) as doc:
            for page in doc:
                text += page.get_text()
    except Exception as e:
        st.error(f"PDF text extraction failed ({os.path.basename(path)}): {e}")
    return text


def _extract_block(text: str, q: str) -> str:
    """
    Extracts the block starting from the line that begins with the question token
    until the next question token or end of text.
    Supports tokens like 3, 3(a), 3(b)(ii).
    """
    if not text.strip():
        return " Not found (empty text)."

    escaped_q = re.escape(q.strip())
    # Next token: start of another question number with optional chained parts
    # Examples: 3, 3(a), 3(a)(ii), 12(c)(iv)
    next_token = r"^\s*\d+(\([a-z]\))*(\([ivxl]+\))*\b"

    # Multiline + dotall so ^/$ apply line-wise
    pattern = rf"(?ms)^\s*{escaped_q}[^\n]*\n(?:.*?\n)*?(?={next_token}|\Z)"
    m = re.search(pattern, text)
    return m.group(0).strip() if m else " Not found."


def extract_specific_question(text: str, question_number: str) -> str:
    return _extract_block(text, question_number)


def extract_specific_answer(text: str, question_number: str) -> str:
    return _extract_block(text, question_number)

def extract_file_name(filename: str) -> str:
    if filename.endswith(("11.pdf", "12.pdf", "13.pdf")):
        return "MCQ"
    return "Question"

def ask_llm(qp_text: str, ms_text: str, question_number: str, name: str) -> str:
    question = extract_specific_question(qp_text, question_number)
    answer = extract_specific_answer(ms_text, question_number)
    fileName = extract_file_name(name)

    prompt = f"""You are a helpful A-Level Physics tutor.

You will be given a question and its official marking scheme answer.

Your job is to:
- ONLY explain the official answer in simple, clear, step-by-step form.
- DO NOT add anything not already in the answer.
- Use easy language that a student can understand.

--- Question ---
{question}

--- Official Answer ---
{answer}

-- FileName --
{fileName}

if the FileName is MCQ then read the Question and suggest why it is the correct answer.
if the FileName is Question ... then read and explain answer only (Now explain the answer step-by-step, without adding anything extra.)

If the answer is only a single letter, then it is a MCQ Question. In that case, you have to read the question and suggest why it is the correct answer.

Always format mathematical equations using LaTeX, and enclose them between double dollar signs ($$ ... $$) instead of square brackets.
All other math related stuff that requires special display also needs to be enclosed between double dollar signs ($$ ... $$)as well.
We have added you as a backend prompt handling bot. You just need to answer what st.markdown(result) can handle.
"""

    try:
        if _openai_mode == "new" and _openai_client:
            resp = _openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=800,
            )
            return (resp.choices[0].message.content or "").strip()
        elif _openai_mode == "old":
            import openai  # type: ignore
            resp = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=800,
            )
            return (resp.choices[0].message.content or "").strip()
        else:
            return " AI Error: OpenAI client not configured. Add your OPENAI_API_KEY to .streamlit/secrets.toml."
    except Exception as e:
        return f" AI Error: {e}"


def display_pdf_inline(path: str, zoom_percent: int = 100, height_px: int = 800) -> str:
    """Embed a PDF inline. Width is responsive; zoom affects CSS scale."""
    try:
        with open(path, "rb") as f:
            base64_pdf = base64.b64encode(f.read()).decode("utf-8")
        scale = max(50, min(200, int(zoom_percent))) / 100.0
        return f"""
        <div style="width:100%; border:1px solid #e9ecef; border-radius:8px; overflow:auto">
            <div style="transform: scale({scale}); transform-origin: top left; width: 100%;">
                <iframe src="data:application/pdf;base64,{base64_pdf}"
                        style="width:100%; height:{height_px}px; border:0;"></iframe>
            </div>
        </div>
        """
    except Exception:
        return "<div class='error-alert'> Unable to render PDF inline.</div>"


def file_info(path: str):
    try:
        with fitz.open(path) as doc:
            return len(doc), os.path.getsize(path)
    except Exception:
        return 0, 0


def check_user_subscription(email: str):
    """Check subscription status from your backend; returns a dict."""
    if not BACKEND_URL:
        return {"has_subscription": False}
    try:
        resp = requests.post(f"{BACKEND_URL}/check-subscription", json={"email": email}, timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {"has_subscription": False}


def redirect_to_stripe_checkout(plan: str, email: str):
    """Create Stripe checkout session and show link."""
    if not stripe or not STRIPE_SECRET_KEY:
        st.error("Stripe is not configured. Add STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY to secrets.")
        return

    price_ids = {
        'basic': BASIC_PRICE_ID,
        'plus': PLUS_PRICE_ID,
        'pro': PRO_PRICE_ID
    }

    if plan not in price_ids:
        st.error("Invalid plan selected.")
        return

    try:
        # Fallback host if query param not present
        current_url = (st.query_params.get("host_url") or ["http://localhost:8501"])[0]
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            customer_email=email,
            line_items=[{'price': price_ids[plan], 'quantity': 1}],
            mode='subscription',
            success_url=f"{current_url}?success=true&plan={plan}",
            cancel_url=f"{current_url}?canceled=true",
            metadata={'plan': plan, 'user_email': email}
        )
        st.markdown(f"[ Proceed to Checkout]({checkout_session.url})")
        st.info("Click the link above to open the secure Stripe checkout.")
    except Exception as e:
        st.error(f"Error creating checkout session: {e}")

# ============== CSS Styling ==============
def load_css():
    st.markdown(
        """
    <style>
    .main { padding: 1rem; }
    .app-header { background: linear-gradient(90deg, #667eea 0%, #764ba2 100%); padding: 2rem; border-radius: 10px; margin-bottom: 2rem; text-align: center; color: white; }
    .app-header h1 { font-size: 2.5rem; margin: 0; text-shadow: 2px 2px 4px rgba(0,0,0,0.3); }
    .app-header p { font-size: 1.2rem; margin: 0.5rem 0 0 0; opacity: 0.9; }
    .stats-container { display: flex; gap: 1rem; margin: 2rem 0; }
    .stat-card { background: white; padding: 1.5rem; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); text-align: center; flex: 1; border-left: 4px solid #667eea; }
    .stat-number { font-size: 2rem; font-weight: bold; color: #667eea; margin: 0; }
    .stat-label { color: #666; margin: 0.5rem 0 0 0; font-size: 0.9rem; }
    
    .content-card { background: white; padding: 2rem; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); margin-bottom: 2rem; }
    .plans-container { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 2rem; margin: 2rem 0; }
    .plan-card { background: white; border: 2px solid #e9ecef; border-radius: 15px; padding: 2rem; text-align: center; transition: all 0.3s; position: relative; overflow: hidden; }
    .plan-card:hover { transform: translateY(-5px); box-shadow: 0 10px 25px rgba(0,0,0,0.15); border-color: #667eea; }
    .plan-card.popular { border-color: #28a745; position: relative; }
    .plan-card.popular::before { content: "MOST POPULAR"; position: absolute; top: 15px; right: -35px; background: #28a745; color: white; padding: 5px 40px; font-size: 0.8rem; font-weight: bold; transform: rotate(45deg); }
    .plan-title { font-size: 1.5rem; font-weight: bold; color: #333; margin-bottom: 1rem; }
    .plan-price { font-size: 3rem; font-weight: bold; color: #667eea; margin: 1rem 0; }
    .plan-features { list-style: none; padding: 0; margin: 1.5rem 0; }
    .plan-features li { padding: 0.5rem 0; border-bottom: 1px solid #f0f0f0; }
    .plan-features li:last-child { border-bottom: none; }
    .primary-button { background: #667eea; color: white; border: none; padding: 1rem 2rem; border-radius: 8px; font-size: 1rem; font-weight: 600; cursor: pointer; transition: all 0.3s; width: 100%; }
    .progress-container { background: #f0f0f0; border-radius: 10px; padding: 3px; margin: 1rem 0; }
    .progress-bar { background: linear-gradient(90deg, #667eea, #764ba2); height: 20px; border-radius: 8px; transition: width 0.3s; }
    .success-alert { background: #d4edda; border: 1px solid #c3e6cb; color: #155724; padding: 1rem; border-radius: 8px; margin: 1rem 0; }
    .warning-alert { background: #fff3cd; border: 1px solid #ffeaa7; color: #856404; padding: 1rem; border-radius: 8px; margin: 1rem 0; }
    .error-alert { background: #f8d7da; border: 1px solid #f5c6cb; color: #721c24; padding: 1rem; border-radius: 8px; margin: 1rem 0; }

    /* Popup Modal Styles */
    .popup-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0, 0, 0, 0.7); display: flex; justify-content: center; align-items: center; z-index: 9999; }
    .popup-content { background: white; border-radius: 20px; padding: 2rem; max-width: 1200px; max-height: 90vh; overflow-y: auto; position: relative; box-shadow: 0 20px 40px rgba(0,0,0,0.3); animation: popupSlide 0.3s ease-out; }
    @keyframes popupSlide { from { transform: translateY(-50px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
    .popup-header { text-align: center; margin-bottom: 2rem; padding-bottom: 1rem; border-bottom: 2px solid #f0f0f0; }
    .popup-title { font-size: 2rem; font-weight: bold; color: #333; margin: 0 0 0.5rem 0; }
    .popup-subtitle { color: #666; font-size: 1.1rem; }
    .close-button { position: absolute; top: 15px; right: 20px; background: none; border: none; font-size: 2rem; cursor: pointer; color: #999; width: 40px; height: 40px; display: flex; align-items: center; justify-content: center; border-radius: 50%; transition: all 0.3s; }
    .close-button:hover { background: #f0f0f0; color: #333; }
    .popup-plans { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1.5rem; margin: 2rem 0; }
    .popup-plan-card { background: #f8f9fa; border: 2px solid #e9ecef; border-radius: 15px; padding: 1.5rem; text-align: center; transition: all 0.3s; position: relative; cursor: pointer; }
    .popup-plan-card:hover { transform: translateY(-3px); box-shadow: 0 8px 25px rgba(0,0,0,0.15); border-color: #667eea; }
    .popup-plan-card.popular { border-color: #28a745; background: #f8fff9; }
    .popup-plan-card.popular::before { content: "MOST POPULAR"; position: absolute; top: 10px; right: -25px; background: #28a745; color: white; padding: 3px 30px; font-size: 0.7rem; font-weight: bold; transform: rotate(45deg); }

    /* ✅ Make text black inside white cards/containers */
    .stat-card, .nav-container, .content-card, .plan-card, .popup-content, .popup-plan-card { color: #000; }

    @media (max-width: 768px) {
        .app-header h1 { font-size: 2rem; }
        .plans-container { grid-template-columns: 1fr; }
        .popup-plans { grid-template-columns: 1fr; }
        .stats-container { flex-direction: column; }
        .popup-content { margin: 1rem; padding: 1rem; }
    }
    </style>
    """,
        unsafe_allow_html=True,
    )

load_css()

# ============== Subscription Popup Component ==============
def render_subscription_popup():
    if not st.session_state.show_subscription_popup:
        return

    popup_html = """
    <div class="popup-overlay" id="subscriptionPopup">
        <div class="popup-content">
            <button class="close-button" onclick="document.getElementById('subscriptionPopup').style.display='none'">&times;</button>
            <div class="popup-header">
                <div class="popup-title">🚀 Upgrade to Continue Learning</div>
                <div class="popup-subtitle">You've used all your free searches. Choose a plan to unlock unlimited explanations!</div>
            </div>
            <div class="popup-plans">
                <div class="popup-plan-card" onclick="window.location.href='?choose=basic'">
                    <div class="plan-title">Basic Plan</div>
                    <div class="plan-price">$5<span style="font-size: 1rem; color: #666;">/month</span></div>
                    <ul class="plan-features">
                        <li>✅ 50 explanations/month</li>
                        <li>✅ All question papers</li>
                        <li>✅ PDF downloads</li>
                        <li>✅ Basic support</li>
                    </ul>
                </div>
                <div class="popup-plan-card popular" onclick="window.location.href='?choose=plus'">
                    <div class="plan-title">Plus Plan</div>
                    <div class="plan-price">$20<span style="font-size: 1rem; color: #666;">/month</span></div>
                    <ul class="plan-features">
                        <li>✅ 200 explanations/month</li>
                        <li>✅ All question papers</li>
                        <li>✅ PDF downloads</li>
                        <li>✅ Priority support</li>
                        <li>✅ Advanced analytics</li>
                    </ul>
                </div>
                <div class="popup-plan-card" onclick="window.location.href='?choose=pro'">
                    <div class="plan-title">Pro Plan</div>
                    <div class="plan-price">$100<span style="font-size: 1rem; color: #666;">/month</span></div>
                    <ul class="plan-features">
                        <li>✅ 1000 explanations/month</li>
                        <li>✅ All question papers</li>
                        <li>✅ PDF downloads</li>
                        <li>✅ Premium support</li>
                        <li>✅ Advanced analytics</li>
                        <li>✅ Custom uploads</li>
                    </ul>
                </div>
            </div>
        </div>
    </div>
    """

    st.markdown(popup_html, unsafe_allow_html=True)

# ============== Header Section ==============
def render_header():
    st.markdown(
        """
    <div class="app-header">
        <h1>📘 A-Level Physics Explainer</h1>
        <p>Get detailed explanations for any A-Level Physics question with AI-powered analysis</p>
    </div>
    """,
        unsafe_allow_html=True,
    )

# ============== Navigation ==============
def render_navigation():
    st.markdown('<div class="nav-container">', unsafe_allow_html=True)
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        if st.button("🏠 Home", key="nav_home"):
            st.session_state.current_page = "home"
    with col2:
        if st.button("📚 Question Explainer", key="nav_explainer"):
            st.session_state.current_page = "explainer"
    with col3:
        if st.button("💳 Subscription", key="nav_subscription"):
            st.session_state.current_page = "subscription"
    with col4:
        if st.button("📊 My Account", key="nav_account"):
            st.session_state.current_page = "account"
    st.markdown('</div>', unsafe_allow_html=True)

# ============== User Stats ==============
def render_user_stats():
    if not st.session_state.user_email:
        return
    subscription = check_user_subscription(st.session_state.user_email)
    if subscription.get("has_subscription"):
        searches_used = subscription.get("searches_used", 0)
        search_limit = subscription.get("search_limit", 10)
        plan = subscription.get("plan", "Free").title()
        progress_percentage = min(100, (searches_used / max(1, search_limit)) * 100)
        st.markdown(
            f"""
        <div class="stats-container">
            <div class="stat-card">
                <div class="stat-number">{searches_used}</div>
                <div class="stat-label">Searches Used</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{search_limit}</div>
                <div class="stat-label">Monthly Limit</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{plan}</div>
                <div class="stat-label">Current Plan</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{max(0, search_limit - searches_used)}</div>
                <div class="stat-label">Remaining</div>
            </div>
        </div>
        <div class="progress-container">
            <div class="progress-bar" style="width: {progress_percentage}%;"></div>
        </div>
        """,
            unsafe_allow_html=True,
        )

# ============== Home Page ==============
def render_home_page():
    render_header()
    st.markdown(
        """
    <div class="content-card">
        <h2>🎯 AI Help Pastpapers</h2>
        <p>This powerful tool helps you understand complex A-Level Physics questions by providing step-by-step explanations using AI technology.</p>
        <h3>✨ Features:</h3>
        <ul>
            <li> Access to extensive question paper database</li>
            <li> AI-powered detailed explanations</li>
            <li> Track your learning progress</li>
            <li> Download and view PDFs inline</li>
            <li> Search specific questions instantly</li>
        </ul>
        <h3> 💡 How to get started:</h3>
        <ol>
            <li>Navigate to the Question Explainer</li>
            <li>Select your exam session and paper</li>
            <li>Enter the question number you need help with</li>
            <li>Get instant AI-powered explanations!</li>
        </ol>
    </div>
    """,
        unsafe_allow_html=True,
    )


# ============== Question Explainer Page ==============
def render_explainer_page():
    st.markdown("##  Question Explainer")
    render_user_stats()

    sessions = get_qp_files_by_session()
    if not sessions:
        st.markdown('<div class="warning-alert"> No question papers found in the database or MongoDB not reachable.</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)
        return

    with st.sidebar:
        st.markdown("###  Select Files")
        selected_session = st.selectbox("Session", list(sessions.keys()))
        selected_qp = st.selectbox("Question Paper", sessions[selected_session])

    if selected_qp:
        selected_ms = selected_qp.replace("_qp_", "_ms_")
        qp_path = get_pdf_file(selected_qp)
        ms_path = get_pdf_file(selected_ms)

        if not qp_path or not ms_path:
            st.markdown('<div class="error-alert"> Could not load PDF files from database.</div>', unsafe_allow_html=True)
        else:
            col1, col2 = st.columns(2)
            with col1:
                st.markdown("###  Question Paper")
                pages, size = file_info(qp_path)
                st.caption(f" {pages} pages |  {round(size / 1024, 2)} KB")
                with open(qp_path, "rb") as f:
                    st.download_button(" Download QP", f, file_name=selected_qp)
                with st.expander(" View QP "):
                    st.markdown(display_pdf_inline(qp_path,), unsafe_allow_html=True)

            with col2:
                st.markdown("###  Marking Scheme")
                pages, size = file_info(ms_path)
                st.caption(f" {pages} pages |  {round(size / 1024, 2)} KB")
                with open(ms_path, "rb") as f:
                    st.download_button(" Download MS", f, file_name=selected_ms)
                with st.expander(" View MS "):
                    st.markdown(display_pdf_inline(ms_path, ), unsafe_allow_html=True)

            st.markdown("---")
            st.subheader(" Get AI Explanation")
            question_number = st.text_input("Enter Question Number (e.g., 3, 3(a), 3(b)(ii))", key="qnum")
            explain_btn = st.button(" Explain")

            if explain_btn and question_number.strip():
                # Check free limit logic
                has_sub = False
                if st.session_state.user_email:
                    sub = check_user_subscription(st.session_state.user_email)
                    has_sub = sub.get("has_subscription", False)

                if not has_sub:
                    st.session_state['search_count'] += 1
                    if st.session_state['search_count'] > 3:
                        st.session_state['locked'] = True
                        st.session_state['show_subscription_popup'] = True

                if st.session_state['locked'] and not has_sub:
                    st.warning("You've reached the free limit (3). Please upgrade to continue.")
                    render_subscription_popup()
                else:
                    with st.spinner("Thinking..."):
                        qp_text = extract_text(qp_path)
                        ms_text = extract_text(ms_path)
                        explanation = ask_llm(qp_text, ms_text, question_number, qp_path)
                    st.markdown("### ✅ Explanation")
                    st.markdown(explanation)

    st.markdown('</div>', unsafe_allow_html=True)

# ============== Subscription Page ==============
def render_subscription_page():
    # Add CSS for equal height cards
    st.markdown(
        """
        <style>
        .plan-card {
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            height: 100%; /* Ensures all cards have equal height */
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("## 💳 Subscription")

    st.info("Add your email, pick a plan, and proceed to Stripe checkout.")

    # Input for user email
    email = st.text_input("Email for subscription receipts", value=st.session_state.user_email)
    if email:
        st.session_state.user_email = email

    # Subscription plans layout
    col1, col2, col3 = st.columns(3)

    # Basic Plan
    with col1:
        st.markdown(
            """
            <div class="plan-card">
                <div class="plan-title">Basic</div>
                <div class="plan-price">$5<span>/mo</span></div>
                <ul class="plan-features">
                    <li>50 explanations/month</li>
                    <li>All question papers</li>
                    <li>PDF downloads</li>
                    <li>Basic support</li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("Choose Basic"):
            if not st.session_state.user_email:
                st.warning("Please enter your email above first.")
            else:
                redirect_to_stripe_checkout('basic', st.session_state.user_email)

    # Plus Plan
    with col2:
        st.markdown(
            """
            <div class="plan-card popular">
                <div class="plan-title">Plus</div>
                <div class="plan-price">$20<span>/mo</span></div>
                <ul class="plan-features">
                    <li>200 explanations/month</li>
                    <li>All question papers</li>
                    <li>PDF downloads</li>
                    <li>Priority support</li>
                    <li>Advanced analytics</li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("Choose Plus"):
            if not st.session_state.user_email:
                st.warning("Please enter your email above first.")
            else:
                redirect_to_stripe_checkout('plus', st.session_state.user_email)

    # Pro Plan
    with col3:
        st.markdown(
            """
            <div class="plan-card">
                <div class="plan-title">Pro</div>
                <div class="plan-price">$100<span>/mo</span></div>
                <ul class="plan-features">
                    <li>1000 explanations/month</li>
                    <li>All question papers</li>
                    <li>PDF downloads</li>
                    <li>Premium support</li>
                    <li>Advanced analytics</li>
                    <li>Custom uploads</li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("Choose Pro"):
            if not st.session_state.user_email:
                st.warning("Please enter your email above first.")
            else:
                redirect_to_stripe_checkout('pro', st.session_state.user_email)

    # Success / cancel feedback via query params (?success=true or ?canceled=true)
    if st.query_params.get("success") == ["true"]:
        st.success("🎉 Payment successful! Your subscription is now active.")
        st.session_state.show_subscription_popup = False
        st.balloons()
    if st.query_params.get("canceled") == ["true"]:
        st.warning("Payment was canceled. You can try again anytime.")

    st.markdown('</div>', unsafe_allow_html=True)

# ============== Account Page ==============
def render_account_page():
    st.markdown("## 📊 My Account")

    st.text_input("Email", key="user_email")
    if st.button("Check Subscription"):
        if not st.session_state.user_email:
            st.warning("Please enter your email first.")
        else:
            sub = check_user_subscription(st.session_state.user_email)
            st.session_state.subscription_status = sub
            if sub.get("has_subscription"):
                remaining = sub.get('search_limit', 0) - sub.get('searches_used', 0)
                st.success(f"✅ Active plan: {sub.get('plan', 'Unknown').title()} | Remaining: {remaining}")
            else:
                st.info("No active subscription found.")

    render_user_stats()
    st.markdown('</div>', unsafe_allow_html=True)

# ============== Router ==============
def render_navigation_bar_and_route():
    render_navigation()

    # Handle plan choice from popup via query param (?choose=basic)
    params = st.query_params
    choose = (params.get("choose") or [None])[0]
    if choose in {"basic", "plus", "pro"}:
        st.session_state.current_page = "subscription"

    page = st.session_state.get("current_page", "home")

    if page == "home":
        render_home_page()
    elif page == "explainer":
        render_explainer_page()
    elif page == "subscription":
        render_subscription_page()
    elif page == "account":
        render_account_page()
    else:
        render_home_page()

    # Render popup if needed
    render_subscription_popup()

def main():
    render_navigation_bar_and_route()

if __name__ == "__main__":
    main()

