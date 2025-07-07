# collegue/health_server.py
from fastapi import FastAPI
import uvicorn

health_app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None) # Pas besoin de docs pour ça

@health_app.get("/_health")
async def root():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(health_app, host="0.0.0.0", port=4122) # Port différent, ex: 4122
