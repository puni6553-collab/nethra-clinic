"""NetraCare — FastAPI app serving BOTH the API and the built web UI (iteration 1).

Run:      uvicorn app.main:app --reload
UI:       http://127.0.0.1:8000/
API docs: http://127.0.0.1:8000/docs

The React UI is built to frontend/dist and served from here, so one process
on one port gives you the whole application. Build the UI after changing it:
    cd frontend && npm run build
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, lab, seed
from .database import Session, engine, init_db
from .routers import appointments, auth, chat, clinic, leave, llm_routes

# frontend/dist, produced by `npm run build`.
DIST_DIR = Path(config.BASE_DIR).resolve().parent / "frontend" / "dist"
ASSETS_DIR = DIST_DIR / "assets"


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Create tables, load the data pack, then try the partner lab (iter 6)."""
    init_db()
    with Session(engine) as db:
        seed.load_data(db)
    # CityCare Lab is optional: a down partner never stops the app.
    await lab.connect()
    yield


app = FastAPI(
    title="NetraCare API",
    description="Booking API for the fictional NetraCare eye clinic. All rules live here.",
    version="1.0.0",
    lifespan=lifespan,
)

# Same-origin once built (UI and API share port 8000), so this isn't required
# for the shipped app — kept only so the Vite dev server on :5173 can still
# reach the API directly during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(appointments.router)
app.include_router(llm_routes.router)  # fact, redflag, urgent cases, summary
app.include_router(chat.router)  # patient chat + staff search debug (iter 3)
app.include_router(leave.router)  # R7: doctor leave + the Staff rebooking list
app.include_router(clinic.router)  # public: the landing page reads this

# The built bundle lives at /assets/<hashed-file>.css|js, referenced by index.html.
# Mounted after the API routers so /api/... always wins.
if ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


@app.get("/api/health", tags=["meta"])
def health() -> dict:
    now = config.now()
    return {
        "status": "ok",
        "now": now.isoformat(timespec="seconds"),
        "clock": "APP_NOW (fixed)" if config.APP_NOW else "system",
        "booking_window_days": config.BOOKING_WINDOW_DAYS,
        "cancel_cutoff_hours": config.CANCEL_CUTOFF_HOURS,
        "lab_mcp": lab.status(),
    }


@app.get("/health", include_in_schema=False)
def health_alias() -> dict:
    """Bare /health, because health-checkers probe it by convention.

    Without this, monitoring tools hit /health, get a 404, and assume the
    service is down — even though /api/health answers perfectly well.
    """
    return health()


@app.get("/", include_in_schema=False, response_model=None)
def index() -> FileResponse | JSONResponse:
    """Serve the built UI. Falls back to a JSON index if it isn't built yet.

    Without a build, you'd get a blank page and no clue why — this explains it.
    """
    page = DIST_DIR / "index.html"
    if not page.exists():
        return JSONResponse(
            content={
                "name": "NetraCare API (UI not built)",
                "docs": "/docs",
                "how_to_build": "cd frontend && npm run build",
                "note": "The web UI has not been built yet, so this is the API only.",
            }
        )
    # Never let the browser cache the entry page: after a rebuild it must
    # pick up the new hashed asset filenames immediately.
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Turn FastAPI's 422 shapes into the same {code, message} the UI expects."""
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        msg = first.get("msg", "Invalid request.")
        message = f"{loc}: {msg}" if loc else msg
    else:
        message = "Invalid request."
    return JSONResponse(
        status_code=422,
        content={"detail": {"code": "VALIDATION_ERROR", "message": message}},
    )
