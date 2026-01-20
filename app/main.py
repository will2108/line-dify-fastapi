from fastapi import FastAPI, Request, Header, HTTPException, Body, Query
import os
import hmac
import hashlib
import base64
import logging
import json
import time
import threading
import requests
import re
from typing import Optional, Dict, Any, List, Tuple

app = FastAPI()
logging.basicConfig(level=logging.INFO)

# =========================================================
# Env
# =========================================================
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_API_URL = os.getenv("DIFY_API_URL", "https://api.dify.ai/v1/chat-messages")

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_REPLY_API = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_API = "https://api.line.me/v2/bot/message/push"

# Optional tool auth (match your OpenAPI schema query param "key")
TOOL_API_KEY = os.getenv("TOOL_API_KEY")  # if set -> require ?key=...

EY_API_BASE = "https://www.ey.gov.tw/OpenData/api"
COMMON_HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}

LINE_DELIVERY_MODE = os.getenv("LINE_DELIVERY_MODE", "ack_push").lower()
LINE_MAX_CHARS = int(os.getenv("LINE_MAX_CHARS", "800"))

NO_TOKEN_FALLBACK_SECONDS = int(os.getenv("NO_TOKEN_FALLBACK_SECONDS", "35"))
OVERALL_DEADLINE_SECONDS = int(os.getenv("OVERALL_DEADLINE_SECONDS", "120"))

ACK_TEXT = os.getenv("ACK_TEXT", "收到，我查一下（約 10 秒）。")
BUSY_TEXT = os.getenv("BUSY_TEXT", "AI 服務忙碌中，請稍後再試")

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# =========================================================
# Debug buffers
# =========================================================
LAST_SEEN: Dict[str, Any] = {
    "at": None,
    "source": None,
    "push_to": None,
    "reply_token": None,
    "user_text": None,
}

SSE_TAIL: List[Dict[str, Any]] = []
SSE_TAIL_MAX = 80

SSE_RAW: List[Dict[str, Any]] = []
SSE_RAW_MAX = 50


# =========================================================
# Health / Debug
# =========================================================
@app.get("/")
def health():
    return {"status": "ok", "mode": LINE_DELIVERY_MODE}


@app.get("/debug/last")
def debug_last():
    return LAST_SEEN


@app.get("/debug/sse")
def debug_sse():
    return {"count": len(SSE_TAIL), "tail": SSE_TAIL[-10:]}


@app.get("/debug/sse/raw_last")
def debug_sse_raw_last():
    return SSE_RAW[-1] if SSE_RAW else {}


@app.get("/debug/sse/raw_tail")
def debug_sse_raw_tail(n: int = Query(5, ge=1, le=50)):
    return {"count": len(SSE_RAW), "tail": SSE_RAW[-n:]}


@app.post("/debug/reset")
def debug_reset():
    SSE_TAIL.clear()
    SSE_RAW.clear()
    return {"ok": True}


# =========================================================
# LINE signature verify
# =========================================================
def verify_line_signature(body: bytes, signature: Optional[str]) -> bool:
    if not signature or not LINE_CHANNEL_SECRET:
        return False
    mac = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode()
    return hmac.compare_digest(expected, signature)


# =========================================================
# Helpers
# =========================================================
def _truncate_for_line(text: str, max_chars: int = LINE_MAX_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    suffix = "\n（內容過長已截斷）"
    keep = max_chars - len(suffix)
    if keep <= 0:
        return text[:max_chars]
    return text[:keep].rstrip() + suffix


def _line_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def _extract_push_to_id(source: Dict[str, Any]) -> str:
    # user / group / room
    return source.get("userId") or source.get("groupId") or source.get("roomId") or ""


def line_reply(reply_token: str, text: str) -> None:
    if not LINE_CHANNEL_ACCESS_TOKEN or not reply_token:
        return
    try:
        resp = requests.post(
            LINE_REPLY_API,
            headers=_line_headers(),
            json={
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": _truncate_for_line(text)}],
            },
            timeout=8,
        )
        if resp.status_code != 200:
            logging.error("❌ LINE reply failed %s: %s", resp.status_code, resp.text)
    except Exception:
        logging.exception("❌ LINE reply exception")


def line_push(to_id: str, text: str) -> None:
    if not LINE_CHANNEL_ACCESS_TOKEN or not to_id:
        return
    try:
        resp = requests.post(
            LINE_PUSH_API,
            headers=_line_headers(),
            json={
                "to": to_id,
                "messages": [{"type": "text", "text": _truncate_for_line(text)}],
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logging.error("❌ LINE push failed %s: %s", resp.status_code, resp.text)
    except Exception:
        logging.exception("❌ LINE push exception")


def _check_tool_key(key: Optional[str]) -> None:
    if not TOOL_API_KEY:
        return
    if not key or key != TOOL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid tool key")


# =========================================================
# Dify SSE parsing
# =========================================================
IGNORE_EVENTS = {
    "workflow_started",
    "node_started",
    "node_finished",
    "agent_log",
    "tool_started",
    "tool_finished",
    "retriever_started",
    "retriever_finished",
}
END_EVENTS = {"message_end", "agent_end", "workflow_finished"}

BAD_LITERALS = {
    "agent_message",
    "agent_thought",
    "agent_log",
    "message",
    "message_delta",
    "workflow_started",
    "workflow_finished",
    "node_started",
    "node_finished",
    "tool_started",
    "tool_finished",
    "retriever_started",
    "retriever_finished",
}


def _sse_tail_add(kind: str, obj: Any = None, **kw) -> None:
    item = {"t": int(time.time()), "kind": kind}
    if isinstance(obj, dict):
        item["event"] = obj.get("event")
        item["keys"] = sorted(list(obj.keys()))[:30]
    if kw:
        item.update(kw)
    SSE_TAIL.append(item)
    if len(SSE_TAIL) > SSE_TAIL_MAX:
        del SSE_TAIL[: len(SSE_TAIL) - SSE_TAIL_MAX]


def _sse_raw_add(obj: Dict[str, Any]) -> None:
    SSE_RAW.append(obj)
    if len(SSE_RAW) > SSE_RAW_MAX:
        del SSE_RAW[: len(SSE_RAW) - SSE_RAW_MAX]


def _looks_bad_text(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return True
    if s in BAD_LITERALS:
        return True
    if UUID_RE.match(s):
        return True
    # looks like event_name (snake_case)
    if len(s) <= 30 and all((c.islower() or c == "_") for c in s) and "_" in s:
        return True
    return False


def _pick_answer_fields(d: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for k in ("answer", "delta", "text", "content"):
        v = d.get(k)
        if isinstance(v, str):
            v = v.strip()
            if v:
                out.append(v)
    return out


def _extract_answer(obj: Dict[str, Any]) -> str:
    """
    IMPORTANT:
    Dify streaming 的 agent_message.answer 可能是一個字一個字吐，
    所以不能用 len<=2 過濾，否則永遠收不到答案。
    """
    event = (obj.get("event") or "").strip()
    if event in IGNORE_EVENTS:
        return ""
    if event == "error":
        return ""

    candidates: List[str] = []
    candidates.extend(_pick_answer_fields(obj))

    data = obj.get("data")
    if isinstance(data, dict):
        candidates.extend(_pick_answer_fields(data))

    for s in candidates:
        if _looks_bad_text(s):
            continue
        return s  # allow 1-char tokens
    return ""


def _is_good_final_thought(obj: Dict[str, Any]) -> bool:
    if (obj.get("event") or "").strip() != "agent_thought":
        return False
    thought = (obj.get("thought") or "").strip()
    if not thought:
        return False
    if UUID_RE.match(thought):
        return False
    # 如果 thought 是工具執行資訊，通常 tool 會有值；你貼的正常答案 tool=""
    tool = (obj.get("tool") or "").strip()
    if tool:
        return False
    if len(thought) < 10:
        return False
    return True


def _parse_sse_block(lines: List[str], last_event_line: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    SSE block example:
      event: agent_message
      data: {...}
      <blank line>
    """
    event_name = last_event_line
    data_str = None

    for raw in lines:
        s = (raw or "").strip()
        if not s:
            continue
        if s.startswith("event:"):
            event_name = s[len("event:") :].strip()
        elif s.startswith("data:"):
            data_str = s[len("data:") :].strip()

    if not data_str or data_str == "[DONE]":
        return (None, event_name)

    try:
        obj = json.loads(data_str)
        if isinstance(obj, dict):
            if "event" not in obj and event_name:
                obj["event"] = event_name
            return (obj, event_name)
        return (None, event_name)
    except Exception:
        _sse_tail_add("bad_json", raw_preview=(data_str or "")[:200])
        return (None, event_name)


def dify_call_agent_streaming(query: str, user_id: str) -> str:
    if not DIFY_API_KEY:
        _sse_tail_add("config_error", note="DIFY_API_KEY missing")
        return "（DIFY_API_KEY 未設定）"

    payload = {"inputs": {}, "query": query, "response_mode": "streaming", "user": user_id}
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    no_text_deadline = time.time() + NO_TOKEN_FALLBACK_SECONDS
    overall_deadline = time.time() + OVERALL_DEADLINE_SECONDS

    parts: List[str] = []
    got_any_answer = False
    last_good_thought = ""

    try:
        with requests.post(
            DIFY_API_URL,
            headers=headers,
            json=payload,
            stream=True,
            timeout=(15, 180),
        ) as r:
            if r.status_code != 200:
                _sse_tail_add("http_error", status=r.status_code, body_preview=(r.text or "")[:300])
                logging.error("❌ Dify response %s: %s", r.status_code, r.text)
                return BUSY_TEXT

            last_event_line = ""
            block: List[str] = []

            for line in r.iter_lines(decode_unicode=True):
                now = time.time()

                if now > overall_deadline:
                    final = "".join(parts).strip()
                    if not final and last_good_thought:
                        final = last_good_thought
                    _sse_tail_add("overall_timeout", got_any_answer=got_any_answer, text_len=len(final))
                    return _truncate_for_line(final) if final else BUSY_TEXT

                if line is None:
                    continue

                # empty line => end of one SSE event
                if line.strip() == "":
                    if block:
                        obj, last_event_line = _parse_sse_block(block, last_event_line)
                        block = []
                        if not obj:
                            continue

                        _sse_raw_add(obj)

                        event = obj.get("event")
                        if event == "error":
                            _sse_tail_add("dify_error_event", obj=obj)
                            return BUSY_TEXT

                        if _is_good_final_thought(obj):
                            last_good_thought = (obj.get("thought") or "").strip()

                        ans = _extract_answer(obj)
                        _sse_tail_add(
                            "event",
                            obj=obj,
                            has_text=bool(ans),
                            chunk_preview=(ans[:80] if ans else ""),
                        )

                        if isinstance(event, str) and event in IGNORE_EVENTS:
                            pass
                        else:
                            if ans:
                                got_any_answer = True
                                parts.append(ans)

                        if isinstance(event, str) and event in END_EVENTS:
                            final = "".join(parts).strip()
                            if not final and last_good_thought:
                                final = last_good_thought
                            _sse_tail_add("end", got_any_answer=got_any_answer, text_len=len(final))
                            return _truncate_for_line(final) if final else BUSY_TEXT

                    # after block handled
                    if (not got_any_answer) and (now > no_text_deadline):
                        _sse_tail_add("no_text_timeout", note="no valid answer yet")
                        return BUSY_TEXT
                    continue

                # non-empty line => add to current block
                block.append(line)

                if (not got_any_answer) and (now > no_text_deadline):
                    _sse_tail_add("no_text_timeout", note="no valid answer yet (mid-block)")
                    return BUSY_TEXT

            # stream EOF: handle remaining block
            if block:
                obj, last_event_line = _parse_sse_block(block, last_event_line)
                if obj:
                    _sse_raw_add(obj)
                    if _is_good_final_thought(obj):
                        last_good_thought = (obj.get("thought") or "").strip()
                    ans = _extract_answer(obj)
                    if ans:
                        got_any_answer = True
                        parts.append(ans)

            final = "".join(parts).strip()
            if not final and last_good_thought:
                final = last_good_thought
            _sse_tail_add("eof", got_any_answer=got_any_answer, text_len=len(final))
            return _truncate_for_line(final) if final else BUSY_TEXT

    except requests.exceptions.Timeout as e:
        _sse_tail_add("timeout_exception", error=str(e)[:200])
        return BUSY_TEXT
    except Exception as e:
        _sse_tail_add("exception", error=str(e)[:200])
        logging.exception("❌ Dify unexpected error")
        return BUSY_TEXT


# =========================================================
# Background workers
# =========================================================
def background_ackpush(query: str, dify_user: str, push_to: str) -> None:
    ans = dify_call_agent_streaming(query, dify_user)
    line_push(push_to, ans or BUSY_TEXT)


def background_replyonce(query: str, dify_user: str, reply_token: str) -> None:
    ans = dify_call_agent_streaming(query, dify_user)
    line_reply(reply_token, ans or BUSY_TEXT)


# =========================================================
# LINE Webhook
# =========================================================
@app.post("/line/webhook")
async def line_webhook(request: Request, x_line_signature: str = Header(None)):
    body = await request.body()
    if not verify_line_signature(body, x_line_signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
    events = payload.get("events") or []
    if not events:
        return {"status": "ok"}

    for event in events:
        if event.get("type") != "message":
            continue

        message = event.get("message", {}) or {}
        if message.get("type") != "text":
            continue

        user_text = (message.get("text") or "").strip()
        if not user_text:
            continue

        reply_token = event.get("replyToken", "")
        source = event.get("source", {}) or {}
        dify_user_id = source.get("userId") or "unknown-user"
        push_to = _extract_push_to_id(source)

        LAST_SEEN.update(
            {
                "at": int(time.time()),
                "source": source,
                "push_to": push_to,
                "reply_token": reply_token,
                "user_text": user_text,
            }
        )

        logging.info("LINE source=%s push_to=%s text=%s", source, push_to, user_text)

        if LINE_DELIVERY_MODE == "reply_once":
            if reply_token:
                threading.Thread(
                    target=background_replyonce,
                    args=(user_text, dify_user_id, reply_token),
                    daemon=True,
                ).start()
        else:
            if reply_token:
                line_reply(reply_token, ACK_TEXT)
            if push_to:
                threading.Thread(
                    target=background_ackpush,
                    args=(user_text, dify_user_id, push_to),
                    daemon=True,
                ).start()

    return {"status": "ok"}


# =========================================================
# Tools for Dify (match your OpenAPI schema)
# =========================================================
@app.post("/tool/weather")
def tool_weather(payload: dict = Body(...), key: Optional[str] = Query(None)):
    _check_tool_key(key)
    location = payload.get("location", "台北市")
    time_range = payload.get("time_range", "today")
    return {
        "result": {
            "location": location,
            "time_range": time_range,
            "summary": "未來降雨機率偏高，請留意午後短暫雨",
            "risk_level": "中",
            "source": "中央氣象署",
        }
    }


@app.post("/tool/ey/news")
def ey_news(payload: dict = Body(default={}), key: Optional[str] = Query(None)):
    _check_tool_key(key)
    limit = int(payload.get("limit", 3))

    items: List[Any] = []
    try:
        r = requests.get(
            f"{EY_API_BASE}/ExecutiveYuan/NewsEy",
            params={"top": limit, "pageIndex": 1},
            headers=COMMON_HEADERS,
            timeout=10,
        )
        r.raise_for_status()

        text = (r.text or "").strip()
        if not text:
            items = []
        elif text.startswith("{") or text.startswith("["):
            data = json.loads(text)
            items = data.get("data") or data.get("items") or []
        else:
            logging.error("❌ EY News non-JSON response: %s", text[:200])
            items = []
    except Exception:
        logging.exception("❌ EY News API error")
        items = []

    return {"source": "行政院全球資訊網", "type": "即時新聞", "items": items[:limit]}


@app.post("/tool/ey/policy")
def ey_policy(payload: dict = Body(default={}), key: Optional[str] = Query(None)):
    _check_tool_key(key)
    limit = int(payload.get("limit", 5))
    # TODO: 之後接真資料 -> 換成 EY OpenData 對應 policy 端點
    return {
        "source": "行政院全球資訊網",
        "type": "政策",
        "items": [],
        "limit": limit,
        "note": "此工具已就緒（不再 404）。若要接真資料，請指定 EY OpenData 對應 policy API。",
    }


@app.post("/tool/ey/consumer-warning")
def ey_consumer_warning(payload: dict = Body(default={}), key: Optional[str] = Query(None)):
    _check_tool_key(key)
    limit = int(payload.get("limit", 5))
    # TODO: 之後接真資料 -> 換成 EY OpenData 對應 consumer warning 端點
    return {
        "source": "行政院全球資訊網",
        "type": "消費警示",
        "items": [],
        "limit": limit,
        "note": "此工具已就緒（不再 404）。若要接真資料，請指定 EY OpenData 對應 consumer-warning API。",
    }
