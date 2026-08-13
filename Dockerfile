# Python 3.10 ကို အခြေခံထားပါတယ်
FROM python:3.10-slim

# System level မှာ လိုအပ်တဲ့ ffmpeg ကို သွင်းပါတယ်
RUN apt-get update && apt-get install -y ffmpeg

# အလုပ်လုပ်မယ့် Folder (Directory) ဆောက်ပါတယ်
WORKDIR /app

# လိုအပ်တဲ့ Python Packages တွေ သွင်းဖို့ ဖိုင်ကို ကူးပါတယ်
COPY requirements.txt .

# Packages တွေ သွင်းပါတယ်
RUN pip install --no-cache-dir -r requirements.txt

# ကျန်တဲ့ ကုဒ်ဖိုင် (main.py, index.html) တွေကို ကူးပါတယ်
COPY . .

# Render.com အတွက် Uvicorn ကို Run ပါတယ် (Port 10000)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]