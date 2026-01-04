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

    user_message = body["events"][0]["message"]["text"]
    user_id = body["events"][0]["source"]["userId"]

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
        }
    )

    dify_answer = resp.json().get("answer", "（沒有回應）")

    # 這裡先 log，下一步才真的回 LINE
    return {
        "reply": dify_answer
    }
