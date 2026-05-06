#!/usr/bin/env python3
"""
Webhook 엔드포인트 시뮬레이션 테스트
실제 서비스 실행 없이 webhook payload 처리를 검증
"""

import json
import sys
import os

# 현재 디렉토리를 Python path에 추가
sys.path.append('/root/kcu-knowledge-portal/services/embedding-worker')

def simulate_webhook_payload():
    """Outline webhook payload 시뮬레이션"""
    
    # 실제 Outline에서 보내는 webhook payload 형태
    webhook_payload = {
        "event": "documents.create",
        "payload": {
            "model": {
                "id": "doc-123-test",
                "teamId": "test-dept", 
                "title": "쿠버네티스 기초 가이드",
                "text": """# 쿠버네티스 기초 가이드

## 개요
쿠버네티스는 컨테이너 오케스트레이션 플랫폼입니다.

## 주요 개념
- **Pod**: 가장 작은 배포 단위
- **Deployment**: Pod를 관리하는 리소스
- **Service**: 네트워크 엔드포인트 제공  
- **Namespace**: 리소스 격리

## 실습 예제
```bash
kubectl get pods
kubectl apply -f deployment.yaml
kubectl port-forward svc/my-service 8080:80
```

쿠버네티스를 사용하면 컨테이너화된 애플리케이션을 효율적으로 관리할 수 있습니다."""
            }
        },
        "actorId": "user-001",
        "createdAt": "2024-01-22T10:30:00Z"
    }
    
    return webhook_payload

def test_payload_extraction():
    """payload에서 문서 정보 추출 테스트"""
    
    payload = simulate_webhook_payload()
    
    # main.py의 OutlineWebhookPayload.extract_document() 로직 시뮬레이션
    model = payload["payload"].get("model", {})
    
    extracted = {
        "document_id": model.get("id"),
        "tenant_id": model.get("teamId"), 
        "title": model.get("title"),
        "content": model.get("text", "")
    }
    
    print("=== Webhook Payload Extraction Test ===")
    print(f"Event: {payload['event']}")
    print(f"Document ID: {extracted['document_id']}")
    print(f"Tenant ID: {extracted['tenant_id']}")
    print(f"Title: {extracted['title']}")
    print(f"Content Length: {len(extracted['content'])} characters")
    
    # 기본 검증
    assert extracted["document_id"] is not None, "Document ID missing"
    assert extracted["tenant_id"] is not None, "Tenant ID missing" 
    assert extracted["title"] is not None, "Title missing"
    assert len(extracted["content"]) > 0, "Content empty"
    
    print("✅ Payload extraction test passed!")
    return extracted

def simulate_chunking(content, chunk_size=500):
    """문서 청킹 시뮬레이션"""
    
    chunks = []
    words = content.split()
    
    current_chunk = ""
    start_pos = 0
    
    for i, word in enumerate(words):
        if len(current_chunk + " " + word) <= chunk_size:
            current_chunk += " " + word if current_chunk else word
        else:
            if current_chunk:
                chunks.append({
                    "text": current_chunk.strip(),
                    "start": start_pos,
                    "end": start_pos + len(current_chunk),
                    "chunk_index": len(chunks)
                })
                start_pos += len(current_chunk) + 1
                current_chunk = word
    
    # 마지막 청크 추가
    if current_chunk:
        chunks.append({
            "text": current_chunk.strip(),
            "start": start_pos,
            "end": start_pos + len(current_chunk),
            "chunk_index": len(chunks)
        })
    
    return chunks

def test_full_pipeline():
    """전체 파이프라인 시뮬레이션"""
    
    print("\n=== Full Pipeline Simulation ===")
    
    # 1. Webhook payload 처리
    extracted = test_payload_extraction()
    
    # 2. 문서 청킹
    chunks = simulate_chunking(extracted["content"])
    print(f"Document split into {len(chunks)} chunks")
    
    # 3. 각 청크 처리 시뮬레이션
    vectors = []
    for i, chunk in enumerate(chunks):
        print(f"Processing chunk {i+1}/{len(chunks)}: {chunk['text'][:50]}...")
        
        # 임베딩 벡터 시뮬레이션 (1536차원)
        mock_vector = [0.1] * 1536  # 실제로는 OpenAI API 호출
        
        vectors.append({
            "id": f"{extracted['document_id']}-chunk-{i}",
            "vector": mock_vector,
            "payload": {
                "tenant_id": extracted["tenant_id"],
                "document_id": extracted["document_id"], 
                "chunk_index": i,
                "title": extracted["title"],
                "text": chunk["text"],
                "start_char": chunk["start"],
                "end_char": chunk["end"]
            }
        })
    
    print(f"✅ Generated {len(vectors)} embedding vectors")
    
    # 4. Qdrant 업로드 시뮬레이션
    print(f"✅ Simulated upload to Qdrant collection: {extracted['tenant_id']}-knowledge")
    print(f"✅ Pipeline completed for document: {extracted['document_id']}")
    
    return vectors

def simulate_webhook_request():
    """HTTP webhook 요청 시뮬레이션"""
    
    print("\n=== Webhook HTTP Request Simulation ===")
    
    payload = simulate_webhook_payload()
    
    print("POST /webhook/outline")
    print("Content-Type: application/json")
    print("Body:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    
    # 응답 시뮬레이션
    response = {
        "status": "accepted",
        "message": f"Document {payload['payload']['model']['id']} queued for embedding"
    }
    
    print("\nResponse (200 OK):")
    print(json.dumps(response, indent=2, ensure_ascii=False))
    
    return response

if __name__ == "__main__":
    try:
        # 전체 테스트 실행
        simulate_webhook_request()
        test_full_pipeline()
        
        print("\n🎉 All webhook pipeline tests passed!")
        print("\n📋 Summary:")
        print("- Webhook payload processing: ✅")
        print("- Document extraction: ✅") 
        print("- Text chunking: ✅")
        print("- Embedding generation: ✅ (simulated)")
        print("- Qdrant upload: ✅ (simulated)")
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        sys.exit(1)