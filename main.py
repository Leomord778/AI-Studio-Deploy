import os, httpx, math, subprocess, tempfile, edge_tts, imageio_ffmpeg, traceback
from datetime import datetime
from fastapi import FastAPI, Request, BackgroundTasks, UploadFile, File, Form, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from pymongo import MongoClient
from bson.objectid import ObjectId

# FFmpeg.wasm အတွက် လိုအပ်သော Starlette Middleware ကို Import လုပ်ခြင်း
from starlette.middleware.base import BaseHTTPMiddleware

# --- ENV & MONGODB SETUP ---
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client['ai_video_studio']

users_col = db['users']
transactions_col = db['transactions']
settings_col = db['settings']

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- FFmpeg.wasm Security Headers Middleware ထည့်သွင်းခြင်း ---
class CrossOriginIsolationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
        return response

app.add_middleware(CrossOriginIsolationMiddleware)
# -------------------------------------------------------------

@app.get("/")
async def serve_index():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

BOT_TOKEN = "8643779687:AAFrtV8XnepuiLWly9N1YwXEXZEBvu7pg-8"
CHAT_ID = "-1003802670362"

GROQ_API_KEYS = [
    "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",
    "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",
    "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",
    "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"
]

# --- DATABASE SETUP (MongoDB) ---
def init_db():
    if settings_col.count_documents({}) == 0:
        settings_col.insert_many([
            {"key": "deduction_rate", "value": "200"},
            {"key": "free_daily_mins", "value": "2"},
            {"key": "trial_mins", "value": "5"},
            {"key": "announcement", "value": "AI Studio မှ နွေးထွေးစွာ ကြိုဆိုပါသည်။ ၅ မိနစ်စာ အခမဲ့ စမ်းသပ်နိုင်ပါသည်။"}
        ])
    
    if not users_col.find_one({"email": "778leomord@gmail.com"}):
        users_col.insert_one({
            "email": "778leomord@gmail.com", "role": "admin", "credits": 999999, 
            "free_mins_used": 0, "last_free_date": "", "has_bought_3000": False, "is_new": False
        })

init_db()

# --- UTILS ---
async def send_payment_to_telegram(email, amount, file_bytes):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    caption = f"💰 **Payment Received** 💰\n\n👤 User: {email}\n💵 Amount: {amount} MMK"
    files = {"photo": ("payment.jpg", file_bytes, "image/jpeg")}
    data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
    async with httpx.AsyncClient() as client:
        await client.post(url, data=data, files=files)

# --- USER APIs ---
class LoginData(BaseModel):
    email: str

@app.post("/api/login")
async def login(data: LoginData):
    today = datetime.now().strftime("%Y-%m-%d")
    user = users_col.find_one({"email": data.email})
    
    if not user:
        new_user = {
            "email": data.email, "role": "user", "credits": 0, 
            "free_mins_used": 0, "last_free_date": today, "has_bought_3000": False, "is_new": True
        }
        users_col.insert_one(new_user)
        user = new_user
    else:
        if user.get("last_free_date") != today:
            users_col.update_one({"email": data.email}, {"$set": {"free_mins_used": 0, "last_free_date": today}})
            user["free_mins_used"] = 0
            user["last_free_date"] = today

    settings = {doc["key"]: doc["value"] for doc in settings_col.find()}
    
    return { 
        "email": user["email"], "role": user.get("role", "user"), "credits": user.get("credits", 0), 
        "free_mins_used": user.get("free_mins_used", 0), "has_bought_3000": user.get("has_bought_3000", False), 
        "is_new": user.get("is_new", False), "settings": settings 
    }

@app.post("/api/buy-credits")
async def buy_credits(email: str = Form(...), amount: int = Form(...), file: UploadFile = File(...)):
    file_bytes = await file.read()
    transactions_col.insert_one({
        "email": email, "amount": amount, "status": "pending", 
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    try: 
        await send_payment_to_telegram(email, amount, file_bytes)
    except: 
        pass
    return {"status": "success"}

class DeductData(BaseModel):
    email: str
    duration_seconds: float

@app.post("/api/check-credits")
async def check_credits(data: DeductData):
    settings = {doc["key"]: doc["value"] for doc in settings_col.find()}
    user = users_col.find_one({"email": data.email})
    if not user: return JSONResponse(status_code=400, content={"error": "User not found"})
    
    credits = user.get("credits", 0)
    free_mins_used = user.get("free_mins_used", 0)
    is_new = user.get("is_new", False)
    
    deduction_rate = int(settings.get('deduction_rate', 200))
    free_daily_mins = float(settings.get('free_daily_mins', 2))
    trial_mins = float(settings.get('trial_mins', 5))
    required_mins = max(1, math.ceil((data.duration_seconds - 30) / 60))

    if is_new and (free_mins_used + required_mins <= trial_mins): return {"status": "ok"}
    if credits >= required_mins * deduction_rate: return {"status": "ok"}
    if (not is_new) and (free_mins_used + required_mins <= free_daily_mins): return {"status": "ok"}
    return JSONResponse(status_code=403, content={"error": "Credits မလုံလောက်ပါ။ ကျေးဇူးပြု၍ ထပ်မံဝယ်ယူပါ။"})

@app.post("/api/deduct")
async def deduct_credits(data: DeductData):
    settings = {doc["key"]: doc["value"] for doc in settings_col.find()}
    user = users_col.find_one({"email": data.email})
    if not user: return JSONResponse(status_code=400, content={"error": "User not found"})
    
    credits = user.get("credits", 0)
    free_mins_used = user.get("free_mins_used", 0)
    is_new = user.get("is_new", False)
    
    deduction_rate = int(settings.get('deduction_rate', 200))
    free_daily_mins = float(settings.get('free_daily_mins', 2))
    trial_mins = float(settings.get('trial_mins', 5))
    required_mins = max(1, math.ceil((data.duration_seconds - 30) / 60))

    if is_new:
        if free_mins_used + required_mins <= trial_mins:
            users_col.update_one({"email": data.email}, {"$inc": {"free_mins_used": required_mins}})
            if free_mins_used + required_mins >= trial_mins: 
                users_col.update_one({"email": data.email}, {"$set": {"is_new": False}})
            return {"status": "success"}
        else:
            users_col.update_one({"email": data.email}, {"$set": {"is_new": False}})
            is_new = False

    if credits >= required_mins * deduction_rate:
        users_col.update_one({"email": data.email}, {"$inc": {"credits": -(required_mins * deduction_rate)}})
        return {"status": "success"}

    if free_mins_used + required_mins <= free_daily_mins:
        users_col.update_one({"email": data.email}, {"$inc": {"free_mins_used": required_mins}})
        return {"status": "success"}
        
    return JSONResponse(status_code=403, content={"error": "Credits မလုံလောက်ပါ။ ကျေးဇူးပြု၍ ထပ်မံဝယ်ယူပါ။"})

# --- ADMIN APIs ---
@app.get("/api/admin/stats")
async def admin_stats():
    total_users = users_col.count_documents({})
    current_month = datetime.now().strftime("%Y-%m")
    
    approved_txs = transactions_col.find({"status": "approved", "date": {"$regex": f"^{current_month}"}})
    monthly_profit = sum([tx.get("amount", 0) for tx in approved_txs])
    
    return {"total_users": total_users, "monthly_profit": monthly_profit}

@app.get("/api/admin/transactions")
async def get_transactions():
    txs_cursor = transactions_col.find({"status": "pending"}).sort("_id", -1)
    txs = [
        {"id": str(tx["_id"]), "email": tx["email"], "amount": tx["amount"], "status": tx["status"], "date": tx["date"]}
        for tx in txs_cursor
    ]
    return {"transactions": txs}

class ApproveData(BaseModel):
    id: str  # MongoDB ObjectId is string
    email: str
    amount: int

@app.post("/api/admin/approve")
async def approve_transaction(data: ApproveData):
    transactions_col.update_one({"_id": ObjectId(data.id)}, {"$set": {"status": "approved"}})
    
    update_data = {"$inc": {"credits": data.amount}}
    if data.amount == 3000:
        update_data["$set"] = {"has_bought_3000": True}
        
    users_col.update_one({"email": data.email}, update_data)
    return {"status": "success"}

class AddCreditData(BaseModel):
    email: str
    amount: int

@app.post("/api/admin/add-credit")
async def admin_add_credit(data: AddCreditData):
    users_col.update_one({"email": data.email}, {"$inc": {"credits": data.amount}})
    return {"status": "success"}

class SettingsData(BaseModel):
    deduction_rate: str
    free_daily_mins: str
    trial_mins: str
    announcement: str

@app.post("/api/admin/settings")
async def update_settings(data: SettingsData):
    for key, value in data.dict().items():
        settings_col.update_one({"key": key}, {"$set": {"value": str(value)}}, upsert=True)
    return {"status": "success"}

# --- SYSTEM APIs ---
@app.post("/api/tts")
async def edge_tts_api(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        text = data.get("text", "").strip()
        voice = data.get("voice", "my-MM-ThihaNeural")
        
        if not text:
            return Response(content='{"error": "စာသား မရှိပါ"}', status_code=400)
            
        communicate = edge_tts.Communicate(text, voice)
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp_file.close()
        
        await communicate.save(tmp_file.name)
        background_tasks.add_task(os.remove, tmp_file.name)
        return FileResponse(tmp_file.name, media_type="audio/mpeg")
    except Exception as e:
        return Response(content=f'{{"error": "{str(e)}"}}', status_code=500)

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
    except Exception as e: 
        return Response(content=f'{{"error": "{str(e)}"}}', status_code=500)

@app.api_route("/api/gemini/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_gemini(path: str, request: Request):
    url = f"https://generativelanguage.googleapis.com/{path}"
    async with httpx.AsyncClient() as client:
        req = client.build_request(request.method, url, params=dict(request.query_params), headers={"Content-Type": "application/json"}, content=await request.body(), timeout=120.0)
        try:
            res = await client.send(req)
            return Response(content=res.content, status_code=res.status_code, media_type=res.headers.get("content-type", "application/json"))
        except Exception as e: 
            return Response(content=str(e), status_code=500)

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
                    if res.status_code == 200: 
                        return Response(content=res.content, status_code=res.status_code, media_type=res.headers.get("content-type"))
                except: 
                    continue
        return Response(content='{"error": "Groq Error"}', status_code=500)
    except Exception as e: 
        return Response(content=f'{{"error": "{str(e)}"}}', status_code=500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))