import logging
from typing import List, Dict
import openai

from config import settings

logger = logging.getLogger(__name__)

# OpenAI 설정
openai.api_key = settings.LLM_API_KEY


async def generate_answer(query: str, context_documents: List[Dict]) -> str:
    """LLM으로 답변 생성"""
    try:
        logger.info(f"Generating answer for: {query}")
        
        # 컨텍스트 구성
        context = "\n\n".join([
            f"[{doc['title']}]\n{doc['content']}"
            for doc in context_documents
        ])
        
        # 프롬프트 구성
        messages = [
            {
                "role": "system",
                "content": "당신은 KCU 지식포털의 AI 어시스턴트입니다. 주어진 문서 내용을 바탕으로 정확하고 도움이 되는 답변을 제공하세요."
            },
            {
                "role": "user",
                "content": f"다음 문서들을 참고하여 질문에 답변해 주세요.\n\n{context}\n\n질문: {query}"
            }
        ]
        
        # OpenAI API 호출
        response = openai.ChatCompletion.create(
            model=settings.LLM_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=500
        )
        
        answer = response['choices'][0]['message']['content']
        logger.info("Answer generated successfully")
        
        return answer
        
    except Exception as e:
        logger.error(f"Error generating answer: {e}", exc_info=True)
        raise
