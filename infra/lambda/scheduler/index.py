"""
Lambda function that triggers a trading cycle on the AgentCore Runtime.

Invoked by EventBridge Scheduler (3 rules: MORNING / INTRADAY / EOD_SIGNAL).
Reads dynamic config (session_id, mode) from SSM Parameter Store — written
by the dashboard API when the user starts/stops trading from the UI.

Environment variables (set by CDK):
    AGENTCORE_RUNTIME_ARN  — AgentCore runtime ARN
    SCHEDULER_CONFIG_PARAM — SSM parameter name with runtime config
    ALPACA_SECRET_ARN      — Secrets Manager ARN for Alpaca API credentials
"""

import hashlib
import json
import logging
import os
from urllib.request import Request, urlopen
from urllib.error import URLError

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
secrets = boto3.client("secretsmanager")


def _get_config() -> dict:
    """Read scheduler config from SSM Parameter Store."""
    param_name = os.environ["SCHEDULER_CONFIG_PARAM"]
    resp = ssm.get_parameter(Name=param_name)
    return json.loads(resp["Parameter"]["Value"])


def _is_market_open_today() -> bool:
    """Check if the US stock market is/was open today using Alpaca's calendar API.

    Uses raw HTTP (no alpaca-py dependency). Handles post-close EOD triggers
    by checking the calendar for today's date, not just the clock.
    Fails open on errors.
    """
    secret_arn = os.environ.get("ALPACA_SECRET_ARN", "")
    if not secret_arn:
        logger.warning("ALPACA_SECRET_ARN not set — skipping market calendar check.")
        return True

    try:
        resp = secrets.get_secret_value(SecretId=secret_arn)
        creds = json.loads(resp["SecretString"])
        mode_config = _get_config()
        mode = mode_config.get("mode", "paper")
        mode_creds = creds.get(mode, creds.get("paper", {}))
        api_key = mode_creds.get("api_key", "")
        secret_key = mode_creds.get("secret_key", "")

        base_url = "https://paper-api.alpaca.markets" if mode == "paper" else "https://api.alpaca.markets"
        headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        }

        from datetime import datetime, timezone, timedelta
        et_offset = timezone(timedelta(hours=-4))  # EDT
        today_str = datetime.now(et_offset).strftime("%Y-%m-%d")

        # Use Calendar API: if today is in the calendar, the market was open today
        req = Request(
            f"{base_url}/v2/calendar?start={today_str}&end={today_str}",
            headers=headers,
        )
        with urlopen(req, timeout=5) as resp:
            cal = json.loads(resp.read().decode())

        if cal and cal[0].get("date") == today_str:
            return True

        logger.info("Market closed today (%s) — not in calendar.", today_str)
        return False
    except Exception as exc:
        logger.warning("Market calendar check failed (%s) — assuming open.", exc)
        return True


def handler(event, context):
    cycle = event.get("cycle", "EOD_SIGNAL")
    if cycle not in ("EOD_SIGNAL", "MORNING", "INTRADAY"):
        raise ValueError(f"Invalid cycle type: {cycle}")

    # Read dynamic config
    config = _get_config()
    if not config.get("enabled"):
        logger.info("Scheduler disabled — skipping %s cycle", cycle)
        return {"statusCode": 200, "skipped": True, "reason": "disabled"}

    # Check market calendar — skip holidays
    if not _is_market_open_today():
        logger.info("Market closed today — skipping %s cycle", cycle)
        return {"statusCode": 200, "skipped": True, "reason": "market_closed"}

    session_id = config.get("session_id", "live-scheduled")
    mode = config.get("mode", "paper")
    runtime_arn = os.environ["AGENTCORE_RUNTIME_ARN"]

    # Deterministic runtime session ID (same algorithm as api/server.py)
    runtime_session_id = hashlib.sha256(session_id.encode()).hexdigest()[:33]

    invoke_input = {
        "action": "run",
        "mode": mode,
        "cycle": cycle,
        "session_id": session_id,
    }
    model_id = config.get("model_id")
    if model_id:
        invoke_input["model_id"] = model_id

    payload = json.dumps({"input": invoke_input})

    client = boto3.client("bedrock-agentcore")

    logger.info(
        "Invoking AgentCore: cycle=%s mode=%s session=%s",
        cycle, mode, session_id,
    )

    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        qualifier="DEFAULT",
        runtimeSessionId=runtime_session_id,
        contentType="application/json",
        accept="application/json",
        payload=payload.encode(),
    )

    body = ""
    if "body" in response:
        body = response["body"].read().decode("utf-8")

    logger.info("AgentCore response: %s", body[:500])

    return {
        "statusCode": 200,
        "cycle": cycle,
        "mode": mode,
        "session_id": session_id,
        "response": body[:500],
    }
