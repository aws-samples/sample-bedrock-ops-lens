"""FastAPI entrypoint. Wires lifespan (DB pool), middleware (CORS, auth),
all routers, and the SPA static-files mount (for production builds)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .auth import install_auth
from .config import settings
from .db import close_pool, init_pool
from .routers import (
    agents as agents_router,
    by_user as by_user_router,
    compliance as compliance_router,
    cost as cost_router,
    errors as errors_router,
    extras as extras_router,
    latency as latency_router,
    model_insights as model_insights_router,
    model_lifecycle as model_lifecycle_router,
    ops_insights as ops_router,
    ops_review as opsreview_router,
    overview as overview_router,
    peak as peak_router,
    quota_drilldown as quota_drilldown_router,
    tags as tags_router,
    utility as utility_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    yield
    await close_pool()


app = FastAPI(
    title="Bedrock Ops Lens API",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# Order matters: CORS before auth so OPTIONS preflight isn't blocked.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
install_auth(app)   # registers AuthMiddleware + /api/auth/* endpoints when AUTH_ENABLED=true

# Routers — every path is /api/...
app.include_router(utility_router.router,    prefix="/api", tags=["utility"])
app.include_router(overview_router.router,   prefix="/api", tags=["overview"])
app.include_router(by_user_router.router,    prefix="/api", tags=["by-user"])
app.include_router(agents_router.router,     prefix="/api", tags=["agents"])
app.include_router(compliance_router.router, prefix="/api", tags=["compliance"])
app.include_router(errors_router.router,     prefix="/api", tags=["errors"])
app.include_router(latency_router.router,    prefix="/api", tags=["latency"])
app.include_router(peak_router.router,       prefix="/api", tags=["peak"])
app.include_router(ops_router.router,        prefix="/api", tags=["ops-insights"])
app.include_router(extras_router.router,     prefix="/api", tags=["extras"])
app.include_router(cost_router.router,       prefix="/api", tags=["cost"])
app.include_router(tags_router.router,       prefix="/api", tags=["tags"])
app.include_router(model_insights_router.router, prefix="/api", tags=["model-insights"])
app.include_router(model_lifecycle_router.router, prefix="/api", tags=["model-lifecycle"])
app.include_router(opsreview_router.router,  prefix="/api", tags=["ops-review"])
app.include_router(quota_drilldown_router.router, prefix="/api", tags=["quota-drilldown"])

# SPA static mount goes LAST so /api/* takes precedence.
_static_dir = Path(__file__).parent.parent / "static"
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="spa")


# ---------------------------------------------------------------------------
# AWS Lambda entry-point.
#
# When the same container image runs in Lambda (CMD points at this handler),
# Mangum turns the Lambda invocation event into an ASGI scope and back. The
# `lifespan="off"` is important: Lambda cold-starts each instance, so FastAPI
# lifespan events would run on every cold start (re-creating the asyncpg
# pool, re-fetching JWKS, etc.). We initialise lazily inside request handlers
# instead — already the pattern this codebase uses.
#
# In Fargate / uvicorn this `handler` is just a module-level reference no
# one calls — zero overhead.
# ---------------------------------------------------------------------------
try:
    from mangum import Mangum  # type: ignore
    # lifespan="auto" runs FastAPI startup/shutdown hooks at the boundaries
    # of each Lambda invocation. We pay the asyncpg-pool init cost once per
    # cold start (~50ms) — same as in Fargate. The earlier `lifespan="off"`
    # skipped startup, leaving init_pool() never called, which broke every
    # DB-touching endpoint with "DB pool not initialized".
    handler = Mangum(app, lifespan="auto")
except ImportError:
    # mangum is in requirements.txt; only absent if someone installed a stripped
    # subset of deps. Fall back to a noop so import never fails outright.
    handler = None
