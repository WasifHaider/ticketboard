from contextlib import asynccontextmanager

from fastapi import FastAPI
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
    app.include_router(projects_routes.router)
    app.include_router(tickets_routes.router)

    static_dir = config.BASE_DIR / "ticketboard" / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


app = create_app()
