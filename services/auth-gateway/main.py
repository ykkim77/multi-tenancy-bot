from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
import logging

from auth.dev_auth import dev_auth_router
from config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Auth Gateway", version="1.0.0")

# DEV 모드 라우터 등록
app.include_router(dev_auth_router, prefix="/auth")


@app.get("/")
async def root():
    """루트 엔드포인트"""
    return {
        "service": "Auth Gateway",
        "version": "1.0.0",
        "mode": settings.AUTH_MODE
    }


@app.get("/health")
async def health_check():
    """헬스 체크"""
    return {"status": "healthy", "mode": settings.AUTH_MODE}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
