import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import auth, catalogue, clients, conversations, doctor, me, messages, model_images, ot, platform_sso, records, visits
from app.config import DEFAULT_SECRET, get_settings
from app.db import dispose_engine
from app.logging_setup import setup_logging
from app.services import DomainError

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    s = get_settings()
    provider = s.resolved_llm[0]
    log.info("Starting %s (env=%s, llm=%s, analysis=%s)", s.app_name, s.environment, provider, s.analysis_engine)
    if s.environment == "production":
        problems = []
        if s.jwt_secret == DEFAULT_SECRET:
            problems.append("JWT_SECRET is the default")
        if s.otp_mode == "mock":
            problems.append("OTP_MODE=mock accepts a fixed code")
        if problems:
            raise RuntimeError("Refusing to start in production: " + "; ".join(problems))
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.app_name, version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origin_list, allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        response = await call_next(request)
        # NFR-06: health data must not be cached on the device.
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status, content={**exc.extra, "detail": exc.message, "code": exc.code})

    @app.get("/api/health", tags=["health"])
    async def health():
        from app.integrations.platform_auth import gateway_tokens
        from app.integrations.skin_models import wants_token

        urls = [u for u in (s.skin_diagnosis_ws_url, s.skin_post_treatment_ws_url) if u]
        return {"status": "ok", "llm": s.resolved_llm[0], "analysis_engine": s.analysis_engine,
                "skin_models": {"diagnosis": bool(s.skin_diagnosis_ws_url), "post_treatment": bool(s.skin_post_treatment_ws_url),
                                # where the model gateway token comes from (never the token itself)
                                "gateway_token": gateway_tokens.status() if any(wants_token(u) for u in urls) else None},
                "platform_sso": s.wfh_auth_mode != "none"}

    # platform_sso shares the /auth prefix: POST /api/auth/platform-session sits next to /api/auth/login.
    for r in (auth.router, platform_sso.router, clients.router, visits.router, conversations.router, doctor.router,
              catalogue.router, catalogue.public, ot.router, records.router, me.router, messages.router, model_images.router):
        app.include_router(r, prefix="/api")
    return app


app = create_app()
