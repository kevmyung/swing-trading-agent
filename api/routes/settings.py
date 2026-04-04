"""Settings routes — /api/settings/*"""

import json
import logging
import os

from fastapi import APIRouter

from api.shared import get_cloud_config, read_settings, write_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


# ─── Secrets Manager helpers ────────────────────────────────────────────────


def _resolve_secret_arns() -> tuple[str | None, str | None]:
    """Resolve Alpaca and Polygon secret ARNs.

    Checks env vars first (AgentCore Runtime sets these), then falls back
    to SSM Parameter Store lookups (for API server running locally).
    """
    alpaca_arn = os.environ.get("ALPACA_SECRET_ARN")
    polygon_arn = os.environ.get("POLYGON_SECRET_ARN")
    if alpaca_arn and polygon_arn:
        return alpaca_arn, polygon_arn

    try:
        import boto3
        cfg = get_cloud_config() or {}
        region = cfg.get("region", os.environ.get("AWS_REGION", "us-west-2"))
        ssm = boto3.client("ssm", region_name=region)
        project = os.environ.get("PROJECT_NAME", "swing-trading-agent")
        env_name = os.environ.get("ENVIRONMENT", "dev")
        if not alpaca_arn:
            try:
                resp = ssm.get_parameter(Name=f"/{project}/{env_name}/secrets/alpaca-arn")
                alpaca_arn = resp["Parameter"]["Value"]
            except Exception as e:
                logger.error("Failed to resolve Alpaca secret ARN from SSM: %s", e)
        if not polygon_arn:
            try:
                resp = ssm.get_parameter(Name=f"/{project}/{env_name}/secrets/polygon-arn")
                polygon_arn = resp["Parameter"]["Value"]
            except Exception as e:
                logger.error("Failed to resolve Polygon secret ARN from SSM: %s", e)
    except Exception as e:
        logger.error("Failed to connect to SSM for secret ARN resolution: %s", e)

    return alpaca_arn, polygon_arn


def _sync_keys_to_secrets_manager(keys: dict):
    """Write API keys to AWS Secrets Manager (cloud mode only)."""
    alpaca_arn, polygon_arn = _resolve_secret_arns()

    if not alpaca_arn and not polygon_arn:
        logger.warning("No secret ARNs found — cannot sync to Secrets Manager")
        raise RuntimeError("Secret ARNs not configured — check SSM parameters")

    import boto3
    cfg = get_cloud_config() or {}
    region = cfg.get("region", os.environ.get("AWS_REGION", "us-west-2"))
    sm = boto3.client("secretsmanager", region_name=region)

    if alpaca_arn:
        secret_value = json.dumps({
            "paper": {
                "api_key": keys.get("alpaca_paper_api_key", ""),
                "secret_key": keys.get("alpaca_paper_secret_key", ""),
            },
            "live": {
                "api_key": keys.get("alpaca_live_api_key", ""),
                "secret_key": keys.get("alpaca_live_secret_key", ""),
            },
        })
        sm.put_secret_value(SecretId=alpaca_arn, SecretString=secret_value)
        logger.info("Synced Alpaca keys to Secrets Manager (%s)", alpaca_arn[-20:])

    if polygon_arn and keys.get("polygon_api_key"):
        secret_value = json.dumps({"api_key": keys.get("polygon_api_key", "")})
        sm.put_secret_value(SecretId=polygon_arn, SecretString=secret_value)
        logger.info("Synced Polygon key to Secrets Manager")


# ─── API Keys ───────────────────────────────────────────────────────────────


@router.get("/keys")
def get_api_keys():
    """Get stored API keys (secrets are masked)."""
    settings = read_settings()
    keys = settings.get("keys", {})
    masked = {}
    for k, v in keys.items():
        if ("secret" in k or "api_key" in k) and v:
            masked[k] = v[:4] + "••••" if len(v) > 4 else "••••"
        else:
            masked[k] = v
    return masked


@router.put("/keys")
def save_api_keys(body: dict):
    """Save API keys. Values containing '••••' are treated as unchanged."""
    settings = read_settings()
    existing = settings.get("keys", {})
    for k, v in body.items():
        if "••••" not in str(v):
            existing[k] = v
    settings["keys"] = existing
    write_settings(settings)

    try:
        _sync_keys_to_secrets_manager(existing)
    except Exception as e:
        logger.warning("Failed to sync keys to Secrets Manager: %s", e)
        return {"status": "saved", "secrets_manager": "failed", "error": str(e)}
    return {"status": "saved", "secrets_manager": "synced"}


# ─── Model Settings ────────────────────────────────────────────────────────


@router.get("/model")
def get_model_settings():
    """Get current model configuration."""
    settings = read_settings()
    model_cfg = settings.get("model", {})
    from config.settings import get_settings
    s = get_settings()
    return {
        "model_id": model_cfg.get("model_id", s.bedrock_model_id),
        "extended_thinking_enabled": model_cfg.get("extended_thinking_enabled", s.extended_thinking_enabled),
        "extended_thinking_budget": model_cfg.get("extended_thinking_budget", s.extended_thinking_budget),
        "extended_thinking_effort": model_cfg.get("extended_thinking_effort", s.extended_thinking_effort),
    }


@router.put("/model")
def save_model_settings(body: dict):
    """Save model settings and apply to runtime."""
    from config.settings import get_settings

    settings = read_settings()
    settings["model"] = {
        "model_id": body.get("model_id", get_settings().bedrock_model_id),
        "extended_thinking_enabled": body.get("extended_thinking_enabled", False),
        "extended_thinking_budget": body.get("extended_thinking_budget", 2048),
        "extended_thinking_effort": body.get("extended_thinking_effort", "medium"),
    }
    write_settings(settings)

    os.environ["BEDROCK_MODEL_ID"] = settings["model"]["model_id"]
    os.environ["EXTENDED_THINKING_ENABLED"] = str(settings["model"]["extended_thinking_enabled"]).lower()
    os.environ["EXTENDED_THINKING_BUDGET"] = str(settings["model"]["extended_thinking_budget"])
    os.environ["EXTENDED_THINKING_EFFORT"] = settings["model"]["extended_thinking_effort"]
    get_settings.cache_clear()

    return {"status": "saved", "model": settings["model"]}
