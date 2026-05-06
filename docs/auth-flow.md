# 인증 흐름

## DEV 모드 (개발 환경)

### 흐름도
```
사용자 → Outline → Auth Gateway (DEV) → 자동 인증 → Outline
```

### 상세 흐름
1. 사용자가 Outline 접속
2. "Continue with KCU SSO" 버튼 클릭
3. Auth Gateway `/auth/login` 호출
4. Auth Gateway가 자동으로 인증 코드 생성
5. Outline 콜백 URL로 리다이렉트
6. Outline이 Auth Gateway `/auth/token`에서 액세스 토큰 획득
7. Outline이 Auth Gateway `/auth/me`에서 사용자 정보 획득
8. Outline 로그인 완료

### 특징
- 사용자 상호작용 없음
- 기본 사용자: `admin@kcu.ac.kr`
- 빠른 개발 및 테스트

## PROD 모드 (운영 환경)

### 흐름도
```
사용자 → Outline → Auth Gateway → KCU SSO → 인증 → Auth Gateway → Outline
```

### 상세 흐름
1. 사용자가 Outline 접속
2. "Continue with KCU SSO" 버튼 클릭
3. Auth Gateway가 KCU SSO로 리다이렉트
4. 사용자가 KCU 계정으로 로그인
5. KCU SSO가 Auth Gateway 콜백 호출
6. Auth Gateway가 KCU SSO에서 토큰 획득
7. Auth Gateway가 Outline 콜백 호출
8. Outline이 Auth Gateway에서 사용자 정보 획득
9. Outline 로그인 완료

### 특징
- 실제 KCU SSO 연동
- 사용자별 권한 관리
- 세션 관리

## OIDC 설정

### Outline 환경 변수
```yaml
- name: OIDC_CLIENT_ID
  value: "outline-client"
- name: OIDC_CLIENT_SECRET
  value: "outline-secret"
- name: OIDC_AUTH_URI
  value: "http://localhost:8000/auth/login"
- name: OIDC_TOKEN_URI
  value: "http://auth-gateway.kcu-system.svc.cluster.local:8000/auth/token"
- name: OIDC_USERINFO_URI
  value: "http://auth-gateway.kcu-system.svc.cluster.local:8000/auth/me"
- name: OIDC_DISPLAY_NAME
  value: "KCU SSO"
```

### Auth Gateway 환경 변수
```yaml
- name: AUTH_MODE
  value: "DEV"  # 또는 "PROD"
- name: DEFAULT_USER_ID
  value: "admin@kcu.ac.kr"
- name: DEFAULT_USER_NAME
  value: "관리자"
```

## 세션 관리

### DEV 모드
- In-memory 세션 저장소
- 재시작 시 세션 초기화
- 개발/테스트 전용

### PROD 모드
- Redis 세션 저장소
- 영속적 세션 관리
- 분산 환경 지원
