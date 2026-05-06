import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """RAG API 설정"""
    
    # OpenAI API 설정
    EMBEDDING_API_KEY: str = os.getenv("EMBEDDING_API_KEY", "")
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "text-embedding-ada-002")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-3.5-turbo")
    
    # Qdrant 설정
    QDRANT_URL: str = os.getenv("QDRANT_URL", "http://qdrant:6333")
    QDRANT_API_KEY: str = os.getenv("QDRANT_API_KEY", "")
    QDRANT_COLLECTION: str = os.getenv("QDRANT_COLLECTION", "kcu-knowledge")
    
    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
