# KCU 지식포털 배포 가이드

## 사전 요구사항

### 필수 도구
- Docker
- Kind (Kubernetes in Docker)
- kubectl
- OpenAI API Key

### 시스템 요구사항
- CPU: 4 cores 이상
- RAM: 8GB 이상
- Disk: 20GB 이상

## 배포 단계

### 1. Kind 클러스터 생성

```bash
kind create cluster --name kcu-demo --config kind-config-persistent.yaml
```

### 2. Docker 이미지 빌드

```bash
./scripts/build-local.sh
```

### 3. OpenAI API Key 설정

```bash
export OPENAI_API_KEY="sk-your-api-key"
```

### 4. 배포 실행

```bash
./scripts/quick-start.sh
```

### 5. Webhook 설정

```bash
kubectl apply -f manifests/outline/webhook-setup-job.yaml
```

### 6. 접속 확인

```bash
# Terminal 1
kubectl port-forward -n tenant-test-dept svc/outline 3000:80

# Terminal 2
kubectl port-forward -n kcu-system svc/auth-gateway 8000:8000

# 브라우저에서 http://localhost:3000 접속
```

## 트러블슈팅

### Pod가 시작되지 않는 경우
```bash
kubectl describe pod <pod-name> -n <namespace>
kubectl logs <pod-name> -n <namespace>
```

### Webhook이 작동하지 않는 경우
```bash
# Webhook 상태 확인
kubectl exec -n tenant-test-dept outline-postgres-0 -- psql -U outline -d outline -c "SELECT name, enabled FROM webhook_subscriptions;"

# 활성화
kubectl exec -n tenant-test-dept outline-postgres-0 -- psql -U outline -d outline -c "UPDATE webhook_subscriptions SET enabled = true WHERE url = 'http://embedding-worker:8000/webhook/outline';"
```

### 이미지 로드 오류
```bash
# 이미지 다시 빌드 및 로드
docker build -t embedding-worker:latest ./services/embedding-worker
kind load docker-image embedding-worker:latest --name kcu-demo
kubectl rollout restart deployment/embedding-worker -n tenant-test-dept
```

## 정리

```bash
./scripts/cleanup.sh
kind delete cluster --name kcu-demo
```
