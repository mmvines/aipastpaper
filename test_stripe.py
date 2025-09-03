# app.py
import os
import re
import base64
from collections import defaultdict
import json
from datetime import datetime

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
    page_title="A-Level Physics Explainer",
    page_icon="📘",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ============== READ SECRETS & ENV ==============
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))
BACKEND_URL = st.secrets.get("BACKEND_URL", os.getenv("BACKEND_URL", "http://localhost:5000"))

# Updated Stripe Configuration
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

# Price IDs from secrets
BASIC_PRICE_ID = (
    st.secrets.get("BASIC_PRICE_ID")
    or (st.secrets.get("stripe", {}).get("BASIC_PRICE_ID") if isinstance(st.secrets.get("stripe", {}), dict) else None)
    or os.getenv("BASIC_PRICE_ID", "")
)
PLUS_PRICE_ID = (
    st.secrets.get("PLUS_PRICE_ID")
    or (st.secrets.get("stripe", {}).get("PLUS_PRICE_ID") if isinstance(st.secrets.get("stripe", {}), dict) else None)
    or os.getenv("PLUS_PRICE_ID", "")
)
PRO_PRICE_ID = (
    st.secrets.get("PRO_PRICE_ID")
    or (st.secrets.get("stripe", {}).get("PRO_PRICE_ID") if isinstance(st.secrets.get("stripe", {}), dict) else None)
    or os.getenv("PRO_PRICE_ID", "")
)

STRIPE_WEBHOOK_SECRET = (
    st.secrets.get("STRIPE_WEBHOOK_SECRET")
    or (st.secrets.get("stripe", {}).get("WEBHOOK_SECRET") if isinstance(st.secrets.get("stripe", {}), dict) else None)
    or os.getenv("STRIPE_WEBHOOK_SECRET", "")
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
    papers_collection = db["papers_metadata"]
    ratings_collection = db["explanation_ratings"]
    admin_users = db["admin_users"]
    subscriptions_collection = db["subscriptions"]  # Added for Stripe integration
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
    st.session_state.setdefault("is_admin", False)
    st.session_state.setdefault("admin_password", "")
    st.session_state.setdefault("explanation_ratings", {})
    st.session_state.setdefault("loading_state", False)

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
                file_id = fs.put(f.read(), filename=filename)
                # Store metadata
                save_paper_metadata(filename, file_id)
                uploaded += 1
        except Exception as e:
            st.warning(f"⚠️ Failed to upload {filename}: {e}")
    return uploaded

if _mongo_ok:
    upload_pdfs_to_mongo(DATA_DIR)

# ============== Content Management Functions ==============

def save_paper_metadata(filename: str, file_id, exam_board=None, subject=None, year=None, session=None):
    """Save paper metadata for better organization"""
    if not _mongo_ok:
        return
    
    # Auto-extract metadata from filename
    if not exam_board or not subject or not year or not session:
        parts = filename.split("_")
        if len(parts) >= 3:
            exam_board = parts[0] if not exam_board else exam_board
            session_code = parts[1] if not session else session
            if len(session_code) >= 3:
                year = f"20{session_code[1:3]}" if not year else year
                month_map = {"m": "March", "s": "May-June", "w": "Oct-Nov"}
                session = month_map.get(session_code[0].lower(), session_code) if not session else session
            subject = "Physics" if not subject else subject
    
    metadata = {
        "filename": filename,
        "file_id": file_id,
        "exam_board": exam_board or "Unknown",
        "subject": subject or "Physics",
        "year": year or "Unknown",
        "session": session or "Unknown",
        "uploaded_date": datetime.now(),
        "version": 1,
        "moderated": True,  # Auto-approve for now
        "difficulty": "Medium",  # Default difficulty
        "topics": []  # Can be populated later
    }
    
    try:
        papers_collection.update_one(
            {"filename": filename},
            {"$set": metadata},
            upsert=True
        )
    except Exception as e:
        st.error(f"Failed to save metadata: {e}")

def get_paper_metadata(filename: str):
    """Get metadata for a specific paper"""
    if not _mongo_ok:
        return None
    try:
        return papers_collection.find_one({"filename": filename})
    except Exception:
        return None

def update_paper_version(filename: str, new_file_id):
    """Update paper version when a new version is uploaded"""
    if not _mongo_ok:
        return
    try:
        current = papers_collection.find_one({"filename": filename})
        if current:
            new_version = current.get("version", 1) + 1
            papers_collection.update_one(
                {"filename": filename},
                {"$set": {"file_id": new_file_id, "version": new_version, "updated_date": datetime.now()}}
            )
        else:
            save_paper_metadata(filename, new_file_id)
    except Exception as e:
        st.error(f"Failed to update version: {e}")

def save_explanation_rating(question_id: str, rating: int, feedback: str = ""):
    """Save user rating for explanations"""
    if not _mongo_ok:
        return
    try:
        rating_data = {
            "question_id": question_id,
            "rating": rating,
            "feedback": feedback,
            "timestamp": datetime.now(),
            "user_email": st.session_state.get("user_email", "anonymous")
        }
        ratings_collection.insert_one(rating_data)
    except Exception as e:
        st.error(f"Failed to save rating: {e}")

def get_average_rating(question_id: str):
    """Get average rating for a question"""
    if not _mongo_ok:
        return 0, 0
    try:
        pipeline = [
            {"$match": {"question_id": question_id}},
            {"$group": {"_id": None, "avg_rating": {"$avg": "$rating"}, "count": {"$sum": 1}}}
        ]
        result = list(ratings_collection.aggregate(pipeline))
        if result:
            return round(result[0]["avg_rating"], 1), result[0]["count"]
        return 0, 0
    except Exception:
        return 0, 0

def is_admin_user(email: str = None, password: str = None):
    """Check if user is admin"""
    if password == "admin123":  # Simple admin password for demo
        return True
    return False

# ============== Enhanced Stripe Functions ==============

def create_stripe_checkout_session(plan: str, email: str):
    """Create Stripe checkout session and return the URL."""
    if not stripe or not STRIPE_SECRET_KEY:
        st.error("Stripe is not configured. Please add your Stripe keys to secrets.")
        return None

    # Map plan to price ID
    price_map = {
        'basic': BASIC_PRICE_ID,
        'plus': PLUS_PRICE_ID, 
        'pro': PRO_PRICE_ID
    }
    
    price_id = price_map.get(plan.lower())
    if not price_id:
        st.error(f"Price ID not configured for plan: {plan}")
        return None

    # Get the current URL for success/cancel redirects
    try:
        # Try to get the current URL from Streamlit
        current_url = st.get_option("server.baseUrlPath") or "http://localhost:8501"
        if not current_url.startswith("http"):
            current_url = f"https://{current_url}"
    except:
        current_url = "http://localhost:8501"  # Fallback for development

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            customer_email=email,
            line_items=[{
                'price': price_id,
                'quantity': 1,
            }],
            mode='subscription',
            success_url=f"{current_url}?success=true&plan={plan}&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{current_url}?canceled=true",
            metadata={
                'plan': plan,
                'user_email': email,
            },
            allow_promotion_codes=True,
            billing_address_collection='auto',
        )
        return checkout_session.url
    except Exception as e:
        st.error(f"Error creating checkout session: {e}")
        return None

def redirect_to_stripe_checkout(plan: str, email: str):
    """Redirect to Stripe checkout"""
    checkout_url = create_stripe_checkout_session(plan, email)
    if checkout_url:
        st.markdown(f'<meta http-equiv="refresh" content="0; url={checkout_url}">', unsafe_allow_html=True)
        st.info("Redirecting to Stripe checkout...")

def handle_stripe_webhook(payload, signature):
    """Handle Stripe webhooks for subscription events."""
    if not STRIPE_SECRET_KEY or not STRIPE_WEBHOOK_SECRET:
        return False
    
    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return False
    except stripe.error.SignatureVerificationError:
        return False

    # Handle the event
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        # Update user subscription in your database
        customer_email = session.get('customer_email')
        plan = session.get('metadata', {}).get('plan', 'basic')
        
        # Add your database update logic here
        if _mongo_ok and customer_email:
            # Update user subscription status
            subscriptions_collection.update_one(
                {"email": customer_email},
                {
                    "$set": {
                        "plan": plan,
                        "status": "active",
                        "stripe_session_id": session['id'],
                        "stripe_customer_id": session.get('customer'),
                        "created_at": datetime.now(),
                        "searches_used": 0,
                        "last_reset": datetime.now()
                    }
                },
                upsert=True
            )
        
    elif event['type'] == 'invoice.payment_succeeded':
        # Handle successful subscription renewal
        invoice = event['data']['object']
        customer_id = invoice.get('customer')
        
        if _mongo_ok and customer_id:
            # Reset monthly usage
            subscriptions_collection.update_one(
                {"stripe_customer_id": customer_id},
                {
                    "$set": {
                        "searches_used": 0,
                        "last_reset": datetime.now()
                    }
                }
            )
    
    elif event['type'] == 'customer.subscription.deleted':
        # Handle subscription cancellation
        subscription = event['data']['object']
        customer_id = subscription.get('customer')
        
        if _mongo_ok and customer_id:
            subscriptions_collection.update_one(
                {"stripe_customer_id": customer_id},
                {
                    "$set": {
                        "status": "cancelled",
                        "cancelled_at": datetime.now()
                    }
                }
            )
    
    return True

def check_user_subscription(email: str):
    """Check subscription status from MongoDB and Stripe."""
    if not _mongo_ok:
        return {"has_subscription": False}
    
    try:
        # First check local database
        subscription = subscriptions_collection.find_one({"email": email})
        if not subscription:
            return {"has_subscription": False}
        
        # Check if subscription is active
        status = subscription.get('status', 'inactive')
        if status != 'active':
            return {"has_subscription": False}
        
        # Get plan limits
        plan_limits = {
            'basic': 50,
            'plus': 200, 
            'pro': 1000
        }
        
        plan = subscription.get('plan', 'basic')
        search_limit = plan_limits.get(plan, 50)
        searches_used = subscription.get('searches_used', 0)
        
        return {
            "has_subscription": True,
            "plan": plan,
            "search_limit": search_limit,
            "searches_used": searches_used,
            "status": status,
            "stripe_customer_id": subscription.get('stripe_customer_id')
        }
        
    except Exception as e:
        st.error(f"Error checking subscription: {e}")
        return {"has_subscription": False}

def update_user_search_count(email: str):
    """Increment search count for user"""
    if not _mongo_ok or not email:
        return
    
    try:
        subscriptions_collection.update_one(
            {"email": email},
            {
                "$inc": {"searches_used": 1},
                "$set": {"last_used": datetime.now()}
            }
        )
    except Exception as e:
        st.error(f"Failed to update search count: {e}")

# ============== Enhanced Utility Functions ==============

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

def get_papers_by_exam_board():
    """Get papers organized by exam board"""
    if not _mongo_ok:
        return {}
    try:
        exam_boards = papers_collection.distinct("exam_board")
        result = {}
        for board in exam_boards:
            papers = list(papers_collection.find({"exam_board": board}))
            result[board] = papers
        return result
    except Exception:
        return {}

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
        return "❌ Not found (empty text)."

    escaped_q = re.escape(q.strip())
    # Next token: start of another question number with optional chained parts
    # Examples: 3, 3(a), 3(a)(ii), 12(c)(iv)
    next_token = r"^\s*\d+(\([a-z]\))*(\([ivxl]+\))*\b"

    # Multiline + dotall so ^/$ apply line-wise
    pattern = rf"(?ms)^\s*{escaped_q}[^\n]*\n(?:.*?\n)*?(?={next_token}|\Z)"
    m = re.search(pattern, text)
    return m.group(0).strip() if m else "❌ Not found."

def extract_specific_question(text: str, question_number: str) -> str:
    return _extract_block(text, question_number)

def extract_specific_answer(text: str, question_number: str) -> str:
    return _extract_block(text, question_number)

def ask_llm_enhanced(qp_text: str, ms_text: str, question_number: str) -> str:
    """Enhanced AI explanation with step-by-step breakdown"""
    question = extract_specific_question(qp_text, question_number)
    answer = extract_specific_answer(ms_text, question_number)

    prompt = f"""You are a helpful A-Level Physics tutor.

You will be given a question and its official marking scheme answer.

Your job is to:
- ONLY explain the official answer in simple, clear, step-by-step form.
- Break down the solution into numbered steps
- Explain the physics concepts involved
- Use easy language that a student can understand.
- DO NOT add anything not already in the answer.

Format your response as:
**Understanding the Question:**
[Brief explanation of what the question is asking]

**Step-by-Step Solution:**
1. [First step with explanation]
2. [Second step with explanation]
3. [Continue...]

**Key Physics Concepts:**
- [Concept 1]
- [Concept 2]

**Final Answer:**
[Clear final answer]

--- Question ---
{question}

--- Official Answer ---
{answer}

Now explain the answer step-by-step following the format above, without adding anything extra.
"""

    try:
        if _openai_mode == "new" and _openai_client:
            resp = _openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=1200,
            )
            return (resp.choices[0].message.content or "").strip()
        elif _openai_mode == "old":
            import openai  # type: ignore
            resp = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=1200,
            )
            return (resp.choices[0].message.content or "").strip()
        else:
            return "❌ AI Error: OpenAI client not configured. Add your OPENAI_API_KEY to .streamlit/secrets.toml."
    except Exception as e:
        return f"❌ AI Error: {e}"

def ask_llm(qp_text: str, ms_text: str, question_number: str) -> str:
    """Original function for backward compatibility"""
    return ask_llm_enhanced(qp_text, ms_text, question_number)

def get_related_questions(current_question: str, current_paper: str):
    """Get related questions from the same topic"""
    # Simple implementation - can be enhanced with ML
    related = []
    try:
        # Extract question number pattern
        q_num = re.findall(r'\d+', current_question)
        if q_num:
            base_num = int(q_num[0])
            # Suggest adjacent questions
            for i in range(max(1, base_num-2), min(base_num+3, 20)):
                if i != base_num:
                    related.append(str(i))
    except:
        pass
    return related[:3]  # Return max 3 suggestions

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
        return "<div class='error-alert'>❌ Unable to render PDF inline.</div>"

def file_info(path: str):
    try:
        with fitz.open(path) as doc:
            return len(doc), os.path.getsize(path)
    except Exception:
        return 0, 0

# ============== Enhanced CSS Styling ==============
def load_css():
    st.markdown(
        """
    <style>
    /* Enhanced animations and transitions */
    @keyframes fadeIn {
        from { opacity: 0; transform: translateY(20px); }
        to { opacity: 1; transform: translateY(0); }
    }
    
    @keyframes slideIn {
        from { transform: translateX(-100%); }
        to { transform: translateX(0); }
    }
    
    @keyframes pulse {
        0%, 100% { transform: scale(1); }
        50% { transform: scale(1.05); }
    }
    
    @keyframes shimmer {
        0% { background-position: -200% 0; }
        100% { background-position: 200% 0; }
    }
    
    /* Loading skeleton */
    .loading-skeleton {
        background: linear-gradient(90deg, #f0f0f0 25%, #e0e0e0 50%, #f0f0f0 75%);
        background-size: 200% 100%;
        animation: shimmer 2s infinite;
        border-radius: 8px;
        height: 20px;
        margin: 10px 0;
    }
    
    .main { 
        padding: 1rem; 
        animation: fadeIn 0.8s ease-out;
    }
    
    .app-header { 
        background: linear-gradient(135deg, #667eea 0%, #764ba2 50%, #667eea 100%); 
        padding: 2rem; 
        border-radius: 15px; 
        margin-bottom: 2rem; 
        text-align: center; 
        color: white; 
        animation: fadeIn 1s ease-out;
        box-shadow: 0 10px 30px rgba(102, 126, 234, 0.3);
        background-size: 200% 200%;
        animation: gradient-shift 3s ease infinite;
    }
    
    @keyframes gradient-shift {
        0% { background-position: 0% 50%; }
        50% { background-position: 100% 50%; }
        100% { background-position: 0% 50%; }
    }
    
    .app-header h1 { 
        font-size: 2.5rem; 
        margin: 0; 
        text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        animation: pulse 2s infinite;
    }
    
    .app-header p { 
        font-size: 1.2rem; 
        margin: 0.5rem 0 0 0; 
        opacity: 0.9; 
    }
    
    .stats-container { 
        display: flex; 
        gap: 1rem; 
        margin: 2rem 0;
        animation: slideIn 0.8s ease-out;
    }
    
    .stat-card { 
        background: white; 
        padding: 1.5rem; 
        border-radius: 15px; 
        box-shadow: 0 5px 20px rgba(0,0,0,0.1); 
        text-align: center; 
        flex: 1; 
        border-left: 4px solid #667eea;
        transition: all 0.3s ease;
        cursor: pointer;
    }
    
    .stat-card:hover {
        transform: translateY(-5px);
        box-shadow: 0 10px 30px rgba(102, 126, 234, 0.2);
        border-left-width: 8px;
    }
    
    .stat-number { 
        font-size: 2rem; 
        font-weight: bold; 
        color: #667eea; 
        margin: 0;
        transition: color 0.3s ease;
    }
    
    .stat-card:hover .stat-number {
        color: #764ba2;
    }
    
    .stat-label { 
        color: #666; 
        margin: 0.5rem 0 0 0; 
        font-size: 0.9rem; 
    }
    
    .nav-container { 
        background: rgba(255, 255, 255, 0.95); 
        backdrop-filter: blur(10px);
        padding: 1rem; 
        border-radius: 15px; 
        margin-bottom: 2rem;
        box-shadow: 0 5px 20px rgba(0,0,0,0.1);
        border: 1px solid rgba(255, 255, 255, 0.2);
    }
    
    .content-card { 
        background: rgba(255, 255, 255, 0.95);
        backdrop-filter: blur(10px);
        padding: 2rem; 
        border-radius: 15px; 
        margin-bottom: 2rem;
        box-shadow: 0 5px 20px rgba(0,0,0,0.1);
        animation: fadeIn 0.6s ease-out;
        border: 1px solid rgba(255, 255, 255, 0.2);
    }
    
    .plans-container { 
        display: grid; 
        grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); 
        gap: 2rem; 
        margin: 2rem 0; 
    }
    
    .plan-card { 
        background: white; 
        border: 2px solid #e9ecef; 
        border-radius: 20px; 
        padding: 2rem; 
        text-align: center; 
        transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275); 
        position: relative; 
        overflow: hidden;
        cursor: pointer;
    }
    
    .plan-card::before {
        content: '';
        position: absolute;
        top: 0;
        left: -100%;
        width: 100%;
        height: 100%;
        background: linear-gradient(90deg, transparent, rgba(102, 126, 234, 0.1), transparent);
        transition: left 0.5s;
    }
    
    .plan-card:hover::before {
        left: 100%;
    }
    
    .plan-card:hover { 
        transform: translateY(-10px) scale(1.02); 
        box-shadow: 0 20px 40px rgba(0,0,0,0.15); 
        border-color: #667eea; 
    }
    
    .plan-card.popular { 
        border-color: #28a745; 
        position: relative;
        background: linear-gradient(135deg, #ffffff 0%, #f8fff9 100%);
    }
    
    .plan-card.popular::after { 
        content: "MOST POPULAR"; 
        position: absolute; 
        top: 15px; 
        right: -35px; 
        background: linear-gradient(45deg, #28a745, #20c997); 
        color: white; 
        padding: 8px 40px; 
        font-size: 0.8rem; 
        font-weight: bold; 
        transform: rotate(45deg);
        box-shadow: 0 2px 10px rgba(40, 167, 69, 0.3);
    }
    
    .plan-title { 
        font-size: 1.5rem; 
        font-weight: bold; 
        color: #333; 
        margin-bottom: 1rem; 
    }
    
    .plan-price { 
        font-size: 3rem; 
        font-weight: bold; 
        color: #667eea; 
        margin: 1rem 0; 
        text-shadow: 2px 2px 4px rgba(102, 126, 234, 0.1);
    }
    
    .plan-features { 
        list-style: none; 
        padding: 0; 
        margin: 1.5rem 0; 
    }
    
    .plan-features li { 
        padding: 0.8rem 0; 
        border-bottom: 1px solid rgba(0,0,0,0.1); 
        transition: all 0.3s ease;
        position: relative;
    }
    
    .plan-features li:hover {
        background: rgba(102, 126, 234, 0.05);
        padding-left: 10px;
    }
    
    .plan-features li:last-child { 
        border-bottom: none; 
    }
    
    .primary-button, .checkout-button { 
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
        color: white; 
        border: none; 
        padding: 1rem 2rem; 
        border-radius: 12px; 
        font-size: 1rem; 
        font-weight: 600; 
        cursor: pointer; 
        transition: all 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275); 
        width: 100%;
        text-transform: uppercase;
        letter-spacing: 1px;
        position: relative;
        overflow: hidden;
    }
    
    .primary-button:hover, .checkout-button:hover { 
        transform: translateY(-3px); 
        box-shadow: 0 10px 25px rgba(102, 126, 234, 0.4);
        background: linear-gradient(135deg, #764ba2 0%, #667eea 100%);
    }
    
    .primary-button::before, .checkout-button::before {
        content: '';
        position: absolute;
        top: 0;
        left: -100%;
        width: 100%;
        height: 100%;
        background: linear-gradient(90deg, transparent, rgba(255,255,255,0.2), transparent);
        transition: left 0.5s;
    }
    
    .primary-button:hover::before, .checkout-button:hover::before {
        left: 100%;
    }
    
    .progress-container { 
        background: #f0f0f0; 
        border-radius: 15px; 
        padding: 4px; 
        margin: 1rem 0;
        box-shadow: inset 0 2px 4px rgba(0,0,0,0.1);
    }
    
    .progress-bar { 
        background: linear-gradient(90deg, #667eea, #764ba2); 
        height: 25px; 
        border-radius: 12px; 
        transition: width 1s cubic-bezier(0.25, 0.46, 0.45, 0.94);
        position: relative;
        overflow: hidden;
    }
    
    .progress-bar::after {
        content: '';
        position: absolute;
        top: 0;
        left: 0;
        bottom: 0;
        right: 0;
        background-image: linear-gradient(45deg, rgba(255,255,255,.2) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.2) 50%, rgba(255,255,255,.2) 75%, transparent 75%, transparent);
        background-size: 30px 30px;
        animation: move 2s linear infinite;
    }
    
    @keyframes move {
        0% { background-position: 0 0; }
        100% { background-position: 30px 30px; }
    }
    
    .success-alert { 
        background: linear-gradient(135deg, #d4edda 0%, #c3e6cb 100%); 
        border: 1px solid #28a745; 
        color: #155724; 
        padding: 1.5rem; 
        border-radius: 12px; 
        margin: 1rem 0;
        animation: fadeIn 0.5s ease-out;
        box-shadow: 0 5px 15px rgba(40, 167, 69, 0.2);
    }
    
    .warning-alert { 
        background: linear-gradient(135deg, #fff3cd 0%, #ffeaa7 100%); 
        border: 1px solid #ffc107; 
        color: #856404; 
        padding: 1.5rem; 
        border-radius: 12px; 
        margin: 1rem 0;
        animation: fadeIn 0.5s ease-out;
        box-shadow: 0 5px 15px rgba(255, 193, 7, 0.2);
    }
    
    .error-alert { 
        background: linear-gradient(135deg, #f8d7da 0%, #f5c6cb 100%); 
        border: 1px solid #dc3545; 
        color: #721c24; 
        padding: 1.5rem; 
        border-radius: 12px; 
        margin: 1rem 0;
        animation: fadeIn 0.5s ease-out;
        box-shadow: 0 5px 15px rgba(220, 53, 69, 0.2);
    }

    /* Enhanced popup styles */
    .popup-overlay { 
        position: fixed; 
        top: 0; 
        left: 0; 
        width: 100%; 
        height: 100%; 
        background: rgba(0, 0, 0, 0.8); 
        backdrop-filter: blur(5px);
        display: flex; 
        justify-content: center; 
        align-items: center; 
        z-index: 9999;
        animation: fadeIn 0.3s ease-out;
    }
    
    .popup-content { 
        background: white; 
        border-radius: 25px; 
        padding: 2rem; 
        max-width: 1200px; 
        max-height: 90vh; 
        overflow-y: auto; 
        position: relative; 
        box-shadow: 0 25px 50px rgba(0,0,0,0.3); 
        animation: popupSlide 0.5s cubic-bezier(0.175, 0.885, 0.32, 1.275);
        border: 1px solid rgba(255, 255, 255, 0.2);
    }
    
    @keyframes popupSlide { 
        from { transform: translateY(-100px) scale(0.8); opacity: 0; } 
        to { transform: translateY(0) scale(1); opacity: 1; } 
    }
    
    .popup-header { 
        text-align: center; 
        margin-bottom: 2rem; 
        padding-bottom: 1rem; 
        border-bottom: 2px solid #f0f0f0; 
    }
    
    .popup-title { 
        font-size: 2rem; 
        font-weight: bold; 
        color: #333; 
        margin: 0 0 0.5rem 0;
        background: linear-gradient(135deg, #667eea, #764ba2);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
    }
    
    .popup-subtitle { 
        color: #666; 
        font-size: 1.1rem; 
    }
    
    .close-button { 
        position: absolute; 
        top: 15px; 
        right: 20px; 
        background: rgba(255, 255, 255, 0.9); 
        border: none; 
        font-size: 1.5rem; 
        cursor: pointer; 
        color: #666; 
        width: 40px; 
        height: 40px; 
        display: flex; 
        align-items: center; 
        justify-content: center; 
        border-radius: 50%; 
        transition: all 0.3s ease;
        backdrop-filter: blur(10px);
    }
    
    .close-button:hover { 
        background: #ff4757; 
        color: white; 
        transform: rotate(90deg);
    }
    
    /* Admin Panel Styles */
    .admin-panel {
        background: linear-gradient(135deg, #ff6b6b 0%, #ee5a24 100%);
        color: white;
        padding: 1.5rem;
        border-radius: 15px;
        margin: 1rem 0;
        box-shadow: 0 10px 25px rgba(255, 107, 107, 0.3);
    }
    
    .admin-card {
        background: white;
        color: #333;
        padding: 2rem;
        border-radius: 15px;
        margin: 1rem 0;
        box-shadow: 0 5px 20px rgba(0,0,0,0.1);
        border-left: 5px solid #ff6b6b;
        transition: all 0.3s ease;
    }
    
    .admin-card:hover {
        transform: translateX(5px);
        box-shadow: 0 10px 30px rgba(255, 107, 107, 0.2);
    }
    
    /* Rating System */
    .rating-container {
        background: #f8f9fa;
        padding: 1.5rem;
        border-radius: 12px;
        margin: 1rem 0;
        border: 1px solid #e9ecef;
        text-align: center;
    }
    
    .rating-stars {
        font-size: 2rem;
        margin: 1rem 0;
    }
    
    .rating-star {
        color: #ddd;
        cursor: pointer;
        transition: all 0.3s ease;
        margin: 0 5px;
    }
    
    .rating-star:hover,
    .rating-star.active {
        color: #ffd700;
        transform: scale(1.2);
        text-shadow: 0 0 10px rgba(255, 215, 0, 0.5);
    }
    
    /* Difficulty indicators */
    .difficulty-badge {
        display: inline-block;
        padding: 0.4rem 0.8rem;
        border-radius: 20px;
        font-size: 0.8rem;
        font-weight: bold;
        text-transform: uppercase;
        margin: 0.2rem;
        animation: fadeIn 0.5s ease-out;
    }
    
    .difficulty-easy {
        background: linear-gradient(135deg, #2ecc71, #27ae60);
        color: white;
    }
    
    .difficulty-medium {
        background: linear-gradient(135deg, #f39c12, #e67e22);
        color: white;
    }
    
    .difficulty-hard {
        background: linear-gradient(135deg, #e74c3c, #c0392b);
        color: white;
    }
    
    /* Enhanced mobile responsiveness */
    @media (max-width: 768px) {
        .app-header h1 { font-size: 2rem; }
        .app-header { padding: 1.5rem; }
        .plans-container { grid-template-columns: 1fr; }
        .popup-plans { grid-template-columns: 1fr; gap: 1rem; }
        .stats-container { flex-direction: column; }
        .popup-content { 
            margin: 1rem; 
            padding: 1.5rem; 
            border-radius: 15px;
        }
        .popup-plan-card { padding: 1rem; }
        .content-card { padding: 1.5rem; }
        .nav-container { padding: 0.5rem; }
        .stat-card { padding: 1rem; }
        .plan-card { padding: 1.5rem; }
    }
    
    @media (max-width: 480px) {
        .app-header h1 { font-size: 1.5rem; }
        .popup-content { margin: 0.5rem; padding: 1rem; }
        .plan-price { font-size: 2rem; }
        .stats-container { gap: 0.5rem; }
    }
    
    /* Loading states */
    .loading-spinner {
        display: inline-block;
        width: 20px;
        height: 20px;
        border: 3px solid rgba(102, 126, 234, 0.3);
        border-radius: 50%;
        border-top-color: #667eea;
        animation: spin 1s ease-in-out infinite;
        margin-right: 10px;
    }
    
    @keyframes spin {
        to { transform: rotate(360deg); }
    }
    
    /* Enhanced accessibility */
    .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip: rect(0, 0, 0, 0);
        white-space: nowrap;
        border: 0;
    }
    
    /* Focus indicators for accessibility */
    button:focus, input:focus, select:focus {
        outline: 3px solid #667eea;
        outline-offset: 2px;
    }
    
    /* High contrast mode support */
    @media (prefers-contrast: high) {
        .app-header { background: #000; color: #fff; }
        .plan-card { border-width: 3px; }
        .stat-card { border-left-width: 6px; }
    }
    
    /* Reduced motion support */
    @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after {
            animation-duration: 0.01ms !important;
            animation-iteration-count: 1 !important;
            transition-duration: 0.01ms !important;
        }
    }
    
    /* Color scheme for dark mode preference */
    @media (prefers-color-scheme: dark) {
        .content-card, .nav-container {
            background: rgba(30, 30, 30, 0.95);
            color: #fff;
        }
        .stat-card, .plan-card {
            background: #2d3748;
            color: #fff;
        }
    }

    /* Make text black inside white cards/containers */
    .stat-card, .plan-card, .popup-content, .popup-plan-card, .stripe-pricing-container, .custom-plan-card { color: #000; }

    /* Enhanced explanation container */
    .explanation-container {
        background: white;
        border-radius: 15px;
        padding: 2rem;
        margin: 1rem 0;
        box-shadow: 0 5px 20px rgba(0,0,0,0.1);
        border-left: 5px solid #667eea;
        animation: fadeIn 0.8s ease-out;
    }
    
    .step-indicator {
        display: inline-block;
        background: linear-gradient(135deg, #667eea, #764ba2);
        color: white;
        padding: 0.5rem 1rem;
        border-radius: 25px;
        margin: 0.5rem 0;
        font-weight: bold;
        box-shadow: 0 3px 10px rgba(102, 126, 234, 0.3);
    }
    
    .concept-tag {
        display: inline-block;
        background: #f8f9fa;
        color: #495057;
        padding: 0.3rem 0.8rem;
        border-radius: 15px;
        margin: 0.2rem;
        font-size: 0.9rem;
        border: 1px solid #dee2e6;
        transition: all 0.3s ease;
    }
    
    .concept-tag:hover {
        background: #667eea;
        color: white;
        transform: translateY(-2px);
    }
    
    /* Related questions */
    .related-questions {
        background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
        padding: 1.5rem;
        border-radius: 12px;
        margin: 1rem 0;
        border: 1px solid #dee2e6;
    }
    
    .related-question-btn {
        background: white;
        color: #667eea;
        border: 2px solid #667eea;
        padding: 0.8rem 1.5rem;
        border-radius: 25px;
        margin: 0.5rem;
        cursor: pointer;
        transition: all 0.3s ease;
        font-weight: 600;
    }
    
    .related-question-btn:hover {
        background: #667eea;
        color: white;
        transform: translateY(-2px);
        box-shadow: 0 5px 15px rgba(102, 126, 234, 0.3);
    }
    
    /* Popup plans specific styling */
    .popup-plans {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
        gap: 1.5rem;
        margin: 2rem 0;
    }
    
    .popup-plan-card {
        background: white;
        border: 2px solid #e9ecef;
        border-radius: 15px;
        padding: 1.5rem;
        text-align: center;
        transition: all 0.3s ease;
        cursor: pointer;
    }
    
    .popup-plan-card:hover {
        transform: translateY(-5px);
        box-shadow: 0 15px 35px rgba(0,0,0,0.1);
        border-color: #667eea;
    }
    
    .popup-plan-card.popular {
        border-color: #28a745;
        background: linear-gradient(135deg, #ffffff 0%, #f8fff9 100%);
    }
    </style>
    """,
        unsafe_allow_html=True,
    )

load_css()

# ============== Admin Panel Functions ==============
def render_admin_login():
    """Admin login interface"""
    st.markdown('<div class="admin-panel">', unsafe_allow_html=True)
    st.markdown("### Admin Panel Access")
    password = st.text_input("Admin Password", type="password", key="admin_pass_input")
    if st.button("Login as Admin"):
        if is_admin_user(password=password):
            st.session_state.is_admin = True
            st.session_state.admin_password = password
            st.success("Admin access granted!")
            st.rerun()
        else:
            st.error("Invalid admin password")
    st.markdown('</div>', unsafe_allow_html=True)

def render_admin_panel():
    """Complete admin panel for content management"""
    if not st.session_state.is_admin:
        render_admin_login()
        return
    
    st.markdown('<div class="admin-panel">', unsafe_allow_html=True)
    st.markdown("### Admin Panel")
    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown("**Content Management Dashboard**")
    with col2:
        if st.button("Logout", key="admin_logout"):
            st.session_state.is_admin = False
            st.session_state.admin_password = ""
            st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)
    
    # Admin tabs
    tab1, tab2, tab3, tab4 = st.tabs(["Upload Papers", "Manage Content", "Analytics", "Settings"])
    
    with tab1:
        render_upload_interface()
    
    with tab2:
        render_content_management()
    
    with tab3:
        render_admin_analytics()
    
    with tab4:
        render_admin_settings()
def render_admin_settings():
    """Render the admin settings interface"""
    st.markdown('<div class="admin-card">', unsafe_allow_html=True)
    st.markdown("#### Admin Settings")
    
    # Example settings fields
    st.text_input("Platform Name", value="A-Level Physics Explainer", key="platform_name")
    st.text_input("Support Email", value="support@example.com", key="support_email")
    st.selectbox("Default Difficulty Level", ["Easy", "Medium", "Hard"], key="default_difficulty")
    
    if st.button("Save Settings", key="save_settings"):
        st.success("Settings saved successfully!")
    
    st.markdown('</div>', unsafe_allow_html=True)
def render_upload_interface():
    """Enhanced upload interface for admins"""
    st.markdown('<div class="admin-card">', unsafe_allow_html=True)
    st.markdown("#### Upload New Question Papers")
    
    uploaded_files = st.file_uploader(
        "Choose PDF files", 
        type="pdf", 
        accept_multiple_files=True,
        help="Upload question papers and marking schemes"
    )
    
    if uploaded_files:
        col1, col2 = st.columns(2)
        with col1:
            exam_board = st.selectbox("Exam Board", ["9702", "9701", "H2", "Custom"], key="upload_board")
            subject = st.selectbox("Subject", ["Physics", "Chemistry", "Math", "Biology"], key="upload_subject")
        
        with col2:
            year = st.selectbox("Year", [str(y) for y in range(2015, 2030)], key="upload_year")
            session = st.selectbox("Session", ["March", "May-June", "Oct-Nov"], key="upload_session")
        
        difficulty = st.selectbox("Difficulty Level", ["Easy", "Medium", "Hard"], key="upload_difficulty")
        topics = st.multiselect("Topics", [
            "Mechanics", "Waves", "Electricity", "Magnetism", "Nuclear Physics", 
            "Thermodynamics", "Quantum Physics", "Relativity", "Particle Physics"
        ], key="upload_topics")
        
        if st.button("Upload Papers", key="upload_btn"):
            with st.spinner("Uploading papers..."):
                success_count = 0
                for uploaded_file in uploaded_files:
                    try:
                        # Check if file already exists
                        if fs.find_one({"filename": uploaded_file.name}):
                            # Update version
                            file_id = fs.put(uploaded_file.read(), filename=uploaded_file.name)
                            update_paper_version(uploaded_file.name, file_id)
                        else:
                            # New upload
                            file_id = fs.put(uploaded_file.read(), filename=uploaded_file.name)
                            save_paper_metadata(
                                uploaded_file.name, file_id, exam_board, 
                                subject, year, session
                            )
                            # Update additional metadata
                            papers_collection.update_one(
                                {"filename": uploaded_file.name},
                                {"$set": {
                                    "difficulty": difficulty,
                                    "topics": topics,
                                    "moderated": True
                                }}
                            )
                        success_count += 1
                    except Exception as e:
                        st.error(f"Failed to upload {uploaded_file.name}: {e}")
                
                if success_count > 0:
                    st.success(f"Successfully uploaded {success_count} papers!")
                    st.balloons()
    
    st.markdown('</div>', unsafe_allow_html=True)

def render_content_management():
    """Content management interface"""
    st.markdown('<div class="admin-card">', unsafe_allow_html=True)
    st.markdown("#### Manage Existing Papers")
    
    if _mongo_ok:
        papers = list(papers_collection.find().sort("uploaded_date", -1).limit(50))
        
        if papers:
            for paper in papers:
                with st.expander(f"{paper['filename']} (v{paper.get('version', 1)})"):
                    col1, col2, col3 = st.columns(3)
                    
                    with col1:
                        st.write(f"**Exam Board:** {paper.get('exam_board', 'Unknown')}")
                        st.write(f"**Subject:** {paper.get('subject', 'Unknown')}")
                        st.write(f"**Year:** {paper.get('year', 'Unknown')}")
                    
                    with col2:
                        st.write(f"**Session:** {paper.get('session', 'Unknown')}")
                        st.write(f"**Difficulty:** {paper.get('difficulty', 'Medium')}")
                        st.write(f"**Moderated:** {'✅' if paper.get('moderated') else '❌'}")
                    
                    with col3:
                        st.write(f"**Topics:** {', '.join(paper.get('topics', []))}")
                        st.write(f"**Uploaded:** {paper.get('uploaded_date', '').strftime('%Y-%m-%d') if paper.get('uploaded_date') else 'Unknown'}")
                    
                    # Quick actions
                    action_col1, action_col2, action_col3 = st.columns(3)
                    with action_col1:
                        if st.button(f"Delete", key=f"delete_{paper['filename']}"):
                            try:
                                fs.delete(paper['file_id'])
                                papers_collection.delete_one({"filename": paper['filename']})
                                st.success("Paper deleted!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Delete failed: {e}")
                    
                    with action_col2:
                        new_moderation = st.checkbox(
                            "Moderated", 
                            value=paper.get('moderated', True),
                            key=f"mod_{paper['filename']}"
                        )
                        if new_moderation != paper.get('moderated'):
                            papers_collection.update_one(
                                {"filename": paper['filename']},
                                {"$set": {"moderated": new_moderation}}
                            )
                    
                    with action_col3:
                        if st.button(f"View Stats", key=f"stats_{paper['filename']}"):
                            st.info("Stats feature coming soon!")
        else:
            st.info("No papers found in database.")
    
    st.markdown('</div>', unsafe_allow_html=True)

def render_admin_analytics():
    """Admin analytics dashboard"""
    st.markdown('<div class="admin-card">', unsafe_allow_html=True)
    st.markdown("#### Platform Analytics")
    
    if _mongo_ok:
        # Get basic stats
        total_papers = papers_collection.count_documents({})
        total_ratings = ratings_collection.count_documents({})
        total_subscriptions = subscriptions_collection.count_documents({"status": "active"})
        
        # Display key metrics
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Papers", total_papers)
        with col2:
            st.metric("Total Ratings", total_ratings)
        with col3:
            st.metric("Active Subscriptions", total_subscriptions)
        with col4:
            avg_rating = ratings_collection.aggregate([
                {"$group": {"_id": None, "avg": {"$avg": "$rating"}}}
            ])
            avg_val = list(avg_rating)
            avg_score = round(avg_val[0]["avg"], 2) if avg_val else 0
            st.metric("Avg Rating", f"{avg_score}")
        
        # Papers by exam board
        st.markdown("##### Papers by Exam Board")
        board_stats = papers_collection.aggregate([
            {"$group": {"_id": "$exam_board", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ])
        for stat in board_stats:
            st.write(f"**{stat['_id']}:** {stat['count']} papers")
        
        # Subscription stats
        st.markdown("##### Subscription Stats")
        sub_stats = subscriptions_collection.aggregate([
            {"$group": {"_id": "$plan", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ])
        for stat in sub_stats:
            st.write(f"**{stat['_id'].title()} Plan:** {stat['count']} subscribers")
        
        # Recent activity
        st.markdown("##### Recent Activity")
        recent_papers = papers_collection.find().sort("uploaded_date", -1).limit(5)
        for paper in recent_papers:
            upload_date = paper.get('uploaded_date')
            date_str = upload_date.strftime('%Y-%m-%d %H:%M') if upload_date else 'Unknown'
            st.write(f"**{paper['filename']}** - {date_str}")