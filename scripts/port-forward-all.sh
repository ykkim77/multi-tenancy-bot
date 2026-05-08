#!/bin/bash

# 모든 서비스 port-forward (자동 재연결)

NAMESPACE="tenant-test-dept"

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Starting port-forward for all services...${NC}"
echo ""
echo "Services:"
echo "  - Outline:          http://localhost:3000"
echo "  - RAG API:          http://localhost:8001"
echo "  - Embedding Worker: http://localhost:8002"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop all port-forwards${NC}"
echo ""

# Port-forward 함수
port_forward() {
    local SERVICE=$1
    local LOCAL_PORT=$2
    local REMOTE_PORT=$3
    local NAME=$4
    
    while true; do
        echo "[$(date '+%H:%M:%S')] ${NAME}: Connecting..."
        kubectl port-forward -n ${NAMESPACE} svc/${SERVICE} ${LOCAL_PORT}:${REMOTE_PORT} 2>&1 | \
            sed "s/^/[${NAME}] /"
        
        if [ $? -eq 130 ]; then
            exit 0
        fi
        
        echo "[$(date '+%H:%M:%S')] ${NAME}: Reconnecting in 2s..."
        sleep 2
    done
}

# 백그라운드로 각 서비스 port-forward 실행
port_forward "outline" "3000" "3000" "Outline" &
PID_OUTLINE=$!

port_forward "rag-api" "8001" "8000" "RAG-API" &
PID_RAG=$!

port_forward "embedding-worker" "8002" "8000" "Embedding" &
PID_EMBED=$!

# Ctrl+C 핸들러
trap "echo -e '\n${RED}Stopping all port-forwards...${NC}'; kill $PID_OUTLINE $PID_RAG $PID_EMBED 2>/dev/null; exit 0" INT TERM

# 모든 백그라운드 프로세스 대기
wait
