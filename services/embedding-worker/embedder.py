import logging
from typing import List
import openai
from tenacity import retry, wait_exponential, stop_after_attempt

from config import settings

logger = logging.getLogger(__name__)

# OpenAI 클라이언트 초기화
openai.api_key = settings.EMBEDDING_API_KEY


@retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(3))
async def get_embeddings(texts: List[str]) -> List[List[float]]:
    """텍스트 리스트를 임베딩으로 변환
    
    Args:
        texts: 임베딩할 텍스트 리스트
        
    Returns:
        임베딩 벡터 리스트
    """
    try:
        logger.info(f"Generating embeddings for {len(texts)} texts")
        
        # OpenAI API 호출
        response = openai.Embedding.create(
            model=settings.EMBEDDING_MODEL,
            input=texts
        )
        
        # 임베딩 추출
        embeddings = [item['embedding'] for item in response['data']]
        
        logger.info(f"Successfully generated {len(embeddings)} embeddings")
        return embeddings
        
    except Exception as e:
        logger.error(f"Error generating embeddings: {e}")
        raise
