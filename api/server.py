"""
FastAPI server for the trading system dashboard.

Reads backtest sessions and live state from disk, exposes via REST API.
When cloud_resources.json is configured, routes to cloud (AgentCore + S3).
Run: uvicorn api.server:app --reload
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Trading Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Register route modules ────────────────────────────────────────────────

from api.routes.config import router as config_router      # noqa: E402
from api.routes.sessions import router as sessions_router   # noqa: E402
from api.routes.settings import router as settings_router   # noqa: E402
from api.routes.fixtures import router as fixtures_router   # noqa: E402
from api.routes.paper import router as paper_router         # noqa: E402
from api.routes.backtest import router as backtest_router     # noqa: E402
from api.routes.playbook import router as playbook_router    # noqa: E402

app.include_router(config_router)
app.include_router(sessions_router)
app.include_router(settings_router)
app.include_router(fixtures_router)
app.include_router(paper_router)
app.include_router(backtest_router)
app.include_router(playbook_router)
