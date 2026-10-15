"""FastAPI application: the voice agent, the PO follow-up admin and their web pages in one server."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.voice_agent import config  # noqa: F401 - loads .env and sets up logging before anything else
from backend.core import frontend, tracing
from backend.po_followup.api import admin

from backend.voice_agent.routes import routers

log = logging.getLogger("voice_agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("LangSmith tracing %s", "ON" if tracing.enabled() else "off")
    await admin.startup()
    yield
    admin.shutdown()
    tracing.flush()  # send queued traces before the process exits


def create_app() -> FastAPI:
    app = FastAPI(
        title="PO Voice Agent",
        description="Phone and browser voice agent (FreJun + Gemini Live) and the PO follow-up admin.",
        lifespan=lifespan,
    )
    for router in routers:
        app.include_router(router)
    # Shared frontend files (frontend/assets): /assets/ui.css is linked by every page
    app.mount("/assets", StaticFiles(directory=frontend.ASSETS), name="assets")
    # The dashboard sends every overdue PO line at once (~2 MB of JSON); compressed it is a fraction of that
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # Every error is {"error": "<readable message>"}, which is what the web pages show
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'] if p != 'body') or 'body'}: {e['msg']}" for e in exc.errors())
        return JSONResponse({"error": f"Invalid request - {problems}"}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"error": "Internal server error."}, status_code=500)

    return app


app = create_app()
