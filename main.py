import static_ffmpeg
static_ffmpeg.add_paths()
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
import random
import threading
import glob
import yt_dlp
from datetime import datetime, timedelta, timezone
from typing import Optional, Any, List
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import unquote

from fastapi import FastAPI, Request, BackgroundTasks, UploadFile, File, Form, Response, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from bson.objectid import ObjectId
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

GOOGLE_CLIENT_ID = "135328538466-76vbcm81m07i03cqc105d5rrrt3967t4.apps.googleusercontent.com"

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

# Telegram Integration (.env ထဲမှသာ ဆွဲယူမည်)
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

# Groq Keys
raw_groq_env = os.getenv("GROQ_API_KEY") or os.getenv("GROQ_API_KEYS") or ""
DEFAULT_GROQ_KEYS = [k.strip() for k in raw_groq_env.split(",") if k.strip()]

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
        ]),
        "voice_clone_enabled": "false",
        "voice_clone_colab_urls": json.dumps(["https://your-ngrok-url.ngrok-free.app"]),
        "voice_clone_recap_rate_per_min": "80",
        "voice_clone_tts_mode": "credits",
        "voice_clone_tts_chars_per_credit": "50",
        "ads_animator_rate_per_sec": "2",
        "social_kit_rate_per_use": "10",
        "ads_animator_free_revisions": "1",
        "tool_status_downloader": "true",
        "tool_status_animator": "true",
        "tool_status_recap": "true",
        "tool_status_subtitle": "true",
        "tool_status_tts": "true",
        "tool_status_store": "true",
        "server_key_rate_per_min": "50",
        "server_key_first_two_rate": "30",
        "server_key_discount_enabled": "true",
        "own_key_rate_per_min": "20",
        "own_key_first_two_rate": "15",
        "own_key_discount_enabled": "true",
        "primary_provider": "codecraft",
        "primary_base_url": "https://codecraftapi.com/v1",
        "primary_model": "gpt-4o-mini",
        "primary_api_keys": json.dumps([]),
        "secondary_provider": "chat_b_ai",
        "secondary_base_url": "https://chat.b.ai/v1",
        "secondary_model": "gpt-4o",
        "secondary_api_keys": json.dumps([]),
        "groq_api_keys_pool": json.dumps([])
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
    
    try:
        video_history_col.drop_index("expires_at_1")
    except Exception:
        pass
    video_history_col.create_index([("email", ASCENDING)])

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
    # ဖိုင်မဖျက်ရသေးသော သက်တမ်းကုန် ဗီဒီယိုများကိုသာ ရှာဖွေခြင်း
    expired_videos = list(video_history_col.find({
        "expires_at": {"$lte": now},
        "status": {"$ne": "expired"}
    }))
    
    for item in expired_videos:
        u_email = item.get("email")
        cost = int(item.get("cost", 0))
        downloaded = item.get("downloaded", False)
        created_time_str = item.get("created_at").strftime("%Y-%m-%d %H:%M") if item.get("created_at") else "ယခင်"

        # Download မဆွဲခဲ့ပါက Credit ပြန်အမ်းခြင်း
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
            
        # Storage မပြည့်စေရန် Media ဖိုင်ကိုသာ Disk ပေါ်မှ ဖျက်ခြင်း
        delete_media(item.get("media_key"))
        
        # Database Record ကို လုံးဝမဖျက်ဘဲ status ကို expired ဟုသာ မှတ်သားထားခြင်း (Stats မပျောက်စေရန်)
        video_history_col.update_one(
            {"_id": item["_id"]},
            {"$set": {"status": "expired", "media_key": None}}
        )

    # သက်တမ်းကုန်သွားသော User Credits များကို စစ်ဆေးရှင်းလင်းခြင်း
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
    token = ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif request.query_params.get("token"):
        token = request.query_params.get("token").strip()

    if token:
        payload = _decode_session(token)
        if payload and payload.get("email"):
            return str(payload["email"]).strip().lower()

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
    response = FileResponse(path)
    response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

def normalize_chat_url(base_url: str) -> str:
    url = str(base_url).strip().rstrip("/")
    if "chat.b.ai" in url:
        url = url.replace("chat.b.ai", "api.b.ai")
    
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"

async def call_universal_ai(messages: list, temperature: float = 0.7) -> str:
    settings = {doc["key"]: doc["value"] for doc in settings_col.find()}
    
    p_base = str(settings.get("primary_base_url") or "").strip()
    p_model = str(settings.get("primary_model") or "").strip()
    p_keys = parse_settings_keys(settings.get("primary_api_keys", "[]"))

    s_base = str(settings.get("secondary_base_url") or "").strip()
    s_model = str(settings.get("secondary_model") or "").strip()
    s_keys = parse_settings_keys(settings.get("secondary_api_keys", "[]"))

    async def execute_request(provider_tag: str, base_url: str, model: str, key: str):
        url = normalize_chat_url(base_url)
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            "Connection": "close"
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature
        }
        
        print(f"[{provider_tag}] ချိတ်ဆက်နေသည်: {url} | Model: {model}")
        
        async with httpx.AsyncClient(timeout=120.0, http1=True) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                print(f"[{provider_tag} Success] အောင်မြင်စွာ အဖြေရရှိပါပြီ!")
                return data["choices"][0]["message"]["content"]
            raise Exception(f"HTTP {resp.status_code}: {resp.text}")

    # ၁။ Primary API ကို အဓိက အရင်ဆုံး RUN မည်
    if p_base and p_model and p_keys:
        for idx, key in enumerate(p_keys):
            if not key.strip(): continue
            try:
                return await execute_request("Primary API (Main)", p_base, p_model, key.strip())
            except Exception as e:
                print(f"[Primary Key {idx+1} Error]: {str(e)} -> နောက် Key သို့ ကူးမည်")
                continue

    # ၂။ Primary မရတော့မှ Secondary API သို့ ကူးမည်
    if s_base and s_model and s_keys:
        print("[Universal AI Warning]: Primary API မရသဖြင့် Secondary API သို့ ကူးပြောင်းနေပါသည်...")
        for idx, key in enumerate(s_keys):
            if not key.strip(): continue
            try:
                return await execute_request("Secondary API", s_base, s_model, key.strip())
            except Exception as e:
                print(f"[Secondary Key {idx+1} Error]: {str(e)} -> နောက် Key သို့ ကူးမည်")
                continue

    # ၃။ နှစ်ခုစလုံး Error တက်မှသာ အရန် Fallback API သုံးမည်
    print("[Universal AI Alert]: Primary ရော Secondary ပါ မရတော့သဖြင့် အရန် Fallback API သို့ ကူးပြောင်းပါသည်...")
    
    # Fallback A: Groq Key များဖြင့် ကြိုးစားခြင်း
    groq_keys = DEFAULT_GROQ_KEYS + get_groq_pool_keys()
    if groq_keys:
        for g_key in groq_keys:
            try:
                return await execute_request("Fallback (Groq Llama)", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", g_key)
            except Exception as e:
                print(f"[Groq Fallback Error]: {str(e)}")
                continue

    # Fallback B: Server Gemini Key ဖြင့် ကြိုးစားခြင်း
    gemini_key = get_server_gemini_key()
    if gemini_key:
        try:
            combined_text = "\n\n".join([m.get("content", "") for m in messages if m.get("content")])
            gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
            payload = {
                "contents": [{"parts": [{"text": combined_text}]}],
                "generationConfig": {"temperature": temperature}
            }
            async with httpx.AsyncClient(timeout=60.0) as client_http:
                resp = await client_http.post(gemini_url, json=payload, headers={"Content-Type": "application/json"})
                if resp.status_code == 200:
                    res_data = resp.json()
                    return res_data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"[Gemini Fallback Error]: {str(e)}")

    raise HTTPException(status_code=500, detail="Primary၊ Secondary နှင့် Fallback API Keys များ အားလုံး အသုံးပြု၍ မရတော့ပါခင်ဗျာ။")
# User ထံ မပေါက်ကြားသင့်သော လျှို့ဝှက်ချက်များနှင့် Provider အချက်အလက်များ အားလုံး
SECRET_SETTINGS_KEYS = {
    "primary_api_keys",
    "secondary_api_keys",
    "groq_api_keys_pool",
    "voice_clone_colab_urls",
    "primary_base_url",
    "secondary_base_url",
    "primary_provider",
    "secondary_provider",
    "primary_model",
    "secondary_model"
}


@app.post("/api/login")
async def login(request: Request, data: dict = {}):
    credential = data.get("credential")
    email = None
    name = None
    picture = None

    if credential:
        id_info = None

        # နည်းလမ်း ၁: google-auth library ဖြင့် စစ်ဆေးခြင်း (Clock Skew 60s ခွင့်ပြုထားသည်)
        try:
            id_info = id_token.verify_oauth2_token(
                credential, 
                google_requests.Request(), 
                GOOGLE_CLIENT_ID,
                clock_skew_in_seconds=60
            )
        except Exception as e1:
            print(f"[google-auth warning]: {e1} -> Google tokeninfo API ဖြင့် အရန်စစ်ဆေးပါမည်")

        # နည်းလမ်း ၂: Library အဆင်မပြေပါက Google ၏ တရားဝင် tokeninfo API ဖြင့် တိုက်ရိုက် စစ်ဆေးခြင်း
        if not id_info:
            try:
                async with httpx.AsyncClient(timeout=15.0) as http_client:
                    t_resp = await http_client.get(
                        f"https://oauth2.googleapis.com/tokeninfo?id_token={credential}"
                    )
                    if t_resp.status_code == 200:
                        t_data = t_resp.json()
                        # Audience (Client ID) အမှန်တကယ် ကိုက်ညီမှု ရှိ/မရှိ စစ်ဆေးခြင်း
                        if t_data.get("aud") == GOOGLE_CLIENT_ID:
                            id_info = t_data
                        else:
                            print(f"[Google Auth Mismatch]: aud ({t_data.get('aud')}) != CLIENT_ID")
                    else:
                        print(f"[Google tokeninfo HTTP Error]: {t_resp.status_code} - {t_resp.text}")
            except Exception as e2:
                print(f"[tokeninfo network error]: {e2}")

        # စစ်ဆေးမှု နှစ်ခုစလုံး မအောင်မြင်မှသာ ပယ်ချမည်
        if not id_info:
            raise HTTPException(status_code=401, detail="တရားမဝင်သော Google Token ဖြစ်ပါသည်")

        email = id_info.get("email", "").strip().lower()
        name = id_info.get("name") or email.split("@")[0]
        picture = id_info.get("picture") or ""
    else:
        # User Profile Refresh ပြုလုပ်ချိန်တွင် Session Token စစ်ဆေးခြင်း
        try:
            email = get_request_email(request)
        except Exception:
            raise HTTPException(status_code=401, detail="Google Credential သို့မဟုတ် Valid Session Token လိုအပ်ပါသည်")

    user = users_col.find_one({"email": email})
    role = "admin" if email == ADMIN_EMAIL else "user"
    
    if not user:
        user = { 
            "email": email, 
            "name": name or email.split("@")[0], 
            "picture": picture or "", 
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

    all_settings = {doc["key"]: doc["value"] for doc in settings_col.find()}

    for t_key in ["tool_status_downloader", "tool_status_animator", "tool_status_recap", "tool_status_subtitle", "tool_status_tts", "tool_status_store"]:
        if t_key not in all_settings:
            all_settings[t_key] = "true"

    is_admin = (email == ADMIN_EMAIL or user.get("role") == "admin")
    if is_admin:
        client_settings = all_settings
    else:
        client_settings = {k: v for k, v in all_settings.items() if k not in SECRET_SETTINGS_KEYS}

    if "_id" in user: del user["_id"]
    # ယနေ့အတွက် အသုံးပြုပြီးသော Free မိနစ်များနှင့် လက်ကျန် Free မိနစ် တွက်ချက်ခြင်း
    today_str = current_utc().strftime("%Y-%m-%d")
    free_quota = int(all_settings.get("free_minutes_per_day", "0"))
    if user.get("free_mins_date") != today_str:
        free_mins_used = 0
    else:
        free_mins_used = int(user.get("free_mins_used", 0))
    free_mins_remaining = max(0, free_quota - free_mins_used)

    user["free_mins_used"] = free_mins_used
    user["free_mins_remaining"] = free_mins_remaining
    user["free_mins_date"] = today_str
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
        "settings": client_settings, 
        "token": _encode_session(email, user.get("role", "user")) 
    }

@app.post("/api/buy-credits")
async def buy_credits(email: str = Form(...), amount: int = Form(...), file: UploadFile = File(...)):
    u_email = email.strip().lower()
    buy_amount = int(amount)
    
    proof = await save_upload(file, "payment-proofs/credits")
    proof_path = (MEDIA_ROOT / proof["key"]).resolve()

    transactions_col.insert_one({ 
        "type": "credit_topup", 
        "email": u_email, 
        "amount": buy_amount, 
        "status": "pending", 
        "proof": proof["key"], 
        "created_at": current_utc() 
    })

    create_notification(
        u_email, 
        "ငွေလွှဲပြေစာ လက်ခံရရှိပါသည်", 
        "AI Studio မှ Credit ဝယ်ယူမှုအတွက် ကျေးဇူးတင်ရှိပါသည်။ Admin မှ ငွေလွှဲပြေစာအား အမြန်ဆုံး အတည်ပြုပေးပါမည်ခင်ဗျာ။"
    )

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

def calculate_video_cost(email: str, dur_sec: float, is_voice_clone: bool = False, key_mode: str = "server") -> tuple[int, bool, int]:
    full_mins = int(dur_sec // 60)
    rem_sec = dur_sec % 60
    # ၃၀ စက္ကန့် စည်းမျဉ်းအတိုင်း မိနစ် အတိုး/အလျော့ တွက်ချက်ခြင်း
    dur_mins = max(1, full_mins + 1 if rem_sec > 30 else full_mins)

    if is_voice_clone:
        rate_setting = settings_col.find_one({"key": "voice_clone_recap_rate_per_min"})
        base_rate = int(rate_setting.get("value", "80")) if rate_setting else 80
        cost = dur_mins * base_rate
        return cost, False, 0

    if key_mode == "own":
        rate_setting = settings_col.find_one({"key": "own_key_rate_per_min"})
        base_rate = int(rate_setting.get("value", "20")) if rate_setting else 20
        disc_setting = settings_col.find_one({"key": "own_key_discount_enabled"})
        disc_enabled = (disc_setting.get("value", "true") == "true") if disc_setting else True
        first_two_setting = settings_col.find_one({"key": "own_key_first_two_rate"})
        first_two_rate = int(first_two_setting.get("value", "15")) if first_two_setting else 15
    else:
        rate_setting = settings_col.find_one({"key": "server_key_rate_per_min"}) or settings_col.find_one({"key": "deduction_rate_per_min"})
        base_rate = int(rate_setting.get("value", "50")) if rate_setting else 50
        disc_setting = settings_col.find_one({"key": "server_key_discount_enabled"})
        disc_enabled = (disc_setting.get("value", "true") == "true") if disc_setting else True
        first_two_setting = settings_col.find_one({"key": "server_key_first_two_rate"}) or settings_col.find_one({"key": "first_two_min_rate"})
        first_two_rate = int(first_two_setting.get("value", "30")) if first_two_setting else 30

    # User ၏ ယနေ့ အသုံးပြုထားသော Free မိနစ်များကို စစ်ဆေးခြင်း
    today_str = current_utc().strftime("%Y-%m-%d")
    user = users_col.find_one({"email": email}) or {}
    
    free_setting = settings_col.find_one({"key": "free_minutes_per_day"})
    daily_quota = int(free_setting.get("value", "0")) if free_setting else 0

    used_today = int(user.get("free_mins_used", 0)) if user.get("free_mins_date") == today_str else 0
    available_free = max(0, daily_quota - used_today)

    # ဤဗီဒီယိုအတွက် အသုံးပြုခွင့်ရမည့် Free မိနစ်
    free_to_use = min(dur_mins, available_free)
    chargeable_mins = dur_mins - free_to_use

    # ၁။ Free မိနစ် အပြည့်အဝ ရရှိပါက (လုံးဝ အခမဲ့)
    if free_to_use > 0 and chargeable_mins == 0:
        return 0, True, free_to_use

    # ၂။ Free မိနစ်အချို့ရပြီး ကျန်မိနစ်များ ကျန်ရှိပါက (ပိုသောမိနစ်ကို လျှော့ဈေးမပါဘဲ ပုံမှန် Base Rate ဖြင့် ဖြတ်မည်)
    if free_to_use > 0 and chargeable_mins > 0:
        cost = chargeable_mins * base_rate
        return cost, False, free_to_use

    # ၃။ Free မိနစ် လုံးဝမရတော့ပါက (သို့မဟုတ် ပိတ်ထားပါက) ပထမ ၂ မိနစ် လျှော့ဈေးဖြင့် တွက်မည်
    if disc_enabled and first_two_rate > 0:
        if chargeable_mins <= 2:
            cost = first_two_rate
        else:
            cost = first_two_rate + ((chargeable_mins - 2) * base_rate)
    else:
        cost = chargeable_mins * base_rate

    return cost, False, 0

@app.post("/api/video/pre-deduct")
async def pre_deduct_video_cost(request: Request, data: dict):
    email = get_request_email(request)
    dur_sec = float(data.get("duration", 60))
    is_clone = bool(data.get("is_voice_clone", False))
    key_mode = str(data.get("key_mode", "server")).strip().lower()
    
    cost, is_free, free_consumed = calculate_video_cost(email, dur_sec, is_voice_clone=is_clone, key_mode=key_mode)

    user = users_col.find_one({"email": email})
    if email != ADMIN_EMAIL and not is_free:
        if not user or user.get("credits", 0) < cost:
            raise HTTPException(
                status_code=402, 
                detail=f"Credit မလုံလောက်ပါ။ ဗီဒီယိုအတွက် {cost} Credits လိုအပ်ပါသည်။"
            )
        users_col.update_one({"email": email}, {"$inc": {"credits": -cost}})

    # အသုံးပြုလိုက်သော Free မိနစ်ကို ယနေ့စာရင်းတွင် တိုးမြှင့်မှတ်သားခြင်း
    if free_consumed > 0 and email != ADMIN_EMAIL:
        today_str = current_utc().strftime("%Y-%m-%d")
        if user.get("free_mins_date") == today_str:
            users_col.update_one({"email": email}, {"$inc": {"free_mins_used": free_consumed}})
        else:
            users_col.update_one({"email": email}, {"$set": {"free_mins_date": today_str, "free_mins_used": free_consumed}})

    return {
        "status": "success", 
        "deducted": cost if email != ADMIN_EMAIL else 0, 
        "is_free": is_free, 
        "free_consumed": free_consumed
    }

@app.post("/api/video/adjust-fallback-rate")
async def adjust_fallback_rate(request: Request, data: dict):
    email = get_request_email(request)
    dur_sec = float(data.get("duration", 60))
    is_clone = bool(data.get("is_voice_clone", False))

    server_cost, _, _ = calculate_video_cost(email, dur_sec, is_voice_clone=is_clone, key_mode="server")
    own_cost, _, _ = calculate_video_cost(email, dur_sec, is_voice_clone=is_clone, key_mode="own")

    refund_diff = max(0, server_cost - own_cost)
    if email != ADMIN_EMAIL and refund_diff > 0:
        users_col.update_one({"email": email}, {"$inc": {"credits": refund_diff}})
    return {"status": "success", "refunded": refund_diff, "actual_cost": own_cost}

@app.post("/api/video/refund-failed-pre-deduct")
async def refund_failed_pre_deduct(request: Request, data: dict):
    email = get_request_email(request)
    dur_sec = float(data.get("duration", 60))
    is_clone = bool(data.get("is_voice_clone", False))
    key_mode = str(data.get("key_mode", "server")).strip().lower()

    cost, is_free, free_consumed = calculate_video_cost(email, dur_sec, is_voice_clone=is_clone, key_mode=key_mode)
    
    if email != ADMIN_EMAIL:
        if cost > 0 and not is_free:
            users_col.update_one({"email": email}, {"$inc": {"credits": cost}})
        # ဗီဒီယို ပျက်စီးသွားပါက Free မိနစ်ကိုလည်း ပြန်အမ်းပေးခြင်း
        if free_consumed > 0:
            today_str = current_utc().strftime("%Y-%m-%d")
            user = users_col.find_one({"email": email})
            if user and user.get("free_mins_date") == today_str:
                users_col.update_one({"email": email}, {"$inc": {"free_mins_used": -free_consumed}})

    return {"status": "success", "refunded": cost}

@app.post("/api/video/save-client-rendered")
async def save_client_rendered(
    request: Request, 
    file: UploadFile = File(...), 
    duration: str = Form("60"), 
    tool: str = Form("recap"),
    key_mode: str = Form("server")
):
    email = get_request_email(request)
    dur_sec = float(duration)
    cost, is_free, _ = calculate_video_cost(email, dur_sec, key_mode=key_mode)

    upload_res = await save_upload(file, "rendered_videos")
    final_key = upload_res["key"]

    video_doc = {
        "email": email,
        "title": f"{'Recap' if tool=='recap' else 'Subtitle'} Video ({datetime.now().strftime('%d/%m %H:%M')})",
        "tool": tool,
        "duration": duration,
        "key_mode": key_mode,
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

@app.get("/api/products")
async def get_products():
    prods = list(products_col.find().sort("created_at", DESCENDING))
    safe_products = []
    
    for p in prods:
        safe_variants = []
        for v in p.get("variants", []):
            safe_variants.append({
                "name": v.get("name", "Standard"),
                "price_mmk": float(v.get("price_mmk", 0)),
                "price_credits": int(v.get("price_credits", 0)),
                "requires_game_id": bool(v.get("requires_game_id", False)),
                "requires_server_id": bool(v.get("requires_server_id", False))
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

@app.get("/api/admin/stats")
async def admin_get_stats(request: Request):
    require_admin(request)
    now = current_utc()
    total_users = users_col.count_documents({})
    
    start_of_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    start_of_year = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    
    monthly_txs = list(transactions_col.find({"status": "approved", "created_at": {"$gte": start_of_month}}))
    monthly_profit = sum(int(t.get("amount", 0)) for t in monthly_txs)
    
    yearly_txs = list(transactions_col.find({"status": "approved", "created_at": {"$gte": start_of_year}}))
    yearly_profit = sum(int(t.get("amount", 0)) for t in yearly_txs)
    
    return {
        "total_users": total_users,
        "monthly_profit": monthly_profit,
        "yearly_profit": yearly_profit
    }

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
        
        total_vids = video_history_col.count_documents({"email": u_email})
        success_vids = video_history_col.count_documents({"email": u_email, "status": {"$in": ["completed", "expired"]}})
        failed_vids = video_history_col.count_documents({"email": u_email, "status": "failed"})
        
        approved_txs = list(transactions_col.find({"email": u_email, "status": "approved"}))
        total_spent_mmk = sum(int(t.get("amount", 0)) for t in approved_txs)
        
        store_orders_count = orders_col.count_documents({"user_email": u_email})
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
            "image_url": p.get("image_url") or (f"/media/{p['image_key']}" if p.get("image_key") else (f"/media/{p['image_keys'][0]}" if p.get("image_keys") else None)),
            "image_urls": p.get("image_urls") or ([f"/media/{k}" for k in p.get("image_keys", [])] if p.get("image_keys") else ([f"/media/{p['image_key']}"] if p.get("image_key") else [])),
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
    try: variants = json.loads(variants_json)
    except Exception: variants = []

    try: payment_accounts = json.loads(payment_accounts_json)
    except Exception: payment_accounts = []

    for v in variants:
        v["cost_price_mmk"] = float(v.get("cost_price_mmk", 0))
        v["price_mmk"] = float(v.get("price_mmk", 0))
        v["price_credits"] = int(v.get("price_credits", 0))
        v["requires_game_id"] = bool(v.get("requires_game_id", False))
        v["requires_server_id"] = bool(v.get("requires_server_id", False))

    image_keys = []
    image_urls = []
    if images:
        for img in images:
            if img.filename:
                upload_res = await save_upload(img, "product_images")
                image_keys.append(upload_res["key"])
                image_urls.append(f"/media/{upload_res['key']}")

    primary_image = image_urls[0] if image_urls else ""
    primary_key = image_keys[0] if image_keys else ""

    doc = {
        "name": name.strip(),
        "category": category.strip(),
        "description": description.strip(),
        "variants": variants,
        "payment_accounts": payment_accounts,
        "image_key": primary_key,
        "image_keys": image_keys,
        "image_url": primary_image,
        "image_urls": image_urls,
        "created_at": current_utc()
    }
    res = products_col.insert_one(doc)
    return {"message": "Product created successfully", "id": str(res.inserted_id)}

@app.put("/api/admin/products/{product_id}")
async def update_product(
    request: Request,
    product_id: str,
    name: str = Form(...),
    category: str = Form(...),
    description: str = Form(""),
    variants_json: str = Form(...),
    payment_accounts_json: str = Form("[]"),
    images: List[UploadFile] = File(None)
):
    require_admin(request)
    try: variants = json.loads(variants_json)
    except Exception: variants = []

    try: payment_accounts = json.loads(payment_accounts_json)
    except Exception: payment_accounts = []

    for v in variants:
        v["cost_price_mmk"] = float(v.get("cost_price_mmk", 0))
        v["price_mmk"] = float(v.get("price_mmk", 0))
        v["price_credits"] = int(v.get("price_credits", 0))
        v["requires_game_id"] = bool(v.get("requires_game_id", False))
        v["requires_server_id"] = bool(v.get("requires_server_id", False))

    update_doc = {
        "name": name.strip(),
        "category": category.strip(),
        "description": description.strip(),
        "variants": variants,
        "payment_accounts": payment_accounts
    }

    if images and len(images) > 0 and images[0].filename:
        image_keys = []
        image_urls = []
        for img in images:
            if img.filename:
                upload_res = await save_upload(img, "product_images")
                image_keys.append(upload_res["key"])
                image_urls.append(f"/media/{upload_res['key']}")
        update_doc["image_key"] = image_keys[0]
        update_doc["image_keys"] = image_keys
        update_doc["image_url"] = image_urls[0]
        update_doc["image_urls"] = image_urls

    products_col.update_one({"_id": ObjectId(product_id)}, {"$set": update_doc})
    return {"status": "success", "message": "Product updated successfully"}

@app.delete("/api/admin/products/{product_id}")
async def admin_delete_product(request: Request, product_id: str):
    require_admin(request)
    products_col.delete_one({"_id": ObjectId(product_id)})
    return {"status": "success"}

@app.get("/api/messages")
async def get_messages(request: Request, peer: Optional[str] = None):
    email = get_request_email(request)
    target = peer if peer else (ADMIN_EMAIL if email != ADMIN_EMAIL else "")
    query = {"participants": {"$all": [email, target]}} if target else {"participants": email}
    docs = list(messages_col.find(query).sort("created_at", ASCENDING))
    
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

@app.post("/api/voice-clone/tts")
async def voice_clone_proxy(
    request: Request,
    background_tasks: BackgroundTasks,
    text: str = Form(...),
    reference_audio: UploadFile = File(...)
):
    email = get_request_email(request)
    enabled_setting = settings_col.find_one({"key": "voice_clone_enabled"})
    if not enabled_setting or enabled_setting.get("value") != "true":
        raise HTTPException(status_code=403, detail="Voice Clone စနစ်အား Admin မှ ခေတ္တပိတ်ထားပါသည်")

    urls_setting = settings_col.find_one({"key": "voice_clone_colab_urls"})
    colab_urls = json.loads(urls_setting.get("value", "[]")) if urls_setting else []
    if not colab_urls:
        raise HTTPException(status_code=503, detail="ချိတ်ဆက်ထားသော Colab Server မရှိသေးပါ")

    tts_mode_setting = settings_col.find_one({"key": "voice_clone_tts_mode"})
    tts_mode = tts_mode_setting.get("value", "credits") if tts_mode_setting else "credits"
    cost = 0
    if tts_mode == "credits" and email != ADMIN_EMAIL:
        rate_setting = settings_col.find_one({"key": "voice_clone_tts_chars_per_credit"})
        chars_per_credit = int(rate_setting.get("value", "50")) if rate_setting else 50
        cost = max(1, math.ceil(len(text) / chars_per_credit))
        user = users_col.find_one({"email": email})
        if not user or user.get("credits", 0) < cost:
            raise HTTPException(status_code=402, detail=f"Credit မလုံလောက်ပါ။ Voice Clone အတွက် {cost} Credits လိုအပ်ပါသည်")
        users_col.update_one({"email": email}, {"$inc": {"credits": -cost}})

    audio_bytes = await reference_audio.read()
    last_error = ""

    async with httpx.AsyncClient(timeout=45.0) as client_http:
        for base_url in colab_urls:
            if not base_url.strip(): continue
            target_url = base_url.rstrip("/") + "/clone"
            try:
                files = {"audio": (reference_audio.filename or "ref.wav", audio_bytes, "audio/wav")}
                res = await client_http.post(target_url, data={"text": text}, files=files)
                if res.status_code == 200:
                    tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
                    tmp_file.write(res.content)
                    tmp_file.close()
                    background_tasks.add_task(os.remove, tmp_file.name)
                    return FileResponse(tmp_file.name, media_type="audio/wav")
                last_error = f"Colab responded status: {res.status_code}"
            except Exception as e:
                last_error = str(e)
                continue

    if cost > 0 and email != ADMIN_EMAIL:
        users_col.update_one({"email": email}, {"$inc": {"credits": cost}})
    raise HTTPException(status_code=502, detail=f"Colab Server အားလုံး ပိတ်နေပါသည် သို့မဟုတ် လင့်ခ်ပျက်နေပါသည်: {last_error}")

@app.post("/api/tts")
async def edge_tts_api(request: Request, background_tasks: BackgroundTasks):
    try:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        text = data.get("text", "").strip()
        voice = data.get("voice", "my-MM-ThihaNeural")
        rate = data.get("rate", "+10%")

        if not text:
            raise HTTPException(status_code=400, detail="Text is required")

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
        print(f"[Edge TTS Error]: {e}")
        return JSONResponse(status_code=500, content={"error": f"Edge TTS Error: {str(e)}"})

# --- NEURAL COMPUTE ENGINE (MULTI-PROVIDER ORCHESTRATOR) ---

def parse_settings_keys(raw_val) -> list[str]:
    if isinstance(raw_val, list):
        return [k.strip() for k in raw_val if isinstance(k, str) and k.strip()]
    if isinstance(raw_val, str):
        try:
            parsed = json.loads(raw_val)
            if isinstance(parsed, list):
                return [k.strip() for k in parsed if isinstance(k, str) and k.strip()]
        except Exception:
            if raw_val.strip():
                return [raw_val.strip()]
    return []

@app.post("/api/engine/neural-compute")
async def proxy_neural_compute(request: Request):
    get_request_email(request)
    body = await request.json()
    messages = body.get("messages", [])
    temperature = float(body.get("temperature", 0.7))
    
    if not messages:
        raise HTTPException(status_code=400, detail="Messages are required")

    reply_text = await call_universal_ai(messages, temperature)
    
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": reply_text
                }
            }
        ]
    }

@app.api_route("/api/gemini/{path:path}", methods=["GET", "POST"])
async def proxy_gemini(path: str, request: Request):
    get_request_email(request)  # 🔒 Login စစ်ဆေးမှု
    query_params = dict(request.query_params)
    client_key = query_params.get("key", "").strip()
    keys_to_try = []
    
    if client_key and not client_key.startswith("cc_"): 
        keys_to_try.append(client_key)
        
    for _ in range(len(SERVER_GEMINI_KEYS)):
        k = get_server_gemini_key()
        if k and k not in keys_to_try and not k.startswith("cc_"): 
            keys_to_try.append(k)

    body_bytes = await request.body()
    last_res = None

    clean_path = unquote(unquote(path)).replace("%3A", ":")

    async with httpx.AsyncClient(timeout=120.0) as client_http:
        for key in (keys_to_try or [""]):
            try:
                q = dict(query_params)
                if key: q["key"] = key
                url = f"https://generativelanguage.googleapis.com/{clean_path}"
                res = await client_http.request(request.method, url, params=q, headers={"Content-Type": "application/json"}, content=body_bytes)
                last_res = res
                if res.status_code == 200:
                    return Response(content=res.content, status_code=200, media_type="application/json")
            except Exception: 
                continue

    if last_res is not None:
        return Response(content=last_res.content, status_code=last_res.status_code, media_type="application/json")
    return JSONResponse(status_code=500, content={"error": "Server Gemini API Error"})

def get_groq_pool_keys() -> list[str]:
    doc = settings_col.find_one({"key": "groq_api_keys_pool"})
    if doc and doc.get("value"):
        try: return [k.strip() for k in json.loads(doc["value"]) if k.strip()]
        except Exception: return []
    return []

@app.post("/api/groq/transcriptions")
async def proxy_groq(request: Request):
    get_request_email(request)  # 🔒 Login စစ်ဆေးမှု
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
        
        admin_groq_pool = get_groq_pool_keys()
        for k in admin_groq_pool:
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

SFX_DIR = (APP_DIR / "sfx_assets").resolve()
SFX_DIR.mkdir(parents=True, exist_ok=True)

@app.get("/api/protected-sfx/{filename}")
async def get_protected_sfx(filename: str, request: Request):
    get_request_email(request)
    safe_filename = os.path.basename(filename)
    sfx_path = (SFX_DIR / safe_filename).resolve()
    
    if not sfx_path.exists() or not sfx_path.is_file(): 
        raise HTTPException(status_code=404, detail="SFX not found")
        
    media_type = "audio/wav" if safe_filename.lower().endswith(".wav") else "audio/mpeg"
    response = FileResponse(sfx_path, media_type=media_type)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response

# --- ADVANCED MULTI-PLATFORM VIDEO DOWNLOADER ENGINE ---
import re

DOWNLOADS_DIR = (MEDIA_ROOT / "downloads").resolve()
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

def cleanup_old_downloads(folder: str, max_age_seconds: int = 1800):
    """မိနစ် ၃၀ ကျော်သွားသော ဖိုင်ဟောင်းများအား အလိုအလျောက် ရှင်းလင်းခြင်း"""
    try:
        now = time.time()
        for f in glob.glob(os.path.join(folder, "*")):
            if os.path.isfile(f) and (now - os.path.getmtime(f) > max_age_seconds):
                try: os.remove(f)
                except Exception: pass
    except Exception as e:
        print(f"[Download Cleanup Warning]: {e}")

def clean_input_url(raw_text: str) -> str:
    """Xiaohongshu/TikTok App များမှ ကော်ပီကူးလာသော စာသားများထဲမှ URL အစစ်ကို ဆွဲထုတ်ခြင်း"""
    match = re.search(r'(https?://[^\s]+)', raw_text)
    if match:
        clean = match.group(1).strip()
        return clean.split("?")[0] if "xhslink.com" in clean else clean
    return raw_text.strip()

async def fetch_tiktok_media(url: str):
    """TikTok နှင့် Douyin များအတွက် Watermark-free CDN တိုက်ရိုက်ဆွဲယူသည့် စနစ် (Dual Proxy)"""
    import urllib.parse

    # 1. TikWM API
    try:
        api_endpoint = "https://www.tikwm.com/api/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json"
        }
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.post(api_endpoint, data={"url": url, "hd": 1}, headers=headers)
            if resp.status_code == 200:
                res_json = resp.json()
                if res_json.get("code") == 0 and "data" in res_json:
                    data = res_json["data"]
                    title = data.get("title") or "TikTok_Media"

                    if data.get("images") and isinstance(data["images"], list):
                        return {
                            "status": "picker",
                            "title": title,
                            "picker": [{"type": "image", "url": img} for img in data["images"]],
                            "stream_url": data["images"][0]
                        }

                    video_url = data.get("play") or data.get("wmplay")
                    if video_url:
                        if video_url.startswith("/"):
                            video_url = f"https://www.tikwm.com{video_url}"

                        download_url = f"/api/downloader/proxy-file?url={urllib.parse.quote(video_url)}&title={urllib.parse.quote(title)}"
                        return {
                            "status": "direct",
                            "title": title,
                            "stream_url": download_url
                        }
    except Exception as e:
        print(f"[TikWM Error]: {e}")

    # 2. TikSave Fallback API
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            f_resp = await client.get(f"https://api.tiklydown.eu.org/api/download?url={url}")
            if f_resp.status_code == 200:
                f_data = f_resp.json()
                v_url = f_data.get("video", {}).get("noWatermark") or f_data.get("video", {}).get("watermark")
                if v_url:
                    title = f_data.get("title") or "TikTok_Video"
                    download_url = f"/api/downloader/proxy-file?url={urllib.parse.quote(v_url)}&title={urllib.parse.quote(title)}"
                    return {
                        "status": "direct",
                        "title": title,
                        "stream_url": download_url
                    }
    except Exception as e:
        print(f"[Tiklydown Error]: {e}")

    return None

def extract_with_ytdlp(url: str, output_dir: str):
    """YouTube Universal Multi-Client & Dynamic Format Engine"""
    render_secret_cookie = Path("/etc/secrets/cookies.txt")
    local_cookie = APP_DIR / "cookies.txt"
    writable_temp_cookie = Path("/tmp/cookies.txt")

    cookie_file_to_use = None
    if render_secret_cookie.exists():
        try:
            shutil.copy(render_secret_cookie, writable_temp_cookie)
            cookie_file_to_use = str(writable_temp_cookie)
        except Exception:
            cookie_file_to_use = str(render_secret_cookie)
    elif local_cookie.exists():
        cookie_file_to_use = str(local_cookie)

    ydl_opts = {
        # 1. 720p ရှိက 720p ဆွဲမည်
        # 2. Shorts သို့မဟုတ် Height မပါသော HLS ဖိုင်များကိုပါ '?' ဖြင့် အလိုအလျောက် လက်ခံမည်
        # 3. 720p မရှိပါက ရနိုင်သော မည်သည့် format ကိုမဆို Error မတက်ဘဲ အကုန် ဒေါင်းလုဒ် ဆွဲမည်
        'format': 'bv*[height<=?720]+ba/b[height<=?720]/bv*+ba/b',
        'outtmpl': os.path.join(output_dir, '%(id)s.%(ext)s'),
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
        'cookiefile': cookie_file_to_use,
        # iOS ၏ HLS နှင့် Android ၏ DASH Streams နှစ်ခုလုံး ရရှိစေရန် ပေါင်းစပ်အသုံးပြုခြင်း
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
            'Accept-Language': 'en-US,en;q=0.9'
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if not filename.endswith('.mp4'):
            possible_mp4 = filename.rsplit('.', 1)[0] + '.mp4'
            if os.path.exists(possible_mp4):
                filename = possible_mp4
        return {
            "title": info.get('title', 'Downloaded_Video'),
            "filepath": filename
        }
    
@app.get("/api/downloader/proxy-file")
async def proxy_download_file(url: str, title: str = "TikTok_Video"):
    """TikTok Video ကို Max MB (Content-Length) အတိအကျဖြင့် Direct Download ဆွဲစေသည့် Proxy Engine"""
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-")[:50].strip() or "TikTok_Video"
    filename = f"{safe_title}.mp4"

    client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
    req = client.build_request("GET", url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Referer": "https://www.tiktok.com/"
    })
    resp = await client.send(req, stream=True)

    # TikTok CDN မှ ပြန်လာသော File အရွယ်အစား (Bytes) အား ဖတ်ယူခြင်း
    content_length = resp.headers.get("content-length")

    async def file_iterator():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=1024 * 64):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": resp.headers.get("content-type", "video/mp4")
    }

    # Browser က Max MB အတိအကျ သိရှိစေရန် Content-Length ထည့်ပေးခြင်း
    if content_length:
        headers["Content-Length"] = content_length

    return StreamingResponse(file_iterator(), headers=headers)

@app.post("/api/downloader/inspect")
async def inspect_video_download(request: Request, data: dict):
    get_request_email(request)  # 🔒 Login စစ်ဆေးခြင်း
    raw_url = data.get("url", "").strip()
    if not raw_url:
        raise HTTPException(status_code=400, detail="Video URL လိုအပ်ပါသည်")

    # လင့်ခ်ထဲမှ စာသားအပိုများ သန့်စင်ခြင်း
    url = clean_input_url(raw_url)

    # နည်းလမ်း ၁: TikTok / Douyin ဖြစ်ပါက Direct API များဖြင့် Watermark ကင်းစင်စွာ ရယူခြင်း
    if any(d in url.lower() for d in ["tiktok.com", "douyin.com"]):
        tiktok_res = await fetch_tiktok_media(url)
        if tiktok_res:
            return tiktok_res
        raise HTTPException(status_code=400, detail="TikTok ဗီဒီယိုအား ရယူ၍ မရနိုင်ပါ။ Link အမှန်ဖြစ်ကြောင်း သေချာပါစေ။")

    # နည်းလမ်း ၂: YouTube, RedNote, FB, Instagram စသည်တို့အတွက်
    try:
        downloads_dir = str(DOWNLOADS_DIR)
        cleanup_old_downloads(downloads_dir, max_age_seconds=1800)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, extract_with_ytdlp, url, downloads_dir)

        filename = os.path.basename(result["filepath"])
        return {
            "status": "direct",
            "title": result["title"],
            "stream_url": f"/media/downloads/{filename}"
        }
    except Exception as err:
        err_str = str(err)
        print(f"[Downloader Error]: {err_str}")
        if "confirm you're not a bot" in err_str:
            raise HTTPException(status_code=403, detail="YouTube မှ ယာယီ Bot စစ်ဆေးမှုပြုလုပ်နေပါသဖြင့် နောက် ၅ မိနစ်ခန့်အကြာတွင် ပြန်လည်စမ်းသပ်ပေးပါခင်ဗျာ။")
        elif "Unsupported URL" in err_str:
            raise HTTPException(status_code=400, detail="မထောက်ပံ့ထားသော Link ဖြစ်နေပါသည် (လင့်ခ်ထဲတွင် Login စာမျက်နှာ ရောက်နေပါသည်)။")
        raise HTTPException(status_code=500, detail=f"Download မအောင်မြင်ပါ: {err_str[:120]}")
# --- ADS ANIMATOR ENGINE (UNIVERSAL AI FIRST -> GEMINI FALLBACK -> REFUND ON FAIL) ---

@app.post("/api/animator/generate-script")
async def generate_ad_script(request: Request, data: dict):
    email = get_request_email(request)
    product_name = data.get("product_name", "").strip()
    target_duration_sec = int(data.get("duration", 15))
    additional_notes = data.get("notes", "")

    if not product_name:
        raise HTTPException(status_code=400, detail="Product Name လိုအပ်ပါသည်")

    # Settings မှ စက္ကန့်အလိုက် Credit နှုန်းထားစစ်ဆေးခြင်း
    setting_doc = settings_col.find_one({"key": "ads_animator_rate_per_sec"})
    rate_per_sec = int(setting_doc.get("value", "2")) if setting_doc else 2
    total_cost = target_duration_sec * rate_per_sec

    # Admin မဟုတ်ပါက Credit စစ်ဆေးပြီး ကြိုတင်ဖြတ်တောက်ခြင်း
    user = users_col.find_one({"email": email})
    if email != ADMIN_EMAIL:
        if not user or user.get("credits", 0) < total_cost:
            raise HTTPException(
                status_code=402, 
                detail=f"Credit မလုံလောက်ပါ။ {target_duration_sec} စက္ကန့် Ad ဖန်တီးရန် {total_cost} Credits လိုအပ်ပါသည်။"
            )
        users_col.update_one({"email": email}, {"$inc": {"credits": -total_cost}})

    prompt_text = (
        f"You are a professional video ads director. Create an engaging, scene-by-scene "
        f"{target_duration_sec}-second ad storyboard with voiceover scripts for: {product_name}. "
        f"User requirements: {additional_notes}. "
        f"Format clearly with Scene Number, Visual description, and Myanmar/English Narration."
    )

    script_result = None
    universal_error = None

    # Step 1: Universal AI Providers (Primary & Secondary - Codecraft / Chat B.AI) ကို အရင်ဆုံး ဦးစားပေးခေါ်ယူခြင်း
    try:
        script_result = await call_universal_ai([{"role": "user", "content": prompt_text}])
    except Exception as e:
        universal_error = str(e)
        print(f"[Animator Warning] Universal AI Error: {universal_error} -> Fallback to Gemini API")

    # Step 2: Universal AI အဆင်မပြေမှသာ Gemini API သို့ Fallback အနေဖြင့် သုံးခြင်း
    if not script_result:
        gemini_key = get_server_gemini_key()
        if not gemini_key:
            # Universal AI ရော Gemini Key ပါ မရှိပါက Credit ပြန်အမ်းခြင်း
            if email != ADMIN_EMAIL:
                users_col.update_one({"email": email}, {"$inc": {"credits": total_cost}})
            raise HTTPException(
                status_code=500, 
                detail=f"AI Provider အားလုံး ပျက်နေပြီး Gemini Key လည်းမရှိပါ: {universal_error}"
            )

        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key}"
        payload = {
            "contents": [{
                "parts": [{"text": prompt_text}]
            }]
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client_http:
                resp = await client_http.post(gemini_url, json=payload, headers={"Content-Type": "application/json"})
                if resp.status_code == 200:
                    res_data = resp.json()
                    script_result = res_data["candidates"][0]["content"]["parts"][0]["text"]
                else:
                    raise Exception(f"Gemini API Error (HTTP {resp.status_code}): {resp.text}")
        except Exception as e:
            # Provider ရော Gemini ပါ ၂ မျိုးစလုံး ကျသွားပါက User ၏ Credit အား auto-refund ပေးခြင်း
            if email != ADMIN_EMAIL:
                users_col.update_one({"email": email}, {"$inc": {"credits": total_cost}})
            raise HTTPException(status_code=502, detail=f"Script Generation လုံးဝမအောင်မြင်ပါ: {str(e)}")

    return {
        "status": "success",
        "script": script_result,
        "deducted_credits": total_cost if email != ADMIN_EMAIL else 0
    }
# --- SOCIAL MEDIA KIT ENGINE WITH CREDIT DEDUCTION & AUTO-REFUND ---

@app.post("/api/social/generate-kit")
async def generate_social_kit(request: Request, data: dict):
    email = get_request_email(request)
    transcript = data.get("text", "").strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="ဗီဒီယို စာသား (Transcript) မပါရှိပါ")

    # Admin Panel မှ သတ်မှတ်ထားသော Social Kit Credit နှုန်းထား ရယူခြင်း
    setting_doc = settings_col.find_one({"key": "social_kit_rate_per_use"})
    cost = int(setting_doc.get("value", "10")) if setting_doc else 10

    # User Credit စစ်ဆေးခြင်းနှင့် ကြိုတင်ဖြတ်တောက်ခြင်း
    user = users_col.find_one({"email": email})
    if email != ADMIN_EMAIL:
        if not user or user.get("credits", 0) < cost:
            raise HTTPException(
                status_code=402,
                detail=f"Credit မလုံလောက်ပါ။ Social Media Kit ဖန်တီးရန် {cost} Credits လိုအပ်ပါသည်။"
            )
        users_col.update_one({"email": email}, {"$inc": {"credits": -cost}})

    prompt_text = (
        f"Generate viral, engaging social media posts for YouTube, TikTok, and Facebook in Myanmar based on this transcript.\n"
        f"Output format MUST be strictly a valid JSON object without markdown formatting:\n"
        f"{{\n"
        f'  "youtube": {{"title": "...", "desc": "...", "tags": "..."}},\n'
        f'  "tiktok": {{"title": "...", "desc": "...", "tags": "..."}},\n'
        f'  "facebook": {{"title": "...", "desc": "...", "tags": "..."}}\n'
        f"}}\n\n"
        f"Transcript:\n{transcript[:3000]}"
    )

    kit_result = None

    # Step 1: Universal AI Providers ဖြင့် အရင်ဆုံး ကြိုးစားခြင်း
    try:
        ai_resp = await call_universal_ai([
            {"role": "system", "content": "Return ONLY valid JSON."},
            {"role": "user", "content": prompt_text}
        ])
        clean_json = ai_resp.replace("```json", "").replace("```", "").strip()
        kit_result = json.loads(clean_json)
    except Exception as e:
        print(f"[Social Kit Universal Error]: {e} -> Fallback to Gemini")

    # Step 2: အဆင်မပြေပါက Server Gemini Key သို့ Fallback ပြုလုပ်ခြင်း
    if not kit_result:
        gemini_key = get_server_gemini_key()
        if gemini_key:
            try:
                gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
                payload = {
                    "contents": [{"parts": [{"text": prompt_text}]}],
                    "generationConfig": {"responseMimeType": "application/json"}
                }
                async with httpx.AsyncClient(timeout=60.0) as client_http:
                    resp = await client_http.post(gemini_url, json=payload, headers={"Content-Type": "application/json"})
                    if resp.status_code == 200:
                        res_data = resp.json()
                        c_text = res_data["candidates"][0]["content"]["parts"][0]["text"]
                        kit_result = json.loads(c_text.replace("```json", "").replace("```", "").strip())
            except Exception as gemini_err:
                print(f"[Social Kit Gemini Fallback Error]: {gemini_err}")

    # မအောင်မြင်ပါက ဖြတ်တောက်ထားသော Credit အား အလိုအလျောက် ပြန်အမ်းခြင်း (Auto-Refund)
    if not kit_result:
        if email != ADMIN_EMAIL:
            users_col.update_one({"email": email}, {"$inc": {"credits": cost}})
        raise HTTPException(status_code=502, detail="Social Media Kit ထုတ်ယူ၍ မရနိုင်ပါ။ Credit အား ပြန်လည် အမ်းပေးလိုက်ပါပြီ။")

    return {
        "status": "success",
        "content": kit_result,
        "deducted_credits": cost if email != ADMIN_EMAIL else 0
    }
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)