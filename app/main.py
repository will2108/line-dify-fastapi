from fastapi import FastAPI, Request, Header
from fastapi.responses import JSONResponse
import os
import requests
import hmac
import hashlib
import base64
import json

app = FastAPI()

# ===== 環境變數 =====
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_API_URL = os.getenv("DIFY_API_URL", "https://api.dify.ai/v1/chat-messages")

LINE_REPLY_API = "https://api.line.me/v2/bot/message/reply"

# ===== 健康檢查 =====
@app.get("/")
def health():
    return {"status": "ok"}

# ===== LINE Webhook =====
@app.post("/line/webhook")
async def line_webhook(
    request: Request,
    x_line_signature: str = Header(None)
):
    body_bytes = await request.body()
    body = body_bytes.decode("utf-8")

    # ---- 1️ 驗證 LINE Signature ----
    if not validate_signature(body_bytes, x_line_signature):
        #  驗證失敗也要回 200，不然 LINE 會 retry
        return JSONResponse(content={"status": "invalid signature"}, status_code=200)

    data = json.loads(body)

    # ---- 2️ 只處理 message / text ----
    events = data.get("events", [])
    if not events:
        return JSONResponse(content={"status": "no events"}, status_code=200)

    event = events[0]

    if event.get("type") != "message":
        return JSONResponse(content={"status": "not message"}, status_code=200)

    message = event.get("message", {})
    if message.get("type") != "text":
        return JSONResponse(content={"status": "not text"}, status_code=200)

    user_text = message.get("text")
    reply_token = event.get("replyToken")
    user_id = event.get("source", {}).get("userId", "anonymous")

    # ---- 3️ 呼叫 Dify ----
    dify_reply = call_dify(user_text, user_id)

    # ---- 4️ 回 LINE ----
    reply_line(reply_token, dify_reply)

    # 一定回 200
    return JSONResponse(content={"status": "ok"}, status_code=200)


# ===== 工具函式 =====
def validate_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET or not signature:
        return False

    hash = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()

    computed_signature = base64.b64encode(hash).decode("utf-8")
    return computed_signature == signature


def call_dify(text: str, user_id: str) -> str:
    try:
        resp = requests.post(
            DIFY_API_URL,
            headers={
                "Authorization": f"Bearer {DIFY_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "query": text,
                "user": user_id,
                "response_mode": "blocking"
            },
            timeout=20
        )
        resp.raise_for_status()
        return resp.json().get("answer", "⚠️ Dify 沒有回覆")
    except Exception as e:
        return "⚠️ 系統暫時忙碌，請稍後再試"


def reply_line(reply_token: str, text: str):
    try:
        requests.post(
            LINE_REPLY_API,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json"
            },
            json={
                "replyToken": reply_token,
                "messages": [
                    {"type": "text", "text": text}
                ]
            },
            timeout=10
        )
    except Exception:
        pass
