from fastapi import FastAPI, Request, Header, HTTPException, Body
import requests
import os
import hmac
import hashlib
import base64
import logging

app = FastAPI()
logging.basicConfig(level=logging.INFO)

# =========================
# Environment Variables
# =========================
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_API_URL = "https://api.dify.ai/v1/chat-messages"

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_REPLY_API = "https://api.line.me/v2/bot/message/reply"

CWA_API_KEY = os.getenv("CWA_API_KEY")

# ✅ 行政院 OpenData API（注意不是 /api）
EY_API_BASE = "https://www.ey.gov.tw/OpenData/api"

COMMON_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0"
}

# =========================
# Health Check
# =========================
@app.get("/")
def health():
    return {"status": "ok"}

# =========================
# LINE Signature Verify
# =========================
def verify_line_signature(body: bytes, signature: str | None) -> bool:
    if not signature or not LINE_CHANNEL_SECRET:
        return False

    mac = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()

    expected = base64.b64encode(mac).decode()
    return hmac.compare_digest(expected, signature)

# =========================
# LINE Webhook
# =========================
@app.post("/line/webhook")
async def line_webhook(
    request: Request,
    x_line_signature: str = Header(None)
):
    body = await request.body()

    if not verify_line_signature(body, x_line_signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
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

    dify_payload = {
        "inputs": {},
        "query": user_text,
        "response_mode": "blocking",
        "user": user_id
    }

    try:
        resp = requests.post(
            DIFY_API_URL,
            headers={
                "Authorization": f"Bearer {DIFY_API_KEY}",
                "Content-Type": "application/json"
            },
            json=dify_payload,
            timeout=60
        )
        answer = resp.json().get("answer", "（AI 沒有回應）") if resp.status_code == 200 \
            else "（AI 判斷服務暫時無法使用）"
    except Exception:
        logging.exception("❌ Dify call failed")
        answer = "（AI 判斷服務發生錯誤）"

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
    location = payload.get("location", "台北市")
    time_range = payload.get("time_range", "today")

    return {
        "location": location,
        "time_range": time_range,
        "summary": "未來降雨機率偏高，請留意午後短暫雨",
        "risk_level": "中",
        "source": "中央氣象署"
    }

# ======================================================
# Tool 2️⃣ 行政院即時新聞（防炸版）
# ======================================================
@app.post("/tool/ey/news")
def ey_news(payload: dict = Body(default={})):
    limit = payload.get("limit", 3)

    try:
        r = requests.get(
            f"{EY_API_BASE}/ExecutiveYuan/NewsEy",
            params={"top": limit, "pageIndex": 1},
            headers=COMMON_HEADERS,
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logging.exception("❌ EY News API error")
        return {
            "source": "行政院全球資訊網",
            "type": "即時新聞",
            "error": "目前官方新聞服務暫時無法取得",
            "items": []
        }

    items = data.get("data") or data.get("items") or data
    return {
        "source": "行政院全球資訊網",
        "type": "即時新聞",
        "items": items[:limit]
    }

# ======================================================
# Tool 3️⃣ 行政院重要政策（防炸版）
# ======================================================
@app.post("/tool/ey/policy")
def ey_policy(payload: dict = Body(default={})):
    limit = payload.get("limit", 3)

    try:
        r = requests.get(
            f"{EY_API_BASE}/Performance/EyPolicy",
            params={"top": limit, "pageIndex": 1},
            headers=COMMON_HEADERS,
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        logging.exception("❌ EY Policy API error")
        return {
            "source": "行政院全球資訊網",
            "type": "重要政策",
            "error": "目前政策資料暫時無法取得",
            "items": []
        }

    items = data.get("data") or data.get("items") or data
    return {
        "source": "行政院全球資訊網",
        "type": "重要政策",
        "items": items[:limit]
    }

# ======================================================
# Tool 4️⃣ 消費 / 防災警訊（防炸版）
# ======================================================
@app.post("/tool/ey/consumer-warning")
def ey_consumer_warning(payload: dict = Body(default={})):
    limit = payload.get("limit", 3)

    try:
        r = requests.get(
            f"{EY_API_BASE}/ConsumerProtection/WarningNewsCPC",
            params={"top": limit, "pageIndex": 1},
            headers=COMMON_HEADERS,
            timeout=10
        )
        r.raise_for_status()

        # ✅ 關鍵 1：確認是 JSON
        if "application/json" not in r.headers.get("Content-Type", ""):
            raise ValueError("Not JSON response")

        data = r.json()

    except Exception:
        logging.exception("❌ EY Consumer Warning API error")
        return {
            "source": "行政院消費者保護會",
            "type": "消費警訊",
            "risk_level": "未知",
            "error": "目前官方警訊服務暫時無法取得",
            "items": []
        }

    items = data.get("data") or data.get("items") or []
    return {
        "source": "行政院消費者保護會",
        "type": "消費警訊",
        "risk_level": "中" if items else "低",
        "items": items[:limit]
    }
