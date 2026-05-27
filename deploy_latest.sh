#!/bin/bash

echo "🔨 Building Docker image..."
docker build -t frame_v2-web:latest .

echo "🏷 Tagging image..."
docker tag frame_v2-web:latest balu051989/frame_v2-web:latest

echo "📤 Pushing image..."
docker push balu051989/frame_v2-web:latest

echo "🚀 Restarting Kubernetes deployment..."
kubectl rollout restart deployment/frame-v2

echo "⏳ Waiting for rollout..."
kubectl rollout status deployment/frame-v2

echo "📦 Pods:"
kubectl get pods

echo "✅ Done!"

