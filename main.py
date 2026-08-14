import asyncio
import base64
import hashlib
import hmac
import json
import math
import time
import mimetypes
import os
import shutil
import smtplib
import subprocess
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import edge_tts
import httpx
import imageio_ffmpeg
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument

# FFmpeg.wasm အတွက် လိုအပ်သော Starlette Middleware ကို Import လုပ်ခြင်း
from starlette.middleware.base import BaseHTTPMiddleware

try:
    import boto3
except ImportError:
    boto3 = None

load_dotenv()

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", APP_DIR / "media")).resolve()
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is required")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "778leomord@gmail.com").strip().lower()
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "135328538466-76vbcm81m07i03cqc105d5rrrt3967t4.apps.googleusercontent.com")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-this-session-secret")
ALLOW_HEADER_AUTH = os.getenv("ALLOW_HEADER_AUTH", "false").lower() == "true"
CORS_ORIGINS = [item.strip() for item in os.getenv("CORS_ORIGINS", "*").split(",") if item.strip()]
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GROQ_API_KEYS = [item.strip() for item in os.getenv("GROQ_API_KEYS", "").split(",") if item.strip()]

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_REGION = os.getenv("S3_REGION", "")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "")

client = MongoClient(MONGO_URI)
db = client["ai_video_studio"]
users_col = db["users"]
transactions_col = db["transactions"]
settings_col = db["settings"]
video_history_col = db["video_history"]
products_col = db["products"]
orders_col = db["store_orders"]
notifications_col = db["notifications"]
messages_col = db["messages"]

_s3 = None
if S3_BUCKET:
    if boto3 is None:
        raise RuntimeError("boto3 must be installed when S3_BUCKET is configured")
    _s3 = boto3.client(
        "s3",
        region_name=S3_REGION or None,
        endpoint_url=S3_ENDPOINT_URL or None,
        aws_access_key_id=S3_ACCESS_KEY or None,
        aws_secret_access_key=S3_SECRET_KEY or None,
    )


# -----------------------------------------------------------------------------
# Utilities and storage
# -----------------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.utcnow()


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() + "Z" if value else None


def object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=400, detail="Invalid record id")


def normalize_email(value: str) -> str:
    email = (value or "").strip().lower()
    if "@" not in email or len(email) > 254:
        raise HTTPException(status_code=400, detail="Valid Gmail/email is required")
    return email


def _encode_session(email: str, role: str) -> str:
    payload = {"email": email, "role": role, "exp": int(time.time()) + 60 * 60 * 24 * 7}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{signature}"


def _decode_session(token: str) -> dict:
    try:
        raw, signature = token.split(".", 1)
        expected = hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad signature")
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("expired")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")


def get_request_email(request: Request, fallback: Optional[str] = None) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return normalize_email(_decode_session(authorization.split(" ", 1)[1]).get("email", ""))
    if ALLOW_HEADER_AUTH:
        email = request.headers.get("x-user-email") or fallback
        if email:
            return normalize_email(email)
    raise HTTPException(status_code=401, detail="Please sign in first")


def require_admin(request: Request) -> str:
    email = get_request_email(request)
    user = users_col.find_one({"email": email})
    if email != ADMIN_EMAIL and (not user or user.get("role") != "admin"):
        raise HTTPException(status_code=403, detail="Admin permission required")
    return email


def media_key(namespace: str, filename: str) -> str:
    safe_name = "".join(char if char.isalnum() or char in ".-_" else "_" for char in (filename or "file"))
    return f"{namespace}/{utcnow().strftime('%Y/%m/%d')}/{uuid.uuid4().hex}_{safe_name[:90]}"


def upload_bytes(data: bytes, key: str, content_type: str) -> str:
    if _s3:
        _s3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type or "application/octet-stream")
        return key
    path = (MEDIA_ROOT / key).resolve()
    if MEDIA_ROOT not in path.parents:
        raise HTTPException(status_code=400, detail="Invalid storage path")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return key


async def save_upload(upload: UploadFile, namespace: str, allowed_prefixes: tuple[str, ...]) -> dict:
    if not upload or not upload.filename:
        raise HTTPException(status_code=400, detail="File is required")
    content_type = upload.content_type or mimetypes.guess_type(upload.filename)[0] or "application/octet-stream"
    if allowed_prefixes and not any(content_type.startswith(prefix) for prefix in allowed_prefixes):
        raise HTTPException(status_code=400, detail="Unsupported file type")
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    max_bytes = int(os.getenv("MAX_UPLOAD_MB", "150")) * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail="File is too large")
    key = media_key(namespace, upload.filename)
    upload_bytes(data, key, content_type)
    return {"key": key, "name": upload.filename, "content_type": content_type, "size": len(data)}


def public_media_url(key: Optional[str]) -> Optional[str]:
    if not key:
        return None
    if _s3:
        return _s3.generate_presigned_url(
            "get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=60 * 60 * 48
        )
    return f"/media/{key}"


def delete_media(key: Optional[str]) -> None:
    if not key:
        return
    try:
        if _s3:
            _s3.delete_object(Bucket=S3_BUCKET, Key=key)
        else:
            path = (MEDIA_ROOT / key).resolve()
            if MEDIA_ROOT in path.parents and path.exists():
                path.unlink()
    except Exception:
        pass


def serialize_notification(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "type": doc.get("type", "system"),
        "title": doc.get("title", "Notification"),
        "body": doc.get("body", ""),
        "data": doc.get("data", {}),
        "is_read": doc.get("is_read", False),
        "created_at": iso(doc.get("created_at")),
    }


def create_notification(email: str, title: str, body: str, notification_type: str = "system", data: Optional[dict] = None) -> None:
    notifications_col.insert_one(
        {
            "email": normalize_email(email),
            "type": notification_type,
            "title": title,
            "body": body,
            "data": data or {},
            "is_read": False,
            "created_at": utcnow(),
        }
    )


def send_email_sync(recipient: str, subject: str, html: str) -> None:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and SMTP_FROM):
        return
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_FROM
    message["To"] = recipient
    message.set_content("Please open this message in an HTML-compatible mail application.")
    message.add_alternative(html, subtype="html")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=25) as server:
        if SMTP_USE_TLS:
            server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(message)


async def send_payment_to_telegram(email: str, amount: int, file_bytes: bytes, caption_extra: str = "") -> None:
    if not (BOT_TOKEN and CHAT_ID):
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    caption = f"Payment received\nUser: {email}\nAmount: {amount} MMK\n{caption_extra}".strip()
    async with httpx.AsyncClient(timeout=30) as http:
        await http.post(
            url,
            data={"chat_id": CHAT_ID, "caption": caption},
            files={"photo": ("payment.jpg", file_bytes, "image/jpeg")},
        )


def cleanup_expired_media() -> None:
    expired = list(video_history_col.find({"expires_at": {"$lte": utcnow()}}, {"media_key": 1}))
    for item in expired:
        delete_media(item.get("media_key"))
    if expired:
        video_history_col.delete_many({"_id": {"$in": [item["_id"] for item in expired]}})


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(60 * 60)
        cleanup_expired_media()


# -----------------------------------------------------------------------------
# Application lifecycle
# -----------------------------------------------------------------------------
def init_db() -> None:
    defaults = {
        "deduction_rate": "200",
        "free_daily_mins": "2",
        "trial_mins": "5",
        "announcement": "AI Studio မှ နွေးထွေးစွာ ကြိုဆိုပါသည်။",
        "announcement_bg": "#ef4444",
        "announcement_text_color": "#ffffff",
        "marquee_color": "#ef4444",
        "store_version": "0",
    }
    for key, value in defaults.items():
        settings_col.update_one({"key": key}, {"$setOnInsert": {"value": value}}, upsert=True)
    users_col.update_one(
        {"email": ADMIN_EMAIL},
        {
            "$setOnInsert": {
                "email": ADMIN_EMAIL,
                "role": "admin",
                "credits": 999999,
                "free_mins_used": 0,
                "last_free_date": "",
                "has_bought_3000": False,
                "is_new": False,
                "created_at": utcnow(),
            }
        },
        upsert=True,
    )
    video_history_col.create_index([( "expires_at", ASCENDING)], expireAfterSeconds=0)
    video_history_col.create_index([( "email", ASCENDING), ("created_at", DESCENDING)])
    notifications_col.create_index([( "email", ASCENDING), ("created_at", DESCENDING)])
    messages_col.create_index([( "participants", ASCENDING), ("created_at", ASCENDING)])
    orders_col.create_index([( "buyer_email", ASCENDING), ("created_at", DESCENDING)])
    products_col.create_index([( "is_active", ASCENDING), ("category", ASCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    cleanup_expired_media()
    task = asyncio.create_task(cleanup_loop())
    yield
    task.cancel()


app = FastAPI(title="AI Studio", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False if CORS_ORIGINS == ["*"] else True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# FFmpeg.wasm Security Headers Middleware (credentialless)
# -----------------------------------------------------------------------------
class CrossOriginIsolationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Embedder-Policy"] = "credentialless"
        return response

app.add_middleware(CrossOriginIsolationMiddleware)


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class LoginData(BaseModel):
    email: str


class GoogleAuthData(BaseModel):
    credential: str


class DeductData(BaseModel):
    email: str
    duration_seconds: float = Field(gt=0, le=14400)


class AddCreditData(BaseModel):
    email: str
    amount: int = Field(gt=0, le=10_000_000)


class ApproveData(BaseModel):
    id: str


class SettingsData(BaseModel):
    deduction_rate: str = "200"
    free_daily_mins: str = "2"
    trial_mins: str = "5"
    announcement: str = ""
    announcement_bg: str = "#ef4444"
    announcement_text_color: str = "#ffffff"
    marquee_color: str = "#ef4444"


class CreditOrderData(BaseModel):
    product_id: str
    delivery_email: str


class DeliveryData(BaseModel):
    delivery_link: Optional[str] = None
    message: Optional[str] = None


# -----------------------------------------------------------------------------
# Static files and login
# -----------------------------------------------------------------------------
@app.get("/")
async def serve_index():
    return HTMLResponse((APP_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/media/{media_path:path}")
async def serve_media(media_path: str):
    if _s3:
        raise HTTPException(status_code=404, detail="Media is served by private signed links")
    path = (MEDIA_ROOT / media_path).resolve()
    if MEDIA_ROOT not in path.parents or not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Media not found")
    return FileResponse(path)


async def _user_payload(email: str) -> dict:
    today = utcnow().strftime("%Y-%m-%d")
    user = users_col.find_one({"email": email})
    if not user:
        user = {"email": email, "role": "admin" if email == ADMIN_EMAIL else "user", "credits": 0, "free_mins_used": 0, "last_free_date": today, "has_bought_3000": False, "is_new": email != ADMIN_EMAIL, "created_at": utcnow()}
        users_col.insert_one(user)
    elif user.get("last_free_date") != today:
        users_col.update_one({"email": email}, {"$set": {"free_mins_used": 0, "last_free_date": today}})
        user["free_mins_used"] = 0
    settings = {doc["key"]: doc["value"] for doc in settings_col.find()}
    return {"email": email, "role": user.get("role", "user"), "credits": user.get("credits", 0), "free_mins_used": user.get("free_mins_used", 0), "has_bought_3000": user.get("has_bought_3000", False), "is_new": user.get("is_new", False), "settings": settings, "unread_notifications": notifications_col.count_documents({"email": email, "is_read": False})}


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
    payload = await _user_payload(email)
    payload["token"] = _encode_session(email, payload["role"])
    payload["user"] = {"email": email, "name": info.get("name", email.split("@")[0]), "picture": info.get("picture", "")}
    return payload


@app.post("/api/login")
async def login(data: LoginData):
    return await _user_payload(normalize_email(data.email))


# -----------------------------------------------------------------------------
# Credits and payment receipts
# -----------------------------------------------------------------------------
def calculate_required_minutes(duration_seconds: float) -> int:
    return max(1, math.ceil((duration_seconds - 30) / 60))


@app.post("/api/check-credits")
async def check_credits(request: Request, data: DeductData):
    email = get_request_email(request)
    settings = {doc["key"]: doc["value"] for doc in settings_col.find()}
    user = users_col.find_one({"email": email})
    if not user:
        return JSONResponse(status_code=400, content={"error": "User not found"})
    required_mins = calculate_required_minutes(data.duration_seconds)
    deduction_rate = int(settings.get("deduction_rate", 200))
    free_daily_mins = float(settings.get("free_daily_mins", 2))
    trial_mins = float(settings.get("trial_mins", 5))
    free_used = float(user.get("free_mins_used", 0))
    if user.get("is_new") and free_used + required_mins <= trial_mins:
        return {"status": "ok", "required_credits": 0}
    if user.get("credits", 0) >= required_mins * deduction_rate:
        return {"status": "ok", "required_credits": required_mins * deduction_rate}
    if not user.get("is_new") and free_used + required_mins <= free_daily_mins:
        return {"status": "ok", "required_credits": 0}
    return JSONResponse(status_code=403, content={"error": "Credits မလုံလောက်ပါ။ ကျေးဇူးပြု၍ ထပ်မံဝယ်ယူပါ။"})


@app.post("/api/deduct")
async def deduct_credits(request: Request, data: DeductData):
    email = get_request_email(request)
    settings = {doc["key"]: doc["value"] for doc in settings_col.find()}
    user = users_col.find_one({"email": email})
    if not user:
        return JSONResponse(status_code=400, content={"error": "User not found"})
    required_mins = calculate_required_minutes(data.duration_seconds)
    deduction_rate = int(settings.get("deduction_rate", 200))
    free_daily_mins = float(settings.get("free_daily_mins", 2))
    trial_mins = float(settings.get("trial_mins", 5))
    free_used = float(user.get("free_mins_used", 0))
    if user.get("is_new") and free_used + required_mins <= trial_mins:
        users_col.update_one({"email": email}, {"$inc": {"free_mins_used": required_mins}})
        if free_used + required_mins >= trial_mins:
            users_col.update_one({"email": email}, {"$set": {"is_new": False}})
        return {"status": "success", "charged": 0}
    charged = required_mins * deduction_rate
    updated = users_col.find_one_and_update(
        {"email": email, "credits": {"$gte": charged}},
        {"$inc": {"credits": -charged}},
        return_document=ReturnDocument.AFTER,
    )
    if updated:
        return {"status": "success", "charged": charged, "credits": updated["credits"]}
    if not user.get("is_new") and free_used + required_mins <= free_daily_mins:
        users_col.update_one({"email": email}, {"$inc": {"free_mins_used": required_mins}})
        return {"status": "success", "charged": 0}
    return JSONResponse(status_code=403, content={"error": "Credits မလုံလောက်ပါ။ ကျေးဇူးပြု၍ ထပ်မံဝယ်ယူပါ။"})


@app.post("/api/buy-credits")
async def buy_credits(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    amount: int = Form(...),
    file: UploadFile = File(...),
):
    email = get_request_email(request)
    amount = int(amount)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    proof = await save_upload(file, "payment-proofs/credits", ("image/",))
    tx = {
        "type": "credit_topup",
        "email": email,
        "amount": amount,
        "status": "pending",
        "proof": proof,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    result = transactions_col.insert_one(tx)
    try:
        proof_bytes = (MEDIA_ROOT / proof["key"]).read_bytes() if not _s3 else b""
        if proof_bytes:
            background_tasks.add_task(send_payment_to_telegram, email, amount, proof_bytes)
    except Exception:
        pass
    create_notification(email, "Credit top-up submitted", "ငွေလွှဲပြေစာကို Admin စစ်ဆေးနေပါသည်။", "payment", {"transaction_id": str(result.inserted_id)})
    return {"status": "success", "transaction_id": str(result.inserted_id)}


@app.get("/api/credit-history")
async def credit_history(request: Request):
    email = get_request_email(request)
    docs = transactions_col.find({"email": email}).sort("_id", DESCENDING).limit(100)
    return {
        "transactions": [
            {
                "id": str(doc["_id"]),
                "type": doc.get("type", "credit_topup"),
                "amount": doc.get("amount", 0),
                "status": doc.get("status", "pending"),
                "created_at": iso(doc.get("created_at")) or doc.get("date"),
            }
            for doc in docs
        ]
    }


# -----------------------------------------------------------------------------
# Video history
# -----------------------------------------------------------------------------
@app.post("/api/history/videos")
async def save_video_history(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form("AI Studio video"),
    tool: str = Form("recap"),
):
    email = get_request_email(request)
    media = await save_upload(file, "video-history", ("video/",))
    expires_at = utcnow() + timedelta(hours=48)
    doc = {
        "email": email,
        "title": title[:140] or "AI Studio video",
        "tool": tool[:40],
        "media_key": media["key"],
        "content_type": media["content_type"],
        "created_at": utcnow(),
        "expires_at": expires_at,
    }
    result = video_history_col.insert_one(doc)
    return {
        "id": str(result.inserted_id),
        "url": public_media_url(media["key"]),
        "expires_at": iso(expires_at),
    }


@app.get("/api/history/videos")
async def video_history(request: Request):
    cleanup_expired_media()
    email = get_request_email(request)
    docs = video_history_col.find({"email": email, "expires_at": {"$gt": utcnow()}}).sort("created_at", DESCENDING)
    return {
        "items": [
            {
                "id": str(doc["_id"]),
                "title": doc.get("title", "AI Studio video"),
                "tool": doc.get("tool", "recap"),
                "url": public_media_url(doc.get("media_key")),
                "created_at": iso(doc.get("created_at")),
                "expires_at": iso(doc.get("expires_at")),
            }
            for doc in docs
        ]
    }


@app.delete("/api/history/videos/{history_id}")
async def delete_video_history(history_id: str, request: Request):
    email = get_request_email(request)
    doc = video_history_col.find_one_and_delete({"_id": object_id(history_id), "email": email})
    if not doc:
        raise HTTPException(status_code=404, detail="Video not found")
    delete_media(doc.get("media_key"))
    return {"status": "deleted"}


# -----------------------------------------------------------------------------
# Online store
# -----------------------------------------------------------------------------
def serialize_product(doc: dict, include_delivery_link: bool = False) -> dict:
    result = {
        "id": str(doc["_id"]),
        "name": doc.get("name", ""),
        "category": doc.get("category", "Other"),
        "description": doc.get("description", ""),
        "price_credits": doc.get("price_credits", 0),
        "has_expiry": doc.get("has_expiry", False),
        "expiry_at": iso(doc.get("expiry_at")),
        "image_url": public_media_url(doc.get("image_key")),
        "payment_accounts": doc.get("payment_accounts", []),
        "is_active": doc.get("is_active", True),
        "created_at": iso(doc.get("created_at")),
    }
    if include_delivery_link:
        result["delivery_link"] = doc.get("delivery_link", "")
    return result


@app.get("/api/products/categories")
async def product_categories():
    categories = products_col.distinct("category", {"is_active": True})
    return {"categories": sorted([category for category in categories if category])}


@app.get("/api/products")
async def list_products(category: Optional[str] = None):
    query = {"is_active": True, "$or": [{"has_expiry": False}, {"expiry_at": {"$gt": utcnow()}}]}
    if category:
        query["category"] = category
    docs = products_col.find(query).sort("created_at", DESCENDING)
    return {"products": [serialize_product(doc) for doc in docs]}


@app.get("/api/products/{product_id}")
async def product_detail(product_id: str):
    doc = products_col.find_one({"_id": object_id(product_id), "is_active": True})
    if not doc or (doc.get("has_expiry") and doc.get("expiry_at") and doc["expiry_at"] <= utcnow()):
        raise HTTPException(status_code=404, detail="Product is unavailable")
    return {"product": serialize_product(doc)}


@app.get("/api/admin/products")
async def admin_products(request: Request):
    require_admin(request)
    return {"products": [serialize_product(doc, include_delivery_link=True) for doc in products_col.find().sort("created_at", DESCENDING)]}


@app.post("/api/admin/products")
async def create_product(
    request: Request,
    name: str = Form(...),
    category: str = Form(...),
    description: str = Form(""),
    price_credits: int = Form(...),
    has_expiry: bool = Form(False),
    expiry_at: str = Form(""),
    delivery_link: str = Form(""),
    payment_accounts_json: str = Form("[]"),
    image: UploadFile = File(...),
):
    require_admin(request)
    if price_credits <= 0:
        raise HTTPException(status_code=400, detail="Price must be greater than zero")
    try:
        accounts = json.loads(payment_accounts_json)
        if not isinstance(accounts, list):
            raise ValueError()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payment accounts")
    cleaned_accounts = []
    for account in accounts:
        provider = str(account.get("provider", "")).strip()[:40]
        number = str(account.get("number", "")).strip()[:80]
        holder = str(account.get("holder", "")).strip()[:80]
        if provider and number:
            cleaned_accounts.append({"provider": provider, "number": number, "holder": holder})
    expiration = None
    if has_expiry:
        try:
            expiration = datetime.fromisoformat(expiry_at.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(status_code=400, detail="Expiry date is required for an expiring product")
        if expiration <= utcnow():
            raise HTTPException(status_code=400, detail="Expiry date must be in the future")
    uploaded = await save_upload(image, "products", ("image/",))
    doc = {
        "name": name.strip()[:140],
        "category": category.strip()[:80],
        "description": description.strip()[:4000],
        "price_credits": int(price_credits),
        "has_expiry": bool(has_expiry),
        "expiry_at": expiration,
        "image_key": uploaded["key"],
        "payment_accounts": cleaned_accounts,
        "delivery_link": delivery_link.strip()[:2000],
        "is_active": True,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    result = products_col.insert_one(doc)
    settings_col.update_one({"key": "store_version"}, {"$set": {"value": str(utcnow().timestamp())}}, upsert=True)
    for user in users_col.find({"role": {"$ne": "admin"}}, {"email": 1}):
        create_notification(user["email"], "New product added", f"{doc['name']} ကို Online Store တွင် ကြည့်ရှုနိုင်ပါသည်။", "store", {"product_id": str(result.inserted_id)})
    return {"status": "created", "product": serialize_product({**doc, "_id": result.inserted_id}, include_delivery_link=True)}


@app.patch("/api/admin/products/{product_id}/toggle")
async def toggle_product(product_id: str, request: Request):
    require_admin(request)
    current = products_col.find_one({"_id": object_id(product_id)})
    if not current:
        raise HTTPException(status_code=404, detail="Product not found")
    product = products_col.find_one_and_update(
        {"_id": current["_id"]}, {"$set": {"is_active": not bool(current.get("is_active", True)), "updated_at": utcnow()}}, return_document=ReturnDocument.AFTER
    )
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"product": serialize_product(product, include_delivery_link=True)}


@app.delete("/api/admin/products/{product_id}")
async def delete_product(product_id: str, request: Request):
    require_admin(request)
    product = products_col.find_one_and_delete({"_id": object_id(product_id)})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    delete_media(product.get("image_key"))
    return {"status": "deleted"}


# -----------------------------------------------------------------------------
# Store orders
# -----------------------------------------------------------------------------
def serialize_order(doc: dict, include_admin: bool = False) -> dict:
    result = {
        "id": str(doc["_id"]),
        "product_id": str(doc.get("product_id")),
        "product_name": doc.get("product_name", "Product"),
        "price_credits": doc.get("price_credits", 0),
        "payment_method": doc.get("payment_method", "credit"),
        "status": doc.get("status", "pending"),
        "delivery_email": doc.get("delivery_email", ""),
        "delivery_link": doc.get("delivery_link", ""),
        "created_at": iso(doc.get("created_at")),
        "updated_at": iso(doc.get("updated_at")),
    }
    if include_admin:
        result["buyer_email"] = doc.get("buyer_email", "")
        result["receipt_url"] = public_media_url(doc.get("receipt_key"))
        result["payment_account"] = doc.get("payment_account", {})
        result["admin_message"] = doc.get("admin_message", "")
    return result


def get_active_product(product_id: str) -> dict:
    product = products_col.find_one({"_id": object_id(product_id), "is_active": True})
    if not product or (product.get("has_expiry") and product.get("expiry_at") and product["expiry_at"] <= utcnow()):
        raise HTTPException(status_code=404, detail="Product is unavailable")
    return product


def deliver_order(order: dict, delivery_link: Optional[str], message: Optional[str], background_tasks: BackgroundTasks) -> dict:
    resolved_link = (delivery_link or order.get("delivery_link") or "").strip()
    products_col.update_one({"_id": order["product_id"]}, {"$set": {"updated_at": utcnow()}})
    orders_col.update_one(
        {"_id": order["_id"]},
        {"$set": {"status": "delivered", "delivery_link": resolved_link, "admin_message": (message or "").strip(), "updated_at": utcnow(), "delivered_at": utcnow()}},
    )
    body = (message or "သင်ဝယ်ယူထားသော Product ကို ပို့ပေးပြီးပါပြီ။").strip()
    if resolved_link:
        body += f"\n\nDownload / Access Link: {resolved_link}"
    create_notification(order["buyer_email"], "Product delivered", body, "order", {"order_id": str(order["_id"]), "delivery_link": resolved_link})
    html = f"<h2>Product delivered</h2><p>{body.replace(chr(10), '<br>')}</p>"
    background_tasks.add_task(send_email_sync, order["delivery_email"], f"Your product is ready: {order['product_name']}", html)
    return {"status": "delivered", "delivery_link": resolved_link}


@app.post("/api/orders/credit")
async def create_credit_order(request: Request, data: CreditOrderData, background_tasks: BackgroundTasks):
    buyer_email = get_request_email(request)
    delivery_email = normalize_email(data.delivery_email)
    product = get_active_product(data.product_id)
    amount = int(product["price_credits"])
    updated_user = users_col.find_one_and_update(
        {"email": buyer_email, "credits": {"$gte": amount}},
        {"$inc": {"credits": -amount}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated_user:
        raise HTTPException(status_code=403, detail="Credits မလုံလောက်ပါ")
    order = {
        "buyer_email": buyer_email,
        "delivery_email": delivery_email,
        "product_id": product["_id"],
        "product_name": product["name"],
        "price_credits": amount,
        "payment_method": "credit",
        "status": "paid",
        "delivery_link": product.get("delivery_link", ""),
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    result = orders_col.insert_one(order)
    order["_id"] = result.inserted_id
    delivery = deliver_order(order, product.get("delivery_link"), "Credit ဖြင့်ဝယ်ယူမှု အောင်မြင်ပါသည်။", background_tasks)
    return {"order": serialize_order({**order, "status": "delivered", "delivery_link": delivery["delivery_link"]}), "credits": updated_user["credits"]}


@app.post("/api/orders/transfer")
async def create_transfer_order(
    request: Request,
    product_id: str = Form(...),
    delivery_email: str = Form(...),
    payment_provider: str = Form(...),
    payment_number: str = Form(...),
    receipt: UploadFile = File(...),
):
    buyer_email = get_request_email(request)
    delivery_email = normalize_email(delivery_email)
    product = get_active_product(product_id)
    proof = await save_upload(receipt, "payment-proofs/orders", ("image/",))
    order = {
        "buyer_email": buyer_email,
        "delivery_email": delivery_email,
        "product_id": product["_id"],
        "product_name": product["name"],
        "price_credits": int(product["price_credits"]),
        "payment_method": "transfer",
        "payment_account": {"provider": payment_provider[:40], "number": payment_number[:80]},
        "receipt_key": proof["key"],
        "status": "pending",
        "delivery_link": product.get("delivery_link", ""),
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    result = orders_col.insert_one(order)
    create_notification(buyer_email, "Store payment submitted", "ငွေလွှဲပြေစာကို Admin စစ်ဆေးနေပါသည်။", "order", {"order_id": str(result.inserted_id)})
    return {"status": "pending", "order_id": str(result.inserted_id)}


@app.get("/api/orders/me")
async def my_orders(request: Request):
    email = get_request_email(request)
    return {"orders": [serialize_order(doc) for doc in orders_col.find({"buyer_email": email}).sort("created_at", DESCENDING)]}


@app.get("/api/admin/orders")
async def admin_orders(request: Request, status: Optional[str] = None):
    require_admin(request)
    query = {"status": status} if status else {}
    return {"orders": [serialize_order(doc, include_admin=True) for doc in orders_col.find(query).sort("created_at", DESCENDING)]}


@app.post("/api/admin/orders/{order_id}/approve")
async def approve_order(order_id: str, request: Request, data: DeliveryData, background_tasks: BackgroundTasks):
    require_admin(request)
    order = orders_col.find_one_and_update(
        {"_id": object_id(order_id), "status": "pending"},
        {"$set": {"status": "approved", "updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if not order:
        raise HTTPException(status_code=409, detail="Order is no longer pending")
    return deliver_order(order, data.delivery_link, data.message, background_tasks)


# -----------------------------------------------------------------------------
# User and admin messages
# -----------------------------------------------------------------------------
def serialize_message(doc: dict) -> dict:
    attachment = doc.get("attachment") or None
    return {
        "id": str(doc["_id"]),
        "sender_email": doc.get("sender_email", ""),
        "recipient_email": doc.get("recipient_email", ""),
        "body": doc.get("body", ""),
        "attachment": {**attachment, "url": public_media_url(attachment.get("key"))} if attachment else None,
        "created_at": iso(doc.get("created_at")),
        "is_read": doc.get("is_read", False),
    }


@app.get("/api/messages")
async def get_messages(request: Request, peer: Optional[str] = None):
    email = get_request_email(request)
    target = normalize_email(peer or (ADMIN_EMAIL if email != ADMIN_EMAIL else ""))
    if email != ADMIN_EMAIL and target != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Users can only message the admin")
    query = {"participants": {"$all": [email, target]}}
    docs = list(messages_col.find(query).sort("created_at", ASCENDING).limit(300))
    messages_col.update_many({**query, "recipient_email": email}, {"$set": {"is_read": True}})
    return {"messages": [serialize_message(doc) for doc in docs]}


@app.post("/api/messages")
async def send_message(
    request: Request,
    recipient_email: str = Form(...),
    body: str = Form(""),
    attachment: Optional[UploadFile] = File(None),
):
    sender = get_request_email(request)
    recipient = normalize_email(recipient_email) if recipient_email.strip() else ADMIN_EMAIL
    if sender != ADMIN_EMAIL and recipient != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Users can only message the admin")
    if not body.strip() and not attachment:
        raise HTTPException(status_code=400, detail="Message or attachment is required")
    attachment_info = None
    if attachment:
        attachment_info = await save_upload(attachment, "messages", ("image/", "audio/", "video/"))
    doc = {
        "sender_email": sender,
        "recipient_email": recipient,
        "participants": sorted([sender, recipient]),
        "body": body.strip()[:5000],
        "attachment": attachment_info,
        "created_at": utcnow(),
        "is_read": False,
    }
    result = messages_col.insert_one(doc)
    create_notification(recipient, "New message", f"{sender} မှ message အသစ်ရောက်ရှိပါသည်။", "message", {"sender_email": sender})
    return {"message": serialize_message({**doc, "_id": result.inserted_id})}


@app.get("/api/admin/messages/conversations")
async def admin_conversations(request: Request):
    require_admin(request)
    pipeline = [
        {"$match": {"participants": ADMIN_EMAIL}},
        {"$sort": {"created_at": -1}},
        {"$group": {"_id": "$participants", "last": {"$first": "$$ROOT"}, "unread": {"$sum": {"$cond": [{"$and": [{"$eq": ["$recipient_email", ADMIN_EMAIL]}, {"$eq": ["$is_read", False]}]}, 1, 0]}}}},
    ]
    items = []
    for item in messages_col.aggregate(pipeline):
        participants = item["_id"]
        peer = next((entry for entry in participants if entry != ADMIN_EMAIL), ADMIN_EMAIL)
        items.append({"email": peer, "last_message": item["last"].get("body", "Attachment"), "created_at": iso(item["last"].get("created_at")), "unread": item["unread"]})
    return {"conversations": items}


@app.get("/api/notifications")
async def get_notifications(request: Request):
    email = get_request_email(request)
    docs = notifications_col.find({"email": email}).sort("created_at", DESCENDING).limit(100)
    return {"notifications": [serialize_notification(doc) for doc in docs]}


@app.post("/api/notifications/read")
async def mark_notifications_read(request: Request):
    email = get_request_email(request)
    notifications_col.update_many({"email": email, "is_read": False}, {"$set": {"is_read": True}})
    return {"status": "success"}


# -----------------------------------------------------------------------------
# Admin dashboard
# -----------------------------------------------------------------------------
@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    require_admin(request)
    current_month = utcnow().strftime("%Y-%m")
    approved = transactions_col.find({"status": "approved", "created_at": {"$gte": datetime.strptime(current_month, "%Y-%m")}})
    return {
        "total_users": users_col.count_documents({"role": {"$ne": "admin"}}),
        "monthly_profit": sum(tx.get("amount", 0) for tx in approved),
        "pending_credit_topups": transactions_col.count_documents({"status": "pending"}),
        "pending_store_orders": orders_col.count_documents({"status": "pending"}),
    }


@app.get("/api/admin/transactions")
async def get_transactions(request: Request):
    require_admin(request)
    docs = transactions_col.find({"status": "pending"}).sort("_id", DESCENDING)
    return {
        "transactions": [
            {
                "id": str(doc["_id"]),
                "email": doc["email"],
                "amount": doc["amount"],
                "status": doc["status"],
                "date": iso(doc.get("created_at")) or doc.get("date"),
                "proof_url": public_media_url((doc.get("proof") or {}).get("key")),
            }
            for doc in docs
        ]
    }


@app.post("/api/admin/approve")
async def approve_transaction(request: Request, data: ApproveData):
    require_admin(request)
    tx = transactions_col.find_one_and_update(
        {"_id": object_id(data.id), "status": "pending"},
        {"$set": {"status": "approved", "updated_at": utcnow(), "approved_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if not tx:
        raise HTTPException(status_code=409, detail="Transaction is already processed")
    update = {"$inc": {"credits": int(tx["amount"])}}
    if int(tx["amount"]) == 3000:
        update["$set"] = {"has_bought_3000": True}
    users_col.update_one({"email": tx["email"]}, update)
    create_notification(tx["email"], "Credits added", f"{tx['amount']:,} credits ကို account ထဲသို့ ထည့်ပေးပြီးပါပြီ။", "payment", {"transaction_id": str(tx["_id"])})
    return {"status": "success"}


@app.post("/api/admin/add-credit")
async def admin_add_credit(request: Request, data: AddCreditData):
    require_admin(request)
    email = normalize_email(data.email)
    users_col.update_one({"email": email}, {"$inc": {"credits": data.amount}}, upsert=True)
    create_notification(email, "Credits added", f"Admin မှ {data.amount:,} credits ထည့်ပေးပြီးပါပြီ။", "payment")
    return {"status": "success"}


@app.post("/api/admin/settings")
async def update_settings(request: Request, data: SettingsData):
    require_admin(request)
    values = data.model_dump() if hasattr(data, "model_dump") else data.dict()
    values["marquee_color"] = values["announcement_bg"]
    for key, value in values.items():
        settings_col.update_one({"key": key}, {"$set": {"value": str(value)}}, upsert=True)
    return {"status": "success"}


# -----------------------------------------------------------------------------
# Existing AI service endpoints
# -----------------------------------------------------------------------------
@app.post("/api/tts")
async def edge_tts_api(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        text = str(data.get("text", "")).strip()
        voice = str(data.get("voice", "my-MM-ThihaNeural"))
        if not text:
            return JSONResponse(status_code=400, content={"error": "စာသား မရှိပါ"})
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.close()
        await edge_tts.Communicate(text, voice).save(tmp.name)
        background_tasks.add_task(os.remove, tmp.name)
        return FileResponse(tmp.name, media_type="audio/mpeg")
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/api/convert-mp4")
async def convert_to_mp4(request: Request, background_tasks: BackgroundTasks):
    try:
        form = await request.form()
        upload = form.get("file")
        if not upload:
            raise HTTPException(status_code=400, detail="Video file is required")
        source = tempfile.NamedTemporaryFile(delete=False, suffix=".webm")
        source.write(await upload.read())
        source.close()
        target = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        target.close()
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ffmpeg, "-y", "-i", source.name, "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-movflags", "+faststart", target.name], check=True)
        background_tasks.add_task(os.remove, source.name)
        background_tasks.add_task(os.remove, target.name)
        return FileResponse(target.name, media_type="video/mp4", filename="final_video.mp4")
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.api_route("/api/gemini/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_gemini(path: str, request: Request):
    url = f"https://generativelanguage.googleapis.com/{path}"
    async with httpx.AsyncClient(timeout=120) as http:
        try:
            response = await http.request(
                request.method,
                url,
                params=dict(request.query_params),
                headers={"Content-Type": request.headers.get("content-type", "application/json")},
                content=await request.body(),
            )
            return Response(content=response.content, status_code=response.status_code, media_type=response.headers.get("content-type", "application/json"))
        except Exception as exc:
            return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/api/groq/transcriptions")
async def proxy_groq(request: Request):
    form = await request.form()
    upload = form.get("file")
    if not upload:
        return JSONResponse(status_code=400, content={"error": "Audio file is required"})
    if not GROQ_API_KEYS:
        return JSONResponse(status_code=503, content={"error": "GROQ_API_KEYS is not configured"})
    file_bytes = await upload.read()
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    async with httpx.AsyncClient(timeout=120) as http:
        for key in GROQ_API_KEYS:
            try:
                response = await http.post(
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": (upload.filename, file_bytes, upload.content_type)},
                    data={"model": form.get("model", "whisper-large-v3-turbo"), "response_format": form.get("response_format", "verbose_json")},
                )
                if response.status_code == 200:
                    return Response(content=response.content, status_code=200, media_type=response.headers.get("content-type", "application/json"))
            except Exception:
                continue
    return JSONResponse(status_code=500, content={"error": "Groq transcription failed"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))