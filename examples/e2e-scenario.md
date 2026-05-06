# E2E 시나리오: Outline Wiki → Embedding Worker → Qdrant

이 문서는 KCU 지식포털의 전체 흐름을 테스트하는 시나리오를 담고 있습니다.

## 1. 개요

### 1.1 전체 흐름
```
사용자 → Outline Wiki → Webhook → Embedding Worker → Qdrant
                                  ↓
                            RAG API → LLM
```

### 1.2 주요 컴포넌트
- **Outline Wiki**: 문서 작성 및 관리
- **Embedding Worker**: 문서 임베딩 처리
- **Qdrant**: 벡터 데이터베이스
- **RAG API**: 검색 증강 생성 API

## 2. 사전 준비

### 2.1 클러스터 상태 확인
```bash
# 모든 Pod 상태 확인
kubectl get pods -n tenant-test-dept
kubectl get pods -n kcu-system

# 서비스 확인
kubectl get svc -n tenant-test-dept
kubectl get svc -n kcu-system
```

### 2.2 Port-forward 설정
```bash
# Terminal 1: Outline Wiki
kubectl port-forward -n tenant-test-dept svc/outline 3000:80

# Terminal 2: Auth Gateway
kubectl port-forward -n kcu-system svc/auth-gateway 8000:8000
```

## 3. Webhook 설정

### 3.1 Webhook 상태 확인
```bash
kubectl exec -n tenant-test-dept outline-postgres-0 -- psql -U outline -d outline -c "SELECT name, url, enabled, events FROM webhook_subscriptions;"
```

### 3.2 Webhook 강제 활성화
```bash
kubectl exec -n tenant-test-dept outline-postgres-0 -- psql -U outline -d outline -c "UPDATE webhook_subscriptions SET enabled = true WHERE url = 'http://embedding-worker:8000/webhook/outline';"
```

### 3.3 Webhook 실패 기록 삭제
```bash
kubectl exec -n tenant-test-dept outline-postgres-0 -- psql -U outline -d outline -c "DELETE FROM webhook_deliveries;"
```

## 4. 문서 작성 및 테스트

### 4.1 Outline 접속
브라우저에서 `http://localhost:3000` 접근

```bash
kubectl port-forward -n tenant-test-dept svc/outline 3000:80
kubectl port-forward -n kcu-system svc/auth-gateway 8000:8000
```

### 4.2 문서 작성
1. "Continue with KCU SSO" 버튼 클릭
2. Auth Gateway를 통한 자동 로그인 (DEV 모드)
3. 새 문서 생성
4. 내용 작성 후 "Publish" 클릭

### 4.3 로그 모니터링
```bash
# Embedding Worker 로그
kubectl logs -n tenant-test-dept -l app=embedding-worker -f

# Outline 로그
kubectl logs -n tenant-test-dept -l app=outline -f --tail=50
```

예상되는 로그:
```
INFO - Received webhook: event=documents.create, doc=xxx, tenant=xxx
INFO - Processing document: xxx (tenant: xxx)
INFO - Document split into X chunks
INFO - Successfully embedded document xxx: X vectors uploaded
```

### 4.4 Webhook 전달 확인
```bash
kubectl exec -n tenant-test-dept outline-postgres-0 -- psql -U outline -d outline -c "SELECT status, \"statusCode\", \"createdAt\" FROM webhook_deliveries ORDER BY \"createdAt\" DESC LIMIT 5;"
```

### 4.5 Qdrant 벡터 확인
```bash
# Qdrant 포트 포워딩
kubectl port-forward -n tenant-test-dept svc/qdrant 6333:6333

# 브라우저에서 http://localhost:6333/dashboard 접속
# 또는 API로 확인
curl http://localhost:6333/collections/kcu-knowledge
```

## 5. 트러블슈팅

### 5.1 Webhook이 비활성화되는 경우
Outline은 webhook 전달 실패가 누적되면 자동으로 비활성화합니다.

**해결책:**
```bash
# 1. Webhook 재활성화
kubectl exec -n tenant-test-dept outline-postgres-0 -- psql -U outline -d outline -c "UPDATE webhook_subscriptions SET enabled = true WHERE url = 'http://embedding-worker:8000/webhook/outline';"

# 2. 실패 기록 삭제
kubectl exec -n tenant-test-dept outline-postgres-0 -- psql -U outline -d outline -c "DELETE FROM webhook_deliveries;"

# 3. Outline Pod 재시작 (Webhook 프로세서 리셋)
kubectl delete pod -n tenant-test-dept -l app=outline
```

### 5.2 Embedding Worker가 응답하지 않는 경우
**증상:** `wget: download timed out`

**해결책:**
```bash
# 1. Pod 재시작
kubectl delete pod -n tenant-test-dept -l app=embedding-worker

# 2. 로그 확인
kubectl logs -n tenant-test-dept -l app=embedding-worker --tail=50

# 3. 네트워크 연결 테스트
OUTLINE_POD=$(kubectl get pods -n tenant-test-dept -l app=outline -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n tenant-test-dept $OUTLINE_POD -- curl -v http://embedding-worker:8000/health
```

### 5.3 인증 문제 (401 Unauthorized)
**증상:** Outline 로그인 시 401 에러

**해결책:**
```bash
# 1. Auth Gateway 로그 확인
kubectl logs -n kcu-system -l app=auth-gateway --tail=50

# 2. OIDC 설정 확인
kubectl get deployment -n tenant-test-dept outline -o yaml | grep -A 5 OIDC

# 3. Port-forward가 모두 실행 중인지 확인
ps aux | grep port-forward
```

### 5.4 PostgreSQL 연결 문제
**증상:** `FATAL: role "outline" does not exist`

**해결책:**
```bash
# 1. PostgreSQL Pod 재시작
kubectl delete pod -n tenant-test-dept outline-postgres-0

# 2. 연결 테스트
kubectl exec -n tenant-test-dept outline-postgres-0 -- psql -U outline -d outline -c '\dt'
```

### 5.5 documents.publish 이벤트가 안 들어오는 경우
**증상:** `documents.create`는 작동하지만 `documents.publish`는 작동하지 않음

**원인 분석:**
```bash
# Outline 로그에서 실제 발생한 이벤트 확인
kubectl logs -n tenant-test-dept -l app=outline --tail=200 | grep "documents.publish"
```

Outline 로그에 `WebhookProcessor running documents.publish`가 보이지만 embedding-worker에 로그가 없다면:
- Webhook 전달 자체가 실패하고 있는 것
- Webhook 구독이 비활성화되었을 가능성
- 네트워크 연결 문제

**해결책:** 5.1 참조

## 6. 데이터 영속성 확인

### 6.1 PostgreSQL 데이터 확인
```bash
# 문서 수 확인
kubectl exec -n tenant-test-dept outline-postgres-0 -- psql -U outline -d outline -c "SELECT COUNT(*) FROM documents;"

# 최근 문서 확인
kubectl exec -n tenant-test-dept outline-postgres-0 -- psql -U outline -d outline -c "SELECT id, title, \"createdAt\" FROM documents ORDER BY \"createdAt\" DESC LIMIT 5;"
```

### 6.2 PVC 상태 확인
```bash
kubectl get pvc -n tenant-test-dept
kubectl describe pvc outline-data -n tenant-test-dept
```

### 6.3 Qdrant 데이터 확인
```bash
# Collection 정보
curl http://localhost:6333/collections/kcu-knowledge

# 벡터 수 확인
curl http://localhost:6333/collections/kcu-knowledge | jq '.result.vectors_count'
```

## 7. 성능 테스트

### 7.1 대용량 문서 테스트
1. 긴 문서(1000+ 단어) 작성
2. Embedding 처리 시간 측정
3. Chunk 수 확인

### 7.2 동시성 테스트
1. 여러 문서 동시 생성
2. Webhook 큐 처리 확인
3. 실패율 확인

## 8. 정리 및 재시작

### 8.1 전체 재시작
```bash
# 1. Outline 재시작
kubectl rollout restart deployment/outline -n tenant-test-dept

# 2. Embedding Worker 재시작
kubectl rollout restart deployment/embedding-worker -n tenant-test-dept

# 3. 상태 확인
kubectl rollout status deployment/outline -n tenant-test-dept
kubectl rollout status deployment/embedding-worker -n tenant-test-dept
```

### 8.2 Port-forward 정리
```bash
# Port-forward 프로세스 확인
ps aux | grep port-forward

# 종료
pkill -f "port-forward"
```

## 9. 참고 사항

### 9.1 환경 변수
- `WEBHOOK_URL`: `http://embedding-worker:8000/webhook/outline`
- `ALLOWED_PRIVATE_IPS`: `10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`
- `EMBEDDING_API_KEY`: OpenAI API 키 (tenant-secrets)

### 9.2 중요 엔드포인트
- Outline: `http://localhost:3000`
- Auth Gateway: `http://localhost:8000`
- Embedding Worker: `http://embedding-worker:8000` (cluster 내부)
- Qdrant: `http://qdrant:6333` (cluster 내부)

### 9.3 디버깅 팁
1. 항상 로그를 먼저 확인
2. Webhook 상태를 주기적으로 체크
3. 네트워크 연결을 직접 테스트
4. Pod 재시작은 최후의 수단

---

## 부록: 자주 사용하는 명령어 모음

```bash

kubectl port-forward -n tenant-test-dept svc/outline 3000:80
kubectl port-forward -n kcu-system svc/auth-gateway 8000:8000


# 전체 상태 확인
kubectl get all -n tenant-test-dept

# Webhook 상태 확인
kubectl exec -n tenant-test-dept outline-postgres-0 -- psql -U outline -d outline -c "SELECT name, enabled FROM webhook_subscriptions;"

# Webhook 활성화
kubectl exec -n tenant-test-dept outline-postgres-0 -- psql -U outline -d outline -c "UPDATE webhook_subscriptions SET enabled = true WHERE url = 'http://embedding-worker:8000/webhook/outline';"

# 로그 모니터링
kubectl logs -n tenant-test-dept -l app=embedding-worker -f
kubectl logs -n tenant-test-dept -l app=outline -f --tail=50

# Pod 재시작
kubectl delete pod -n tenant-test-dept -l app=outline
kubectl delete pod -n tenant-test-dept -l app=embedding-worker

# 네트워크 테스트
OUTLINE_POD=$(kubectl get pods -n tenant-test-dept -l app=outline -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n tenant-test-dept $OUTLINE_POD -- curl http://embedding-worker:8000/health
```
