"""FastAPI application: wires the routers together."""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config  # noqa: F401 - loads .env and sets up logging before anything else
from .routes import routers

log = logging.getLogger("voice_agent")


def create_app() -> FastAPI:
    app = FastAPI(title="PO Voice Agent")
    for router in routers:
        app.include_router(router)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"error": "Internal server error."}, status_code=500)

    return app


app = create_app()
