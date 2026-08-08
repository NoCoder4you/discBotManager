import hmac
from fastapi import Depends, FastAPI, Header, HTTPException
from app.core.config import get_settings
from app.database import SessionLocal
from app.supervisor.service import BotNotRegistered, IdentityMismatch, SupervisorConflict, SupervisorService

settings=get_settings(); service=SupervisorService(SessionLocal,settings.supervisor_stop_timeout)
app=FastAPI(title="Bot Process Supervisor",docs_url=None,redoc_url=None,openapi_url=None)

def authenticated(x_supervisor_secret: str | None=Header(None)):
    if not settings.supervisor_secret or not x_supervisor_secret or not hmac.compare_digest(x_supervisor_secret,settings.supervisor_secret): raise HTTPException(401,"Invalid supervisor credential")

def call(method,*args):
    try: return getattr(service,method)(*args)
    except BotNotRegistered as exc: raise HTTPException(404,str(exc)) from exc
    except (SupervisorConflict,IdentityMismatch) as exc: raise HTTPException(409,str(exc)) from exc

@app.get("/internal/health",dependencies=[Depends(authenticated)])
def health(): return service.health()
@app.get("/internal/processes",dependencies=[Depends(authenticated)])
def processes(): return call("reconcile")
@app.get("/internal/bots/{bot_id}",dependencies=[Depends(authenticated)])
def status(bot_id:str): return call("status",bot_id)
@app.post("/internal/bots/{bot_id}/start",dependencies=[Depends(authenticated)])
def start(bot_id:str): return call("start",bot_id)
@app.post("/internal/bots/{bot_id}/stop",dependencies=[Depends(authenticated)])
def stop(bot_id:str): return call("stop",bot_id)
@app.post("/internal/bots/{bot_id}/restart",dependencies=[Depends(authenticated)])
def restart(bot_id:str): return call("restart",bot_id)
@app.post("/internal/reconcile",dependencies=[Depends(authenticated)])
def reconcile(): return call("reconcile")
