"""The FastAPI application factory.

APP-CONTRACT.md section 14.1: `create_app()` makes the application,
includes every router, and installs the `ApiError` handler.
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from narratarr import __version__, runner
from narratarr.api import auth
from narratarr.api.common import ApiError
from narratarr.config import get_settings
from narratarr.db import init_db, transaction

logger = logging.getLogger("narratarr.api")

# Every router this app includes, in the order of APP-CONTRACT.md
# section 14.1. W3 is still building review.py, targets.py, and settings.py
# while this module is written, so each import is defensive: a missing
# module is logged and skipped, not a crash. `create_app()` therefore works
# today, and picks up each router the moment its file exists.
ROUTER_MODULES = [
    "narratarr.api.system",
    "narratarr.api.jobs",
    "narratarr.api.review",
    "narratarr.api.targets",
    "narratarr.api.settings",
]

# Set by a test, before `create_app()` runs, to skip the background runner
# thread. The route handlers still exercise their normal code path; only
# the loop that claims and processes jobs on its own schedule is skipped,
# so a test controls exactly when a job is processed.
TEST_DISABLE_RUNNER_ENV = "NARRATARR_TEST_DISABLE_RUNNER"


def create_app() -> FastAPI:
    """Build the Narratarr FastAPI application. Include every router."""
    app = FastAPI(title="Narratarr", version=__version__)
    app.state.runner_thread = None
    app.state.runner_stop = threading.Event()

    for module_name in ROUTER_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            logger.warning("router module %s is not ready yet: %s", module_name, exc)
            continue
        route_router = getattr(module, "router", None)
        if route_router is None:
            logger.warning("router module %s has no 'router' attribute", module_name)
            continue
        app.include_router(route_router)

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        """Render an ApiError as the error envelope of APP-CONTRACT.md section 13."""
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message, "detail": exc.detail}},
        )

    @app.on_event("startup")
    def _on_startup() -> None:
        """Prepare the database, bootstrap the API key, and start the runner."""
        settings = get_settings()
        settings.ensure_directories()
        init_db()

        with transaction() as conn:
            bootstrap_key = auth.ensure_bootstrap_key(conn)
        if bootstrap_key:
            logger.warning("NARRATARR_API_KEY was not set. Generated key: %s", bootstrap_key)
            logger.warning("This key is shown once. Store it now.")

        runner.on_start()

        if os.environ.get(TEST_DISABLE_RUNNER_ENV) == "1":
            return

        app.state.runner_stop.clear()
        app.state.runner_thread = threading.Thread(
            target=runner.run_forever,
            args=(app.state.runner_stop,),
            daemon=True,
            name="narratarr-runner",
        )
        app.state.runner_thread.start()

    _mount_frontend(app)

    @app.on_event("shutdown")
    def _on_shutdown() -> None:
        """Stop the runner thread."""
        app.state.runner_stop.set()
        thread: Optional[threading.Thread] = app.state.runner_thread
        if thread is not None:
            thread.join(timeout=5.0)

    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built single-page application, when it is present.

    The mount goes at `/`, and it goes on LAST. A mount at the root matches
    every path, so a mount added before the routers would swallow every
    `/api/v1` route and the whole API would answer with the index page.

    `html=True` serves `index.html` for a directory request. The frontend
    uses a hash router, so no server-side rewrite rule is needed for a deep
    link.

    A missing directory is not a fault. A developer runs the API with no
    built frontend, and the image build is the only thing that must produce
    one. The API stays usable either way, and the log says which happened.
    """
    from fastapi.staticfiles import StaticFiles

    dist = Path(__file__).resolve().parent.parent.parent / "web" / "dist"
    if not (dist / "index.html").is_file():
        logger.warning(
            "no built frontend at %s. The API works; the user interface does not. "
            "Run `npm run build` in web/, or use the image, which builds it.",
            dist,
        )
        return
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    logger.info("serving the frontend from %s", dist)
