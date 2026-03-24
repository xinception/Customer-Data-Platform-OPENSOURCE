"""FastAPI application entry-point for the Customer Data Platform."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from cdp.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Perform startup and shutdown tasks."""
    logger.info("Starting Customer Data Platform ...")

    # Initialise database tables (create if not exist)
    try:
        from cdp.database import init_db

        await init_db()
        logger.info("Database initialised")
    except Exception:
        logger.exception("Database initialisation failed (non-fatal at startup)")

    # Pre-load AI / ML models (best-effort)
    try:
        from cdp.services.ai.segmentation import SegmentationService

        svc = SegmentationService()
        await svc.load_models()
        logger.info("AI models loaded")
    except ImportError:
        logger.warning("AI segmentation service not available yet - skipping model load")
    except Exception:
        logger.exception("AI model loading failed (non-fatal)")

    yield  # application runs here

    logger.info("Shutting down Customer Data Platform ...")

    # Dispose async engine connection pool
    try:
        from cdp.database import engine

        await engine.dispose()
        logger.info("Database connections closed")
    except Exception:
        logger.exception("Error closing database connections")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""

    app = FastAPI(
        title="Customer Data Platform",
        description=(
            "Open-source Customer Data Platform with real-time event tracking, "
            "AI-driven segmentation, marketing automation, and a 360-degree "
            "customer view."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=[
            {"name": "Health", "description": "System health checks"},
            {"name": "Authentication", "description": "JWT authentication"},
            {"name": "Customers", "description": "Customer profile management"},
            {"name": "Tracking", "description": "Event tracking and consent"},
            {"name": "Campaigns", "description": "Marketing campaign management"},
            {"name": "Workflows", "description": "Automation workflow management"},
            {"name": "Analytics", "description": "Analytics, segments, and predictions"},
        ],
    )

    # ----- CORS -----
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Restrict in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ----- Request timing middleware -----
    @app.middleware("http")
    async def add_timing_header(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        response.headers["X-Process-Time"] = f"{elapsed:.4f}"
        return response

    # ----- Exception handlers -----
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": "Validation error",
                "errors": exc.errors(),
            },
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )

    # ----- Health check -----
    @app.get("/health", tags=["Health"], summary="Health check")
    async def health_check() -> dict[str, Any]:
        """Return service health and version information."""
        return {
            "status": "healthy",
            "version": app.version,
            "service": "Customer Data Platform",
        }

    # ----- Register routers -----
    from cdp.api.routes.auth import router as auth_router
    from cdp.api.routes.customers import router as customers_router
    from cdp.api.routes.tracking import router as tracking_router
    from cdp.api.routes.campaigns import router as campaigns_router
    from cdp.api.routes.workflows import router as workflows_router
    from cdp.api.routes.analytics import router as analytics_router

    app.include_router(auth_router)
    app.include_router(customers_router)
    app.include_router(tracking_router)
    app.include_router(campaigns_router)
    app.include_router(workflows_router)
    app.include_router(analytics_router)

    # ----- Static files (JS tracker, etc.) -----
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


# Singleton application instance used by ASGI servers (uvicorn, gunicorn, etc.)
app = create_app()
