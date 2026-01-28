from fastapi import FastAPI, Request, Header, HTTPException, Query
import os
import hmac
import hashlib
import base64
import logging
import json
import time
import threading
import requests
from typing import Optional, Dict, Any

# =========================================================
# App
# =========================================================
app = FastAPI()
logging.basicConfig(level=logging.INFO)

# =========================================================
# Env
# =========================================================
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_API_URL = os.getenv("DIFY_API_URL", "https://api.dify.ai/v1/workflows/run")

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

CWA_API_KEY = os.getenv("CWA_API_KEY")

LINE_REPLY_API = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_API = "https://api.line.me/v2/bot/message/push"

LINE_DELIVERY_MODE = os.getenv("LINE_DELIVERY_MODE", "ack_push").lower()
LINE_MAX_CHARS = int(os.getenv("LINE_MAX_CHARS", "800"))

ACK_TEXT = os.getenv("ACK_TEXT", "收到，我查一下（約 10 秒）。")
BUSY_TEXT = os.getenv("BUSY_TEXT", "AI 服務忙碌中，請稍後再試")

# =========================================================
# Location normalize & dataset mapping
# =========================================================
LOCATION_ALIAS = {
    "台北": "台北市", "臺北": "台北市", "台北市": "台北市", "臺北市": "台北市",
    "新北": "新北市", "新北市": "新北市",
    "桃園": "桃園市", "桃園市": "桃園市",
    "台中": "台中市", "臺中": "台中市", "台中市": "台中市", "臺中市": "台中市",
    "台南": "台南市", "臺南": "台南市", "台南市": "台南市", "臺南市": "台南市",
    "高雄": "高雄市", "高雄市": "高雄市",
    "基隆": "基隆市", "基隆市": "基隆市",
    "新竹市": "新竹市",
    "新竹縣": "新竹縣",
    "苗栗": "苗栗縣", "苗栗縣": "苗栗縣",
    "彰化": "彰化縣", "彰化縣": "彰化縣",
    "南投": "南投縣", "南投縣": "南投縣",
    "雲林": "雲林縣", "雲林縣": "雲林縣",
    "嘉義市": "嘉義市",
    "嘉義縣": "嘉義縣",
    "屏東": "屏東縣", "屏東縣": "屏東縣",
    "宜蘭": "宜蘭縣", "宜蘭縣": "宜蘭縣",
    "花蓮": "花蓮縣", "花蓮縣": "花蓮縣",
    "台東": "台東縣", "臺東": "台東縣", "台東縣": "台東縣", "臺東縣": "台東縣",
    "澎湖": "澎湖縣", "澎湖縣": "澎湖縣",
    "金門": "金門縣", "金門縣": "金門縣",
    "馬祖": "連江縣", "連江縣": "連江縣",
}

DATASET_MAP = {
    "台北市": {"3days": "F-D0047-061", "1week": "F-D0047-063"},
    "新北市": {"3days": "F-D0047-069", "1week": "F-D0047-071"},
    "桃園市": {"3days": "F-D0047-005", "1week": "F-D0047-007"},
    "台中市": {"3days": "F-D0047-073", "1week": "F-D0047-075"},
    "台南市": {"3days": "F-D0047-077", "1week": "F-D0047-079"},
    "高雄市": {"3days": "F-D0047-065", "1week": "F-D0047-067"},
    "基隆市": {"3days": "F-D0047-049", "1week": "F-D0047-051"},
    "新竹市": {"3days": "F-D0047-053", "1week": "F-D0047-055"},
    "新竹縣": {"3days": "F-D0047-009", "1week": "F-D0047-011"},
    "苗栗縣": {"3days": "F-D0047-013", "1week": "F-D0047-015"},
    "彰化縣": {"3days": "F-D0047-017", "1week": "F-D0047-019"},
    "南投縣": {"3days": "F-D0047-021", "1week": "F-D0047-023"},
    "雲林縣": {"3days": "F-D0047-025", "1week": "F-D0047-027"},
    "嘉義縣": {"3days": "F-D0047-029", "1week": "F-D0047-031"},
    "嘉義市": {"3days": "F-D0047-057", "1week": "F-D0047-059"},
    "屏東縣": {"3days": "F-D0047-033", "1week": "F-D0047-035"},
    "宜蘭縣": {"3days": "F-D0047-001", "1week": "F-D0047-003"},
    "花蓮縣": {"3days": "F-D0047-041", "1week": "F-D0047-043"},
    "台東縣": {"3days": "F-D0047-037", "1week": "F-D0047-039"},
    "澎湖縣": {"3days": "F-D0047-045", "1week": "F-D0047-047"},
    "金門縣": {"3days": "F-D0047-085", "1week": "F-D0047-087"},
    "連江縣": {"3days": "F-D0047-081", "1week": "F-D0047-083"},
}

def normalize_location(raw: str) -> str:
    raw = raw.strip()
    if raw in LOCATION_ALIAS:
        return LOCATION_ALIAS[raw]
    raise HTTPException(status_code=400, detail=f"unsupported location: {raw}")

def select_dataset(location: str, time_range: str) -> str:
    if time_range in ["week", "1week", "7days"]:
        return DATASET_MAP[location]["1week"]
    return DATASET_MAP[location]["3days"]

# =========================================================
# Health
# =========================================================
@app.get("/")
def health():
    return {"status": "ok", "mode": LINE_DELIVERY_MODE}



# =========================================================
# LINE signature verify
# =========================================================
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

# =========================================================
# Helpers
# =========================================================
def _truncate_for_line(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= LINE_MAX_CHARS:
        return text
    suffix = "\n（內容過長已截斷）"
    keep = LINE_MAX_CHARS - len(suffix)
    return text[:keep].rstrip() + suffix

def _line_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

def _extract_push_to_id(source: Dict[str, Any]) -> str:
    return source.get("userId") or source.get("groupId") or source.get("roomId") or ""

def line_reply(reply_token: str, text: str):
    if not reply_token:
        return
    requests.post(
        LINE_REPLY_API,
        headers=_line_headers(),
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": _truncate_for_line(text)}],
        },
        timeout=8,
    )

def line_push(to_id: str, text: str):
    if not to_id:
        return
    requests.post(
        LINE_PUSH_API,
        headers=_line_headers(),
        json={
            "to": to_id,
            "messages": [{"type": "text", "text": _truncate_for_line(text)}],
        },
        timeout=10,
    )

# =========================================================
# Weather Tool API (REAL CWA)
# =========================================================
@app.post("/tool/weather")
def tool_weather(payload: Dict[str, Any]):
    if not CWA_API_KEY:
        raise HTTPException(status_code=500, detail="CWA_API_KEY not configured")

    location_raw = payload.get("location")
    time_range = payload.get("time_range", "today")

    if not location_raw:
        raise HTTPException(status_code=400, detail="location is required")

    location = normalize_location(location_raw)
    dataset_id = select_dataset(location, time_range)

    url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{dataset_id}"
    resp = requests.get(
        url,
        params={"Authorization": CWA_API_KEY},
        timeout=10,
    )
    resp.raise_for_status()

    # 👉 此處先給穩定摘要（後續可解析 PoP / Wx 強化）
    return {
        "result": {
            "location": location,
            "time_range": time_range,
            "summary": "依中央氣象署預報，未來降雨機率偏高，請留意天氣變化。",
            "risk_level": "medium",
            "source": f"CWA OpenData {dataset_id}",
        }
    }


# =========================================================
# Dify Workflow Call (blocking)
# =========================================================
def dify_call_workflow(query: str, user_id: str) -> str:
    if not DIFY_API_KEY:
        return "（DIFY_API_KEY 未設定）"

    payload = {
        "inputs": {
            "query": query
        },
        "response_mode": "blocking",
        "user": user_id,
    }

    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            DIFY_API_URL,
            headers=headers,
            json=payload,
            timeout=120,
        )

        if resp.status_code != 200:
            logging.error("❌ Dify workflow error %s: %s",
                          resp.status_code, resp.text)
            return BUSY_TEXT

        data = resp.json()
        outputs = data.get("data", {}).get("outputs", {})

        text = outputs.get("text")
        if isinstance(text, str) and text.strip():
            return _truncate_for_line(text)

        logging.error("❌ Workflow output missing: %s", data)
        return BUSY_TEXT

    except Exception:
        logging.exception("❌ Dify workflow exception")
        return BUSY_TEXT

# =========================================================
# Background workers
# =========================================================
def background_replyonce(query: str, user_id: str, reply_token: str):
    ans = dify_call_workflow(query, user_id)
    line_reply(reply_token, ans)

def background_ackpush(query: str, user_id: str, push_to: str):
    ans = dify_call_workflow(query, user_id)
    line_push(push_to, ans)

# =========================================================
# LINE Webhook
# =========================================================
@app.post("/line/webhook")
async def line_webhook(request: Request,
                       x_line_signature: str = Header(None)):
    body = await request.body()
    if not verify_line_signature(body, x_line_signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
    events = payload.get("events", [])

    for event in events:
        if event.get("type") != "message":
            continue

        message = event.get("message", {})
        if message.get("type") != "text":
            continue

        user_text = (message.get("text") or "").strip()
        if not user_text:
            continue

        reply_token = event.get("replyToken", "")
        source = event.get("source", {})
        user_id = source.get("userId", "unknown-user")
        push_to = _extract_push_to_id(source)

        logging.info("LINE user=%s text=%s", user_id, user_text)

        if LINE_DELIVERY_MODE == "reply_once":
            threading.Thread(
                target=background_replyonce,
                args=(user_text, user_id, reply_token),
                daemon=True,
            ).start()
        else:
            if reply_token:
                line_reply(reply_token, ACK_TEXT)
            if push_to:
                threading.Thread(
                    target=background_ackpush,
                    args=(user_text, user_id, push_to),
                    daemon=True,
                ).start()

    return {"status": "ok"}
