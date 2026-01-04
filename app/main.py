from fastapi import FastAPI, Request, Header, HTTPException
import requests
import os
import hmac
import hashlib
import base64
import logging

app = FastAPI()
logging.basicConfig(level=logging.INFO)

# -------------------------
# Env
# -------------------------
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_API_URL = "https://api.dify.ai/v1/chat-messages"  # ✅ Chat / Agent Assistant

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_REPLY_API = "https://api.line.me/v2/bot/message/reply"


@app.get("/")
def health():
    return {"status": "ok"}


# -------------------------
# LINE signature verify
# -------------------------
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


# -------------------------
# Webhook
# -------------------------
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

    # -------------------------
    # Call Dify Chat / Agent
    # -------------------------
    dify_payload = {
        "inputs": {},                # ✅ 必填，就算不用
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

        logging.info(f"🤖 Dify status: {resp.status_code}")
        logging.info(f"🤖 Dify body: {resp.text}")

        if resp.status_code != 200:
            answer = "（Dify API 錯誤）"
        else:
            answer = resp.json().get("answer", "（AI 沒有回應）")

    except Exception:
        logging.exception("❌ Dify call failed")
        answer = "（Dify 呼叫失敗）"

    # -------------------------
    # Reply to LINE
    # -------------------------
    requests.post(
        LINE_REPLY_API,
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "replyToken": reply_token,
            "messages": [
                {"type": "text", "text": answer}
            ]
        }
    )

    return {"status": "ok"}
