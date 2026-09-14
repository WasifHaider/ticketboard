from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from ticketboard import config, db
from ticketboard.routes import projects as projects_routes
from ticketboard.routes import tickets as tickets_routes


def create_app(db_path: str | None = None) -> FastAPI:
    resolved_db_path = db_path or config.DB_PATH

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.get_connection(resolved_db_path)
        db.apply_schema(conn)
        app.state.db_path = resolved_db_path
        app.state.conn = conn
        yield
        conn.close()

    app = FastAPI(title="TICKETBOARD", lifespan=lifespan)

    if config.CORS_ORIGINS:
        # Only needed when the frontend is deployed on a different origin
        # than this API (e.g. Vercel). Same-origin local use (the default)
        # never hits this — the browser doesn't send/require CORS headers
        # for same-origin requests.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.CORS_ORIGINS,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(projects_routes.router)
    app.include_router(tickets_routes.router)

    static_dir = config.BASE_DIR / "ticketboard" / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


app = create_app()
