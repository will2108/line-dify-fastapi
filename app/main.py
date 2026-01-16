from fastapi import FastAPI, Request, Header, HTTPException, Body
import requests
import os
import hmac
import hashlib
import base64
import logging
from typing import Optional

# ======================================================
# App & Logging
# ======================================================
app = FastAPI()
logging.basicConfig(level=logging.INFO)

# ======================================================
# Environment Variables
# ======================================================
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_API_URL = "https://api.dify.ai/v1/chat-messages"

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_REPLY_API = "https://api.line.me/v2/bot/message/reply"

CWA_API_KEY = os.getenv("CWA_API_KEY")

# 行政院 OpenData（注意：常回 HTML / 空值）
EY_API_BASE = "https://www.ey.gov.tw/OpenData/api"

# ======================================================
# Health Check
# ======================================================
@app.get("/")
def health():
    return {"status": "ok"}

# ======================================================
# Utils
# ======================================================
def verify_line_signature(body: bytes, signature: Optional[str]) -> bool:
    if not signature or not LINE_CHANNEL_SECRET:
        return False

    mac = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()

    expected = base64.b64encode(mac).decode()
    return hmac.compare_digest(expected, signature)


def safe_json(resp: requests.Response):
    """
    行政院 / OpenData API 專用
    - 非 JSON / 空值 / HTML → 不炸
    """
    try:
        return resp.json()
    except Exception:
        logging.error("❌ Response is not JSON")
        logging.error(resp.text[:300])
        return None


# ======================================================
# LINE Webhook
# ======================================================
@app.post("/line/webhook")
async def line_webhook(
    request: Request,
    x_line_signature: str = Header(None)
):
    body = await request.body()

    if not verify_line_signature(body, x_line_signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
    logging.info(f"📩 LINE payload: {payload}")

    if not payload.get("events"):
        return {"status": "ok"}

    event = payload["events"][0]
    if event.get("type") != "message":
        return {"status": "ok"}

    message = event.get("message", {})
    if message.get("type") != "text":
        return {"status": "ok"}

    user_text = message["text"]
    reply_token = event["replyToken"]
    user_id = event["source"]["userId"]

    logging.info(f"🗣 User: {user_text}")

    # ⚠️ 一定要先準備 fallback answer
    answer = "系統暫時忙碌中，請稍後再試 🙏"

    try:
        dify_payload = {
            "inputs": {},
            "query": user_text,
            "response_mode": "blocking",
            "user": user_id
        }

        resp = requests.post(
            DIFY_API_URL,
            headers={
                "Authorization": f"Bearer {DIFY_API_KEY}",
                "Content-Type": "application/json"
            },
            json=dify_payload,
            timeout=60
        )

        logging.info(f"🤖 Dify status: {resp.status_code}")

        if resp.status_code == 200:
            answer = resp.json().get("answer", answer)
        else:
            answer = "AI 判斷服務暫時無法使用"

    except Exception:
        logging.exception("❌ Dify or Tool failed")

    # ✅ 不論發生什麼事，一定回 LINE
    requests.post(
        LINE_REPLY_API,
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": answer}]
        }
    )

    return {"status": "ok"}

# ======================================================
# Tool 1️⃣ Weather Tool
# ======================================================
@app.post("/tool/weather")
def tool_weather(payload: dict = Body(...)):
    location = payload.get("location")
    time_range = payload.get("time_range", "today")

    if not location:
        raise HTTPException(status_code=400, detail="location is required")

    # 這裡你之後可再接 CWA 真解析，目前先穩定 demo
    return {
        "location": location,
        "time_range": time_range,
        "summary": "未來降雨機率偏高，請留意午後短暫雨",
        "risk_level": "中",
        "source": "中央氣象署"
    }

# ======================================================
# Tool 2️⃣ 行政院即時新聞（安全版）
# ======================================================
@app.post("/tool/ey/news")
def ey_news(payload: dict = Body(default={})):
    limit = payload.get("limit", 3)

    try:
        r = requests.get(f"{EY_API_BASE}/ExecutiveYuan/NewsEy", timeout=10)
        data = safe_json(r)

        if not data:
            return {
                "source": "行政院全球資訊網",
                "error": "官方新聞資料暫時無法取得",
                "items": []
            }

        items = data.get("data") or data.get("items") or []
        return {
            "source": "行政院全球資訊網",
            "type": "即時新聞",
            "items": items[:limit]
        }

    except Exception:
        logging.exception("❌ EY News API error")
        return {
            "source": "行政院全球資訊網",
            "error": "官方新聞資料暫時無法取得",
            "items": []
        }

# ======================================================
# Tool 3️⃣ 行政院重要政策（安全版）
# ======================================================
@app.post("/tool/ey/policy")
def ey_policy(payload: dict = Body(default={})):
    limit = payload.get("limit", 3)

    try:
        r = requests.get(f"{EY_API_BASE}/Performance/EyPolicy", timeout=10)
        data = safe_json(r)

        if not data:
            return {
                "source": "行政院全球資訊網",
                "error": "政策資料暫時無法取得",
                "items": []
            }

        items = data.get("data") or data.get("items") or []
        return {
            "source": "行政院全球資訊網",
            "type": "重要政策",
            "items": items[:limit]
        }

    except Exception:
        logging.exception("❌ EY Policy API error")
        return {
            "source": "行政院全球資訊網",
            "error": "政策資料暫時無法取得",
            "items": []
        }

# ======================================================
# Tool 4️⃣ 消費 / 防災警訊（安全版）
# ======================================================
@app.post("/tool/ey/consumer-warning")
def ey_consumer_warning(payload: dict = Body(default={})):
    limit = payload.get("limit", 3)

    try:
        r = requests.get(
            f"{EY_API_BASE}/ConsumerProtection/WarningNewsCPC",
            timeout=10
        )
        data = safe_json(r)

        if not data:
            return {
                "source": "行政院消費者保護會",
                "risk_level": "未知",
                "error": "消費警訊資料暫時無法取得",
                "items": []
            }

        items = data.get("data") or data.get("items") or []
        return {
            "source": "行政院消費者保護會",
            "risk_level": "中",
            "items": items[:limit]
        }

    except Exception:
        logging.exception("❌ EY Consumer Warning API error")
        return {
            "source": "行政院消費者保護會",
            "risk_level": "未知",
            "error": "消費警訊資料暫時無法取得",
            "items": []
        }
