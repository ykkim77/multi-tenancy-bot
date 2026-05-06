import logging
from typing import List
from langchain.text_splitter import RecursiveCharacterTextSplitter

from config import settings
from embedder import get_embeddings
from qdrant_client_wrapper import get_qdrant_client

logger = logging.getLogger(__name__)


def chunk_document(content: str) -> List[str]:
    """문서를 청크로 분할"""
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.CHUNK_SIZE,
        chunk_overlap=settings.CHUNK_OVERLAP,
        length_function=len,
    )
    
    chunks = text_splitter.split_text(content)
    logger.info(f"Document split into {len(chunks)} chunks")
    return chunks


async def process_document_embedding(
    document_id: str,
    tenant_id: str,
    title: str,
    content: str
):
    """문서 임베딩 처리 (비동기 함수)"""
    try:
        logger.info(f"Processing document: {document_id} (tenant: {tenant_id})")
        
        # content가 None이거나 비어있는지 먼저 확인
        if not content:
            logger.warning(f"Document {document_id} has no content (None), skipping")
            return
        
        # content 길이 안전하게 확인 (메모리 초과 방지)
        try:
            content_len = len(content)
            logger.info(f"Content length: {content_len} characters")
        except:
            logger.error(f"Failed to get content length - content may be too large")
            return
        
        logger.info(f"Title: {title}")
        
        if content_len == 0 or len(content.strip()) == 0:
            logger.warning(f"Document {document_id} has empty content, skipping")
            return
        
        # 최대 content 길이 제한 (메모리 보호)
        MAX_CONTENT_LENGTH = 1_000_000  # 1MB
        if content_len > MAX_CONTENT_LENGTH:
            logger.warning(f"Document {document_id} content too large ({content_len} chars), truncating to {MAX_CONTENT_LENGTH}")
            content = content[:MAX_CONTENT_LENGTH]
        
        logger.info("Starting document chunking...")
        # 문서 청킹
        chunks = chunk_document(content)
        logger.info(f"Chunking completed. Chunks count: {len(chunks)}")
        
        if not chunks:
            logger.warning(f"No chunks generated for document {document_id}")
            return
        
        logger.info("Starting embedding generation...")
        # 임베딩 생성 (비동기 함수를 await로 호출)
        embeddings = await get_embeddings(chunks)
        logger.info(f"Embedding generation completed. Embeddings count: {len(embeddings)}")
        
        if not embeddings or len(embeddings) != len(chunks):
            logger.error(f"Embedding generation failed for document {document_id}")
            return
        
        # Qdrant에 저장
        from qdrant_client.models import PointStruct
        import uuid
        
        points = []
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            point = PointStruct(
                id=str(uuid.uuid4()),
                vector=embedding,
                payload={
                    "document_id": document_id,
                    "tenant_id": tenant_id,
                    "title": title,
                    "content": chunk,
                    "chunk_index": i,
                    "total_chunks": len(chunks)
                }
            )
            points.append(point)
        
        # Batch upsert
        client = get_qdrant_client()
        client.upsert(
            collection_name=settings.QDRANT_COLLECTION,
            points=points
        )
        
        logger.info(
            f"Successfully embedded document {document_id}: "
            f"{len(points)} vectors uploaded to Qdrant"
        )
        
    except Exception as e:
        logger.error(f"Error processing document {document_id}: {e}", exc_info=True)
        raise


def delete_document_vectors(document_id: str, tenant_id: str):
    """문서 벡터 삭제 (동기 함수)"""
    try:
        logger.info(f"Deleting vectors for document: {document_id} (tenant: {tenant_id})")
        
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        
        # document_id와 tenant_id로 필터링하여 삭제
        client = get_qdrant_client()
        client.delete(
            collection_name=settings.QDRANT_COLLECTION,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="document_id",
                        match=MatchValue(value=document_id)
                    ),
                    FieldCondition(
                        key="tenant_id",
                        match=MatchValue(value=tenant_id)
                    )
                ]
            )
        )
        
        logger.info(f"Successfully deleted vectors for document {document_id}")
        
    except Exception as e:
        logger.error(f"Error deleting document {document_id}: {e}", exc_info=True)
        raise
