#!/bin/bash

# Outline port-forward 자동 재연결 스크립트

NAMESPACE="tenant-test-dept"
SERVICE="outline"
LOCAL_PORT="3000"
REMOTE_PORT="3000"

echo "Starting port-forward for Outline (auto-reconnect enabled)..."
echo "Access Outline at: http://localhost:${LOCAL_PORT}"
echo "Press Ctrl+C to stop"
echo ""

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Connecting to ${SERVICE}..."
    kubectl port-forward -n ${NAMESPACE} svc/${SERVICE} ${LOCAL_PORT}:${REMOTE_PORT}
    
    EXIT_CODE=$?
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Connection lost (exit code: ${EXIT_CODE})"
    
    # Ctrl+C로 종료한 경우 (exit code 130)
    if [ $EXIT_CODE -eq 130 ]; then
        echo "Stopped by user"
        exit 0
    fi
    
    echo "Reconnecting in 2 seconds..."
    sleep 2
done
