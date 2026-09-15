"""FastAPI application factory, middleware and global error handling.

Startup deliberately does NOT fail when a dependency is missing. A forward
deployment is judged on how it behaves when the environment is imperfect: if
Postgres is briefly down or transcripts are not yet ingested, the API still
starts and reports the problem through /health/detail and structured errors,
so the operator can see *what* is wrong instead of a crash-looping container.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import artifacts, health, sessions
from app.config import Settings, get_settings
from app.db.engine import dispose_engine, init_engine
from app.db.migrate import run_migrations
from app.errors import AppError, ErrorBody, ErrorCode, ErrorResponse
from app.observability import configure_logging, new_request_id, request_id_var

log = logging.getLogger("app")

REQUEST_ID_HEADER = "X-Request-ID"
RATE_LIMIT_WINDOW_SECONDS = 60.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level, settings.log_format)
    log.info(
        "startup",
        extra={
            "environment": settings.environment,
            "llm_provider": settings.llm_provider,
            "model": settings.model_for(settings.llm_provider),
            "corpus_commit": settings.corpus_commit[:12],
        },
    )
    init_engine(settings)
    try:
        result = await run_migrations()
        log.info("migrations_ready", extra=result)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "migrations_failed_starting_degraded",
            extra={"error_type": type(exc).__name__, "error": str(exc)[:300]},
        )
    yield
    await dispose_engine()
    log.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        summary="Grounded product and growth answers from Lenny's Podcast transcripts.",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
        responses={
            422: {"model": ErrorResponse, "description": "Validation error"},
            500: {"model": ErrorResponse, "description": "Internal error"},
        },
    )

    # Requests resolve configuration from here, never from the global
    # singleton, so the app can be constructed with any Settings instance.
    app.state.settings = settings

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = rid
        # Defence in depth: the API serves JSON only, never HTML, so a response
        # that somehow carries markup must not be sniffed into execution.
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    # Per-process, in-memory sliding-window limiter. `rate_limit_per_minute`
    # was declared in Settings and had a stable error code (ErrorCode.
    # RATE_LIMITED, RateLimited) since checkpoint 1, but nothing ever enforced
    # it -- found live in checkpoint 5's hardening pass by hammering the API
    # and observing every request succeed regardless of volume. In-memory
    # rather than a shared store (Redis, etc.) because this is a single
    # uvicorn process by design (assignment scope; see docs/architecture.md);
    # a multi-process or multi-replica deployment would need a shared backend
    # instead, which is called out there rather than implemented here.
    request_log: dict[str, deque[float]] = defaultdict(deque)

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):  # type: ignore[no-untyped-def]
        limit = settings.rate_limit_per_minute
        if limit <= 0 or request.url.path.startswith("/health"):
            return await call_next(request)
        # X-User-Id first, so one caller's volume can't exhaust another's
        # budget on a shared client IP (NAT, corp proxy); falls back to the
        # connecting address when the header is absent, same as get_current_user_id.
        key = request.headers.get("X-User-Id") or (
            request.client.host if request.client else "unknown"
        )
        now = time.monotonic()
        bucket = request_log[key]
        cutoff = now - RATE_LIMIT_WINDOW_SECONDS
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            rid = request_id_var.get() or new_request_id()
            body = ErrorBody(
                code=ErrorCode.RATE_LIMITED,
                message=f"Rate limit of {limit} requests/minute exceeded.",
                remediation="Slow down and retry in a few seconds.",
                request_id=rid,
            )
            return JSONResponse(
                status_code=429,
                content=ErrorResponse(error=body).model_dump(),
                headers={"Retry-After": "60", REQUEST_ID_HEADER: rid},
            )
        bucket.append(now)
        return await call_next(request)

    # Registered last, deliberately: Starlette's `add_middleware` inserts each
    # new layer at the *front* of the stack, so whichever middleware is added
    # last ends up outermost. CORS has to be outermost so that a browser still
    # gets Access-Control-Allow-Origin on a response the *other* middleware
    # short-circuited (429 from rate_limit, the error envelope from an
    # exception handler) -- registering it first, like every earlier
    # checkpoint did, silently strips CORS headers from exactly the responses
    # a client most needs to read the body of. Found live in checkpoint 5 by
    # curling past the rate limit from a browser-like Origin header and
    # noticing the 429 body had no CORS headers.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID", "X-User-Id"],
        expose_headers=[REQUEST_ID_HEADER],
    )

    _register_error_handlers(app)

    app.include_router(health.router)
    app.include_router(sessions.router)
    app.include_router(sessions.provider_router)
    app.include_router(artifacts.router)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    def envelope(status: int, body: ErrorBody) -> JSONResponse:
        return JSONResponse(status_code=status, content=ErrorResponse(error=body).model_dump())

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        rid = request_id_var.get()
        log.warning(
            "request_failed",
            extra={"code": exc.code, "path": request.url.path, "status": exc.status_code},
        )
        return envelope(exc.status_code, exc.to_body(rid))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # Starlette's own 404/405/etc. would otherwise return {"detail": ...},
        # breaking the single error contract the frontend is written against.
        code = {
            404: ErrorCode.NOT_FOUND,
            422: ErrorCode.VALIDATION_ERROR,
            429: ErrorCode.RATE_LIMITED,
        }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        return envelope(
            exc.status_code,
            ErrorBody(
                code=code,
                message=str(exc.detail) if exc.detail else "Request failed.",
                request_id=request_id_var.get(),
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's raw error list leaks internal field paths and input echoes;
        # reshape it into the same envelope as everything else.
        fields = [
            {"field": ".".join(str(p) for p in err.get("loc", [])[1:]), "issue": err.get("msg")}
            for err in exc.errors()[:10]
        ]
        return envelope(
            422,
            ErrorBody(
                code=ErrorCode.VALIDATION_ERROR,
                message="The request body failed validation.",
                remediation="Correct the highlighted fields and retry.",
                details={"fields": fields},
                request_id=request_id_var.get(),
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_exception", extra={"path": request.url.path})
        return envelope(
            500,
            ErrorBody(
                code=ErrorCode.INTERNAL_ERROR,
                # Never surface the exception text: it can contain connection
                # strings and prompt fragments. The request_id links to the log.
                message="An unexpected internal error occurred.",
                remediation="Retry. If it persists, check the server logs for this request_id.",
                request_id=request_id_var.get(),
            ),
        )


app = create_app()
