from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, Optional
import logging
import os

from config import settings
from processor import process_document_embedding, delete_document_vectors
from qdrant_client_wrapper import init_qdrant

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Embedding Worker", version="1.0.0")


class OutlineWebhookPayload(BaseModel):
    """Outline Webhook payload"""
    event: str
    payload: Dict[str, Any]
    actorId: Optional[str] = None
    createdAt: Optional[str] = None

    def extract_document(self) -> Dict[str, Optional[str]]:
        """Extract document fields from Outline payload
        
        Outline의 webhook payload 구조:
        {
            "event": "documents.create",
            "payload": {
                "model": {
                    "id": "doc-id",
                    "teamId": "team-id",
                    "title": "Document Title",
                    "text": "Document content..."
                }
            }
        }
        """
        # payload.model 또는 payload.document에서 데이터 추출
        model = self.payload.get("model") or self.payload.get("document") or self.payload
        
        document_id = (
            model.get("id")
            or model.get("documentId")
            or self.payload.get("document_id")
            or self.payload.get("documentId")
        )
        
        tenant_id = (
            model.get("teamId")
            or model.get("tenantId")
            or self.payload.get("teamId")
            or self.payload.get("tenantId")
        )
        
        title = model.get("title") or model.get("name")
        content = model.get("text") or model.get("content") or ""

        return {
            "document_id": document_id,
            "tenant_id": tenant_id,
            "title": title,
            "content": content,
        }


@app.on_event("startup")
async def startup_event():
    """애플리케이션 시작 시 Qdrant 초기화"""
    logger.info("Starting Embedding Worker")
    try:
        init_qdrant()
        logger.info("Embedding Worker started successfully")
    except Exception as e:
        logger.error(f"Failed to initialize Qdrant: {e}")
        raise


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    from qdrant_client_wrapper import qdrant_client
    
    try:
        # Qdrant 연결 확인
        collections = qdrant_client.get_collections()
        return {
            "status": "healthy",
            "qdrant": "connected"
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {
            "status": "unhealthy",
            "error": str(e)
        }


@app.post("/webhook/outline")
async def outline_webhook(
    payload: OutlineWebhookPayload,
    background_tasks: BackgroundTasks
):
    """Outline Webhook 핸들러
    
    Outline Wiki에서 문서 변경 시 호출됨
    """
    # 디버깅: payload 구조 로깅
    logger.info(f"Received webhook: event={payload.event}")
    logger.info(f"Payload keys: {list(payload.payload.keys())}")
    
    # model 확인
    model = payload.payload.get("model") or payload.payload.get("document") or payload.payload
    logger.info(f"Model keys: {list(model.keys()) if isinstance(model, dict) else 'Not a dict'}")
    
    # text/content 필드 타입 확인
    text_field = model.get("text") if isinstance(model, dict) else None
    content_field = model.get("content") if isinstance(model, dict) else None
    logger.info(f"text field type: {type(text_field)}, content field type: {type(content_field)}")
    
    doc_fields = payload.extract_document()
    logger.info(
        f"Extracted: doc={doc_fields['document_id']}, tenant={doc_fields['tenant_id']}, "
        f"title={doc_fields['title']}"
    )
    
    # content 타입과 길이 안전하게 확인
    content = doc_fields['content']
    if content is None:
        logger.info("Content is None")
    elif isinstance(content, str):
        logger.info(f"Content is string, length: {len(content)}")
    else:
        logger.warning(f"Content is unexpected type: {type(content)}")
        logger.warning(f"Content value (first 200 chars): {str(content)[:200]}")
    
    # Outline의 실제 이벤트 이름 (복수형 + 변형)
    if payload.event in [
        "documents.create",
        "documents.update", 
        "documents.update.debounced",
        "documents.update.delayed",
        "documents.publish",
        "documents.title_change"
    ]:
        if not doc_fields["document_id"] or not doc_fields["tenant_id"]:
            logger.warning("Webhook payload missing document_id or tenant_id; ignoring.")
            raise HTTPException(
                status_code=422,
                detail="Document ID or tenant ID missing from webhook payload."
            )
        
        # 임베딩 처리 (비동기로 실행)
        try:
            await process_document_embedding(
                doc_fields["document_id"],
                doc_fields["tenant_id"],
                doc_fields["title"] or "Untitled Document",
                doc_fields["content"]
            )
            return {
                "status": "success",
                "message": f"Document {doc_fields['document_id']} processed successfully"
            }
        except Exception as e:
            logger.error(f"Error processing document: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Failed to process document: {str(e)}"
            }
    
    elif payload.event == "documents.delete":
        # 벡터 삭제 (즉시 실행)
        try:
            delete_document_vectors(
                doc_fields["document_id"],
                doc_fields["tenant_id"]
            )
            return {
                "status": "success",
                "message": f"Document {doc_fields['document_id']} deleted successfully"
            }
        except Exception as e:
            logger.error(f"Error deleting document: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Failed to delete document: {str(e)}"
            }
    
    else:
        return {
            "status": "ignored",
            "message": f"Event {payload.event} not handled"
        }


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Embedding Worker",
        "version": "1.0.0",
        "status": "running"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
