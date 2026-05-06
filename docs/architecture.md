# KCU 지식포털 아키텍처

## 전체 구조

```
┌─────────────┐
│   사용자    │
└──────┬──────┘
       │
       ▼
┌─────────────────────────────────────────────┐
│         Outline Wiki (문서 작성)            │
└──────┬──────────────────────────────────────┘
       │ Webhook
       ▼
┌─────────────────────────────────────────────┐
│      Embedding Worker (임베딩 처리)         │
│  - 문서 청킹                                │
│  - OpenAI Embedding 생성                    │
│  - Qdrant에 저장                            │
└──────┬──────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────┐
│      Qdrant (벡터 데이터베이스)             │
└──────┬──────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────┐
│           RAG API (검색 + 생성)             │
│  - Semantic Search                          │
│  - LLM 답변 생성                            │
└─────────────────────────────────────────────┘
```

## 컴포넌트

### 1. Outline Wiki
- **역할**: 문서 작성 및 관리
- **기술**: Node.js, PostgreSQL, Redis
- **인증**: OIDC (Auth Gateway 연동)
- **Webhook**: 문서 변경 시 Embedding Worker로 전송

### 2. Auth Gateway
- **역할**: 통합 인증 (DEV/PROD 모드)
- **기술**: FastAPI, Python
- **모드**:
  - DEV: 자동 로그인
  - PROD: KCU SSO 연동

### 3. Embedding Worker
- **역할**: 문서 임베딩 및 벡터화
- **기술**: FastAPI, LangChain, OpenAI
- **처리 흐름**:
  1. Webhook 수신
  2. 문서 청킹 (RecursiveCharacterTextSplitter)
  3. OpenAI Embedding 생성
  4. Qdrant에 저장

### 4. Qdrant
- **역할**: 벡터 데이터베이스
- **기술**: Qdrant
- **특징**:
  - Cosine Similarity 검색
  - Tenant 격리 (tenant_id 필터)
  - 메타데이터 저장

### 5. RAG API
- **역할**: 검색 증강 생성
- **기술**: FastAPI, OpenAI
- **처리 흐름**:
  1. 쿼리 임베딩 생성
  2. Qdrant 검색
  3. LLM 답변 생성

## 데이터 흐름

### 문서 작성 → 임베딩
1. 사용자가 Outline에서 문서 작성
2. Outline이 Webhook 전송
3. Embedding Worker가 수신
4. 문서 청킹 및 임베딩 생성
5. Qdrant에 벡터 저장

### 검색 → 답변 생성
1. 사용자 쿼리 입력
2. RAG API가 쿼리 임베딩 생성
3. Qdrant에서 유사 문서 검색
4. LLM이 답변 생성
5. 답변 + 출처 반환

## Kubernetes 배포

### Namespace 구조
- `kcu-system`: 공통 서비스 (Auth Gateway)
- `tenant-{id}`: 테넌트별 격리

### 네트워크 정책
- Tenant 격리
- Auth Gateway 접근 허용
- DNS 허용

### 영속성
- PostgreSQL: StatefulSet + PVC
- Qdrant: StatefulSet + PVC
- Outline 데이터: PVC

## 보안

### 인증
- OIDC 기반 인증
- Auth Gateway를 통한 중앙 인증

### 격리
- Namespace 기반 테넌트 격리
- NetworkPolicy를 통한 네트워크 격리
- RBAC를 통한 권한 관리

### Secret 관리
- Kubernetes Secret
- 환경 변수 주입

## 확장성

### 수평 확장
- Embedding Worker: 다중 복제본
- RAG API: 다중 복제본

### 수직 확장
- Resource Requests/Limits 조정

### 성능 최적화
- Redis 캐싱 (Outline)
- Batch Embedding
- Qdrant 인덱싱
