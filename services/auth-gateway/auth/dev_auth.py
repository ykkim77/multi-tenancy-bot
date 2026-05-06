from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse
import logging
import secrets
from datetime import datetime

from config import settings

logger = logging.getLogger(__name__)

dev_auth_router = APIRouter()

# 임시 세션 저장소 (개발 환경용)
sessions = {}


@dev_auth_router.get("/login")
async def dev_login(request: Request):
    """DEV 모드 로그인 - 자동 인증"""
    logger.info("DEV mode login requested")
    
    # 리다이렉트 URL 가져오기
    redirect_uri = request.query_params.get("redirect_uri", "http://localhost:3000/auth/oidc.callback")
    
    # 인증 코드 생성
    auth_code = secrets.token_urlsafe(32)
    
    # 세션에 사용자 정보 저장
    sessions[auth_code] = {
        "user_id": settings.DEFAULT_USER_ID,
        "name": settings.DEFAULT_USER_NAME,
        "email": settings.DEFAULT_USER_ID,
        "created_at": datetime.now().isoformat()
    }
    
    # Outline 콜백으로 리다이렉트
    callback_url = f"{redirect_uri}?code={auth_code}"
    logger.info(f"Redirecting to: {callback_url}")
    
    return RedirectResponse(url=callback_url)


@dev_auth_router.post("/token")
async def dev_token(request: Request):
    """DEV 모드 토큰 발급"""
    logger.info("DEV mode token requested")
    
    # Form 데이터 파싱
    form_data = await request.form()
    code = form_data.get("code")
    
    if not code or code not in sessions:
        logger.error(f"Invalid authorization code: {code}")
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_grant", "error_description": "Invalid authorization code"}
        )
    
    # 세션에서 사용자 정보 가져오기
    user_info = sessions[code]
    
    # 액세스 토큰 생성
    access_token = secrets.token_urlsafe(32)
    sessions[access_token] = user_info
    
    logger.info(f"Token issued for user: {user_info['email']}")
    
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "id_token": access_token  # Outline은 id_token도 요구
    }


@dev_auth_router.get("/me")
async def dev_userinfo(request: Request):
    """DEV 모드 사용자 정보"""
    logger.info("DEV mode userinfo requested")
    
    # Authorization 헤더에서 토큰 추출
    auth_header = request.headers.get("Authorization", "")
    
    if not auth_header.startswith("Bearer "):
        logger.error("Missing or invalid Authorization header")
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "error_description": "Missing or invalid token"}
        )
    
    token = auth_header.replace("Bearer ", "")
    
    if token not in sessions:
        logger.error(f"Invalid token: {token[:10]}...")
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "error_description": "Invalid token"}
        )
    
    user_info = sessions[token]
    logger.info(f"Userinfo returned for: {user_info['email']}")
    
    return {
        "sub": user_info["user_id"],
        "email": user_info["email"],
        "name": user_info["name"],
        "email_verified": True
    }
