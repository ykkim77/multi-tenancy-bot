import logging
from typing import List, Dict
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
import openai

from config import settings

logger = logging.getLogger(__name__)

# Qdrant 클라이언트 초기화
qdrant_client = QdrantClient(
    url=settings.QDRANT_URL,
    api_key=settings.QDRANT_API_KEY if settings.QDRANT_API_KEY else None
)

# OpenAI 설정
openai.api_key = settings.EMBEDDING_API_KEY


async def search_documents(
    query: str,
    tenant_id: str,
    top_k: int = 5
) -> List[Dict]:
    """문서 검색"""
    try:
        logger.info(f"Searching for: {query} (tenant: {tenant_id}, top_k: {top_k})")
        
        # 쿼리 임베딩 생성
        response = openai.Embedding.create(
            model=settings.EMBEDDING_MODEL,
            input=[query]
        )
        query_embedding = response['data'][0]['embedding']
        
        # Qdrant 검색
        search_results = qdrant_client.search(
            collection_name=settings.QDRANT_COLLECTION,
            query_vector=query_embedding,
            query_filter=Filter(
                must=[
                    FieldCondition(
                        key="tenant_id",
                        match=MatchValue(value=tenant_id)
                    )
                ]
            ),
            limit=top_k
        )
        
        # 결과 변환
        documents = []
        for result in search_results:
            documents.append({
                "document_id": result.payload.get("document_id"),
                "tenant_id": result.payload.get("tenant_id"),
                "title": result.payload.get("title"),
                "content": result.payload.get("content"),
                "score": result.score
            })
        
        logger.info(f"Found {len(documents)} documents")
        return documents
        
    except Exception as e:
        logger.error(f"Error searching documents: {e}", exc_info=True)
        raise
