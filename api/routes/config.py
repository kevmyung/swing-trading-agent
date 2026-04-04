"""Config & cloud mode routes — /api/config/*"""

import json

from fastapi import APIRouter

from api.shared import (
    CLOUD_CONFIG_PATH,
    get_cloud_config,
    is_cloud_mode,
    read_settings,
    set_cloud_config,
)

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/mode")
def get_mode():
    """Return current deployment mode and cloud resource info."""
    cfg = get_cloud_config()
    if cfg:
        return {
            "mode": "cloud",
            "region": cfg.get("region"),
            "s3_bucket": cfg.get("s3_bucket"),
            "agentcore_runtime_arn": cfg.get("agentcore_runtime_arn"),
        }
    return {"mode": "local"}


@router.get("/alpaca")
def get_alpaca_status():
    """Check if Alpaca API keys are configured (not default placeholders)."""
    settings = read_settings()
    keys = settings.get("keys", {})
    paper_configured = bool(
        keys.get("alpaca_paper_api_key") and keys.get("alpaca_paper_secret_key")
    )
    live_configured = bool(
        keys.get("alpaca_live_api_key") and keys.get("alpaca_live_secret_key")
    )
    return {
        "configured": paper_configured,
        "paper_configured": paper_configured,
        "live_configured": live_configured,
    }


@router.put("/cloud")
def update_cloud_config(body: dict):
    """Save or clear cloud resource configuration."""
    CLOUD_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not body or not body.get("s3_bucket"):
        if CLOUD_CONFIG_PATH.exists():
            CLOUD_CONFIG_PATH.unlink()
        set_cloud_config(None)
        return {"mode": "local"}
    with open(CLOUD_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=2)
    set_cloud_config(body)
    return {"mode": "cloud", **body}
