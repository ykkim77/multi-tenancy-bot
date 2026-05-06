import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Auth Gateway 설정"""
    
    # 인증 모드 (DEV, PROD)
    AUTH_MODE: str = os.getenv("AUTH_MODE", "DEV")
    
    # DEV 모드 기본 사용자
    DEFAULT_USER_ID: str = os.getenv("DEFAULT_USER_ID", "admin@kcu.ac.kr")
    DEFAULT_USER_NAME: str = os.getenv("DEFAULT_USER_NAME", "관리자")
    
    # OIDC 설정 (PROD 모드용)
    KCU_SSO_URL: str = os.getenv("KCU_SSO_URL", "")
    KCU_SSO_CLIENT_ID: str = os.getenv("KCU_SSO_CLIENT_ID", "")
    KCU_SSO_CLIENT_SECRET: str = os.getenv("KCU_SSO_CLIENT_SECRET", "")
    
    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
