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
    """Check if the US stock market is open today using Alpaca's clock API.

    Uses raw HTTP (no alpaca-py dependency). Fails open on errors.
    """
    secret_arn = os.environ.get("ALPACA_SECRET_ARN", "")
    if not secret_arn:
        logger.warning("ALPACA_SECRET_ARN not set — skipping market calendar check.")
        return True

    try:
        resp = secrets.get_secret_value(SecretId=secret_arn)
        creds = json.loads(resp["SecretString"])
        # Secret structure: {"paper": {"api_key": ..., "secret_key": ...}, "live": {...}}
        # Scheduler config determines mode; default to paper for safety
        mode_config = _get_config()
        mode = mode_config.get("mode", "paper")
        mode_creds = creds.get(mode, creds.get("paper", {}))
        api_key = mode_creds.get("api_key", "")
        secret_key = mode_creds.get("secret_key", "")

        base_url = "https://paper-api.alpaca.markets" if mode == "paper" else "https://api.alpaca.markets"
        req = Request(
            f"{base_url}/v2/clock",
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": secret_key,
            },
        )
        with urlopen(req, timeout=5) as resp:
            clock = json.loads(resp.read().decode())

        if clock.get("is_open"):
            return True

        # Market is closed now. Check if next_open is today.
        next_open = clock.get("next_open", "")
        if next_open:
            # next_open format: "2026-04-04T13:30:00Z" or with offset
            next_open_date = next_open[:10]
            from datetime import datetime, timezone, timedelta
            # Current time in US/Eastern
            et_offset = timezone(timedelta(hours=-4))  # EDT (summer)
            now_et = datetime.now(et_offset)
            today_str = now_et.strftime("%Y-%m-%d")
            if next_open_date == today_str:
                return True
            logger.info("Market closed today (%s). Next open: %s.", today_str, next_open_date)
            return False

        return True
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
