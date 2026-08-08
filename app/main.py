import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.auth.routes import router as auth_router
from app.routes import router
BASE=Path(__file__).parent
def create_app()->FastAPI:
    app=FastAPI(title="Discord Bot Manager",docs_url=None,redoc_url=None)
    app.state.templates=Jinja2Templates(directory=BASE/"templates"); app.mount("/static",StaticFiles(directory=BASE/"static"),name="static")
    app.include_router(auth_router); app.include_router(router)
    @app.exception_handler(500)
    async def internal(_:Request,exc:Exception): logging.getLogger(__name__).exception("Unhandled request error"); return JSONResponse({"detail":"Internal server error"},500)
    return app
app=create_app()
