import uvicorn
from app.core.config import get_settings

if __name__ == "__main__":
    settings=get_settings()
    uvicorn.run("app.supervisor.api:app",host=settings.supervisor_host,port=settings.supervisor_port,access_log=False)
