docker build -t balu051989/frame_v2-web:latest .
docker push balu051989/frame_v2-web:latest
  kubectl rollout restart deployment frame-v2
  kubectl rollout status deployment frame-v2
