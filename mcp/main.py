from fastapi import FastAPI, Body
import boto3
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Any

MCP_BASE_URL = os.getenv("MCP_BASE_URL")

MCP_COST_URL = f"{MCP_BASE_URL}/mcp/get_cost_projection"
MCP_HEALTH_URL = f"{MCP_BASE_URL}/mcp/get_service_health"

# =====================================================
# App
# =====================================================
app = FastAPI(
    title="MCP AppRunner Monitor",
    description="MCP Server for App Runner health & cost analysis",
    version="0.1.0",
)

# =====================================================
# Environment (防呆)
# =====================================================
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-1")

# =====================================================
# TTL Cache (in-memory, MVP 專用)
# =====================================================
_CACHE: Dict[str, Dict[str, Any]] = {}
TTL_SECONDS = 120  # 2 minutes


def get_cache(key: str):
    entry = _CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry["ts"] > TTL_SECONDS:
        del _CACHE[key]
        return None
    return entry["data"]


def set_cache(key: str, data: Any):
    _CACHE[key] = {"ts": time.time(), "data": data}


# =====================================================
# AWS Client Factories (❗重點：不要在 module level 建 client)
# =====================================================
def get_cloudwatch():
    return boto3.client(
        "cloudwatch",
        region_name=AWS_REGION,
    )


def get_cost_explorer():
    # ⚠️ Cost Explorer 只能在 us-east-1
    return boto3.client(
        "ce",
        region_name="us-east-1",
    )


# =====================================================
# Tool 1️⃣ App Runner Service Health
# =====================================================
@app.post("/mcp/get_service_health")
def get_service_health(payload: dict = Body(...)):
    """
    Analyze App Runner service health based on CloudWatch metrics.
    """
    service_name = payload.get("service_name")
    window = payload.get("window", "1h")

    if not service_name:
        return {"error": "service_name is required"}

    cache_key = f"health:{service_name}:{window}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    cloudwatch = get_cloudwatch()

    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=1)

    # MVP：RequestCount
    resp = cloudwatch.get_metric_statistics(
        Namespace="AWS/AppRunner",
        MetricName="RequestCount",
        Dimensions=[
            {"Name": "ServiceName", "Value": service_name}
        ],
        StartTime=start_time,
        EndTime=end_time,
        Period=300,
        Statistics=["Sum"],
    )

    datapoints = resp.get("Datapoints", [])
    total_requests = sum(p["Sum"] for p in datapoints)

    # ⚠️ MVP 假設 error rate（後續可拉 5XX）
    error_rate = 0.03 if total_requests > 100 else 0.0

    # 健康判斷
    system_health = "healthy"
    if error_rate > 0.02:
        system_health = "degraded"
    if error_rate > 0.05:
        system_health = "unhealthy"

    result = {
        "service": service_name,
        "window": window,
        "system_health": system_health,
        "signals": {
            "request_count": total_requests,
            "error_rate": error_rate,
        },
        "trends": {
            "traffic_trend": "rising" if total_requests > 100 else "stable"
        },
        "suspected_causes": (
            ["traffic_spike"] if total_requests > 100 else []
        ),
        "confidence": 0.75,
        "recommended_actions": [
            "Check App Runner concurrency settings",
            "Review downstream timeout",
            "Consider auto-scaling policy",
        ],
    }

    set_cache(cache_key, result)
    return result


# =====================================================
# Tool 2️⃣ Cost Projection (AWS Cost Explorer)
# =====================================================
@app.post("/mcp/get_cost_projection")
def get_cost_projection(payload: dict = Body(...)):
    """
    Estimate monthly cost trend using Cost Explorer.
    """
    timeframe = payload.get("timeframe", "7d")

    cache_key = f"cost:{timeframe}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    ce = get_cost_explorer()

    today = datetime.utcnow().date()
    start_date = today - timedelta(days=7)

    resp = ce.get_cost_and_usage(
        TimePeriod={
            "Start": start_date.isoformat(),
            "End": today.isoformat(),
        },
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
    )

    daily_costs = [
        float(day["Total"]["UnblendedCost"]["Amount"])
        for day in resp["ResultsByTime"]
    ]

    total_cost = sum(daily_costs)
    avg_daily = total_cost / len(daily_costs)
    projected_monthly = avg_daily * 30

    result = {
        "timeframe": timeframe,
        "current_cost_usd": round(total_cost, 2),
        "average_daily_usd": round(avg_daily, 2),
        "projected_monthly_usd": round(projected_monthly, 2),
        "baseline_monthly_usd": 30,
        "burn_rate": round(projected_monthly / 30, 2),
        "anomaly": projected_monthly > 45,
        "drivers": ["traffic_increase"],
        "recommended_actions": [
            "Review App Runner instance size",
            "Check idle concurrency",
            "Introduce caching or request batching",
        ],
    }

    set_cache(cache_key, result)
    return result


# =====================================================
# Health Check (給 App Runner / ALB 用)
# =====================================================
@app.get("/")
def health():
    return {"status": "ok", "component": "mcp-monitor"}
