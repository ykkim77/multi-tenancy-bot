#!/usr/bin/env python3
"""
Mock OIDC Provider for Outline Development
개발용 자동 로그인 Mock 인증 서버 - 운영환경에서는 실제 SSO로 교체 가능
"""

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import jwt
import time
import uuid
import os
from urllib.parse import urlencode, parse_qs, urlparse

app = FastAPI(title="Mock OIDC Provider", version="1.0.0")

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mock 사용자 데이터 (개발용)
MOCK_USER = {
    "sub": "dev-user-001",
    "email": "developer@kcu.ac.kr", 
    "name": "개발자",
    "given_name": "개발자",
    "family_name": "KCU",
    "preferred_username": "developer",
    "picture": "https://via.placeholder.com/150",
    "email_verified": True
}

# JWT 설정 (개발용 - 운영에서는 보안 강화 필요)
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-key-change-in-production")
ISSUER = "http://mock-auth:8000"

# 임시 저장소 (운영에서는 Redis/Database 사용)
auth_codes = {}
access_tokens = {}

@app.get("/")
async def root():
    return {"message": "Mock OIDC Provider - Development Only"}

@app.get("/.well-known/openid-configuration")
async def openid_configuration():
    """OIDC Discovery endpoint - SSO 서버의 표준 설정"""
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/auth",
        "token_endpoint": f"{ISSUER}/token", 
        "userinfo_endpoint": f"{ISSUER}/userinfo",
        "jwks_uri": f"{ISSUER}/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["HS256"],
        "scopes_supported": ["openid", "profile", "email"],
        "claims_supported": ["sub", "email", "name", "given_name", "family_name", "preferred_username", "picture", "email_verified"]
    }

@app.get("/auth")
async def authorize(
    client_id: str,
    redirect_uri: str,
    response_type: str = "code",
    scope: str = "openid profile email",
    state: str = None
):
    """
    Authorization endpoint - 자동 로그인 (개발용)
    운영환경에서는 실제 SSO 로그인 페이지로 리다이렉트
    """
    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
    
    # 개발용: 자동으로 인증 코드 생성
    auth_code = str(uuid.uuid4())
    auth_codes[auth_code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri, 
        "scope": scope,
        "user": MOCK_USER,
        "expires_at": time.time() + 600  # 10분 유효
    }
    
    # Outline으로 리다이렉트 (인증 코드 포함)
    params = {"code": auth_code}
    if state:
        params["state"] = state
        
    redirect_url = f"{redirect_uri}?{urlencode(params)}"
    return RedirectResponse(redirect_url)

@app.post("/token")
async def token_endpoint(
    grant_type: str = Form(...),
    code: str = Form(None),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    redirect_uri: str = Form(...)
):
    """Token endpoint - 인증 코드를 액세스 토큰으로 교환"""
    
    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    
    if code not in auth_codes:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    
    auth_data = auth_codes[code]
    
    # 인증 코드 만료 확인
    if time.time() > auth_data["expires_at"]:
        del auth_codes[code]
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    
    # 클라이언트 검증 (개발용 - 운영에서는 실제 클라이언트 DB 확인)
    if client_id != auth_data["client_id"]:
        return JSONResponse({"error": "invalid_client"}, status_code=400)
    
    # Access Token 생성
    access_token = str(uuid.uuid4())
    
    # ID Token 생성 (JWT)
    now = int(time.time())
    id_token_payload = {
        **auth_data["user"],
        "iss": ISSUER,
        "aud": client_id,
        "iat": now,
        "exp": now + 3600,  # 1시간 유효
        "auth_time": now
    }
    
    id_token = jwt.encode(id_token_payload, JWT_SECRET, algorithm="HS256")
    
    # 토큰 저장
    access_tokens[access_token] = {
        "user": auth_data["user"],
        "client_id": client_id,
        "scope": auth_data["scope"],
        "expires_at": time.time() + 3600
    }
    
    # 사용된 인증 코드 삭제
    del auth_codes[code]
    
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "id_token": id_token,
        "scope": auth_data["scope"]
    }

@app.get("/userinfo")
async def userinfo(request: Request):
    """UserInfo endpoint - 사용자 정보 반환"""
    
    # Authorization 헤더에서 토큰 추출
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    
    access_token = auth_header[7:]  # "Bearer " 제거
    
    if access_token not in access_tokens:
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    
    token_data = access_tokens[access_token]
    
    # 토큰 만료 확인
    if time.time() > token_data["expires_at"]:
        del access_tokens[access_token]
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    
    return token_data["user"]

@app.get("/jwks")
async def jwks():
    """JSON Web Key Set - JWT 서명 검증용"""
    return {
        "keys": [
            {
                "kty": "oct",
                "alg": "HS256", 
                "k": JWT_SECRET,
                "use": "sig"
            }
        ]
    }

@app.get("/health")
async def health():
    """Health check"""
    return {"status": "healthy", "service": "mock-auth"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)