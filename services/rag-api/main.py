from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import logging

from config import settings
from retriever import search_documents
from llm_client import generate_answer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="RAG API", version="1.0.0")


class QueryRequest(BaseModel):
    """쿼리 요청"""
    query: str
    tenant_id: str
    top_k: int = 5


class QueryResponse(BaseModel):
    """쿼리 응답"""
    answer: str
    sources: List[dict]


@app.get("/")
async def root():
    """루트 엔드포인트"""
    return {
        "service": "RAG API",
        "version": "1.0.0",
        "status": "running"
    }


@app.get("/health")
async def health_check():
    """헬스 체크"""
    return {"status": "healthy"}


@app.post("/query", response_model=QueryResponse)
async def query_documents(request: QueryRequest):
    """문서 검색 및 답변 생성"""
    try:
        logger.info(f"Query received: {request.query} (tenant: {request.tenant_id})")
        
        # 1. 문서 검색
        search_results = await search_documents(
            query=request.query,
            tenant_id=request.tenant_id,
            top_k=request.top_k
        )
        
        if not search_results:
            return QueryResponse(
                answer="검색 결과를 찾을 수 없습니다.",
                sources=[]
            )
        
        # 2. LLM으로 답변 생성
        answer = await generate_answer(
            query=request.query,
            context_documents=search_results
        )
        
        # 3. 소스 정보 추출
        sources = [
            {
                "document_id": doc["document_id"],
                "title": doc["title"],
                "content": doc["content"][:200] + "...",
                "score": doc["score"]
            }
            for doc in search_results
        ]
        
        return QueryResponse(
            answer=answer,
            sources=sources
        )
        
    except Exception as e:
        logger.error(f"Error processing query: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
