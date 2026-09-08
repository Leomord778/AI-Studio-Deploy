import os
import httpx
import math
import tempfile
import edge_tts
import base64
import hashlib
import hmac
import json
import uuid
import time
import asyncio
import mimetypes
import shutil
from datetime import datetime, timedelta, timezone
from typing import Optional, Any, List
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, BackgroundTasks, UploadFile, File, Form, Response, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from bson.objectid import ObjectId

mimetypes.add_type('application/wasm', '.wasm')
mimetypes.add_type('text/javascript', '.js')

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI: 
    raise ValueError("Error: MONGO_URI is missing in .env file.")

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

SESSION_SECRET = os.getenv("SESSION_SECRET", "ai-studio-super-secret-key-2026")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "778leomord@gmail.com").strip().lower()

# Telegram Integration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8643779687:AAFrtV8XnepuiLWly9N1YwXEXZEBvu7pg-8").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", " -1003802670362").strip()

DEFAULT_GROQ_KEYS = [
    k.strip() for k in os.getenv(
        "GROQ_API_KEYS", 
        "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,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"
    ).split(",") if k.strip()
]

SERVER_GEMINI_KEYS = [
    k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()
]
gemini_key_index = 0

def get_server_gemini_key() -> str:
    global gemini_key_index
    if not SERVER_GEMINI_KEYS: return ""
    key = SERVER_GEMINI_KEYS[gemini_key_index % len(SERVER_GEMINI_KEYS)]
    gemini_key_index += 1
    return key

MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", APP_DIR / "media")).resolve()
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

STATIC_ROOT = (APP_DIR / "static").resolve()
STATIC_ROOT.mkdir(parents=True, exist_ok=True)

def current_utc(): 
    return datetime.now(timezone.utc)

def init_db():
    defaults = {
        "deduction_rate_per_min": "50", 
        "first_two_min_rate": "30",
        "free_minutes_per_day": "0",
        "tts_mode": "ads",
        "tts_chars_per_credit": "100",
        "ads_smart_link": "https://heiressnicholasfitful.com/q1hniexdb?key=fd1f82d2494bc60d62f89d9e2472f9c8",
        "announcement": "AI Studio မှ နွေးထွေးစွာ ကြိုဆိုပါသည်။ အရည်အသွေးမြင့် ဗီဒီယိုများကို အချိန်တိုအတွင်း ဖန်တီးလိုက်ပါ။",
        "marquee_bg": "#4f46e5", 
        "marquee_color": "#ffffff",
        "telegram_username": "Awthu75",
        "admin_payment_accounts": json.dumps([
            {"provider": "KBZPay", "number": "09421619437", "holder": "Aung Win Thu"},
            {"provider": "WavePay", "number": "09421619437", "holder": "Aung Win Thu"}
        ])
    }
    for key, value in defaults.items(): 
        settings_col.update_one({"key": key}, {"$setOnInsert": {"value": value}}, upsert=True)
    
    if not users_col.find_one({"email": ADMIN_EMAIL}):
        users_col.insert_one({
            "email": ADMIN_EMAIL, 
            "role": "admin", 
            "credits": 999999, 
            "credits_expire_at": None,
            "created_at": current_utc()
        })
    
    video_history_col.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)

async def send_telegram_notification(caption: str, photo_path: Optional[Path] = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=20.0) as http_client:
            if photo_path and photo_path.exists():
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
                with open(photo_path, "rb") as f:
                    files = {"photo": f}
                    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
                    await http_client.post(url, data=data, files=files)
            else:
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                payload = {"chat_id": TELEGRAM_CHAT_ID, "text": caption, "parse_mode": "HTML"}
                await http_client.post(url, json=payload)
    except Exception as e:
        print(f"[Telegram Notification Error] {e}")

def refund_and_cleanup():
    now = current_utc()
    expired_videos = list(video_history_col.find({"expires_at": {"$lte": now}}))
    for item in expired_videos:
        u_email = item.get("email")
        cost = int(item.get("cost", 0))
        downloaded = item.get("downloaded", False)
        created_time_str = item.get("created_at").strftime("%Y-%m-%d %H:%M") if item.get("created_at") else "ယခင်"

        if not downloaded and cost > 0 and u_email != ADMIN_EMAIL:
            users_col.update_one({"email": u_email}, {"$inc": {"credits": cost}})
            create_notification(
                u_email,
                "Credit ပြန်လည်အမ်းငွေ ရရှိပါသည်",
                f"{created_time_str} တွင် ဖန်တီးခဲ့သော ဗီဒီယိုအား ၄၈ နာရီအတွင်း Download ရယူခြင်းမရှိပါသဖြင့် ကုန်ကျခဲ့သော {cost} Credits အား အကောင့်ထဲသို့ ပြန်လည် ထည့်သွင်းပေးလိုက်ပါပြီခင်ဗျာ။"
            )
            create_notification(
                ADMIN_EMAIL,
                "Auto-Refund သတိပေးချက်",
                f"User ({u_email}) မှ {created_time_str} တွင် လုပ်ခဲ့သော ဗီဒီယိုအား Download မဆွဲခဲ့သဖြင့် {cost} Credits အား စနစ်မှ အလိုအလျောက် Refund ပေးလိုက်ပါသည်။"
            )
        delete_media(item.get("media_key"))
        video_history_col.delete_one({"_id": item["_id"]})

    expired_users = list(users_col.find({
        "role": {"$ne": "admin"},
        "credits_expire_at": {"$ne": None, "$lte": now},
        "credits": {"$gt": 0}
    }))
    for u in expired_users:
        users_col.update_one(
            {"_id": u["_id"]},
            {"$set": {"credits": 0, "credits_expire_at": None}}
        )
        create_notification(
            u["email"], 
            "Credit သက်တမ်းကုန်ဆုံးပါပြီ", 
            "ဝယ်ယူထားသော Credit များ သက်တမ်းကုန်ဆုံးသွားပါပြီခင်ဗျာ။ ဆက်လက်အသုံးပြုလိုပါက ပြန်လည်ဖြည့်သွင်းပေးပါရန် မေတ္တာရပ်ခံအပ်ပါသည်။"
        )

async def cleanup_loop():
    while True:
        await asyncio.sleep(1800)
        refund_and_cleanup()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    refund_and_cleanup()
    task = asyncio.create_task(cleanup_loop())
    yield
    task.cancel()

app = FastAPI(title="AI Studio Pro", lifespan=lifespan)

@app.middleware("http")
async def add_cross_origin_isolation_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    response.headers["Cross-Origin-Embedder-Policy"] = "credentialless"
    response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")

def _encode_session(email: str, role: str) -> str:
    payload = {"email": email, "role": role, "exp": int(time.time()) + 60 * 60 * 24 * 7}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{signature}"

def _decode_session(token: str) -> Optional[dict]:
    try:
        raw, signature = token.split(".", 1)
        expected = hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected): return None
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if int(payload.get("exp", 0)) < int(time.time()): return None
        return payload
    except Exception: return None

def get_request_email(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        payload = _decode_session(auth[7:].strip())
        if payload and payload.get("email"): return str(payload["email"]).strip().lower()
    raise HTTPException(status_code=401, detail="ကျေးဇူးပြု၍ အကောင့် Login အရင်ဝင်ပေးပါ။")

def require_admin(request: Request) -> str:
    email = get_request_email(request)
    user = users_col.find_one({"email": email})
    if email != ADMIN_EMAIL and (not user or user.get("role") != "admin"): 
        raise HTTPException(status_code=403, detail="Admin လုပ်ပိုင်ခွင့် လိုအပ်ပါသည်။")
    return email

def media_key(namespace: str, filename: str) -> str:
    safe_name = "".join(char if char.isalnum() or char in ".-_" else "_" for char in (filename or "file"))
    return f"{namespace}/{current_utc().strftime('%Y/%m/%d')}/{uuid.uuid4().hex}_{safe_name[:90]}"

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))

async def save_upload(upload: UploadFile, namespace: str) -> dict:
    content_type = upload.content_type or "application/octet-stream"
    key = media_key(namespace, upload.filename or "file")
    path = (MEDIA_ROOT / key).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with path.open("wb") as output:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk: break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    path.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="File အရွယ်အစား ကြီးလွန်းပါသည်။")
                output.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    return {"key": key, "name": upload.filename, "content_type": content_type, "size": total}

def delete_media(key: Optional[str]) -> None:
    if not key: return
    try:
        path = (MEDIA_ROOT / key).resolve()
        if path.exists(): path.unlink()
    except Exception: pass

def create_notification(email: str, title: str, body: str, notification_type: str = "system") -> None:
    if not email: return
    notifications_col.insert_one({
        "email": email.strip().lower(),
        "type": notification_type,
        "title": title,
        "body": body,
        "is_read": False,
        "created_at": current_utc()
    })

# --- CORE PAGE & MEDIA ---

@app.get("/")
async def serve_index():
    index_path = APP_DIR / "index.html"
    if not index_path.exists(): 
        return HTMLResponse(content="<h1 style='color:red;'>Error: index.html ဖိုင်ကို ရှာမတွေ့ပါ။</h1>", status_code=404)
    with open(index_path, "r", encoding="utf-8") as f: 
        return HTMLResponse(content=f.read())

@app.get("/favicon.ico", include_in_schema=False)
async def favicon(): 
    return Response(status_code=204)

@app.get("/media/{media_path:path}")
async def serve_media(media_path: str):
    path = (MEDIA_ROOT / media_path).resolve()
    if not path.exists() or not path.is_file(): 
        raise HTTPException(status_code=404, detail="Media ဖိုင် ရှာမတွေ့ပါ။")
    return FileResponse(path)

# --- AUTH & USER PROFILE ---

@app.post("/api/login")
async def login(data: dict):
    email = data.get("email", "").strip().lower()
    if not email: return JSONResponse(status_code=400, content={"error": "Email is required"})
    user = users_col.find_one({"email": email})
    role = "admin" if email == ADMIN_EMAIL else "user"
    if not user:
        user = { 
            "email": email, 
            "name": data.get("name") or email.split("@")[0], 
            "picture": data.get("picture") or "", 
            "role": role, 
            "credits": 1000, 
            "credits_expire_at": None,
            "tts_count": 0,
            "created_at": current_utc() 
        }
        users_col.insert_one(user)

    if user.get("role") != "admin" and user.get("credits_expire_at"):
        exp_at = user["credits_expire_at"]
        if isinstance(exp_at, datetime):
            if exp_at.tzinfo is None:
                exp_at = exp_at.replace(tzinfo=timezone.utc)
            if exp_at <= current_utc():
                users_col.update_one({"email": email}, {"$set": {"credits": 0, "credits_expire_at": None}})
                user["credits"] = 0
                user["credits_expire_at"] = None

    settings = {doc["key"]: doc["value"] for doc in settings_col.find()}
    if "_id" in user: del user["_id"]
    
    remaining_days = None
    if user.get("credits_expire_at") and isinstance(user["credits_expire_at"], datetime):
        exp_date = user["credits_expire_at"]
        if exp_date.tzinfo is None: exp_date = exp_date.replace(tzinfo=timezone.utc)
        diff = exp_date - current_utc()
        remaining_days = max(0, diff.days + 1)
        user["credits_expire_at"] = exp_date.strftime("%Y-%m-%d")

    return { 
        **user, 
        "remaining_days": remaining_days,
        "settings": settings, 
        "token": _encode_session(email, user.get("role", "user")) 
    }

@app.post("/api/buy-credits")
async def buy_credits(email: str = Form(...), amount: int = Form(...), file: UploadFile = File(...)):
    u_email = email.strip().lower()
    buy_amount = int(amount)
    
    # ပြေစာပုံ သိမ်းဆည်းခြင်း
    proof = await save_upload(file, "payment-proofs/credits")
    proof_path = (MEDIA_ROOT / proof["key"]).resolve()

    # Database ထဲသို့ Transaction ထည့်သွင်းခြင်း
    transactions_col.insert_one({ 
        "type": "credit_topup", 
        "email": u_email, 
        "amount": buy_amount, 
        "status": "pending", 
        "proof": proof["key"], 
        "created_at": current_utc() 
    })

    # User အတွက် Web Notification ပေးပို့ခြင်း
    create_notification(
        u_email, 
        "ငွေလွှဲပြေစာ လက်ခံရရှိပါသည်", 
        "AI Studio မှ Credit ဝယ်ယူမှုအတွက် ကျေးဇူးတင်ရှိပါသည်။ Admin မှ ငွေလွှဲပြေစာအား အမြန်ဆုံး အတည်ပြုပေးပါမည်ခင်ဗျာ။"
    )

    # Admin Telegram ဆီသို့ ပြေစာဓာတ်ပုံနှင့်တကွ Noti တိုက်ရိုက် ပေးပို့ခြင်း
    caption = (
        f"💳 <b>[New Credit Topup Order]</b>\n\n"
        f"👤 <b>User Email:</b> <code>{u_email}</code>\n"
        f"💰 <b>Amount:</b> <b>{buy_amount:,} Credits (MMK)</b>\n"
        f"🕒 <b>Time:</b> {current_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        f"📌 <i>Admin Dashboard > Approvals တွင် အတည်ပြုပေးပါ။</i>"
    )
    await send_telegram_notification(caption, photo_path=proof_path)

    return {
        "status": "success", 
        "message": "ငွေလွှဲပြေစာ ပေးပို့ပြီးပါပြီ။ ဝယ်ယူအားပေးမှုကို ကျေးဇူးတင်ရှိပါသည်။ Admin ဘက်မှ အတည်ပြုပေးသည်အထိ ခေတ္တစောင့်ဆိုင်းပေးပါခင်ဗျာ။"
    }

@app.get("/api/credit-history")
async def get_credit_history(request: Request):
    email = get_request_email(request)
    txs = list(transactions_col.find({"email": email}).sort("created_at", DESCENDING))
    return {
        "transactions": [{
            "id": str(d["_id"]),
            "amount": d.get("amount", 0),
            "status": d.get("status", "pending"),
            "created_at": d.get("created_at").isoformat() if d.get("created_at") else ""
        } for d in txs]
    }

@app.get("/api/notifications")
async def get_notifications(request: Request):
    email = get_request_email(request)
    notifs = list(notifications_col.find({"email": email}).sort("created_at", DESCENDING).limit(30))
    unread_count = notifications_col.count_documents({"email": email, "is_read": False})
    return {
        "notifications": [{
            "id": str(n["_id"]),
            "title": n.get("title", ""),
            "body": n.get("body", ""),
            "is_read": n.get("is_read", False),
            "created_at": n.get("created_at").strftime("%Y-%m-%d %H:%M") if n.get("created_at") else ""
        } for n in notifs],
        "unread_count": unread_count
    }

@app.post("/api/notifications/mark-read")
async def mark_notifications_read(request: Request):
    email = get_request_email(request)
    notifications_col.update_many({"email": email, "is_read": False}, {"$set": {"is_read": True}})
    return {"status": "success"}

# --- VIDEO COST CALCULATION & BILLING ---

def calculate_video_cost(email: str, dur_sec: float) -> tuple[int, bool]:
    dur_mins = max(1, math.ceil(dur_sec / 60.0))

    free_setting = settings_col.find_one({"key": "free_minutes_per_day"})
    free_mins = int(free_setting.get("value", "0")) if free_setting else 0

    rate_setting = settings_col.find_one({"key": "deduction_rate_per_min"})
    base_rate = int(rate_setting.get("value", "50")) if rate_setting else 50

    first_two_setting = settings_col.find_one({"key": "first_two_min_rate"})
    first_two_rate = int(first_two_setting.get("value", "30")) if first_two_setting else 30

    if free_mins > 0:
        if dur_mins <= free_mins:
            return 0, True
        else:
            dur_mins -= free_mins

    if dur_mins <= 2:
        cost = first_two_rate
    else:
        cost = first_two_rate + ((dur_mins - 2) * base_rate)

    return cost, False

@app.post("/api/video/pre-deduct")
async def pre_deduct_video_cost(request: Request, data: dict):
    email = get_request_email(request)
    dur_sec = float(data.get("duration", 60))
    cost, is_free = calculate_video_cost(email, dur_sec)

    user = users_col.find_one({"email": email})
    if email != ADMIN_EMAIL and not is_free:
        if not user or user.get("credits", 0) < cost:
            raise HTTPException(
                status_code=402, 
                detail=f"Credit မလုံလောက်ပါ။ ဗီဒီယိုအတွက် {cost} Credits လိုအပ်ပါသည်။"
            )
        users_col.update_one({"email": email}, {"$inc": {"credits": -cost}})

    return {"status": "success", "deducted": cost if email != ADMIN_EMAIL else 0, "is_free": is_free}

@app.post("/api/video/save-client-rendered")
async def save_client_rendered(request: Request, file: UploadFile = File(...), duration: str = Form("60"), tool: str = Form("recap")):
    email = get_request_email(request)
    dur_sec = float(duration)
    cost, is_free = calculate_video_cost(email, dur_sec)

    upload_res = await save_upload(file, "rendered_videos")
    final_key = upload_res["key"]

    video_doc = {
        "email": email,
        "title": f"{'Recap' if tool=='recap' else 'Subtitle'} Video ({datetime.now().strftime('%d/%m %H:%M')})",
        "tool": tool,
        "duration": duration,
        "cost": cost if email != ADMIN_EMAIL else 0,
        "status": "completed",
        "downloaded": False,
        "media_key": final_key,
        "content_type": "video/mp4",
        "created_at": current_utc(),
        "expires_at": current_utc() + timedelta(hours=48)
    }
    inserted = video_history_col.insert_one(video_doc)
    return {"status": "success", "result_url": f"/media/{final_key}", "video_id": str(inserted.inserted_id), "media_key": final_key}

@app.post("/api/video/consume-download")
async def consume_download(request: Request, data: dict):
    email = get_request_email(request)
    media_key = data.get("media_key")
    video_id = data.get("video_id")

    query = {"_id": ObjectId(video_id)} if video_id else {"media_key": media_key}
    video = video_history_col.find_one(query)
    if not video:
        raise HTTPException(status_code=404, detail="ဗီဒီယို ရှာမတွေ့ပါ")

    video_history_col.update_one({"_id": video["_id"]}, {"$set": {"downloaded": True}})
    return {"status": "success"}

@app.get("/api/history/videos")
async def get_video_history(request: Request):
    email = get_request_email(request)
    return {
        "items": [{
            "id": str(d["_id"]), 
            "title": d.get("title", "Video"), 
            "tool": d.get("tool"), 
            "url": f"/media/{d.get('media_key')}", 
            "expires_at": d.get("expires_at").strftime("%Y-%m-%d %H:%M") if d.get("expires_at") else "",
            "created_at": d.get("created_at").isoformat() if d.get("created_at") else ""
        } for d in video_history_col.find({"email": email}).sort("created_at", DESCENDING)]
    }

# --- STORE & TELEGRAM ORDER API ---

@app.get("/api/products")
async def get_products():
    prods = list(products_col.find().sort("created_at", DESCENDING))
    safe_products = []
    
    for p in prods:
        # User တွေဆီ ပို့မည့် variants စာရင်းထဲမှ မူရင်းဈေး (cost_price_mmk) ကို လုံးဝ ဖြုတ်ပစ်ခြင်း
        safe_variants = []
        for v in p.get("variants", []):
            safe_variants.append({
                "name": v.get("name", "Standard"),
                "price_mmk": float(v.get("price_mmk", 0)),
                "price_credits": int(v.get("price_credits", 0)),
                "requires_game_id": bool(v.get("requires_game_id", False)),
                "requires_server_id": bool(v.get("requires_server_id", False))
                # 🔒 cost_price_mmk မပါဝင်တော့ပါ
            })
            
        img_url = p.get("image_url", "")
        if not img_url and p.get("image_key"):
            img_url = f"/media/{p['image_key']}"
        elif not img_url and p.get("image_urls"):
            img_url = p.get("image_urls")[0]

        img_urls = p.get("image_urls", [])
        if not img_urls and p.get("image_keys"):
            img_urls = [f"/media/{k}" for k in p.get("image_keys")]

        safe_products.append({
            "id": str(p["_id"]),
            "name": p.get("name", ""),
            "category": p.get("category", ""),
            "description": p.get("description", ""),
            "image_url": img_url,
            "image_urls": img_urls,
            "variants": safe_variants,
            "payment_accounts": p.get("payment_accounts", [])
        })

    return {"products": safe_products}

@app.post("/api/store/order")
async def submit_store_order(
    request: Request,
    product_name: str = Form(...),
    variant_name: str = Form(...),
    price_mmk: int = Form(...),
    user_email: str = Form(...),
    payment_provider: str = Form("KBZPay"),
    game_account_id: Optional[str] = Form(None),
    game_server_id: Optional[str] = Form(None),
    file: UploadFile = File(...)
):
    user = get_request_email(request)
    proof = await save_upload(file, "store_proofs")
    proof_path = (MEDIA_ROOT / proof["key"]).resolve()
    
    order_doc = {
        "user_email": user_email.strip().lower(),
        "product_name": product_name,
        "variant_name": variant_name,
        "price_mmk": price_mmk,
        "payment_provider": payment_provider,
        "game_account_id": game_account_id,
        "game_server_id": game_server_id,
        "proof_url": f"/media/{proof['key']}",
        "status": "pending",
        "created_at": current_utc()
    }
    inserted = orders_col.insert_one(order_doc)

    caption = (
        f"🛒 <b>[New Store Order Received]</b>\n\n"
        f"👤 <b>User:</b> {user_email}\n"
        f"📦 <b>Item:</b> {product_name} ({variant_name})\n"
        f"💰 <b>Price:</b> {price_mmk:,} MMK\n"
        f"💳 <b>Payment:</b> {payment_provider}\n"
    )
    if game_account_id:
        caption += f"🎮 <b>Game ID:</b> <code>{game_account_id}</code>\n"
    if game_server_id:
        caption += f"🌐 <b>Server ID:</b> <code>{game_server_id}</code>\n"
    caption += f"🕒 <b>Time:</b> {current_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}"

    await send_telegram_notification(caption, photo_path=proof_path)

    create_notification(
        user_email,
        "အော်ဒါတင်ခြင်း အောင်မြင်ပါသည်",
        f"သင်ဝယ်ယူထားသော {product_name} ({variant_name}) အတွက် ငွေလွှဲပြေစာ လက်ခံရရှိပါပြီခင်ဗျာ။ Admin မှ စစ်ဆေးပြီး ပစ္စည်းအမြန်ဆုံး ဖြည့်သွင်းပေးပါမည်။"
    )
    return {"status": "success", "order_id": str(inserted.inserted_id)}

# --- ADMIN CONTROL PANEL ---

@app.get("/api/admin/badges")
async def admin_get_badges(request: Request):
    require_admin(request)
    pending_approvals = transactions_col.count_documents({"status": "pending"})
    unread_messages = messages_col.count_documents({"recipient_email": ADMIN_EMAIL, "is_read": False})
    pending_orders = orders_col.count_documents({"status": "pending"})
    return {
        "pending_approvals": pending_approvals,
        "unread_messages": unread_messages,
        "pending_orders": pending_orders,
        "total_alerts": pending_approvals + unread_messages + pending_orders
    }

@app.get("/api/admin/orders")
async def admin_get_orders(request: Request):
    require_admin(request)
    orders = list(orders_col.find().sort("created_at", DESCENDING))
    return {
        "orders": [{
            "id": str(o["_id"]),
            "user_email": o.get("user_email"),
            "product_name": o.get("product_name"),
            "variant_name": o.get("variant_name"),
            "price_mmk": o.get("price_mmk", 0),
            "payment_provider": o.get("payment_provider"),
            "game_account_id": o.get("game_account_id"),
            "game_server_id": o.get("game_server_id"),
            "proof_url": o.get("proof_url"),
            "status": o.get("status", "pending"),
            "created_at": o.get("created_at").strftime("%Y-%m-%d %H:%M") if o.get("created_at") else ""
        } for o in orders]
    }

@app.post("/api/admin/orders/deliver")
async def admin_deliver_order(
    request: Request,
    order_id: str = Form(...),
    message: str = Form(...),
    file: Optional[UploadFile] = File(None)
):
    require_admin(request)
    order = orders_col.find_one({"_id": ObjectId(order_id)})
    if not order:
        raise HTTPException(status_code=404, detail="Order ရှာမတွေ့ပါ")

    user_email = order["user_email"]
    attachment = None
    if file:
        att = await save_upload(file, "chat_attachments")
        attachment = {"url": f"/media/{att['key']}", "type": att["content_type"], "name": att["name"]}

    doc = {
        "sender_email": ADMIN_EMAIL,
        "recipient_email": user_email,
        "participants": sorted([ADMIN_EMAIL, user_email]),
        "body": f"🎁 [Order Delivered: {order.get('product_name')} - {order.get('variant_name')}]\n\n{message.strip()}",
        "attachment": attachment,
        "created_at": current_utc(),
        "is_read": False
    }
    messages_col.insert_one(doc)
    orders_col.update_one({"_id": ObjectId(order_id)}, {"$set": {"status": "completed"}})
    
    create_notification(
        user_email,
        "ပစ္စည်းပို့ဆောင်ပြီးပါပြီ",
        f"သင်ဝယ်ယူထားသော {order.get('product_name')} အတွက် ပစ္စည်းပို့ဆောင်မှု ရောက်ရှိပါပြီ။ Support Chat တွင် ဝင်ရောက်စစ်ဆေးနိုင်ပါပြီခင်ဗျာ။"
    )
    return {"status": "success"}

@app.get("/api/admin/users")
async def admin_get_users(request: Request):
    require_admin(request)
    all_users = list(users_col.find().sort("created_at", DESCENDING))
    res = []
    for u in all_users:
        u_email = u.get("email", "").strip().lower()
        
        # ဗီဒီယိုစာရင်း (Total, Success, Error)
        total_vids = video_history_col.count_documents({"email": u_email})
        success_vids = video_history_col.count_documents({"email": u_email, "status": {"$ne": "failed"}})
        failed_vids = video_history_col.count_documents({"email": u_email, "status": "failed"})
        
        # စုစုပေါင်း ဝယ်ယူထားသည့် ငွေပမာဏ (Approved Topups)
        approved_txs = list(transactions_col.find({"email": u_email, "status": "approved"}))
        total_spent_mmk = sum(int(t.get("amount", 0)) for t in approved_txs)
        
        # Store Order စာရင်း
        store_orders_count = orders_col.count_documents({"user_email": u_email})

        # TTS အသုံးပြုမှု စာရင်း
        tts_count = u.get("tts_count", 0)

        rem_days = None
        exp_date_str = ""
        if u.get("credits_expire_at") and isinstance(u["credits_expire_at"], datetime):
            exp_date = u["credits_expire_at"]
            if exp_date.tzinfo is None: exp_date = exp_date.replace(tzinfo=timezone.utc)
            diff = exp_date - current_utc()
            rem_days = max(0, diff.days + 1)
            exp_date_str = exp_date.strftime("%Y-%m-%d")

        res.append({
            "email": u_email,
            "name": u.get("name", u_email.split("@")[0]),
            "role": u.get("role", "user"),
            "credits": u.get("credits", 0),
            "credits_expire_at": exp_date_str,
            "remaining_days": rem_days,
            "total_spent_mmk": total_spent_mmk,
            "total_videos": total_vids,
            "success_videos": success_vids,
            "failed_videos": failed_vids,
            "tts_count": tts_count,
            "store_orders_count": store_orders_count,
            "joined_date": u.get("created_at").strftime("%Y-%m-%d %H:%M") if u.get("created_at") else "N/A"
        })
    return {"users": res}

@app.get("/api/admin/chats")
async def admin_get_all_chats(request: Request):
    require_admin(request)
    distinct_senders = messages_col.distinct("sender_email")
    chat_users = [e for e in distinct_senders if e and e != ADMIN_EMAIL]
    
    threads = []
    for u_email in chat_users:
        last_msg = messages_col.find_one(
            {"participants": u_email},
            sort=[("created_at", DESCENDING)]
        )
        threads.append({
            "user_email": u_email,
            "last_message": last_msg.get("body", "") if last_msg else "",
            "last_time": last_msg.get("created_at").strftime("%Y-%m-%d %H:%M") if last_msg and last_msg.get("created_at") else ""
        })
    return {"threads": threads}

@app.get("/api/admin/user-details/{user_email}")
async def admin_get_user_details(request: Request, user_email: str):
    require_admin(request)
    target_email = user_email.strip().lower()
    user = users_col.find_one({"email": target_email})
    if not user:
        raise HTTPException(status_code=404, detail="User ရှာမတွေ့ပါ")
    
    videos = list(video_history_col.find({"email": target_email}).sort("created_at", DESCENDING))
    transactions = list(transactions_col.find({"email": target_email}).sort("created_at", DESCENDING))
    orders = list(orders_col.find({"user_email": target_email}).sort("created_at", DESCENDING))
    
    return {
        "user": {
            "email": user.get("email"),
            "name": user.get("name"),
            "credits": user.get("credits", 0),
            "credits_expire_at": user.get("credits_expire_at").strftime("%Y-%m-%d") if user.get("credits_expire_at") else None,
            "role": user.get("role", "user"),
            "tts_count": user.get("tts_count", 0),
            "joined_date": user.get("created_at").strftime("%Y-%m-%d %H:%M") if user.get("created_at") else "N/A"
        },
        "videos": [{
            "id": str(v["_id"]),
            "title": v.get("title", "Video"),
            "tool": v.get("tool"),
            "status": v.get("status", "completed"),
            "cost": v.get("cost", 0),
            "created_at": v.get("created_at").strftime("%Y-%m-%d %H:%M") if v.get("created_at") else ""
        } for v in videos],
        "transactions": [{
            "id": str(t["_id"]),
            "amount": t.get("amount", 0),
            "status": t.get("status", "pending"),
            "created_at": t.get("created_at").strftime("%Y-%m-%d %H:%M") if t.get("created_at") else ""
        } for t in transactions],
        "orders": [{
            "id": str(o["_id"]),
            "product_name": o.get("product_name"),
            "variant_name": o.get("variant_name"),
            "price_mmk": o.get("price_mmk", 0),
            "status": o.get("status", "pending"),
            "created_at": o.get("created_at").strftime("%Y-%m-%d %H:%M") if o.get("created_at") else ""
        } for o in orders]
    }

@app.post("/api/admin/settings")
async def update_settings(request: Request, data: dict):
    require_admin(request)
    for key, value in data.items():
        settings_col.update_one({"key": key}, {"$set": {"value": str(value)}}, upsert=True)
    return {"status": "success"}

@app.get("/api/admin/revenue-analytics")
async def get_revenue_analytics(
    request: Request,
    period: str = "monthly",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
):
    require_admin(request)
    now = current_utc()
    date_filter = {}

    if period == "monthly":
        date_filter = {"$gte": datetime(now.year, now.month, 1, tzinfo=timezone.utc)}
    elif period == "yearly":
        date_filter = {"$gte": datetime(now.year, 1, 1, tzinfo=timezone.utc)}
    elif period == "custom" and start_date and end_date:
        try:
            s = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            e = datetime.strptime(f"{end_date} 23:59:59", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            date_filter = {"$gte": s, "$lte": e}
        except Exception:
            pass

    tx_query = {"status": "approved"}
    order_query = {"status": {"$in": ["completed", "delivered"]}}
    video_query = {}

    if date_filter and period != "lifetime":
        tx_query["created_at"] = date_filter
        order_query["created_at"] = date_filter
        video_query["created_at"] = date_filter

    tx_list = list(transactions_col.find(tx_query))
    order_list = list(orders_col.find(order_query))
    video_list = list(video_history_col.find(video_query))

    prod_docs = list(products_col.find({}))
    cost_map = {}
    for p in prod_docs:
        for v in p.get("variants", []):
            cost_map[f"{p.get('name')}_{v.get('name')}"] = float(v.get("cost_price_mmk", 0))

    services_summary = {
        "topup": {"name": "Credit Topup Packages", "revenue": 0, "profit": 0, "count": 0},
        "store": {"name": "Digital Store & Games", "revenue": 0, "profit": 0, "count": 0},
        "recap": {"name": "Movie Recap Tool", "credits_consumed": 0, "count": 0},
        "subtitle": {"name": "AI Subtitle Burn-in", "credits_consumed": 0, "count": 0}
    }

    user_sales = {}
    total_credit_revenue = 0
    total_store_revenue = 0
    total_store_cost = 0

    for t in tx_list:
        u_email = t.get("email", "Unknown")
        amt = float(t.get("amount", 0))
        total_credit_revenue += amt
        services_summary["topup"]["revenue"] += amt
        services_summary["topup"]["profit"] += amt
        services_summary["topup"]["count"] += 1

        user_sales.setdefault(u_email, {"revenue": 0, "profit": 0, "topup_count": 0, "orders_count": 0, "items": []})
        user_sales[u_email]["revenue"] += amt
        user_sales[u_email]["profit"] += amt
        user_sales[u_email]["topup_count"] += 1
        
        c_at = t.get("created_at")
        date_str = c_at.strftime("%Y-%m-%d %H:%M") if isinstance(c_at, datetime) else str(c_at or "-")
        user_sales[u_email]["items"].append({
            "service": "Credit Topup",
            "item_name": f"{amt:,.0f} MMK Package",
            "cost_price": 0,
            "sale_price": amt,
            "profit": amt,
            "date": date_str
        })

    for o in order_list:
        u_email = o.get("user_email", "Unknown")
        sale = float(o.get("price_mmk", 0))
        cost = cost_map.get(f"{o.get('product_name')}_{o.get('variant_name')}", 0)
        profit = sale - cost
        total_store_revenue += sale
        total_store_cost += cost

        services_summary["store"]["revenue"] += sale
        services_summary["store"]["profit"] += profit
        services_summary["store"]["count"] += 1

        user_sales.setdefault(u_email, {"revenue": 0, "profit": 0, "topup_count": 0, "orders_count": 0, "items": []})
        user_sales[u_email]["revenue"] += sale
        user_sales[u_email]["profit"] += profit
        user_sales[u_email]["orders_count"] += 1
        
        c_at = o.get("created_at")
        date_str = c_at.strftime("%Y-%m-%d %H:%M") if isinstance(c_at, datetime) else str(c_at or "-")
        user_sales[u_email]["items"].append({
            "service": "Store Product",
            "item_name": f"{o.get('product_name')} ({o.get('variant_name')})",
            "cost_price": cost,
            "sale_price": sale,
            "profit": profit,
            "date": date_str
        })

    for v in video_list:
        tool = v.get("tool", "recap")
        cost_cr = float(v.get("cost", 0))
        if tool == "subtitle":
            services_summary["subtitle"]["count"] += 1
            services_summary["subtitle"]["credits_consumed"] += cost_cr
        else:
            services_summary["recap"]["count"] += 1
            services_summary["recap"]["credits_consumed"] += cost_cr

    real_total_users = users_col.count_documents({})
    real_total_videos = video_history_col.count_documents({})

    total_revenue = total_credit_revenue + total_store_revenue
    total_profit = total_credit_revenue + (total_store_revenue - total_store_cost)

    return {
        "summary": {
            "total_revenue": total_revenue,
            "total_profit": total_profit,
            "total_users": real_total_users,
            "total_videos": real_total_videos
        },
        "services": services_summary,
        "users_breakdown": [
            {"email": k, **v} for k, v in sorted(user_sales.items(), key=lambda x: x[1]["revenue"], reverse=True)
        ]
    }

@app.get("/api/admin/transactions")
async def get_transactions(request: Request):
    require_admin(request)
    return {
        "transactions": [{
            "id": str(d["_id"]), 
            "email": d.get("email", ""), 
            "amount": d.get("amount", 0), 
            "status": d.get("status", "pending"), 
            "proof": f"/media/{d.get('proof')}" if d.get('proof') else ""
        } for d in transactions_col.find({"status": "pending"}).sort("_id", DESCENDING)]
    }

@app.post("/api/admin/approve")
async def approve_transaction(request: Request, data: dict):
    require_admin(request)
    tx_id = data.get('id')
    valid_days = int(data.get('valid_days', 30))

    tx = transactions_col.find_one_and_update(
        {"_id": ObjectId(tx_id)}, 
        {"$set": {"status": "approved"}}, 
        return_document=ReturnDocument.AFTER
    )
    if tx:
        user_email = tx.get("email", "")
        amount = int(tx.get("amount", 0))
        if user_email and amount > 0:
            expire_time = current_utc() + timedelta(days=valid_days)
            users_col.update_one(
                {"email": user_email}, 
                {
                    "$inc": {"credits": amount},
                    "$set": {"credits_expire_at": expire_time}
                }
            )
            exp_date_str = expire_time.strftime('%Y-%m-%d')
            create_notification(
                user_email, 
                "Credit ရောက်ရှိပါပြီ", 
                f"AI Studio မှ Credit ဝယ်ယူမှုအတွက် အထူးကျေးဇူးတင်ရှိပါသည်။ {amount} credits အား သင့်အကောင့်ထဲသို့ ထည့်သွင်းပေးပြီးပါပြီ။ သက်တမ်းကုန်ဆုံးမည့်ရက်စွဲမှာ {exp_date_str} ({valid_days} ရက်) ဖြစ်ပါသည်။"
            )
    return {"status": "success"}

@app.post("/api/admin/add-credit")
async def admin_add_credit(request: Request, data: dict):
    require_admin(request)
    user_email = data.get('email', '').strip().lower()
    amount = int(data.get('amount', 0))
    valid_days = int(data.get('valid_days', 30))

    if user_email and amount > 0:
        expire_time = current_utc() + timedelta(days=valid_days)
        users_col.update_one(
            {"email": user_email}, 
            {
                "$inc": {"credits": amount},
                "$set": {"credits_expire_at": expire_time}
            }
        )
        exp_date_str = expire_time.strftime('%Y-%m-%d')
        create_notification(
            user_email, 
            "Credit ရောက်ရှိပါပြီ", 
            f"AI Studio မှ ဝယ်ယူအားပေးမှုအတွက် အထူးပင်ကျေးဇူးတင်ရှိပါသည်။ {amount} credits ရောက်ရှိပါပြီခင်ဗျာ။ သင့် Credit များသည် {exp_date_str} ရက်နေ့တွင် သက်တမ်းကုန်ဆုံးပါမည်။"
        )
    return {"status": "success"}

@app.get("/api/admin/products")
async def admin_get_products(request: Request):
    require_admin(request)
    prods = list(products_col.find().sort("created_at", DESCENDING))
    return {
        "products": [{
            "id": str(p["_id"]),
            "name": p.get("name", ""),
            "category": p.get("category", ""),
            "description": p.get("description", ""),
            "image_url": f"/media/{p['image_key']}" if p.get("image_key") else (f"/media/{p['image_keys'][0]}" if p.get("image_keys") else None),
            "image_urls": [f"/media/{k}" for k in p.get("image_keys", [])] if p.get("image_keys") else ([f"/media/{p['image_key']}"] if p.get("image_key") else []),
            "variants": p.get("variants", [])
        } for p in prods]
    }

@app.post("/api/admin/products")
async def create_product(
    request: Request,
    name: str = Form(...),
    category: str = Form(...),
    description: str = Form(""),
    variants_json: str = Form(...),
    payment_accounts_json: str = Form("[]"),
    images: List[UploadFile] = File(None)
):
    require_admin(request)
    
    try:
        variants = json.loads(variants_json)
    except Exception:
        variants = []

    try:
        payment_accounts = json.loads(payment_accounts_json)
    except Exception:
        payment_accounts = []

    # variants တစ်ခုချင်းစီထဲရှိ cost_price_mmk (မူရင်းရင်းနှီးဈေး) ကို float အဖြစ် သေချာပြောင်းလဲသိမ်းဆည်းခြင်း
    for v in variants:
        v["cost_price_mmk"] = float(v.get("cost_price_mmk", 0))
        v["price_mmk"] = float(v.get("price_mmk", 0))
        v["price_credits"] = int(v.get("price_credits", 0))
        v["requires_game_id"] = bool(v.get("requires_game_id", False))
        v["requires_server_id"] = bool(v.get("requires_server_id", False))

    # ပုံများ Upload သိမ်းဆည်းခြင်း
    image_urls = []
    if images:
        for img in images:
            if img.filename:
                file_ext = os.path.splitext(img.filename)[1]
                saved_filename = f"prod_{uuid.uuid4().hex[:10]}{file_ext}"
                dest_path = os.path.join("media", saved_filename)
                with open(dest_path, "wb") as f:
                    shutil.copyfileobj(img.file, f)
                image_urls.append(f"/media/{saved_filename}")

    primary_image = image_urls[0] if image_urls else ""

    doc = {
        "name": name.strip(),
        "category": category.strip(),
        "description": description.strip(),
        "variants": variants,
        "payment_accounts": payment_accounts,
        "image_url": primary_image,
        "image_urls": image_urls,
        "created_at": datetime.utcnow()
    }
    
    res = products_col.insert_one(doc)
    return {"message": "Product created successfully", "id": str(res.inserted_id)}

@app.delete("/api/admin/products/{product_id}")
async def admin_delete_product(request: Request, product_id: str):
    require_admin(request)
    prod = products_col.find_one({"_id": ObjectId(product_id)})
    if prod:
        for k in prod.get("image_keys", []):
            delete_media(k)
        if prod.get("image_key"):
            delete_media(prod["image_key"])
    products_col.delete_one({"_id": ObjectId(product_id)})
    return {"status": "success"}

# --- RICH MEDIA MESSAGING ---

@app.get("/api/messages")
async def get_messages(request: Request, peer: Optional[str] = None):
    email = get_request_email(request)
    target = peer if peer else (ADMIN_EMAIL if email != ADMIN_EMAIL else "")
    query = {"participants": {"$all": [email, target]}} if target else {"participants": email}
    docs = list(messages_col.find(query).sort("created_at", ASCENDING))
    
    # Admin က User ဆီက စာများကို ဖွင့်ဖတ်လိုက်သည်နှင့် unread ကို read အဖြစ် ပြောင်းပေးခြင်း
    if email == ADMIN_EMAIL and target:
        messages_col.update_many(
            {"sender_email": target, "recipient_email": ADMIN_EMAIL, "is_read": False},
            {"$set": {"is_read": True}}
        )
    elif email != ADMIN_EMAIL:
        messages_col.update_many(
            {"sender_email": ADMIN_EMAIL, "recipient_email": email, "is_read": False},
            {"$set": {"is_read": True}}
        )

    return {
        "messages": [{
            "id": str(d["_id"]), 
            "sender_email": d.get("sender_email"), 
            "body": d.get("body", ""), 
            "attachment": d.get("attachment"), 
            "created_at": d.get("created_at").isoformat() if d.get("created_at") else ""
        } for d in docs]
    }

@app.post("/api/messages")
async def send_message(
    request: Request, 
    recipient_email: str = Form(""), 
    body: str = Form(""), 
    file: Optional[UploadFile] = File(None)
):
    sender = get_request_email(request)
    recipient = recipient_email.strip().lower() if recipient_email else ADMIN_EMAIL
    attachment = None
    if file:
        att = await save_upload(file, "chat_attachments")
        attachment = {
            "url": f"/media/{att['key']}", 
            "type": att["content_type"], 
            "name": att["name"]
        }
    
    doc = {
        "sender_email": sender,
        "recipient_email": recipient,
        "participants": sorted([sender, recipient]),
        "body": body.strip(),
        "attachment": attachment,
        "created_at": current_utc(),
        "is_read": False
    }
    messages_col.insert_one(doc)
    return {"status": "sent"}

# --- AI & TTS PROXY (WITH STRICT DEDUCTION & USAGE TRACKING) ---

@app.post("/api/tts")
async def edge_tts_api(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        text = data.get("text", "").strip()
        voice = data.get("voice", "my-MM-ThihaNeural")
        rate = data.get("rate", "+10%")
        
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Login ဝင်ရောက်ပေးပါ")

        payload = _decode_session(auth[7:].strip())
        if not payload or not payload.get("email"):
            raise HTTPException(status_code=401, detail="Session သက်တမ်းကုန်ဆုံးသွားပါသည်")

        u_email = str(payload["email"]).strip().lower()
        tts_mode_setting = settings_col.find_one({"key": "tts_mode"})
        tts_mode = tts_mode_setting.get("value", "ads") if tts_mode_setting else "ads"
        
        if tts_mode == "credits" and u_email != ADMIN_EMAIL:
            char_rate_setting = settings_col.find_one({"key": "tts_chars_per_credit"})
            chars_per_credit = int(char_rate_setting.get("value", "100")) if char_rate_setting else 100
            cost = max(1, math.ceil(len(text) / chars_per_credit))
            
            user = users_col.find_one({"email": u_email})
            if not user or user.get("credits", 0) < cost:
                raise HTTPException(
                    status_code=402, 
                    detail=f"Credit မလုံလောက်ပါ။ စာလုံးရေ ({len(text)}) အတွက် {cost} Credits လိုအပ်ပါသည်။"
                )
            users_col.update_one({"email": u_email}, {"$inc": {"credits": -cost}})

        # TTS အသုံးပြုမှု အကြိမ်ရေ မှတ်သားခြင်း
        users_col.update_one({"email": u_email}, {"$inc": {"tts_count": 1}})

        communicate = edge_tts.Communicate(text, voice, rate=rate)
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp_file.close()
        await communicate.save(tmp_file.name)
        background_tasks.add_task(os.remove, tmp_file.name)
        return FileResponse(tmp_file.name, media_type="audio/mpeg")
    except HTTPException:
        raise
    except Exception as e: 
        return JSONResponse(status_code=500, content={"error": f"Edge TTS Error: {str(e)}"})

@app.api_route("/api/gemini/{path:path}", methods=["GET", "POST"])
async def proxy_gemini(path: str, request: Request):
    query_params = dict(request.query_params)
    client_key = query_params.get("key", "").strip()
    keys_to_try = []
    if client_key: keys_to_try.append(client_key)
    for _ in range(len(SERVER_GEMINI_KEYS)):
        k = get_server_gemini_key()
        if k and k not in keys_to_try: keys_to_try.append(k)

    body_bytes = await request.body()
    last_res = None

    async with httpx.AsyncClient(timeout=120.0) as client_http:
        for key in (keys_to_try or [""]):
            try:
                q = dict(query_params)
                if key: q["key"] = key
                url = f"https://generativelanguage.googleapis.com/{path}"
                res = await client_http.request(request.method, url, params=q, headers={"Content-Type": "application/json"}, content=body_bytes)
                last_res = res
                if res.status_code == 200:
                    return Response(content=res.content, status_code=200, media_type="application/json")
            except Exception: 
                continue

    if last_res is not None:
        return Response(content=last_res.content, status_code=last_res.status_code, media_type="application/json")
    return JSONResponse(status_code=500, content={"error": "Server Gemini API Error"})

@app.post("/api/groq/transcriptions")
async def proxy_groq(request: Request):
    try:
        form = await request.form()
        file = form.get("file")
        if not file: 
            return JSONResponse(status_code=400, content={"error": "အသံဖိုင် မပါရှိပါ"})
            
        file_bytes = await file.read()
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        
        custom_groq_key = request.headers.get("x-groq-key", "").strip()
        keys_to_try = []
        if custom_groq_key: 
            keys_to_try.append(custom_groq_key)
        for k in DEFAULT_GROQ_KEYS:
            if k and k not in keys_to_try: 
                keys_to_try.append(k)

        if not keys_to_try:
            return JSONResponse(status_code=400, content={"error": "Groq API Key ထည့်သွင်းထားခြင်း မရှိပါ"})

        last_error_detail = "Keys မအောင်မြင်ပါ"
        async with httpx.AsyncClient(timeout=120.0) as client_http:
            for key in keys_to_try:
                try:
                    res = await client_http.post(
                        url, 
                        headers={"Authorization": f"Bearer {key.strip()}"}, 
                        files={"file": (file.filename or "audio.wav", file_bytes, "audio/wav")}, 
                        data={"model": "whisper-large-v3-turbo", "response_format": "verbose_json"}
                    )
                    if res.status_code == 200: 
                        return Response(content=res.content, status_code=200, media_type="application/json")
                    else:
                        err_json = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
                        last_error_detail = err_json.get("error", {}).get("message", res.text)
                except Exception as e:
                    last_error_detail = str(e)
                    continue

        return JSONResponse(status_code=502, content={"error": f"Groq Error: {last_error_detail}"})
    except Exception as e: 
        return JSONResponse(status_code=500, content={"error": str(e)})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)