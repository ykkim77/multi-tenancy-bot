import logging
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from config import settings

logger = logging.getLogger(__name__)

# Global Qdrant client
qdrant_client = None


def init_qdrant():
    """Qdrant 클라이언트 초기화 및 컬렉션 생성"""
    global qdrant_client
    
    try:
        # Qdrant 클라이언트 생성
        qdrant_client = QdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY if settings.QDRANT_API_KEY else None,
            timeout=30
        )
        
        logger.info(f"Qdrant client initialized: {settings.QDRANT_URL}")
        
        # 컬렉션 존재 확인
        collections = qdrant_client.get_collections().collections
        collection_names = [c.name for c in collections]
        
        if settings.QDRANT_COLLECTION not in collection_names:
            logger.info(f"Creating collection: {settings.QDRANT_COLLECTION}")
            
            # 컬렉션 생성 (text-embedding-ada-002는 1536 차원)
            qdrant_client.create_collection(
                collection_name=settings.QDRANT_COLLECTION,
                vectors_config=VectorParams(
                    size=1536,
                    distance=Distance.COSINE
                )
            )
            
            # 인덱스 생성
            qdrant_client.create_payload_index(
                collection_name=settings.QDRANT_COLLECTION,
                field_name="document_id",
                field_schema="keyword"
            )
            
            qdrant_client.create_payload_index(
                collection_name=settings.QDRANT_COLLECTION,
                field_name="tenant_id",
                field_schema="keyword"
            )
            
            logger.info(f"Collection created: {settings.QDRANT_COLLECTION}")
        else:
            logger.info(f"Collection exists: {settings.QDRANT_COLLECTION}")
        
    except Exception as e:
        logger.error(f"Failed to initialize Qdrant: {e}")
        raise


def get_qdrant_client() -> QdrantClient:
    """Qdrant 클라이언트 반환"""
    if qdrant_client is None:
        init_qdrant()
    return qdrant_client
