eval $(minikube docker-env)
docker build -t frame_v2-web:latest .
kubectl rollout restart deployment/frame-v2
