"""FastAPI application factory."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from paperclaw import __version__
from paperclaw.config import paperclaw_home, load_settings
from paperclaw.server.routes import (
    auto,
    benchmarks,
    brainstorm,
    chat,
    doctor,
    domains,
    experiments,
    hardware,
    ideas,
    resources,
    settings,
    skills,
    writing_styles,
)
from paperclaw.server.store import Store

# Built web frontend (frontend/dist/web) is served at / when present.
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "web"


def create_app(home: Path | None = None) -> FastAPI:
    home = home or paperclaw_home()
    home.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="PaperClaw", version=__version__)
    app.state.home = home
    app.state.store = Store(home)
    app.state.settings = load_settings(home)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(brainstorm.router)
    app.include_router(domains.router)
    app.include_router(ideas.router)
    app.include_router(chat.router)
    app.include_router(resources.router)
    app.include_router(settings.router)
    app.include_router(hardware.router)
    app.include_router(doctor.router)
    app.include_router(writing_styles.router)
    app.include_router(benchmarks.router)
    app.include_router(experiments.router)
    app.include_router(auto.router)
    app.include_router(skills.router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    if FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")

    return app
