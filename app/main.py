import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.auth.routes import router as auth_router
from app.routes import router
from app.admin_routes import router as admin_router
from app.backup_routes import router as backup_router
from app.data_routes import router as data_router
from app.database_routes import router as database_router
from app.scheduler.routes import router as scheduler_router
from app.incident_routes import router as incident_router
from app.database import SessionLocal
BASE=Path(__file__).parent
def create_app()->FastAPI:
    app=FastAPI(title="Discord Bot Manager",docs_url=None,redoc_url=None)
    app.state.session_factory=SessionLocal
    app.state.templates=Jinja2Templates(directory=BASE/"templates"); app.mount("/static",StaticFiles(directory=BASE/"static"),name="static")
    app.include_router(auth_router); app.include_router(admin_router); app.include_router(backup_router); app.include_router(data_router); app.include_router(database_router); app.include_router(scheduler_router); app.include_router(incident_router); app.include_router(router)
    @app.on_event("startup")
    def reconcile_incidents():
        from app.services.incidents import IncidentService
        with app.state.session_factory() as db: IncidentService(db).reconcile(); db.commit()
    @app.exception_handler(500)
    async def internal(_:Request,exc:Exception): logging.getLogger(__name__).exception("Unhandled request error"); return JSONResponse({"detail":"Internal server error"},500)
    return app
app=create_app()
