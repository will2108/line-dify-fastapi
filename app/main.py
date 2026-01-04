from fastapi import FastAPI, Request, Header
import requests
import os
import hmac
import hashlib
import base64

app = FastAPI()

DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_API_URL = os.getenv("DIFY_API_URL", "https://api.dify.ai/v1/chat-messages")

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

@app.get("/")
def health():
    return {"status": "ok"}

def verify_line_signature(body: bytes, signature: str):
    hash = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    computed = base64.b64encode(hash).decode("utf-8")
    return computed == signature

@app.post("/line/webhook")
async def line_webhook(
    request: Request,
    x_line_signature: str = Header(None)
):
    body = await request.body()

    # 1️ 驗證 LINE 來源
    if not verify_line_signature(body, x_line_signature):
        return {"status": "invalid signature"}

    data = await request.json()
    event = data["events"][0]

    # LINE verify 時沒有 message，要直接回 200
    if "message" not in event:
        return {"status": "ok"}

    user_message = event["message"]["text"]
    reply_token = event["replyToken"]
    user_id = event["source"]["userId"]

    # 2️ 呼叫 Dify
    resp = requests.post(
        DIFY_API_URL,
        headers={
            "Authorization": f"Bearer {DIFY_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "query": user_message,
            "user": user_id,
            "response_mode": "blocking"
        },
        timeout=30
    )

    dify_answer = resp.json().get("answer", "（沒有回應）")

    # 3️ 回 LINE
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "replyToken": reply_token,
            "messages": [
                {"type": "text", "text": dify_answer}
            ]
        }
    )

    return {"status": "ok"}
