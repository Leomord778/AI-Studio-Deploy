import os, httpx, math, subprocess, tempfile, edge_tts, imageio_ffmpeg, traceback
import base64, hashlib, hmac, json, mimetypes, shutil, smtplib, uuid, time, asyncio
from datetime import datetime, timedelta
from typing import Optional
from email.message import EmailMessage
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, BackgroundTasks, UploadFile, File, Form, Response, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from bson.objectid import ObjectId
from bson.errors import InvalidId

from starlette.middleware.base import BaseHTTPMiddleware

# --- ENV & MONGODB SETUP ---
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client['ai_video_studio']

users_col = db['users']
transactions_col = db['transactions']
settings_col = db['settings']
video_history_col = db["video_history"]
products_col = db["products"]
orders_col = db["store_orders"]
notifications_col = db["notifications"]
messages_col = db["messages"]

# လုံခြုံရေး Tokens
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-this-session-secret")
BOT_TOKEN = "8643779687:AAFrtV8XnepuiLWly9N1YwXEXZEBvu7pg-8"
CHAT_ID = "-1003802670362"
GOOGLE_CLIENT_ID = "135328538466-76vbcm81m07i03cqc105d5rrrt3967t4.apps.googleusercontent.com"
GROQ_API_KEYS = [
    "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",
    "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",
    "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",
    "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"
]

APP_DIR = Path(__file__).resolve().parent
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", APP_DIR / "media")).resolve()
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

# Email Setup
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

# --- Database & 48h Cleanup Loop ---
def init_db():
    defaults = {
        "deduction_rate": "200",
        "free_daily_mins": "2",
        "trial_mins": "5",
        "announcement": "AI Studio မှ နွေးထွေးစွာ ကြိုဆိုပါသည်။ ၅ မိနစ်စာ အခမဲ့ စမ်းသပ်နိုင်ပါသည်။",
        "marquee_color": "#ef4444",
        "announcement_text_color": "#ffffff"
    }
    for key, value in defaults.items():
        settings_col.update_one({"key": key}, {"$setOnInsert": {"value": value}}, upsert=True)
    
    if not users_col.find_one({"email": "778leomord@gmail.com"}):
        users_col.insert_one({
            "email": "778leomord@gmail.com", "role": "admin", "credits": 999999, 
            "free_mins_used": 0, "last_free_date": "", "has_bought_3000": False, "is_new": False
        })
    video_history_col.create_index([( "expires_at", ASCENDING)], expireAfterSeconds=0)

def cleanup_expired_media():
    expired = list(video_history_col.find({"expires_at": {"$lte": datetime.utcnow()}}, {"media_key": 1}))
    for item in expired:
        delete_media(item.get("media_key"))
    if expired:
        video_history_col.delete_many({"_id": {"$in": [item["_id"] for item in expired]}})

async def cleanup_loop():
    while True:
        await asyncio.sleep(60 * 60)
        cleanup_expired_media()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    cleanup_expired_media()
    task = asyncio.create_task(cleanup_loop())
    yield
    task.cancel()

app = FastAPI(title="AI Studio", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- CORS & Isolation Middleware (For Google Login and FFmpeg) ---
class CrossOriginIsolationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        response.headers["Cross-Origin-Embedder-Policy"] = "credentialless"
        return response

app.add_middleware(CrossOriginIsolationMiddleware)

# --- UTILS ---
def _encode_session(email: str, role: str) -> str:
    payload = {"email": email, "role": role, "exp": int(time.time()) + 60 * 60 * 24 * 7}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{signature}"

def normalize_email(value: str) -> str:
    return (value or "").strip().lower()

def get_request_email(request: Request) -> str:
    email = request.headers.get("x-user-email")
    if email: return normalize_email(email)
    raise HTTPException(status_code=401, detail="Please sign in first")

def require_admin(request: Request) -> str:
    email = get_request_email(request)
    user = users_col.find_one({"email": email})
    if email != "778leomord@gmail.com" and (not user or user.get("role") != "admin"):
        raise HTTPException(status_code=403, detail="Admin permission required")
    return email

def media_key(namespace: str, filename: str) -> str:
    safe_name = "".join(char if char.isalnum() or char in ".-_" else "_" for char in (filename or "file"))
    return f"{namespace}/{datetime.utcnow().strftime('%Y/%m/%d')}/{uuid.uuid4().hex}_{safe_name[:90]}"

def upload_bytes(data: bytes, key: str, content_type: str) -> str:
    path = (MEDIA_ROOT / key).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return key

async def save_upload(upload: UploadFile, namespace: str, allowed_prefixes: tuple) -> dict:
    content_type = upload.content_type or mimetypes.guess_type(upload.filename)[0] or "application/octet-stream"
    data = await upload.read()
    key = media_key(namespace, upload.filename)
    upload_bytes(data, key, content_type)
    return {"key": key, "name": upload.filename, "content_type": content_type, "size": len(data)}

def public_media_url(key: Optional[str]) -> Optional[str]:
    if not key: return None
    return f"/media/{key}"

def delete_media(key: Optional[str]) -> None:
    if not key: return
    try:
        path = (MEDIA_ROOT / key).resolve()
        if MEDIA_ROOT in path.parents and path.exists(): path.unlink()
    except Exception: pass

def create_notification(email: str, title: str, body: str, notification_type: str = "system", data: Optional[dict] = None) -> None:
    notifications_col.insert_one({
        "email": normalize_email(email), "type": notification_type, "title": title,
        "body": body, "data": data or {}, "is_read": False, "created_at": datetime.utcnow(),
    })

def send_email_sync(recipient: str, subject: str, html: str) -> None:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD): return
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_FROM
    message["To"] = recipient
    message.set_content("Please open this message in an HTML-compatible mail application.")
    message.add_alternative(html, subtype="html")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=25) as server:
        if SMTP_USE_TLS: server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(message)

async def send_payment_to_telegram(email: str, amount: int, file_bytes: bytes, caption_extra: str = "") -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    caption = f"Payment received\nUser: {email}\nAmount: {amount} MMK\n{caption_extra}".strip()
    async with httpx.AsyncClient(timeout=30) as http:
        await http.post(url, data={"chat_id": CHAT_ID, "caption": caption}, files={"photo": ("payment.jpg", file_bytes, "image/jpeg")})

# --- APIs ---
@app.get("/")
async def serve_index():
    with open("index.html", "r", encoding="utf-8") as f: return HTMLResponse(content=f.read())

@app.get("/media/{media_path:path}")
async def serve_media(media_path: str):
    path = (MEDIA_ROOT / media_path).resolve()
    if not path.exists() or not path.is_file(): raise HTTPException(status_code=404, detail="Media not found")
    return FileResponse(path)

class GoogleAuthData(BaseModel):
    credential: str

@app.post("/api/auth/google")
async def google_auth(data: GoogleAuthData):
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": data.credential})
    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="Google sign-in verification failed")
    
    info = response.json()
    if info.get("aud") != GOOGLE_CLIENT_ID or info.get("email_verified") not in (True, "true"):
        raise HTTPException(status_code=401, detail="Google account verification failed")
    
    email = normalize_email(info.get("email", ""))
    today = datetime.utcnow().strftime("%Y-%m-%d")
    user = users_col.find_one({"email": email})
    
    if not user:
        new_user = {
            "email": email, 
            "name": info.get("name") or email.split("@")[0],
            "picture": info.get("picture") or "",
            "role": "admin" if email == "778leomord@gmail.com" else "user", 
            "credits": 0, 
            "free_mins_used": 0, 
            "last_free_date": today, 
            "has_bought_3000": False, 
            "is_new": True,
            "created_at": datetime.utcnow()
        }
        users_col.insert_one(new_user)
        user = new_user
    else:
        updates = {}
        if user.get("last_free_date") != today:
            updates["free_mins_used"] = 0
            updates["last_free_date"] = today
            user["free_mins_used"] = 0
            user["last_free_date"] = today
        
        if info.get("name") and info.get("name") != user.get("name"):
            updates["name"] = info.get("name")
            user["name"] = info.get("name")
        if info.get("picture") and info.get("picture") != user.get("picture"):
            updates["picture"] = info.get("picture")
            user["picture"] = info.get("picture")
            
        if updates:
            users_col.update_one({"email": email}, {"$set": updates})

    settings = {doc["key"]: doc["value"] for doc in settings_col.find()}
    
    return { 
        "user": {
            "email": user["email"], 
            "name": user.get("name", ""),
            "picture": user.get("picture", "")
        },
        "email": user["email"], 
        "name": user.get("name", ""),
        "picture": user.get("picture", ""),
        "role": user.get("role", "user"), 
        "credits": user.get("credits", 0), 
        "free_mins_used": user.get("free_mins_used", 0), 
        "has_bought_3000": user.get("has_bought_3000", False), 
        "is_new": user.get("is_new", False), 
        "settings": settings,
        "token": _encode_session(email, user.get("role", "user"))
    }

class LoginData(BaseModel):
    email: str
    name: Optional[str] = None
    picture: Optional[str] = None

@app.post("/api/login")
async def login(data: LoginData):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    email = normalize_email(data.email)
    user = users_col.find_one({"email": email})
    if not user:
        new_user = { "email": email, "name": data.name or email.split("@")[0], "picture": data.picture or "", "role": "admin" if email == "778leomord@gmail.com" else "user", "credits": 0, "free_mins_used": 0, "last_free_date": today, "has_bought_3000": False, "is_new": True, "created_at": datetime.utcnow() }
        users_col.insert_one(new_user)
        user = new_user
    else:
        updates = {}
        if user.get("last_free_date") != today: updates["free_mins_used"] = 0; updates["last_free_date"] = today
        if data.name: updates["name"] = data.name
        if data.picture: updates["picture"] = data.picture
        if updates: users_col.update_one({"email": email}, {"$set": updates})

    settings = {doc["key"]: doc["value"] for doc in settings_col.find()}
    return { "email": user["email"], "name": user.get("name", ""), "picture": user.get("picture", ""), "role": user.get("role", "user"), "credits": user.get("credits", 0), "free_mins_used": user.get("free_mins_used", 0), "has_bought_3000": user.get("has_bought_3000", False), "is_new": user.get("is_new", False), "settings": settings, "token": _encode_session(email, user.get("role", "user")) }

class DeductData(BaseModel):
    email: str
    duration_seconds: float

@app.post("/api/check-credits")
async def check_credits(data: DeductData):
    settings = {doc["key"]: doc["value"] for doc in settings_col.find()}
    user = users_col.find_one({"email": data.email})
    if not user: return JSONResponse(status_code=400, content={"error": "User not found"})
    required_mins = max(1, math.ceil((data.duration_seconds - 30) / 60))
    if user.get("is_new") and (user.get("free_mins_used", 0) + required_mins <= float(settings.get('trial_mins', 5))): return {"status": "ok"}
    if user.get("credits", 0) >= required_mins * int(settings.get('deduction_rate', 200)): return {"status": "ok"}
    if (not user.get("is_new")) and (user.get("free_mins_used", 0) + required_mins <= float(settings.get('free_daily_mins', 2))): return {"status": "ok"}
    return JSONResponse(status_code=403, content={"error": "Credits မလုံလောက်ပါ။ ကျေးဇူးပြု၍ ထပ်မံဝယ်ယူပါ။"})

@app.post("/api/deduct")
async def deduct_credits(data: DeductData):
    settings = {doc["key"]: doc["value"] for doc in settings_col.find()}
    user = users_col.find_one({"email": data.email})
    if not user: return JSONResponse(status_code=400, content={"error": "User not found"})
    required_mins = max(1, math.ceil((data.duration_seconds - 30) / 60))
    deduction_rate = int(settings.get('deduction_rate', 200))
    
    if user.get("is_new"):
        if user.get("free_mins_used", 0) + required_mins <= float(settings.get('trial_mins', 5)):
            users_col.update_one({"email": data.email}, {"$inc": {"free_mins_used": required_mins}})
            if user.get("free_mins_used", 0) + required_mins >= float(settings.get('trial_mins', 5)): users_col.update_one({"email": data.email}, {"$set": {"is_new": False}})
            return {"status": "success"}
        else: users_col.update_one({"email": data.email}, {"$set": {"is_new": False}})

    if user.get("credits", 0) >= required_mins * deduction_rate:
        users_col.update_one({"email": data.email}, {"$inc": {"credits": -(required_mins * deduction_rate)}})
        return {"status": "success"}

    if user.get("free_mins_used", 0) + required_mins <= float(settings.get('free_daily_mins', 2)):
        users_col.update_one({"email": data.email}, {"$inc": {"free_mins_used": required_mins}})
        return {"status": "success"}
        
    return JSONResponse(status_code=403, content={"error": "Credits မလုံလောက်ပါ။ ကျေးဇူးပြု၍ ထပ်မံဝယ်ယူပါ။"})

@app.post("/api/buy-credits")
async def buy_credits(request: Request, background_tasks: BackgroundTasks, email: str = Form(...), amount: int = Form(...), file: UploadFile = File(...)):
    proof = await save_upload(file, "payment-proofs/credits", ("image/",))
    tx = { "type": "credit_topup", "email": email, "amount": int(amount), "status": "pending", "proof": proof, "created_at": datetime.utcnow() }
    result = transactions_col.insert_one(tx)
    try:
        proof_bytes = (MEDIA_ROOT / proof["key"]).read_bytes()
        background_tasks.add_task(send_payment_to_telegram, email, int(amount), proof_bytes)
    except: pass
    create_notification(email, "Credit top-up submitted", "ငွေလွှဲပြေစာကို Admin စစ်ဆေးနေပါသည်။", "payment", {"transaction_id": str(result.inserted_id)})
    return {"status": "success"}

@app.get("/api/credit-history")
async def credit_history(request: Request):
    email = get_request_email(request)
    docs = transactions_col.find({"email": email}).sort("_id", DESCENDING).limit(100)
    return {"transactions": [{"id": str(d["_id"]), "amount": d.get("amount", 0), "status": d.get("status", "pending"), "created_at": d.get("created_at", d.get("date")).isoformat()+"Z" if type(d.get("created_at"))==datetime else d.get("date")} for d in docs]}

# History
@app.post("/api/history/videos")
async def save_video_history(request: Request, file: UploadFile = File(...), title: str = Form("AI Studio video"), tool: str = Form("recap")):
    email = get_request_email(request)
    media = await save_upload(file, "video-history", ("video/",))
    expires_at = datetime.utcnow() + timedelta(hours=48)
    doc = { "email": email, "title": title[:140], "tool": tool[:40], "media_key": media["key"], "content_type": media["content_type"], "created_at": datetime.utcnow(), "expires_at": expires_at }
    video_history_col.insert_one(doc)
    return {"status": "success"}

@app.get("/api/history/videos")
async def get_video_history(request: Request):
    cleanup_expired_media()
    email = get_request_email(request)
    docs = video_history_col.find({"email": email, "expires_at": {"$gt": datetime.utcnow()}}).sort("created_at", DESCENDING)
    return {"items": [{"id": str(d["_id"]), "title": d.get("title", "Video"), "tool": d.get("tool"), "url": public_media_url(d.get("media_key")), "expires_at": d.get("expires_at").isoformat()+"Z"} for d in docs]}

@app.delete("/api/history/videos/{history_id}")
async def delete_video_history(history_id: str, request: Request):
    email = get_request_email(request)
    doc = video_history_col.find_one_and_delete({"_id": ObjectId(history_id), "email": email})
    if doc: delete_media(doc.get("media_key"))
    return {"status": "deleted"}

# Store & Orders
@app.get("/api/products")
async def list_products():
    docs = products_col.find({"is_active": True, "$or": [{"has_expiry": False}, {"expiry_at": {"$gt": datetime.utcnow()}}]}).sort("created_at", DESCENDING)
    return {"products": [{"id": str(d["_id"]), "name": d.get("name"), "category": d.get("category"), "description": d.get("description"), "price_credits": d.get("price_credits"), "has_expiry": d.get("has_expiry"), "expiry_at": d.get("expiry_at").isoformat()+"Z" if d.get("expiry_at") else None, "image_url": public_media_url(d.get("image_key")), "payment_accounts": d.get("payment_accounts", [])} for d in docs]}

@app.post("/api/orders/credit")
async def create_credit_order(request: Request, data: dict, background_tasks: BackgroundTasks):
    buyer_email = get_request_email(request)
    product = products_col.find_one({"_id": ObjectId(data["product_id"]), "is_active": True})
    amount = int(product["price_credits"])
    updated_user = users_col.find_one_and_update({"email": buyer_email, "credits": {"$gte": amount}}, {"$inc": {"credits": -amount}}, return_document=ReturnDocument.AFTER)
    if not updated_user: raise HTTPException(status_code=403, detail="Credits မလုံလောက်ပါ")
    
    order = { "buyer_email": buyer_email, "delivery_email": data["delivery_email"], "product_id": product["_id"], "product_name": product["name"], "price_credits": amount, "payment_method": "credit", "status": "delivered", "delivery_link": product.get("delivery_link", ""), "created_at": datetime.utcnow() }
    orders_col.insert_one(order)
    create_notification(buyer_email, "Product delivered", f"Credit ဖြင့်ဝယ်ယူမှု အောင်မြင်ပါသည်။\nDownload: {product.get('delivery_link', '')}", "order")
    background_tasks.add_task(send_email_sync, data["delivery_email"], f"Your product is ready", f"<p>Download Link: {product.get('delivery_link', '')}</p>")
    return {"status": "success", "credits": updated_user["credits"]}

@app.post("/api/orders/transfer")
async def create_transfer_order(request: Request, product_id: str = Form(...), delivery_email: str = Form(...), payment_provider: str = Form(...), payment_number: str = Form(...), receipt: UploadFile = File(...)):
    buyer_email = get_request_email(request)
    product = products_col.find_one({"_id": ObjectId(product_id)})
    proof = await save_upload(receipt, "payment-proofs/orders", ("image/",))
    order = { "buyer_email": buyer_email, "delivery_email": delivery_email, "product_id": product["_id"], "product_name": product["name"], "price_credits": int(product["price_credits"]), "payment_method": "transfer", "payment_account": {"provider": payment_provider, "number": payment_number}, "receipt_key": proof["key"], "status": "pending", "created_at": datetime.utcnow() }
    orders_col.insert_one(order)
    create_notification(buyer_email, "Store payment submitted", "ငွေလွှဲပြေစာကို Admin စစ်ဆေးနေပါသည်။", "order")
    return {"status": "pending"}

@app.get("/api/orders/me")
async def my_orders(request: Request):
    email = get_request_email(request)
    docs = orders_col.find({"buyer_email": email}).sort("created_at", DESCENDING)
    return {"orders": [{"id": str(d["_id"]), "product_name": d.get("product_name"), "status": d.get("status"), "created_at": d.get("created_at").isoformat()+"Z"} for d in docs]}

# Messaging
@app.get("/api/messages")
async def get_messages(request: Request, peer: Optional[str] = None):
    email = get_request_email(request)
    target = peer if peer else ("778leomord@gmail.com" if email != "778leomord@gmail.com" else "")
    query = {"participants": {"$all": [email, target]}}
    docs = list(messages_col.find(query).sort("created_at", ASCENDING).limit(300))
    messages_col.update_many({**query, "recipient_email": email}, {"$set": {"is_read": True}})
    return {"messages": [{"id": str(d["_id"]), "sender_email": d.get("sender_email"), "body": d.get("body", ""), "attachment": {"url": public_media_url(d.get("attachment", {}).get("key")), "content_type": d.get("attachment", {}).get("content_type")} if d.get("attachment") else None, "created_at": d.get("created_at").isoformat()+"Z"} for d in docs]}

# 🌟 ERROR FIX: SEND CHAT MESSAGE IN BACKEND 🌟
@app.post("/api/messages")
async def send_message(request: Request, recipient_email: str = Form(default=""), body: str = Form(default=""), attachment: Optional[UploadFile] = File(default=None)):
    sender = get_request_email(request)
    recipient = recipient_email if recipient_email else "778leomord@gmail.com"
    attachment_info = await save_upload(attachment, "messages", ("image/", "audio/", "video/")) if attachment else None
    doc = { "sender_email": sender, "recipient_email": recipient, "participants": sorted([sender, recipient]), "body": body.strip()[:5000], "attachment": attachment_info, "created_at": datetime.utcnow(), "is_read": False }
    messages_col.insert_one(doc)
    create_notification(recipient, "New message", f"{sender} မှ message အသစ်ရောက်ရှိပါသည်။", "message")
    return {"message": {"id": str(doc["_id"]), "sender_email": doc["sender_email"], "body": doc["body"], "attachment": {"url": public_media_url(attachment_info["key"]), "content_type": attachment_info["content_type"]} if attachment_info else None, "created_at": doc["created_at"].isoformat()+"Z"}}

@app.get("/api/notifications")
async def get_notifications(request: Request):
    email = get_request_email(request)
    docs = notifications_col.find({"email": email}).sort("created_at", DESCENDING).limit(100)
    return {"notifications": [{"id": str(d["_id"]), "title": d.get("title"), "body": d.get("body"), "is_read": d.get("is_read"), "created_at": d.get("created_at").isoformat()+"Z"} for d in docs]}

@app.post("/api/notifications/read")
async def mark_notifications_read(request: Request):
    email = get_request_email(request)
    notifications_col.update_many({"email": email, "is_read": False}, {"$set": {"is_read": True}})
    return {"status": "success"}

# --- ADMIN APIs ---
@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    require_admin(request)
    current_month = datetime.utcnow().strftime("%Y-%m")
    approved = transactions_col.find({"status": "approved", "created_at": {"$gte": datetime.strptime(current_month, "%Y-%m")}})
    return { "total_users": users_col.count_documents({"role": {"$ne": "admin"}}), "monthly_profit": sum(tx.get("amount", 0) for tx in approved) }

@app.get("/api/admin/transactions")
async def get_transactions(request: Request):
    require_admin(request)
    docs = transactions_col.find({"status": "pending"}).sort("_id", DESCENDING)
    return {"transactions": [{"id": str(d["_id"]), "email": d["email"], "amount": d["amount"], "status": d["status"], "date": d.get("created_at", d.get("date")).isoformat()+"Z" if type(d.get("created_at"))==datetime else d.get("date")} for d in docs]}

class ApproveData(BaseModel):
    id: str  
    email: Optional[str] = None
    amount: Optional[int] = None

@app.post("/api/admin/approve")
async def approve_transaction(request: Request, data: ApproveData):
    require_admin(request)
    tx = transactions_col.find_one_and_update({"_id": ObjectId(data.id)}, {"$set": {"status": "approved"}}, return_document=ReturnDocument.AFTER)
    if tx:
        users_col.update_one({"email": tx["email"]}, {"$inc": {"credits": tx["amount"]}})
        create_notification(tx["email"], "Credits added", f"{tx['amount']:,} credits ထည့်ပေးပြီးပါပြီ။", "payment")
    return {"status": "success"}

class AddCreditData(BaseModel):
    email: str
    amount: int

@app.post("/api/admin/add-credit")
async def admin_add_credit(request: Request, data: AddCreditData):
    require_admin(request)
    users_col.update_one({"email": data.email}, {"$inc": {"credits": data.amount}})
    create_notification(data.email, "Credits added", f"Admin မှ {data.amount:,} credits ထည့်ပေးပြီးပါပြီ။")
    return {"status": "success"}

class SettingsData(BaseModel):
    deduction_rate: str
    free_daily_mins: str
    trial_mins: str
    announcement: str
    marquee_color: str = "#ef4444"
    announcement_bg: str = "#ef4444"
    announcement_text_color: str = "#ffffff"

@app.post("/api/admin/settings")
async def update_settings(request: Request, data: SettingsData):
    require_admin(request)
    for key, value in data.dict().items(): settings_col.update_one({"key": key}, {"$set": {"value": str(value)}}, upsert=True)
    return {"status": "success"}

@app.get("/api/admin/products")
async def admin_products(request: Request):
    require_admin(request)
    docs = products_col.find().sort("created_at", DESCENDING)
    return {"products": [{"id": str(d["_id"]), "name": d.get("name"), "category": d.get("category"), "price_credits": d.get("price_credits"), "is_active": d.get("is_active", True), "image_url": public_media_url(d.get("image_key"))} for d in docs]}

@app.post("/api/admin/products")
async def create_product(request: Request, name: str = Form(...), category: str = Form(...), description: str = Form(""), price_credits: int = Form(...), has_expiry: bool = Form(False), expiry_at: str = Form(""), delivery_link: str = Form(""), payment_accounts_json: str = Form("[]"), image: UploadFile = File(...)):
    require_admin(request)
    expiration = datetime.fromisoformat(expiry_at.replace("Z", "+00:00")).replace(tzinfo=None) if has_expiry and expiry_at else None
    uploaded = await save_upload(image, "products", ("image/",))
    doc = { "name": name, "category": category, "description": description, "price_credits": int(price_credits), "has_expiry": bool(has_expiry), "expiry_at": expiration, "image_key": uploaded["key"], "payment_accounts": json.loads(payment_accounts_json), "delivery_link": delivery_link, "is_active": True, "created_at": datetime.utcnow() }
    result = products_col.insert_one(doc)
    for user in users_col.find({"role": {"$ne": "admin"}}, {"email": 1}): create_notification(user["email"], "New product added", f"{name} ကို Online Store တွင် ကြည့်ရှုနိုင်ပါသည်။", "store")
    return {"status": "created"}

@app.patch("/api/admin/products/{product_id}/toggle")
async def toggle_product(product_id: str, request: Request):
    require_admin(request)
    current = products_col.find_one({"_id": ObjectId(product_id)})
    products_col.update_one({"_id": ObjectId(product_id)}, {"$set": {"is_active": not bool(current.get("is_active", True))}})
    return {"status": "success"}

@app.get("/api/admin/orders")
async def admin_orders(request: Request):
    require_admin(request)
    docs = orders_col.find().sort("created_at", DESCENDING)
    return {"orders": [{"id": str(d["_id"]), "buyer_email": d.get("buyer_email"), "delivery_email": d.get("delivery_email"), "product_name": d.get("product_name"), "status": d.get("status"), "receipt_url": public_media_url(d.get("receipt_key"))} for d in docs]}

class DeliveryData(BaseModel):
    delivery_link: Optional[str] = None
    message: Optional[str] = None

@app.post("/api/admin/orders/{order_id}/approve")
async def approve_order(order_id: str, request: Request, data: DeliveryData, background_tasks: BackgroundTasks):
    require_admin(request)
    order = orders_col.find_one_and_update({"_id": ObjectId(order_id)}, {"$set": {"status": "delivered", "delivery_link": data.delivery_link}}, return_document=ReturnDocument.AFTER)
    if order:
        body = data.message or "သင်ဝယ်ယူထားသော Product ကို ပို့ပေးပြီးပါပြီ။"
        if data.delivery_link: body += f"\n\nDownload Link: {data.delivery_link}"
        create_notification(order["buyer_email"], "Product delivered", body, "order")
        background_tasks.add_task(send_email_sync, order["delivery_email"], f"Your product is ready: {order['product_name']}", f"<h2>Product delivered</h2><p>{body.replace(chr(10), '<br>')}</p>")
    return {"status": "success"}

@app.get("/api/admin/messages/conversations")
async def admin_conversations(request: Request):
    require_admin(request)
    pipeline = [ {"$match": {"participants": "778leomord@gmail.com"}}, {"$sort": {"created_at": -1}}, {"$group": {"_id": "$participants", "unread": {"$sum": {"$cond": [{"$and": [{"$eq": ["$recipient_email", "778leomord@gmail.com"]}, {"$eq": ["$is_read", False]}]}, 1, 0]}}}} ]
    items = [{"email": next((e for e in doc["_id"] if e != "778leomord@gmail.com"), ""), "unread": doc["unread"]} for doc in messages_col.aggregate(pipeline)]
    return {"conversations": items}

# --- SYSTEM APIs ---
@app.post("/api/tts")
async def edge_tts_api(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        text = data.get("text", "").strip()
        voice = data.get("voice", "my-MM-ThihaNeural")
        if not text: return Response(content='{"error": "စာသား မရှိပါ"}', status_code=400)
        communicate = edge_tts.Communicate(text, voice)
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp_file.close()
        await communicate.save(tmp_file.name)
        background_tasks.add_task(os.remove, tmp_file.name)
        return FileResponse(tmp_file.name, media_type="audio/mpeg")
    except Exception as e: return Response(content=f'{{"error": "{str(e)}"}}', status_code=500)

@app.post("/api/convert-mp4")
async def convert_to_mp4(request: Request, background_tasks: BackgroundTasks):
    try:
        form = await request.form()
        file = form.get("file")
        in_temp = tempfile.NamedTemporaryFile(delete=False, suffix=".webm")
        in_temp.write(await file.read())
        in_temp.close()
        out_temp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        out_temp.close()
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ffmpeg_exe, "-y", "-i", in_temp.name, "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-movflags", "+faststart", out_temp.name], check=True)
        background_tasks.add_task(os.remove, in_temp.name)
        background_tasks.add_task(os.remove, out_temp.name)
        return FileResponse(out_temp.name, media_type="video/mp4", filename="final_video.mp4")
    except Exception as e: return Response(content=f'{{"error": "{str(e)}"}}', status_code=500)

@app.api_route("/api/gemini/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_gemini(path: str, request: Request):
    url = f"https://generativelanguage.googleapis.com/{path}"
    async with httpx.AsyncClient() as client:
        req = client.build_request(request.method, url, params=dict(request.query_params), headers={"Content-Type": "application/json"}, content=await request.body(), timeout=120.0)
        try:
            res = await client.send(req)
            return Response(content=res.content, status_code=res.status_code, media_type=res.headers.get("content-type", "application/json"))
        except Exception as e: return Response(content=str(e), status_code=500)

@app.post("/api/groq/transcriptions")
async def proxy_groq(request: Request):
    try:
        form = await request.form()
        file = form.get("file")
        file_bytes = await file.read()
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        async with httpx.AsyncClient() as client:
            for key in GROQ_API_KEYS:
                if not key.strip() or "YOUR_" in key: continue
                try:
                    res = await client.post(url, headers={"Authorization": f"Bearer {key.strip()}"}, files={"file": (file.filename, file_bytes, file.content_type)}, data={"model": form.get("model", "whisper-large-v3-turbo"), "response_format": form.get("response_format", "verbose_json")}, timeout=120.0)
                    if res.status_code == 200: return Response(content=res.content, status_code=res.status_code, media_type=res.headers.get("content-type"))
                except: continue
        return Response(content='{"error": "Groq Error"}', status_code=500)
    except Exception as e: return Response(content=f'{{"error": "{str(e)}"}}', status_code=500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))