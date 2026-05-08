#!/bin/bash

# Local build script for Kind cluster

set -e

echo "=== Building Docker images for Kind ==="

# Auth Gateway
echo "Building auth-gateway..."
docker build -t auth-gateway:latest ./services/auth-gateway

# Embedding Worker
echo "Building embedding-worker..."
docker build -t embedding-worker:latest ./services/embedding-worker

# RAG API
echo "Building rag-api..."
docker build -t rag-api:latest ./services/rag-api

# Load images into Kind
echo "Loading images into Kind cluster..."
kind load docker-image auth-gateway:latest --name kcu-demo
kind load docker-image embedding-worker:latest --name kcu-demo
kind load docker-image rag-api:latest --name kcu-demo

echo "=== Build complete! ==="
