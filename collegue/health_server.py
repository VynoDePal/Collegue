# collegue/health_server.py
from fastapi import FastAPI
import uvicorn

import sys
import os
from pathlib import Path

parent_dir = str(Path(__file__).parent.parent.absolute())
sys.path.insert(0, parent_dir)

from collegue.config import settings

health_app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

@health_app.get("/_health")
async def root():
    return {"status": "ok"}

# Endpoint de découverte OAuth Protected Resource (MCP)
@health_app.get('/.well-known/oauth-protected-resource')
async def oauth_protected_resource():
    if settings.OAUTH_ENABLED and settings.OAUTH_ISSUER:
        auth_server = (settings.OAUTH_AUTH_SERVER_PUBLIC or settings.OAUTH_ISSUER).rstrip('/')
        return {
            'authorization_servers': [auth_server],
            'resource_id': settings.OAUTH_AUDIENCE or settings.APP_NAME.lower(),
            'scopes_supported': settings.OAUTH_REQUIRED_SCOPES,
        }
    return {}

if __name__ == "__main__":
    uvicorn.run(health_app, host="0.0.0.0", port=4122)
