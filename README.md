# KCU 지식포털 - Kubernetes 기반 멀티테넌트 지식포털 + RAG 챗봇 SaaS

## 개요

본 시스템은 하나의 Kubernetes 클러스터에서 기관·부서 단위 테넌트별로 완전히 격리된 지식포털(Outline Wiki) + 임베딩 파이프라인 + Qdrant Vector DB + 대화형 AI 챗봇을 자동으로 구축·운영하는 멀티테넌트 SaaS 플랫폼입니다.

## 핵심 특징

- **멀티테넌시**: Kubernetes Namespace 단위 완전 격리
- **Wiki 시스템**: Outline (오픈소스) 기반 "KCU 지식포털"
- **인증 시스템**: ISign+ SA-WEB SSO (운영) + DEV MODE (개발)
- **자동 프로비저닝**: Kubernetes Operator 패턴
- **RAG 챗봇**: 테넌트별 격리된 Qdrant 기반 검색증강생성

## 시스템 아키텍처

```
┌─────────────────────────────────────────────────────────────────┐
│                        Ingress Controller                        │
│                 (SSL/TLS, Routing by Host/Path)                  │
└───────────────┬─────────────────────────────────────────────────┘
                │
    ┌───────────┴───────────┐
    │                       │
    ▼                       ▼
┌─────────┐         ┌──────────────┐
│ SA-WEB  │         │   Admin      │
│ (SSO)   │         │   Console    │
└─────────┘         └──────────────┘
    │                       │
    │                       │ (Kubernetes API)
    ▼                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Auth Gateway Service                          │
│              (SSO Token 검증 / DEV MODE 분기)                    │
└───────────────┬─────────────────────────────────────────────────┘
                │
    ┌───────────┴───────────┬───────────────────┐
    │                       │                   │
    ▼                       ▼                   ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  Namespace:     │  │  Namespace:     │  │  Namespace:     │
│  tenant-dept-a  │  │  tenant-dept-b  │  │  tenant-dept-c  │
├─────────────────┤  ├─────────────────┤  ├─────────────────┤
│                 │  │                 │  │                 │
│  ┌───────────┐  │  │  ┌───────────┐  │  │  ┌───────────┐  │
│  │ Outline   │  │  │  │ Outline   │  │  │  │ Outline   │  │
│  │   Wiki    │  │  │  │   Wiki    │  │  │  │   Wiki    │  │
│  └─────┬─────┘  │  │  └─────┬─────┘  │  │  └─────┬─────┘  │
│        │webhook │  │        │webhook │  │        │webhook │
│  ┌─────▼─────┐  │  │  ┌─────▼─────┐  │  │  ┌─────▼─────┐  │
│  │ Embedding │  │  │  │ Embedding │  │  │  │ Embedding │  │
│  │  Worker   │  │  │  │  Worker   │  │  │  │  Worker   │  │
│  └─────┬─────┘  │  │  └─────┬─────┘  │  │  └─────┬─────┘  │
│        │        │  │        │        │  │        │        │
│  ┌─────▼─────┐  │  │  ┌─────▼─────┐  │  │  ┌─────▼─────┐  │
│  │  Qdrant   │  │  │  │  Qdrant   │  │  │  │  Qdrant   │  │
│  │ Vector DB │  │  │  │ Vector DB │  │  │  │ Vector DB │  │
│  └─────┬─────┘  │  │  └─────┬─────┘  │  │  └─────┬─────┘  │
│        │        │  │        │        │  │        │        │
│  ┌─────▼─────┐  │  │  ┌─────▼─────┐  │  │  ┌─────▼─────┐  │
│  │    RAG    │  │  │  │    RAG    │  │  │  │    RAG    │  │
│  │ Chat API  │  │  │  │ Chat API  │  │  │  │ Chat API  │  │
│  └───────────┘  │  │  └───────────┘  │  │  └───────────┘  │
│                 │  │                 │  │                 │
└─────────────────┘  └─────────────────┘  └─────────────────┘
```

## 프로젝트 구조

```
kcu-knowledge-portal/
├── docs/                           # 문서 및 다이어그램
│   ├── architecture.md
│   ├── auth-flow.md
│   └── deployment-guide.md
├── operator/                       # Kubernetes Operator (Go)
│   ├── api/v1/                    # CRD 정의
│   ├── controllers/               # Reconciler
│   ├── config/                    # Kubernetes manifests
│   └── Dockerfile
├── services/                      # 마이크로서비스
│   ├── auth-gateway/             # 인증 게이트웨이
│   ├── admin-console/            # 관리자 콘솔
│   ├── embedding-worker/         # 임베딩 파이프라인
│   └── rag-api/                  # RAG Chat API
├── manifests/                     # Kubernetes 리소스
│   ├── base/                     # 공통 리소스
│   ├── outline/                  # Outline 배포
│   ├── qdrant/                   # Qdrant 배포
│   └── security/                 # NetworkPolicy, RBAC
├── scripts/                       # 유틸리티 스크립트
│   ├── dev-setup.sh
│   └── demo.sh
└── examples/                      # 예제 및 데모
    └── e2e-scenario.md
```

## 기술 스택

- **Kubernetes Operator**: Kubebuilder (Go 1.21+)
- **API Services**: FastAPI (Python 3.11+)
- **Wiki System**: Outline
- **Vector Database**: Qdrant
- **Embedding**: OpenAI-compatible API
- **LLM Gateway**: OpenAI-compatible API
- **Authentication**: ISign+ SA-WEB / DEV MODE

## 인증 모드

### 운영 모드 (AUTH_MODE=SSO)
- ISign+ SA-WEB 통합인증
- SSO 토큰 기반 사용자 인증
- tenant_id 기반 접근 제어

### 개발자 모드 (AUTH_MODE=DEV)
- SSO 우회 테스트 환경
- HTTP Header 기반 Mock 인증
- 로컬/CI 환경 개발 지원

## 빠른 시작

### 사전 요구사항
- Kubernetes 1.27+
- kubectl
- Docker
- Go 1.21+
- Python 3.11+

### 개발 환경 설정
```bash
# 1. Operator 빌드 및 배포
cd operator
make install
make run

# 2. 서비스 실행 (DEV MODE)
export AUTH_MODE=DEV
cd services/auth-gateway
pip install -r requirements.txt
uvicorn main:app --reload

# 3. 테넌트 생성
kubectl apply -f examples/chatspace-sample.yaml
```

## 보안 고려사항

- Kubernetes Namespace 단위 격리
- NetworkPolicy로 테넌트 간 통신 차단
- RBAC 최소 권한 원칙
- Secret 테넌트별 분리
- DEV MODE 운영 환경 비활성화 필수

## 라이선스

Proprietary - KCU Internal Use Only
