from fastapi import FastAPI, Request, Header, HTTPException
import requests
import os
import hmac
import hashlib
import base64

app = FastAPI()

DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_API_URL = "https://api.dify.ai/v1/chat-messages"

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

LINE_REPLY_API = "https://api.line.me/v2/bot/message/reply"


@app.get("/")
def health():
    return {"status": "ok"}


def verify_line_signature(body: bytes, signature: str | None):
    if not signature:
        return False
    hash = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash).decode()
    return hmac.compare_digest(expected, signature)


@app.post("/line/webhook")
async def line_webhook(
    request: Request,
    x_line_signature: str = Header(None)
):
    body = await request.body()

    # 1️⃣ 驗證 LINE 簽章（Verify 會帶）
    if not verify_line_signature(body, x_line_signature):
        # Verify 失敗 LINE 會顯示 NG
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()

    # 2️⃣ ⭐ 非常重要：LINE Verify / 系統事件，events 可能是空的
    if "events" not in payload or len(payload["events"]) == 0:
        return {"status": "ok"}  # 一定要 200

    event = payload["events"][0]

    # 3️⃣ 只處理「文字訊息」
    if event.get("type") != "message":
        return {"status": "ok"}

    message = event.get("message", {})
    if message.get("type") != "text":
        return {"status": "ok"}

    user_text = message["text"]
    reply_token = event["replyToken"]
    user_id = event["source"]["userId"]

    # 4️⃣ 呼叫 Dify
    dify_resp = requests.post(
        DIFY_API_URL,
        headers={
            "Authorization": f"Bearer {DIFY_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "query": user_text,
            "user": user_id,
            "response_mode": "blocking"
        },
        timeout=30
    )

    if dify_resp.status_code != 200:
        dify_answer = "（Dify API 錯誤）"
    else:
        dify_answer = dify_resp.json().get("answer", "（AI 沒有回應）")

    # 5️⃣ 回 LINE
    requests.post(
        LINE_REPLY_API,
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
