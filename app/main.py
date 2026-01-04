from fastapi import FastAPI, Request
import requests
import os

app = FastAPI()

DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_API_URL = os.getenv("DIFY_API_URL", "https://api.dify.ai/v1/chat-messages")

@app.get("/")
def health():
    return {"status": "ok"}

@app.post("/line/webhook")
async def line_webhook(request: Request):
    body = await request.json()
    print("LINE payload:", body)

    events = body.get("events", [])
    if not events:
        # 沒事件也要回 200
        return {"status": "ok"}

    event = events[0]

    # 不是 message（例如 Verify webhook）
    if event.get("type") != "message":
        return {"status": "ok"}

    # 不是文字（貼圖、圖片）
    message = event.get("message", {})
    if message.get("type") != "text":
        return {"status": "ok"}

    user_message = message.get("text")
    user_id = event.get("source", {}).get("userId")

    # 保護：沒有 key 就不要打 API
    if not DIFY_API_KEY:
        print("DIFY_API_KEY not set")
        return {"status": "ok"}

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
        timeout=10
    )

    print("Dify response:", resp.text)

    return {"status": "ok"}
