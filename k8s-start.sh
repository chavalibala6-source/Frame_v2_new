#!/bin/bash

# K8s Startup Script (runs on system restart)
# Auto-deploys K8s without building image

set -e

cd ~/frame_v2

echo "🚀 Starting Frame V2 Kubernetes ($(date))"

# Apply Kubernetes configurations
kubectl apply -f k8s/postgres-statefulset.yaml
kubectl apply -f k8s/postgres-service.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/frame-v2-service.yaml

# Rollout restart to ensure fresh deployment
kubectl rollout restart deployment/frame-v2
kubectl rollout restart statefulset/postgres

# Wait for deployment
kubectl rollout status deployment/frame-v2 --timeout=5m

echo "✅ Frame V2 started at $(date)" >> /tmp/frame-v2-k8s-startup.log
